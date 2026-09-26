from __future__ import annotations

import ast
import copy
import difflib
import re
from pathlib import Path
from typing import Any

from .io_utils import hash_tree, stable_hash


SCHEMA_VERSION = "bvi.visible_ast_counterfactual_gate.v1"
ACCEPT_COUNTERFACTUAL = "ACCEPT_COUNTERFACTUAL"
REPAIR_COUNTERFACTUAL = "REPAIR_COUNTERFACTUAL"
REJECT_COUNTERFACTUAL = "REJECT_COUNTERFACTUAL"
ABSTAIN_COUNTERFACTUAL = "ABSTAIN_COUNTERFACTUAL"

_REQUEST_CONTRACT = re.compile(
    r"generalize its current (?P<description>.+?) behavior through an optional "
    r"`(?P<parameter>[^`]+)` parameter\. Use `(?P<default>[^`]+)` as the compatibility default",
    re.DOTALL,
)
_BEHAVIOR = re.compile(r"In the package's (?P<behavior>.+?) behavior, identify", re.DOTALL)
_FAMILY_BY_DESCRIPTION = {
    "fixed decision-threshold": "hardcoded_threshold",
    "fixed cardinality or selected-position": "fixed_cardinality",
    "fixed schema-field": "fixed_schema",
    "fixed format or path-policy": "fixed_format_path",
}


def _literal(text: str) -> Any:
    try:
        return ast.literal_eval(text)
    except (SyntaxError, ValueError):
        for parser in (int, float):
            try:
                return parser(text)
            except (TypeError, ValueError):
                pass
    return text


