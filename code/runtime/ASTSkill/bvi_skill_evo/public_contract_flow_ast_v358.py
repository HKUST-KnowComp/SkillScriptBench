from __future__ import annotations

import ast
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from bvi_skill_evo.balanced_hybrid_ast_v316 import (
    _assert_public_package,
    _tree_hash,
)
from bvi_skill_evo.coarse_source_flow import (
    _call_target,
    _compatible,
    _module_names,
    _node_hash,
    _parameters,
    _resolve_imports,
)
from bvi_skill_evo.parameter_flow_ast import build_visible_parameter_flow_facts
from bvi_skill_evo.proposal_first_closure_gate_v292 import (
    ACCEPT,
    build_proposal_first_closure_report,
)
from bvi_skill_evo.unused_parameter_ast import (
    _candidate_sites,
    _documentation_evidence,
    _optional_parameters,
)
from skillscriptbench.io_utils import canonical_json_hash
from skillscriptbench.python_structural_anomaly_facts_v254 import (
    build_compact_python_structural_anomaly_facts,
)
from skillscriptbench.structural_evolution_v65 import parse_request_contract


SCHEMA_VERSION = "3.58-public-contract-flow-ast-v3"
METHOD_ID = "public_proposal_conditioned_contract_flow_ast_v3"
MAX_STRONG_OBLIGATIONS = 16
MAX_GENERIC_HYPOTHESES = 8

_TOKEN_STOP = {
    "all",
    "and",
    "behavior",
    "for",
    "from",
    "helper",
    "into",
    "normal",
    "one",
    "package",
    "path",
    "public",
    "return",
    "script",
    "scripts",
    "skill",
    "supplied",
    "that",
    "the",
    "this",
    "use",
    "using",
    "with",
    "workflow",
}


def _tokens(value: str) -> set[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return {
        token.casefold()
        for token in re.findall(
            r"[A-Za-z][A-Za-z0-9]*", expanded.replace("_", " ").replace("-", " ")
        )
        if len(token) >= 3 and token.casefold() not in _TOKEN_STOP
    }


def _python_index(package: Path) -> dict[str, Any]:
    trees: dict[Path, ast.Module] = {}
    sources: dict[Path, str] = {}
    functions: dict[Path, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    duplicate_functions: dict[Path, set[str]] = {}
    modules: dict[str, Path] = {}
    parse_errors: list[dict[str, str]] = []
    scripts = package / "scripts"
    for path in sorted(scripts.rglob("*.py")) if scripts.is_dir() else []:
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package)
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source, filename=relative.as_posix())
        except (SyntaxError, UnicodeError, ValueError) as exc:
            parse_errors.append(
                {
                    "path": relative.as_posix(),
                    "error": f"{type(exc).__name__}:{exc}",
                }
            )
            continue
        counts = Counter(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        definitions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and counts[node.name] == 1
        }
        trees[relative] = tree
        sources[relative] = source
        functions[relative] = definitions
        duplicate_functions[relative] = {
            name for name, count in counts.items() if count > 1
        }
        for name in _module_names(relative):
            modules[name] = relative
    return {
        "trees": trees,
        "sources": sources,
        "functions": functions,
        "duplicate_functions": duplicate_functions,
        "modules": modules,
        "parse_errors": parse_errors,
    }


def _conservative_parameter_loads(
    function: ast.FunctionDef | ast.AsyncFunctionDef, name: str
) -> list[ast.Name]:
    """Count direct and closure loads; shadowing yields a safe false negative."""
    return [
        node
        for statement in function.body
        for node in ast.walk(statement)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id == name
    ]


class _DirectScopeConstants(ast.NodeVisitor):
    def __init__(self) -> None:
        self.nodes: list[ast.Constant] = []

    def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802
        self.nodes.append(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        return

    def visit_AsyncFunctionDef(  # noqa: N802
        self, node: ast.AsyncFunctionDef
    ) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        return


def _direct_scope_candidate_sites(
    source: str,
    relative: str,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    default_value: Any,
    parameter: str,
) -> list[dict[str, Any]]:
    visitor = _DirectScopeConstants()
    for statement in function.body:
        visitor.visit(statement)
    direct_ids = {
        (int(node.lineno), int(node.col_offset), type(node).__name__)
        for node in visitor.nodes
        if hasattr(node, "lineno") and hasattr(node, "col_offset")
    }
    return [
        row
        for row in _candidate_sites(
            source, relative, function, default_value, parameter
        )
        if (
            int(row["start_line"]),
            int(row["start_column"]),
            str(row["node_type"]),
        )
        in direct_ids
    ]


class _DirectCalls(ast.NodeVisitor):
    def __init__(self, root: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.root = root
        self.calls: list[ast.Call] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        self.calls.append(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        if node is self.root:
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        if node is self.root:
            self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        return


def _function_source(source: str, function: ast.AST) -> str:
    return ast.get_source_segment(source, function) or ast.unparse(function)


def _changed_symbols(parent: Path, current: Path) -> tuple[list[str], set[tuple[str, str]]]:
    parent_index = _python_index(parent)
    current_index = _python_index(current)
    parent_paths = set(parent_index["sources"])
    current_paths = set(current_index["sources"])
    changed_paths: set[str] = {
        path.as_posix()
        for path in parent_paths | current_paths
        if parent_index["sources"].get(path) != current_index["sources"].get(path)
    }
    changed: set[tuple[str, str]] = set()
    for path in parent_paths & current_paths:
        left = parent_index["functions"].get(path, {})
        right = current_index["functions"].get(path, {})
        for symbol in set(left) & set(right):
            if _function_source(parent_index["sources"][path], left[symbol]) != _function_source(
                current_index["sources"][path], right[symbol]
            ):
                changed.add((path.as_posix(), symbol))
    return sorted(changed_paths), changed


def _caller_relevance(
    *,
    path: str,
    symbol: str,
    request_text: str,
    skill_text: str,
    changed_symbols: set[tuple[str, str]],
) -> dict[str, Any]:
    request_tokens = _tokens(request_text)
    overlap = sorted(request_tokens & _tokens(symbol))
    exact_request = bool(
        re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])",
            request_text,
            flags=re.IGNORECASE,
        )
    )
    documented_symbol = bool(
        re.search(rf"`{re.escape(symbol)}`", skill_text, flags=re.IGNORECASE)
    )
    changed = (path, symbol) in changed_symbols
    score = (
        100 * int(changed)
        + 60 * int(exact_request)
        + 24 * int(documented_symbol)
        + 18 * len(overlap)
        - 2 * int(symbol.startswith("_"))
    )
    return {
        "score": score,
        "proposal_changed_symbol": changed,
        "exact_request_symbol": exact_request,
        "documented_code_symbol": documented_symbol,
        "request_token_overlap": overlap,
    }