def _normal_symbol(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", text.strip()).strip("_").casefold()


def extract_visible_evolution_contract(case_root: str | Path) -> dict[str, Any]:
    root = Path(case_root).resolve()
    request_path = root / "REQUEST.md"
    facts_path = root / "AST_FACTS.json"
    if not request_path.is_file() or not facts_path.is_file():
        return {
            "schema_version": "bvi.visible_evolution_contract.v1",
            "status": "ABSTAIN",
            "reason": "visible_request_or_ast_facts_missing",
            "answer_free": True,
        }

    request = request_path.read_text(encoding="utf-8", errors="replace")
    request_match = _REQUEST_CONTRACT.search(request)
    behavior_match = _BEHAVIOR.search(request)
    if request_match is None or behavior_match is None:
        return {
            "schema_version": "bvi.visible_evolution_contract.v1",
            "status": "ABSTAIN",
            "reason": "request_contract_not_recognized",
            "answer_free": True,
        }

    description = request_match.group("description").strip()
    family = _FAMILY_BY_DESCRIPTION.get(description)
    if family is None:
        return {
            "schema_version": "bvi.visible_evolution_contract.v1",
            "status": "ABSTAIN",
            "reason": "unsupported_evolution_family",
            "description": description,
            "answer_free": True,
        }

    parameter = request_match.group("parameter")
    default = _literal(request_match.group("default"))
    symbol = _normal_symbol(behavior_match.group("behavior"))
    import json

    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    ranked_sites = facts.get("ranked_structural_sites") or []
    matches = [
        site
        for site in ranked_sites
        if site.get("family") == family
        and site.get("parameter_candidate") == parameter
        and site.get("default") == default
        and _normal_symbol(str(site.get("symbol", ""))) == symbol
    ]
    status = "READY" if len(matches) == 1 else "ABSTAIN"
    reason = "unique_visible_target" if len(matches) == 1 else (
        "visible_target_not_found" if not matches else "visible_target_ambiguous"
    )
    report = {
        "schema_version": "bvi.visible_evolution_contract.v1",
        "status": status,
        "reason": reason,
        "family": family,
        "behavior": behavior_match.group("behavior").strip(),
        "symbol": symbol,
        "parameter": parameter,
        "compatibility_default": default,
        "target_candidates": matches,
        "target_candidate_count": len(matches),
        "request_evidence": request_match.group(0),
        "answer_free": True,
        "task_verifier_used": False,
        "hidden_artifacts_used": False,
    }
    report["contract_hash"] = stable_hash(report)
    return report


def _function_nodes(tree: ast.AST, symbol: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol
    ]


class _CounterfactualTransformer(ast.NodeTransformer):
    def __init__(self, symbol: str, site: dict[str, Any], parameter: str, default: Any):
        self.symbol = symbol
        self.site = site
        self.parameter = parameter
        self.default = default
        self.in_target = False
        self.replaced = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        return self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        return self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.AST:
        previous = self.in_target
        self.in_target = node.name == self.symbol
        if self.in_target:
            names = {
                argument.arg
                for argument in [
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                ]
            }
            if self.parameter not in names:
                node.args.args.append(ast.arg(arg=self.parameter, annotation=None))
                node.args.defaults.append(ast.Constant(value=self.default))
        node = self.generic_visit(node)
        self.in_target = previous
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if (
            self.in_target
            and getattr(node, "lineno", None) == self.site.get("line")
            and getattr(node, "col_offset", None) == self.site.get("column")
            and node.value == self.default
        ):
            self.replaced += 1
            return ast.copy_location(ast.Name(id=self.parameter, ctx=ast.Load()), node)
        return node


class _Canonicalizer(ast.NodeTransformer):
    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        return self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        return self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.AST:
        node = self.generic_visit(node)
        node.returns = None
        for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
            argument.annotation = None
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body = node.body[1:]
        return node


def _canonical_dump(tree: ast.AST) -> str:
    normalized = _Canonicalizer().visit(copy.deepcopy(tree))
    ast.fix_missing_locations(normalized)
    return ast.dump(normalized, include_attributes=False)


def _canonical_source(tree: ast.AST) -> str:
    normalized = _Canonicalizer().visit(copy.deepcopy(tree))
    ast.fix_missing_locations(normalized)
    return ast.unparse(normalized)


def _static_bindings(tree: ast.Module) -> dict[str, Any]:
    bindings: dict[str, Any] = {}
    for node in tree.body:
        name: str | None = None
        value: ast.AST | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            value = node.value
        if name is None or value is None:
            continue
        try:
            bindings[name] = ast.literal_eval(value)
        except (ValueError, TypeError):
            continue
    return bindings


def _normalize_parameter_default(
    tree: ast.Module,
    symbol: str,
    parameter_name: str,
    value: Any,
) -> ast.Module:
    normalized = copy.deepcopy(tree)
    functions = _function_nodes(normalized, symbol)
    if len(functions) != 1:
        return normalized
    function = functions[0]
    positional = [*function.args.posonlyargs, *function.args.args]
    positional_names = [argument.arg for argument in positional]
    if parameter_name in positional_names:
        index = positional_names.index(parameter_name)
        first_default = len(positional) - len(function.args.defaults)
        if index >= first_default:
            function.args.defaults[index - first_default] = ast.Constant(value=value)
    else:
        keyword_names = [argument.arg for argument in function.args.kwonlyargs]
        if parameter_name in keyword_names:
            function.args.kw_defaults[keyword_names.index(parameter_name)] = ast.Constant(value=value)
    ast.fix_missing_locations(normalized)
    return normalized


def _top_level_nodes(tree: ast.Module, target_symbol: str) -> dict[str, ast.AST]:
    nodes: dict[str, ast.AST] = {}
    for index, node in enumerate(tree.body):
        name = getattr(node, "name", None)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and name == target_symbol:
            continue
        if name:
            key = f"{type(node).__name__}:{name}"
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [target.id for target in targets if isinstance(target, ast.Name)]
            key = f"{type(node).__name__}:{','.join(names) or index}"
        else:
            key = f"{type(node).__name__}:{index}"
        nodes[key] = node
    return nodes


def _source_for_node(node: ast.AST, source: str) -> str:
    segment = ast.get_source_segment(source, node)
    return segment if segment is not None else ast.unparse(node)


def _structural_residuals(
    parent_tree: ast.Module,
    candidate_tree: ast.Module,
    reference_tree: ast.Module,
    *,
    symbol: str,
    parameter: str,
    compatibility_default: Any,
    parent_source: str,
    candidate_source: str,
) -> dict[str, Any]:
    candidate_functions = _function_nodes(candidate_tree, symbol)
    reference_functions = _function_nodes(reference_tree, symbol)
    target_exact = False
    target_diff: list[str] = []
    if len(candidate_functions) == 1 and len(reference_functions) == 1:
        candidate_target = _canonical_source(candidate_functions[0]).splitlines()
        reference_target = _canonical_source(reference_functions[0]).splitlines()
        target_exact = candidate_target == reference_target
        if not target_exact:
            target_diff = list(
                difflib.unified_diff(
                    candidate_target,
                    reference_target,
                    fromfile="candidate_target_ast",
                    tofile="minimal_visible_target_ast",
                    lineterm="",
                    n=1,
                )
            )[:80]

    parent_nodes = _top_level_nodes(parent_tree, symbol)
    candidate_nodes = _top_level_nodes(candidate_tree, symbol)
    deltas: list[dict[str, Any]] = []
    for key in sorted(set(parent_nodes) | set(candidate_nodes)):
        parent_node = parent_nodes.get(key)
        candidate_node = candidate_nodes.get(key)
        if parent_node is None:
            deltas.append(
                {
                    "node": key,
                    "delta": "added",
                    "candidate_source": _source_for_node(candidate_node, candidate_source),
                }
            )
        elif candidate_node is None:
            deltas.append(
                {
                    "node": key,
                    "delta": "removed",
                    "expected_parent_source": _source_for_node(parent_node, parent_source),
                }
            )
        elif _canonical_dump(parent_node) != _canonical_dump(candidate_node):
            deltas.append(
                {
                    "node": key,
                    "delta": "modified",
                    "candidate_source": _source_for_node(candidate_node, candidate_source),
                    "expected_parent_source": _source_for_node(parent_node, parent_source),
                }
            )
    return {
        "target_symbol": symbol,
        "target_exact_minimal_match": target_exact,
        "target_ast_diff": target_diff,
        "non_target_deltas": deltas,
        "non_target_delta_count": len(deltas),
        "allowed_target_edit": {
            "add_optional_parameter": parameter,
            "compatibility_default": compatibility_default,
            "replace_only_localized_node": True,
        },
        "evidence_scope": "visible_parent_request_and_ast_only",
    }


def synthesize_visible_counterfactual(
    package_root: str | Path,
    contract: dict[str, Any],
    *,
    target_site: dict[str, Any] | None = None,
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    if contract.get("status") != "READY" and target_site is None:
        return {
            "status": "ABSTAIN",
            "reason": contract.get("reason", "contract_not_ready"),
        }
    site = target_site or contract["target_candidates"][0]
    source_path = package / str(site.get("path", ""))
    if not source_path.is_file():
        return {"status": "ABSTAIN", "reason": "target_source_missing", "target_site": site}
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    except (OSError, SyntaxError) as exc:
        return {
            "status": "ABSTAIN",
            "reason": f"parent_parse_error:{type(exc).__name__}:{exc}",
            "target_site": site,
        }
    transformer = _CounterfactualTransformer(
        contract["symbol"],
        site,
        contract["parameter"],
        contract["compatibility_default"],
    )
    counterfactual = transformer.visit(copy.deepcopy(tree))
    ast.fix_missing_locations(counterfactual)
    if transformer.replaced != 1:
        return {
            "status": "ABSTAIN",
            "reason": "target_node_not_uniquely_replaceable",
            "replacement_count": transformer.replaced,
            "target_site": site,
        }
    report = {
        "status": "READY",
        "reason": "visible_target_node_replaced",
        "target_site": site,
        "source_path": str(site["path"]),
        "counterfactual_ast_hash": stable_hash(_canonical_dump(counterfactual)),
        "counterfactual_tree": counterfactual,
        "answer_free": True,
        "task_verifier_used": False,
        "hidden_artifacts_used": False,
    }
    return report


def _parameter_default(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    parameter_name: str,
    static_bindings: dict[str, Any] | None = None,
) -> tuple[bool, Any]:
    positional = [*function.args.posonlyargs, *function.args.args]
    positional_names = [argument.arg for argument in positional]
    if parameter_name in positional_names:
        index = positional_names.index(parameter_name)
        first_default = len(positional) - len(function.args.defaults)
        if index < first_default:
            return False, None
        node = function.args.defaults[index - first_default]
    else:
        keyword_names = [argument.arg for argument in function.args.kwonlyargs]
        if parameter_name not in keyword_names:
            return False, None
        node = function.args.kw_defaults[keyword_names.index(parameter_name)]
        if node is None:
            return False, None
    try:
        return True, ast.literal_eval(node)
    except (ValueError, TypeError):
        if isinstance(node, ast.Name) and node.id in (static_bindings or {}):
            return True, static_bindings[node.id]
        return False, None


def evaluate_visible_counterfactual_candidate(
    case_root: str | Path,
    candidate_root: str | Path | None,
    *,
    target_site: dict[str, Any] | None = None,
    contract: dict[str, Any] | None = None,
    parent_hashes: dict[str, str] | None = None,
    candidate_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    case = Path(case_root).resolve()
    parent = case / "package"
    contract = copy.deepcopy(contract) if contract is not None else extract_visible_evolution_contract(case)
    if candidate_root is None:
        report = {
            "schema_version": SCHEMA_VERSION,
            "decision": REJECT_COUNTERFACTUAL,
            "decision_reason": "candidate_unavailable",
            "contract": contract,
            "task_verifier_used": False,
            "hidden_artifacts_used": False,
        }
        report["report_hash"] = stable_hash(report)
        return report
    candidate = Path(candidate_root).resolve()
    if target_site is None and contract.get("status") != "READY":
        report = {
            "schema_version": SCHEMA_VERSION,
            "decision": ABSTAIN_COUNTERFACTUAL,
            "decision_reason": contract.get("reason"),
            "contract": contract,
            "task_verifier_used": False,
            "hidden_artifacts_used": False,
        }
        report["report_hash"] = stable_hash(report)
        return report

    reference = synthesize_visible_counterfactual(parent, contract, target_site=target_site)
    if reference.get("status") != "READY":
        report = {
            "schema_version": SCHEMA_VERSION,
            "decision": ABSTAIN_COUNTERFACTUAL,
            "decision_reason": reference.get("reason"),
            "contract": contract,
            "counterfactual": {key: value for key, value in reference.items() if key != "counterfactual_tree"},
            "task_verifier_used": False,
            "hidden_artifacts_used": False,
        }
        report["report_hash"] = stable_hash(report)
        return report

    site = reference["target_site"]
    relative_source = str(site["path"])
    candidate_source = candidate / relative_source
    structural_failures: list[str] = []
    if not (candidate / "SKILL.md").is_file():
        structural_failures.append("skill_document_missing")
    if not candidate_source.is_file():
        structural_failures.append("target_source_missing")
    parent_hashes = parent_hashes if parent_hashes is not None else hash_tree(parent)
    candidate_hashes = (
        candidate_hashes
        if candidate_hashes is not None
        else (hash_tree(candidate) if candidate.is_dir() else {})
    )
    changed_paths = sorted(
        path
        for path in set(parent_hashes) | set(candidate_hashes)
        if parent_hashes.get(path) != candidate_hashes.get(path)
    )
    outside_scope = [
        path for path in changed_paths if path != "SKILL.md" and not path.startswith("scripts/")
    ]
    if outside_scope:
        structural_failures.append("edit_scope_violation")
    if structural_failures:
        report = {
            "schema_version": SCHEMA_VERSION,
            "decision": REJECT_COUNTERFACTUAL,
            "decision_reason": "generic_structure_failure",
            "structural_failures": structural_failures,
            "outside_scope": outside_scope,
            "changed_paths": changed_paths,
            "contract": contract,
            "task_verifier_used": False,
            "hidden_artifacts_used": False,
        }
        report["report_hash"] = stable_hash(report)
        return report

    candidate_source_text = ""
    parent_source_text = ""
    structural_residuals: dict[str, Any] | None = None
    try:
        candidate_source_text = candidate_source.read_text(encoding="utf-8")
        parent_source_text = (parent / relative_source).read_text(encoding="utf-8")
        candidate_tree = ast.parse(candidate_source_text, filename=str(candidate_source))
        parent_tree = ast.parse(parent_source_text, filename=str(parent / relative_source))
    except (OSError, SyntaxError) as exc:
        decision = REPAIR_COUNTERFACTUAL
        reason = f"candidate_parse_error:{type(exc).__name__}:{exc}"
        candidate_tree = None
    else:
        functions = _function_nodes(candidate_tree, contract["symbol"])
        if len(functions) != 1:
            decision = REPAIR_COUNTERFACTUAL
            reason = "public_target_symbol_not_unique"
        else:
            has_default, observed_default = _parameter_default(
                functions[0], contract["parameter"], _static_bindings(candidate_tree)
            )
            comparison_tree = _normalize_parameter_default(
                candidate_tree,
                contract["symbol"],
                contract["parameter"],
                contract["compatibility_default"],
            )
            structural_residuals = _structural_residuals(
                parent_tree,
                comparison_tree,
                reference["counterfactual_tree"],
                symbol=contract["symbol"],
                parameter=contract["parameter"],
                compatibility_default=contract["compatibility_default"],
                parent_source=parent_source_text,
                candidate_source=candidate_source_text,
            )
            if not has_default or observed_default != contract["compatibility_default"]:
                decision = REPAIR_COUNTERFACTUAL
                reason = "optional_parameter_or_default_missing"
            else:
                skill_text = (candidate / "SKILL.md").read_text(
                    encoding="utf-8", errors="replace"
                )
                doc_sync = (
                    contract["symbol"] in skill_text and contract["parameter"] in skill_text
                )
                exact_match = _canonical_dump(comparison_tree) == _canonical_dump(
                    reference["counterfactual_tree"]
                )
                changed_non_test_scripts = [
                    path
                    for path in changed_paths
                    if path.startswith("scripts/")
                    and path != relative_source
                    and "/tests/" not in f"/{path}"
                    and not path.startswith("scripts/tests/")
                ]
                if not doc_sync:
                    decision = REPAIR_COUNTERFACTUAL
                    reason = "visible_document_contract_missing"
                elif exact_match and not changed_non_test_scripts:
                    decision = ACCEPT_COUNTERFACTUAL
                    reason = "exact_visible_ast_counterfactual_match"
                else:
                    decision = ABSTAIN_COUNTERFACTUAL
                    reason = (
                        "extra_package_semantic_delta"
                        if changed_non_test_scripts
                        else "extra_target_semantic_delta"
                    )

    report = {
        "schema_version": SCHEMA_VERSION,
        "decision": decision,
        "decision_reason": reason,
        "contract": contract,
        "counterfactual": {
            key: value for key, value in reference.items() if key != "counterfactual_tree"
        },
        "changed_paths": changed_paths,
        "candidate_tree_hash": stable_hash(candidate_hashes),
        "structural_residuals": structural_residuals,
        "task_verifier_used": False,
        "hidden_artifacts_used": False,
        "answer_free": True,
        "claim_boundary": (
            "Acceptance certifies equality to one minimal counterfactual compiled from the visible request and "
            "AST target. Abstention covers linked or additional semantic edits. This is not complete task correctness."
        ),
    }
    report["report_hash"] = stable_hash(report)
    return report


def choose_sham_target(contract: dict[str, Any], ast_facts: dict[str, Any]) -> dict[str, Any] | None:
    real_ids = {site.get("site_id") for site in contract.get("target_candidates", [])}
    for site in ast_facts.get("ranked_structural_sites") or []:
        if site.get("site_id") in real_ids:
            continue
        if site.get("path") and site.get("symbol") and site.get("line") is not None:
            return site
    return None


def choose_matched_sham_target(
    contract: dict[str, Any], ast_facts: dict[str, Any]
) -> dict[str, Any] | None:
    """Choose the most structurally similar package-local site that is still wrong."""
    real_sites = contract.get("target_candidates") or []
    real_ids = {site.get("site_id") for site in real_sites}
    real_site = real_sites[0] if real_sites else {}
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for index, site in enumerate(ast_facts.get("ranked_structural_sites") or []):
        if site.get("site_id") in real_ids:
            continue
        if not site.get("path") or not site.get("symbol") or site.get("line") is None:
            continue
        score = 0
        score += 8 if site.get("family") == contract.get("family") else 0
        score += 4 if site.get("parameter_candidate") == contract.get("parameter") else 0
        score += 2 if site.get("role") == real_site.get("role") else 0
        score += 2 if site.get("node_type") == real_site.get("node_type") else 0
        score += 1 if site.get("path") == real_site.get("path") else 0
        candidates.append((score, -index, site))
    return max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


def derive_matched_sham_contract(
    contract: dict[str, Any], ast_facts: dict[str, Any]
) -> dict[str, Any] | None:
    """Build a valid counterfactual contract around a real but incorrect AST site."""
    site = choose_matched_sham_target(contract, ast_facts)
    if site is None:
        return None
    sham = copy.deepcopy(contract)
    sham.update(
        {
            "status": "READY",
            "reason": "matched_package_local_wrong_target",
            "behavior": str(site["symbol"]),
            "symbol": _normal_symbol(str(site["symbol"])),
            "compatibility_default": site.get("default"),
            "target_candidates": [copy.deepcopy(site)],
            "target_candidate_count": 1,
            "sham_control": True,
            "real_contract_hash": contract.get("contract_hash"),
        }
    )
    sham.pop("contract_hash", None)
    sham["contract_hash"] = stable_hash(sham)
    return sham