def _global_formal_origin_mismatches(
    package: Path,
    request_text: str,
    *,
    changed_symbols: set[tuple[str, str]],
) -> dict[str, Any]:
    index = _python_index(package)
    skill_text = (package / "SKILL.md").read_text(encoding="utf-8")
    findings: list[dict[str, Any]] = []
    for caller_path, tree in index["trees"].items():
        imported_symbols, module_aliases = _resolve_imports(tree, index["modules"])
        for caller_symbol, caller in index["functions"].get(caller_path, {}).items():
            caller_parameters = {row["name"]: row for row in _parameters(caller)}
            if not caller_parameters:
                continue
            visitor = _DirectCalls(caller)
            visitor.visit(caller)
            relevance = _caller_relevance(
                path=caller_path.as_posix(),
                symbol=caller_symbol,
                request_text=request_text,
                skill_text=skill_text,
                changed_symbols=changed_symbols,
            )
            for call in visitor.calls:
                target = _call_target(call, imported_symbols, module_aliases)
                if target is None:
                    continue
                callee_path, callee_symbol = target
                callee = index["functions"].get(callee_path, {}).get(callee_symbol)
                if callee is None:
                    continue
                callee_parameters = _parameters(callee)
                callee_by_name = {row["name"]: row for row in callee_parameters}
                slots: list[tuple[str, str | int, str | None, ast.expr]] = []
                for position, argument in enumerate(call.args):
                    formal = (
                        callee_parameters[position]["name"]
                        if position < len(callee_parameters)
                        else None
                    )
                    slots.append(("positional", position, formal, argument))
                slots.extend(
                    ("keyword", str(keyword.arg), str(keyword.arg), keyword.value)
                    for keyword in call.keywords
                    if keyword.arg is not None
                )
                for call_kind, call_slot, formal, argument in slots:
                    if (
                        formal is None
                        or formal not in caller_parameters
                        or formal not in callee_by_name
                        or not isinstance(argument, ast.Name)
                        or argument.id not in caller_parameters
                        or argument.id == formal
                    ):
                        continue
                    caller_annotation = caller_parameters[formal]["annotation"]
                    callee_annotation = callee_by_name[formal]["annotation"]
                    annotations_compatible = _compatible(
                        caller_annotation, callee_annotation
                    )
                    if caller_annotation and callee_annotation and not annotations_compatible:
                        continue
                    confidence = (
                        0.99
                        if call_kind == "keyword"
                        else 0.98
                        if annotations_compatible
                        else 0.90
                    )
                    finding = {
                        "finding_class": "contract_backed_obligation",
                        "kind": "formal_origin_mismatch",
                        "confidence": confidence,
                        "caller_path": caller_path.as_posix(),
                        "caller_symbol": caller_symbol,
                        "callee_path": callee_path.as_posix(),
                        "callee_symbol": callee_symbol,
                        "call_line": int(call.lineno),
                        "call_kind": call_kind,
                        "call_slot": call_slot,
                        "callee_formal_parameter": formal,
                        "observed_origin": argument.id,
                        "same_named_origin_available": True,
                        "caller_annotation": caller_annotation,
                        "callee_annotation": callee_annotation,
                        "node_id": (
                            f"{caller_path.as_posix()}:{call.lineno}:"
                            f"{call.col_offset}:Call"
                        ),
                        "node_sha256": _node_hash(call),
                        "call_source": ast.get_source_segment(
                            index["sources"][caller_path], call
                        ),
                        "localization_evidence": relevance,
                        "repair_goal": (
                            "Inspect whether this package-local call should pass the available "
                            "same-named caller value into the corresponding callee formal."
                        ),
                        "semantic_correctness_inferred": False,
                    }
                    finding["finding_hash"] = canonical_json_hash(finding)
                    findings.append(finding)

    reciprocal: set[tuple[str, str, str]] = set()
    by_caller: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in findings:
        by_caller[(str(row["caller_path"]), str(row["caller_symbol"]))].append(row)
    for rows in by_caller.values():
        pairs = {
            (str(row["callee_formal_parameter"]), str(row["observed_origin"]))
            for row in rows
        }
        for row in rows:
            pair = (str(row["callee_formal_parameter"]), str(row["observed_origin"]))
            if (pair[1], pair[0]) in pairs:
                reciprocal.add(
                    (
                        str(row["caller_path"]),
                        str(row["caller_symbol"]),
                        str(row["finding_hash"]),
                    )
                )
                row["reciprocal_swap_evidence"] = True
                row["confidence"] = max(float(row["confidence"]), 0.995)
                row["finding_hash"] = canonical_json_hash(
                    {key: value for key, value in row.items() if key != "finding_hash"}
                )

    findings.sort(
        key=lambda row: (
            -int((row.get("localization_evidence") or {}).get("score") or 0),
            -float(row["confidence"]),
            str(row["caller_path"]),
            int(row["call_line"]),
            str(row["callee_formal_parameter"]),
        )
    )
    occurrence_counts: Counter[tuple[Any, ...]] = Counter()
    for row in findings:
        occurrence_key = (
            row.get("caller_path"),
            row.get("caller_symbol"),
            row.get("callee_path"),
            row.get("callee_symbol"),
            row.get("call_kind"),
            row.get("call_slot"),
            row.get("callee_formal_parameter"),
            row.get("observed_origin"),
        )
        occurrence_counts[occurrence_key] += 1
        row["obligation_occurrence_index"] = occurrence_counts[occurrence_key]
        row["finding_hash"] = canonical_json_hash(
            {key: value for key, value in row.items() if key != "finding_hash"}
        )
    return {
        "finding_count": len(findings),
        "findings": findings,
        "parse_errors": index["parse_errors"],
        "duplicate_function_names_skipped": {
            path.as_posix(): sorted(values)
            for path, values in index["duplicate_functions"].items()
            if values
        },
        "reciprocal_swap_finding_count": len(reciprocal),
    }


def _documented_unused_parameter_facts(package: Path) -> dict[str, Any]:
    skill_text = (package / "SKILL.md").read_text(encoding="utf-8")
    findings: list[dict[str, Any]] = []
    index = _python_index(package)
    for relative, functions in index["functions"].items():
        source = index["sources"][relative]
        public_top_level_function_ids = {
            id(node)
            for node in index["trees"][relative].body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for function in functions.values():
            if id(function) not in public_top_level_function_ids:
                continue
            for parameter, default_node in _optional_parameters(function):
                try:
                    default_value = ast.literal_eval(default_node)
                except (TypeError, ValueError):
                    continue
                documentation = _documentation_evidence(
                    skill_text, function.name, parameter
                )
                loads = _conservative_parameter_loads(function, parameter)
                sites = _direct_scope_candidate_sites(
                    source,
                    relative.as_posix(),
                    function,
                    default_value,
                    parameter,
                )
                if documentation is None or loads or not sites:
                    continue
                finding = {
                    "finding_class": "contract_backed_obligation",
                    "kind": "documented_optional_parameter_unused",
                    "path": relative.as_posix(),
                    "function_symbol": function.name,
                    "parameter": parameter,
                    "default_expression": ast.unparse(default_node),
                    "parameter_load_count": 0,
                    "parameter_load_scope": (
                        "function_body_including_nested_closure_references; "
                        "shadowed nested names conservatively count as used"
                    ),
                    "documentation_evidence": documentation,
                    "candidate_sites": sites,
                    "confidence": round(float(sites[0]["confidence"]), 4),
                    "activation_source": "automatic_public_document_signature_join",
                    "function_scope": "module_direct_top_level_definition",
                    "repair_goal": (
                        "Make the documented optional parameter influence the matching behavior "
                        "while preserving its compatibility default."
                    ),
                    "semantic_correctness_inferred": False,
                }
                finding["finding_hash"] = canonical_json_hash(finding)
                findings.append(finding)
    findings.sort(
        key=lambda row: (
            -float(row["confidence"]),
            str(row["path"]),
            str(row["function_symbol"]),
            str(row["parameter"]),
        )
    )
    return {"finding_count": len(findings), "findings": findings}


def _contract_request_text(
    *, function: str, parameter: str, default: str | None, context: str
) -> str:
    default_clause = (
        f" using `{default}` as the compatibility default" if default else ""
    )
    return (
        f"Repair the public `{function}` helper. It accepts the optional `{parameter}` "
        f"parameter{default_clause}. Restore the documented behavior data flow. {context}"
    )


def _request_contracts(request_text: str) -> list[dict[str, str | None]]:
    rows: list[dict[str, str | None]] = []
    bullet_pattern = re.compile(
        r"(?m)^\s*-\s*`(?P<function>[A-Za-z_][A-Za-z0-9_]*)`\s*:\s*"
        r"(?P<context>[^\n]+)"
    )
    for match in bullet_pattern.finditer(request_text):
        context = match.group("context")
        parameter = re.search(
            r"optional\s+`([A-Za-z_][A-Za-z0-9_]*)`\s+parameter",
            context,
            flags=re.IGNORECASE,
        )
        if parameter is None:
            continue
        default = re.search(
            r"(?:uses|using)\s+`([^`]+)`\s+as\s+the\s+compatibility\s+default",
            context,
            flags=re.IGNORECASE,
        )
        rows.append(
            {
                "function": match.group("function"),
                "parameter": parameter.group(1),
                "default": default.group(1) if default else None,
                "context": context,
                "source": "request_bullet_contract",
            }
        )
    parsed = parse_request_contract(request_text)
    if parsed.function and parsed.parameter:
        rows.append(
            {
                "function": parsed.function,
                "parameter": parsed.parameter,
                "default": repr(parsed.default) if parsed.has_default else None,
                "context": request_text,
                "source": "request_single_contract",
            }
        )
    result: list[dict[str, str | None]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        identity = (str(row["function"]), str(row["parameter"]))
        if identity in seen:
            continue
        seen.add(identity)
        result.append(row)
    return result


def _parameter_flow_facts(package: Path, request_text: str) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    abstentions: list[dict[str, str]] = []
    contracts = _request_contracts(request_text)
    for index, contract in enumerate(contracts, start=1):
        synthetic = _contract_request_text(
            function=str(contract["function"]),
            parameter=str(contract["parameter"]),
            default=str(contract["default"]) if contract["default"] is not None else None,
            context=str(contract["context"]),
        )
        try:
            packet = build_visible_parameter_flow_facts(package, synthetic)
        except (OSError, SyntaxError, TypeError, ValueError) as exc:
            abstentions.append(
                {
                    "function": str(contract["function"]),
                    "parameter": str(contract["parameter"]),
                    "reason": f"{type(exc).__name__}:{exc}",
                }
            )
            continue
        for finding in packet.get("findings") or []:
            row = dict(finding)
            row.update(
                {
                    "finding_class": "contract_backed_obligation",
                    "kind": str(row.pop("category", "parameter_flow_disconnect")),
                    "contract_id": f"contract-{index:02d}",
                    "contract_source": contract["source"],
                    "confidence": 0.995,
                    "repair_goal": (
                        "Restore the documented parameter flow through the visible resolver or alias "
                        "without changing the public signature or compatibility default."
                    ),
                    "semantic_correctness_inferred": False,
                }
            )
            row["finding_hash"] = canonical_json_hash(row)
            findings.append(row)
    return {
        "contract_count": len(contracts),
        "finding_count": len(findings),
        "findings": findings,
        "abstentions": abstentions,
    }


def _generic_hypotheses(
    parent: Path,
    current: Path,
    request_text: str,
    *,
    changed_paths: set[str],
    changed_symbols: set[tuple[str, str]],
) -> dict[str, Any]:
    if not any((current / "scripts").rglob("*.py")):
        return {"finding_count": 0, "findings": []}
    try:
        parent_facts = build_compact_python_structural_anomaly_facts(
            parent,
            request_text,
            maximum_findings=16,
            maximum_local_edges=32,
            maximum_def_use=24,
        )
        current_facts = build_compact_python_structural_anomaly_facts(
            current,
            request_text,
            maximum_findings=16,
            maximum_local_edges=32,
            maximum_def_use=24,
        )
    except (OSError, SyntaxError, TypeError, ValueError) as exc:
        return {
            "finding_count": 0,
            "findings": [],
            "error": f"{type(exc).__name__}:{exc}",
        }

    def identity(row: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(row.get("finding_type") or ""),
            str(row.get("path") or ""),
            str(row.get("symbol") or ""),
            str(row.get("node_sha256") or ""),
        )

    parent_ids = {identity(row) for row in parent_facts.get("findings") or []}
    findings: list[dict[str, Any]] = []
    for source in current_facts.get("findings") or []:
        row = {
            key: source.get(key)
            for key in (
                "finding_type",
                "confidence",
                "path",
                "symbol",
                "node_type",
                "node_id",
                "node_sha256",
                "line",
                "end_line",
                "observed_source",
                "explanation",
                "evidence",
                "request_token_overlap",
            )
        }
        relation = (
            "proposal_changed_symbol"
            if (str(row["path"]), str(row["symbol"])) in changed_symbols
            else "proposal_changed_path"
            if str(row["path"]) in changed_paths
            else "retained_parent_hypothesis"
            if identity(source) in parent_ids
            else "introduced_by_proposal"
        )
        row.update(
            {
                "finding_class": "generic_review_hypothesis",
                "proposal_relation": relation,
                "semantic_correctness_inferred": False,
            }
        )
        row["finding_hash"] = canonical_json_hash(row)
        findings.append(row)
    findings.sort(
        key=lambda row: (
            0
            if row["proposal_relation"] == "proposal_changed_symbol"
            else 1
            if row["proposal_relation"] == "proposal_changed_path"
            else 2
            if row["proposal_relation"] == "introduced_by_proposal"
            else 3,
            -float(row.get("confidence") or 0.0),
            -len(row.get("request_token_overlap") or []),
            str(row.get("path") or ""),
            int(row.get("line") or 0),
        )
    )
    proposal_related = [
        row
        for row in findings
        if row["proposal_relation"] != "retained_parent_hypothesis"
    ]
    return {
        "finding_count": min(len(proposal_related), MAX_GENERIC_HYPOTHESES),
        "findings": proposal_related[:MAX_GENERIC_HYPOTHESES],
        "untruncated_finding_count": len(proposal_related),
        "retained_parent_hypothesis_count": sum(
            row["proposal_relation"] == "retained_parent_hypothesis"
            for row in findings
        ),
        "all_detected_hypothesis_count": len(findings),
    }


def _obligation_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    kind = str(row.get("kind") or row.get("finding_type") or "")
    if kind == "documented_optional_parameter_unused":
        return (
            kind,
            row.get("path"),
            row.get("function_symbol"),
            row.get("parameter"),
        )
    if kind == "formal_origin_mismatch":
        return (
            kind,
            row.get("caller_path"),
            row.get("caller_symbol"),
            row.get("callee_path"),
            row.get("callee_symbol"),
            row.get("call_kind"),
            row.get("call_slot"),
            row.get("callee_formal_parameter"),
            row.get("observed_origin"),
            row.get("obligation_occurrence_index"),
        )
    return (
        kind,
        row.get("path"),
        row.get("caller_symbol") or row.get("function_symbol"),
        row.get("requested_parameter") or row.get("parameter"),
        row.get("contract_id"),
    )


def _editable_nodes(findings: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for finding in findings:
        sites = list(finding.get("candidate_sites") or [])
        if not sites and (
            finding.get("path") or finding.get("caller_path")
        ) and (finding.get("observed_source") or finding.get("call_source")):
            sites = [finding]
        for site in sites:
            identity = (
                str(site.get("path") or site.get("caller_path") or ""),
                str(site.get("node_id") or ""),
                str(
                    site.get("observed_source")
                    or site.get("observed_expression")
                    or site.get("call_source")
                    or ""
                ),
            )
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(dict(site))
    return rows


def build_public_contract_flow_packet(
    parent_package: str | Path,
    current_package: str | Path,
    request_text: str,
) -> dict[str, Any]:
    parent = Path(parent_package).resolve()
    current = Path(current_package).resolve()
    _assert_public_package(parent)
    _assert_public_package(current)
    changed_paths_list, changed_symbols = _changed_symbols(parent, current)
    changed_paths = set(changed_paths_list)
    documented = _documented_unused_parameter_facts(current)
    parameter_flow = _parameter_flow_facts(current, request_text)
    source_flow = _global_formal_origin_mismatches(
        current,
        request_text,
        changed_symbols=changed_symbols,
    )
    source_findings = list(source_flow["findings"])
    all_obligations = [
        *documented["findings"],
        *parameter_flow["findings"],
        *source_findings,
    ]
    all_obligations.sort(
        key=lambda row: (
            0 if str(row.get("kind")) != "formal_origin_mismatch" else 1,
            -float(row.get("confidence") or 0.0),
            -int((row.get("localization_evidence") or {}).get("score") or 0),
            str(row.get("path") or row.get("caller_path") or ""),
            int(row.get("line") or row.get("call_line") or 0),
        )
    )
    obligations = all_obligations[:MAX_STRONG_OBLIGATIONS]
    generic = _generic_hypotheses(
        parent,
        current,
        request_text,
        changed_paths=changed_paths,
        changed_symbols=changed_symbols,
    )
    prompt_findings = [*obligations, *generic["findings"]]
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": (
            "contract_flow_obligations_available"
            if obligations
            else "generic_hypotheses_only"
            if generic["findings"]
            else "abstain_no_structural_signal"
        ),
        "method": METHOD_ID,
        "generic_invariant": (
            "Treat documentation/signature joins, visible parameter def-use disconnects, and "
            "same-named caller-to-callee origin mismatches as typed repair obligations. Treat "
            "other AST anomalies only as review hypotheses."
        ),
        "confidence_policy": {
            "contract_backed_obligations_may_drive_one_bounded_revision": True,
            "generic_review_hypotheses_are_not_error_verdicts": True,
            "semantic_correctness_inferred": False,
            "typed_obligations_form_a_set_level_completeness_contract": True,
            "abstain_when_no_public_evidence_justifies_change": True,
        },
        "localization_decision": {
            "decision": "review_typed_obligation_subgraph" if obligations else "soft_review_only",
            "contract_obligation_count": len(obligations),
            "contract_obligation_count_untruncated": len(all_obligations),
            "generic_hypothesis_count": len(generic["findings"]),
        },
        "constraints": {
            "read_complete_visible_package": True,
            "preserve_public_signatures_and_defaults": True,
            "obligation_nodes_are_not_an_edit_whitelist": True,
            "maximum_revision_edits": 3,
        },
        "findings": prompt_findings,
        "def_use_findings": [
            row
            for row in obligations
            if "flow" in str(row.get("kind") or "")
            or str(row.get("kind")) == "formal_origin_mismatch"
        ],
        "editable_nodes": _editable_nodes(obligations),
        "proposal_context": {
            "changed_paths": changed_paths_list,
            "changed_symbols": [
                {"path": path, "symbol": symbol}
                for path, symbol in sorted(changed_symbols)
            ],
        },
        "component_counts": {
            "documented_unused_parameter": documented["finding_count"],
            "request_parameter_flow": parameter_flow["finding_count"],
            "global_formal_origin_mismatch": source_flow["finding_count"],
            "included_formal_origin_mismatch": sum(
                str(row.get("kind")) == "formal_origin_mismatch"
                for row in obligations
            ),
            "generic_hypothesis": len(generic["findings"]),
            "generic_hypothesis_untruncated": generic.get(
                "untruncated_finding_count", generic["finding_count"]
            ),
            "retained_parent_hypothesis_excluded_from_prompt": generic.get(
                "retained_parent_hypothesis_count", 0
            ),
            "all_detected_generic_hypothesis": generic.get(
                "all_detected_hypothesis_count", generic["finding_count"]
            ),
        },
        "obligation_identities": [list(_obligation_identity(row)) for row in obligations],
        "all_obligation_identities": [
            list(_obligation_identity(row)) for row in all_obligations
        ],
        "parameter_flow_abstentions": parameter_flow["abstentions"],
        "source_flow_parse_errors": source_flow["parse_errors"],
        "source_flow_duplicate_function_names_skipped": source_flow[
            "duplicate_function_names_skipped"
        ],
        "source_scope": (
            "public request, public SKILL.md, visible runtime scripts, and actual parent-to-current diff"
        ),
        "parent_tree_hash": _tree_hash(parent),
        "current_tree_hash": _tree_hash(current),
        "benchmark_bundled_visible_ast_facts_consumed": False,
        "d38_evolution_facts_consumed": False,
        "hidden_artifacts_consumed": False,
        "task_verifier_consumed": False,
        "gold_or_oracle_consumed": False,
        "reward_consumed": False,
        "claim_boundary": (
            "The packet identifies public structural obligations and review hypotheses. It can "
            "support localization, closure, compatibility checks, and abstention, but it does not "
            "establish task-level semantic correctness."
        ),
    }
    result["facts_hash"] = canonical_json_hash(result)
    return result


def validate_public_contract_flow_packet(packet: dict[str, Any]) -> str:
    body = dict(packet)
    expected = str(body.pop("facts_hash", ""))
    if not expected or canonical_json_hash(body) != expected:
        raise ValueError("public_contract_flow_facts_hash_invalid")
    if packet.get("method") != METHOD_ID:
        raise ValueError("public_contract_flow_method_invalid")
    forbidden = (
        "benchmark_bundled_visible_ast_facts_consumed",
        "d38_evolution_facts_consumed",
        "hidden_artifacts_consumed",
        "task_verifier_consumed",
        "gold_or_oracle_consumed",
        "reward_consumed",
    )
    if any(packet.get(field) is not False for field in forbidden):
        raise ValueError("public_contract_flow_scope_invalid")
    return expected


def build_public_contract_flow_report(
    parent_package: str | Path,
    candidate_package: str | Path,
    request_text: str,
    *,
    frozen_raw_facts: dict[str, Any],
) -> dict[str, Any]:
    parent = Path(parent_package).resolve()
    candidate = Path(candidate_package).resolve()
    facts_hash = validate_public_contract_flow_packet(frozen_raw_facts)
    base = build_proposal_first_closure_report(
        parent,
        candidate,
        request_text,
        visible_structural_facts=frozen_raw_facts,
    )
    candidate_scan_reused_frozen_packet = (
        frozen_raw_facts.get("current_tree_hash") == _tree_hash(candidate)
    )
    if candidate_scan_reused_frozen_packet:
        candidate_facts = frozen_raw_facts
        candidate_error = None
    else:
        try:
            candidate_facts = build_public_contract_flow_packet(
                parent, candidate, request_text
            )
            candidate_error = None
        except (OSError, SyntaxError, TypeError, ValueError) as exc:
            candidate_facts = None
            candidate_error = f"{type(exc).__name__}:{exc}"
    parent_prompt_ids = {
        tuple(row) for row in frozen_raw_facts.get("obligation_identities") or []
    }
    parent_all_ids = {
        tuple(row)
        for row in frozen_raw_facts.get("all_obligation_identities")
        or frozen_raw_facts.get("obligation_identities")
        or []
    }
    candidate_all_ids = {
        tuple(row)
        for row in (candidate_facts or {}).get("all_obligation_identities")
        or (candidate_facts or {}).get("obligation_identities")
        or []
    }
    initial_count = len(parent_prompt_ids)
    initial_count_untruncated = len(parent_all_ids)
    residual_count = len(candidate_all_ids & parent_prompt_ids)
    new_count = len(candidate_all_ids - parent_all_ids)
    obligation_checks = {
        "candidate_contract_flow_scan_available": candidate_facts is not None,
        "no_new_contract_flow_obligations": new_count == 0,
        "all_visible_contract_flow_obligations_resolved_when_present": (
            initial_count == 0 or residual_count == 0
        ),
    }
    checks = dict(base.get("checks") or {})
    if initial_count:
        checks.update(obligation_checks)
    else:
        checks["candidate_contract_flow_scan_available"] = obligation_checks[
            "candidate_contract_flow_scan_available"
        ]
        checks["no_new_contract_flow_obligations"] = obligation_checks[
            "no_new_contract_flow_obligations"
        ]
    result = dict(base)
    result.pop("gate_hash", None)
    result.update(
        {
            "schema_version": SCHEMA_VERSION,
            "method": METHOD_ID,
            "checks": checks,
            "failed_checks": sorted(name for name, passed in checks.items() if not passed),
            "decision": ACCEPT if all(checks.values()) else "REVISE",
            "frozen_raw_contract_flow_facts_hash": facts_hash,
            "contract_flow_residual": {
                "initial_obligation_count": initial_count,
                "initial_obligation_count_untruncated": initial_count_untruncated,
                "residual_initial_obligation_count": residual_count,
                "new_obligation_count": new_count,
                "resolved_obligation_count": initial_count - residual_count,
                "candidate_facts_hash": (
                    candidate_facts.get("facts_hash") if candidate_facts else None
                ),
                "candidate_scan_reused_frozen_packet": (
                    candidate_scan_reused_frozen_packet
                ),
                "candidate_scan_error": candidate_error,
            },
            "benchmark_bundled_visible_ast_facts_consumed": False,
            "d38_evolution_facts_consumed": False,
            "hidden_artifacts_consumed": False,
            "task_verifier_consumed": False,
            "gold_or_oracle_consumed": False,
            "reward_consumed": False,
            "claim_boundary": (
                "Acceptance establishes parseability, bounded visible scope, public API preservation, "
                "no new typed obligation, and complete closure of every visible initial typed "
                "obligation when present. It is not a task-level "
                "semantic correctness verdict."
            ),
        }
    )
    result["gate_hash"] = canonical_json_hash(result)
    return result


def materializer_report_adapter(
    parent_package: str | Path,
    candidate_package: str | Path,
    request_text: str,
    *,
    parent_advisory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if parent_advisory is None:
        raise ValueError("frozen_public_contract_flow_facts_required")
    return build_public_contract_flow_report(
        parent_package,
        candidate_package,
        request_text,
        frozen_raw_facts=parent_advisory,
    )
