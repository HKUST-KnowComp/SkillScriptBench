from __future__ import annotations

import ast
import copy
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from bvi_skill_evo.balanced_hybrid_ast_v316 import _assert_public_package, _tree_hash
from bvi_skill_evo.public_contract_flow_ast_v358 import (
    _documented_unused_parameter_facts,
    _parameter_flow_facts,
    _request_contracts,
)
from bvi_skill_evo import public_runtime_python_parameter_effect_v424 as single
from bvi_skill_evo.python_span_patch_v246 import (
    ACCEPT_STRUCTURALLY,
    PYTHON_SPAN_PATCH_TOOL,
    apply_python_span_patch,
)
from skillscriptbench.io_utils import canonical_json_hash, hash_tree, sha256_file


SCHEMA_VERSION = "4.38-public-runtime-python-parameter-closure-v1"
METHOD_ID = "public_runtime_python_parameter_flow_closure_v1"
FAMILY = "parameter_flow_closure"
MAX_CLOSURE_EDITS = 2
MIN_CONFIDENCE = 0.90
SUPPORTED_FINDING_KINDS = {
    "argument_to_resolver_return_disconnect",
    "parameter_to_local_alias_disconnect",
    "documented_optional_parameter_unused",
}

PYTHON_CLOSURE_PATCH_TOOL = copy.deepcopy(PYTHON_SPAN_PATCH_TOOL)
PYTHON_CLOSURE_PATCH_TOOL["function"]["description"] = (
    "Submit zero, one, or two exact Python AST-expression replacements for one visible "
    "parameter-flow closure. Every listed obligation must be repaired together. The nodes "
    "localize structural disconnects but do not reveal replacement semantics."
)
PYTHON_CLOSURE_PATCH_TOOL["function"]["parameters"]["properties"]["edits"][
    "maxItems"
] = MAX_CLOSURE_EDITS


def _finding_priority(kind: str) -> int:
    return {
        "argument_to_resolver_return_disconnect": 0,
        "parameter_to_local_alias_disconnect": 1,
        "documented_optional_parameter_unused": 2,
    }.get(kind, 99)


def _group_findings(
    findings: Iterable[dict[str, Any]], nodes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for source in findings:
        row = dict(source)
        if str(row.get("kind")) not in SUPPORTED_FINDING_KINDS:
            continue
        key = single._finding_key(row)
        if all(key):
            grouped[key].append(row)

    obligations: list[dict[str, Any]] = []
    for (path, function, parameter), rows in sorted(grouped.items()):
        mapped: list[tuple[int, float, int, dict[str, Any], dict[str, Any]]] = []
        for finding in rows:
            confidence = float(finding.get("confidence") or 0.0)
            if confidence < MIN_CONFIDENCE:
                continue
            for site in single._candidate_sites(finding):
                runtime = single._map_site_to_runtime_node(site, nodes)
                if runtime is not None:
                    mapped.append(
                        (
                            _finding_priority(str(finding.get("kind"))),
                            -confidence,
                            int(site.get("rank") or 1),
                            finding,
                            runtime,
                        )
                    )
        if not mapped:
            continue
        mapped.sort(key=lambda item: (item[0], item[1], item[2], str(item[4]["site_id"])))
        best = mapped[0]
        if len(mapped) > 1 and best[:3] == mapped[1][:3] and str(best[4]["site_id"]) != str(
            mapped[1][4]["site_id"]
        ):
            continue
        primary, node = best[3], best[4]
        kinds = sorted({str(row.get("kind")) for row in rows})
        confidence = max(float(row.get("confidence") or 0.0) for row in rows)
        defaults = sorted(
            {
                str(row.get("default_expression"))
                for row in rows
                if row.get("default_expression") is not None
            }
        )
        obligations.append(
            {
                "family": FAMILY,
                "confidence": round(confidence, 4),
                "path": path,
                "function_symbol": function,
                "parameter": parameter,
                "compatibility_defaults": defaults,
                "source_finding_kinds": kinds,
                "primary_source_finding_kind": min(kinds, key=_finding_priority),
                "source_finding_hashes": sorted(
                    str(row.get("finding_hash"))
                    for row in rows
                    if row.get("finding_hash")
                ),
                "contract_ids": sorted(
                    str(row.get("contract_id"))
                    for row in rows
                    if row.get("contract_id")
                ),
                "repair_goal": (
                    "Restore this documented parameter-flow path while preserving the public "
                    "signature and compatibility default."
                ),
                "editable_node": node,
                "semantic_correctness_inferred": False,
            }
        )
    return obligations


def _node_from_editable(
    source: str, editable: dict[str, Any]
) -> tuple[ast.Module, ast.expr] | None:
    tree = ast.parse(source)
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.expr)
        and type(node).__name__ == str(editable["node_type"])
        and getattr(node, "lineno", None) == int(editable["line"])
        and getattr(node, "col_offset", None) == int(editable["column"])
        and getattr(node, "end_lineno", None) == int(editable["end_line"])
        and getattr(node, "end_col_offset", None) == int(editable["end_column"])
    ]
    return (tree, matches[0]) if len(matches) == 1 else None


def _terminal_call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _unique_mapping_key(branch: ast.AST) -> str | None:
    values = {
        str(node.args[0].value)
        for node in ast.walk(branch)
        if isinstance(node, ast.Call)
        and _terminal_call_name(node) == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    return next(iter(values)) if len(values) == 1 else None


def _branch_translation_ambiguity(
    package: Path, obligation: dict[str, Any]
) -> dict[str, Any] | None:
    if str(obligation.get("parameter") or "").casefold() not in {"field", "key"}:
        return None
    editable = single._editable_view(package, obligation["editable_node"])
    source = (package / str(editable["path"])).read_text(encoding="utf-8")
    resolved = _node_from_editable(source, editable)
    if resolved is None:
        return None
    tree, target = resolved
    if not isinstance(target, ast.Constant) or not isinstance(target.value, str):
        return None
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    current: ast.AST | None = target
    while current is not None and not isinstance(
        current, (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        if isinstance(current, ast.IfExp):
            body_has_target = any(node is target for node in ast.walk(current.body))
            else_has_target = any(node is target for node in ast.walk(current.orelse))
            if body_has_target == else_has_target:
                return None
            sibling = current.orelse if body_has_target else current.body
            sibling_key = _unique_mapping_key(sibling)
            if sibling_key is not None and sibling_key != target.value:
                return {
                    "reason": "conditional_schema_translation_has_two_publicly_plausible_keys",
                    "path": editable["path"],
                    "function_symbol": obligation["function_symbol"],
                    "parameter": obligation["parameter"],
                    "selected_literal": target.value,
                    "sibling_mapping_key": sibling_key,
                    "semantic_correctness_inferred": False,
                }
            return None
        current = parents.get(current)
    return None


def _select_closure(
    obligations: list[dict[str, Any]], request_text: str
) -> tuple[list[dict[str, Any]], str | None, list[dict[str, Any]]]:
    contracts = _request_contracts(request_text)
    unique_contracts: list[tuple[str, str]] = []
    for row in contracts:
        identity = (str(row["function"]), str(row["parameter"]))
        if identity not in unique_contracts:
            unique_contracts.append(identity)
    if len(unique_contracts) > MAX_CLOSURE_EDITS:
        return [], "request_contract_count_exceeds_bounded_closure", contracts
    if unique_contracts:
        selected: list[dict[str, Any]] = []
        for function, parameter in unique_contracts:
            matches = [
                row
                for row in obligations
                if row["function_symbol"] == function and row["parameter"] == parameter
            ]
            if len(matches) != 1:
                return [], "request_contract_not_uniquely_localized", contracts
            selected.append(matches[0])
        return selected, None, contracts
    if len(obligations) == 1:
        return [obligations[0]], None, contracts
    return (
        [],
        "no_supported_parameter_flow_contract"
        if not obligations
        else "multiple_unbound_parameter_flow_obligations",
        contracts,
    )


def build_public_python_parameter_closure(
    package_root: str | Path, request_text: str
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    _assert_public_package(package)
    documented = _documented_unused_parameter_facts(package)
    parameter_flow = _parameter_flow_facts(package, request_text)
    source_packet = {
        "derivation": "public_document_signature_and_request_parameter_flow_only",
        "documented_unused_parameter": documented,
        "request_parameter_flow": parameter_flow,
    }
    findings = [
        row
        for row in [
            *(documented.get("findings") or []),
            *(parameter_flow.get("findings") or []),
        ]
        if row.get("finding_class") == "contract_backed_obligation"
    ]
    nodes = single._python_nodes(package)
    obligations = _group_findings(findings, nodes)
    selected, abstain_reason, request_contracts = _select_closure(
        obligations, request_text
    )
    ambiguities = [
        value
        for row in selected
        if (value := _branch_translation_ambiguity(package, row)) is not None
    ]
    if ambiguities:
        selected = []
        abstain_reason = "branch_translation_semantics_ambiguous"
    editable = [
        single._editable_view(package, row["editable_node"]) for row in selected
    ]
    decision = "PROPOSE" if editable else "ABSTAIN"
    public_obligations = [
        {key: copy.deepcopy(value) for key, value in row.items() if key != "editable_node"}
        for row in selected
    ]
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "source_scope": "public_parent_request_skill_markdown_and_python_scripts_only",
        "package_tree_hash": _tree_hash(package),
        "localization_decision": {
            "decision": decision,
            "selected_family": FAMILY if selected else None,
            "confidence": min(
                (float(row["confidence"]) for row in selected), default=0.0
            ),
            "eligible_obligation_count": len(obligations),
            "request_contract_count": len(request_contracts),
            "selected_obligation_count": len(selected),
            "editable_node_count": len(editable),
            "abstain_reason": None if selected else abstain_reason,
        },
        "obligations": public_obligations,
        "obligation": public_obligations[0] if len(public_obligations) == 1 else None,
        "editable_nodes": editable,
        "alternative_obligations": [
            {
                key: copy.deepcopy(value)
                for key, value in row.items()
                if key != "editable_node"
            }
            for row in obligations[:8]
        ],
        "ambiguities": ambiguities,
        "counts": {
            "python_script_node_count": len(nodes),
            "source_contract_finding_count": len(findings),
            "eligible_parameter_flow_count": len(obligations),
            "selected_obligation_count": len(selected),
            "editable_node_count": len(editable),
        },
        "edit_contract": {
            "maximum_edits": MAX_CLOSURE_EDITS,
            "required_edit_count": len(editable),
            "atomic_closure_required": len(editable) > 1,
            "exact_node_binding_required": True,
            "outside_selected_nodes_preserved_by_construction": True,
            "public_signatures_must_be_preserved": True,
            "residual_disconnect_check_required": True,
            "semantic_correctness_inferred": False,
        },
        "source_public_contract_packet_hash": canonical_json_hash(source_packet),
        "source_component_counts": {
            "documented_unused_parameter": int(documented.get("finding_count") or 0),
            "request_parameter_flow": int(parameter_flow.get("finding_count") or 0),
            "request_parameter_flow_abstention": len(
                parameter_flow.get("abstentions") or []
            ),
        },
        "benchmark_bundled_facts_consumed": False,
        "hidden_artifacts_consumed": False,
        "task_verifier_consumed": False,
        "gold_or_oracle_consumed": False,
        "reward_consumed": False,
        "semantic_correctness_inferred": False,
        "claim_boundary": (
            "The packet localizes one bounded public parameter-flow closure. It does not "
            "supply replacement expressions or certify semantic correctness."
        ),
    }
    result["facts_hash"] = canonical_json_hash(result)
    return result


def validate_public_python_parameter_closure(
    facts: dict[str, Any], package_root: str | Path
) -> str:
    body = dict(facts)
    expected = str(body.pop("facts_hash", ""))
    if not expected or canonical_json_hash(body) != expected:
        raise ValueError("python_parameter_closure_facts_hash_invalid")
    if facts.get("schema_version") != SCHEMA_VERSION or facts.get("method") != METHOD_ID:
        raise ValueError("python_parameter_closure_method_invalid")
    if any(
        facts.get(field) is not False
        for field in (
            "benchmark_bundled_facts_consumed",
            "hidden_artifacts_consumed",
            "task_verifier_consumed",
            "gold_or_oracle_consumed",
            "reward_consumed",
        )
    ):
        raise ValueError("python_parameter_closure_scope_invalid")
    package = Path(package_root).resolve()
    _assert_public_package(package)
    if facts.get("package_tree_hash") != _tree_hash(package):
        raise ValueError("python_parameter_closure_package_changed")
    registry = {str(row["site_id"]): row for row in single._python_nodes(package)}
    editable = list(facts.get("editable_nodes") or [])
    if len(editable) > MAX_CLOSURE_EDITS or len(
        {str(row.get("node_id")) for row in editable}
    ) != len(editable):
        raise ValueError("python_parameter_closure_editable_shape_invalid")
    for row in editable:
        node = registry.get(str(row.get("node_id")))
        if node is None:
            raise ValueError("python_parameter_closure_unknown_node")
        if str(row.get("node_sha256")) != str(node.get("node_source_sha256")):
            raise ValueError("python_parameter_closure_node_hash_mismatch")
        if str(row.get("file_sha256")) != sha256_file(package / str(row["path"])):
            raise ValueError("python_parameter_closure_file_hash_mismatch")
    decision = str((facts.get("localization_decision") or {}).get("decision"))
    obligations = list(facts.get("obligations") or [])
    if decision == "PROPOSE" and not (
        1 <= len(editable) == len(obligations) <= MAX_CLOSURE_EDITS
    ):
        raise ValueError("python_parameter_closure_proposal_shape_invalid")
    if decision == "ABSTAIN" and (editable or obligations):
        raise ValueError("python_parameter_closure_abstain_has_editable_content")
    return expected


def build_same_package_wrong_closure(
    package_root: str | Path, real_facts: dict[str, Any]
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    validate_public_python_parameter_closure(real_facts, package)
    result = copy.deepcopy(real_facts)
    result.pop("facts_hash", None)
    real_nodes = list(real_facts.get("editable_nodes") or [])
    if not real_nodes:
        result["control_metadata"] = {
            "control": "same-package-wrong-python-closure",
            "real_node_overlap_count": 0,
            "same_editable_node_count": True,
        }
        result["facts_hash"] = canonical_json_hash(result)
        return result
    registry = single._python_nodes(package)
    forbidden = {str(row["node_id"]) for row in real_nodes}
    selected: list[dict[str, Any]] = []
    tiers: list[str] = []
    for real in real_nodes:
        literal_type = str((real.get("facts") or {}).get("literal_type") or "")
        candidates = [
            row
            for row in registry
            if str(row["site_id"]) not in forbidden
            and str(row["site_id"]) not in {str(value["node_id"]) for value in selected}
            and str(row["node_type"]) == str(real["node_type"])
            and str((row.get("facts") or {}).get("literal_type") or "")
            == literal_type
        ]
        strict = [row for row in candidates if str(row["role"]) == str(real["role"])]
        same_symbol = [
            row for row in candidates if str(row.get("symbol")) == str(real.get("symbol"))
        ]
        if strict:
            pool = strict
            tier = "same_node_type_parent_role_and_literal_type"
        elif same_symbol:
            pool = same_symbol
            tier = "same_node_and_literal_type_same_symbol"
        else:
            pool = candidates
            tier = "same_node_and_literal_type_same_package"
        pool.sort(
            key=lambda row: (
                str(row.get("symbol")) != str(real.get("symbol")),
                abs(
                    len(str(row.get("observed_source") or ""))
                    - len(str(real.get("observed_source") or ""))
                ),
                str(row["path"]),
                int((row.get("byte_span") or {}).get("start") or 0),
                str(row["site_id"]),
            )
        )
        if not pool:
            raise ValueError("python_parameter_closure_sham_decoy_unavailable")
        selected.append(single._editable_view(package, pool[0]))
        tiers.append(tier)
    result["editable_nodes"] = selected
    result["control_metadata"] = {
        "control": "same-package-wrong-python-closure",
        "construction": "deterministic_shape_matched_package_local_derangement",
        "real_node_overlap_count": len(
            forbidden & {str(row["node_id"]) for row in selected}
        ),
        "same_editable_node_count": len(selected) == len(real_nodes),
        "shape_matching_tiers": tiers,
        "hidden_or_verifier_feedback_used": False,
    }
    result["facts_hash"] = canonical_json_hash(result)
    validate_public_python_parameter_closure(result, package)
    return result


def apply_public_python_closure_patch(
    content: str,
    source_package: str | Path,
    candidate_package: str | Path,
    *,
    frozen_facts: dict[str, Any],
) -> dict[str, Any]:
    validate_public_python_parameter_closure(frozen_facts, source_package)
    if (frozen_facts.get("localization_decision") or {}).get("decision") != "PROPOSE":
        raise ValueError("python_parameter_closure_abstained")
    result = apply_python_span_patch(
        content,
        source_package,
        candidate_package,
        visible_ast_facts=frozen_facts,
        require_ast_binding=True,
    )
    if (result.get("structural_gate") or {}).get("decision") != ACCEPT_STRUCTURALLY:
        raise ValueError("python_parameter_closure_patch_structurally_rejected")
    synthetic_request = "\n".join(
        [
            "Repair every listed public helper contract in this skill package:",
            "",
            *[
                f"- `{row['function_symbol']}`: its optional `{row['parameter']}` parameter "
                "does not reach the documented reusable behavior."
                for row in frozen_facts.get("obligations") or []
            ],
        ]
    )
    closure_gate = analyze_candidate_closure(
        source_package,
        candidate_package,
        synthetic_request,
        frozen_facts=frozen_facts,
    )
    result["parameter_flow_closure_gate"] = closure_gate
    result["structural_gate"]["decision"] = closure_gate["decision"]
    result["structural_gate"]["checks"].update(
        {
            "all_parameter_flow_obligations_resolved": closure_gate["checks"][
                "all_parameter_flow_obligations_resolved"
            ],
            "atomic_closure_checked": True,
        }
    )
    return result


def _residual_identities(
    package: Path, request_text: str, target_identities: set[tuple[str, str]]
) -> list[dict[str, str]]:
    rows = [
        *(_documented_unused_parameter_facts(package).get("findings") or []),
        *(_parameter_flow_facts(package, request_text).get("findings") or []),
    ]
    residual = []
    for row in rows:
        identity = (single._finding_function(row), single._finding_parameter(row))
        if identity in target_identities and str(row.get("kind")) in SUPPORTED_FINDING_KINDS:
            residual.append(
                {
                    "function_symbol": identity[0],
                    "parameter": identity[1],
                    "kind": str(row.get("kind")),
                }
            )
    return residual


def analyze_candidate_closure(
    parent_package: str | Path,
    candidate_package: str | Path,
    request_text: str,
    *,
    frozen_facts: dict[str, Any],
) -> dict[str, Any]:
    parent = Path(parent_package).resolve()
    candidate = Path(candidate_package).resolve()
    validate_public_python_parameter_closure(frozen_facts, parent)
    syntax_errors: list[str] = []
    for path in sorted((candidate / "scripts").rglob("*.py")) if candidate.is_dir() else []:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError, ValueError) as exc:
            syntax_errors.append(f"{path.relative_to(candidate)}:{type(exc).__name__}:{exc}")
    parent_hashes = hash_tree(parent)
    candidate_hashes = hash_tree(candidate) if candidate.is_dir() else {}
    changed_paths = sorted(
        path
        for path in set(parent_hashes) | set(candidate_hashes)
        if parent_hashes.get(path) != candidate_hashes.get(path)
    )
    targets = {
        (str(row["function_symbol"]), str(row["parameter"]))
        for row in frozen_facts.get("obligations") or []
    }
    try:
        residual = _residual_identities(candidate, request_text, targets)
    except (OSError, SyntaxError, TypeError, ValueError) as exc:
        residual = [
            {
                "function_symbol": "",
                "parameter": "",
                "kind": f"analysis_unavailable:{type(exc).__name__}:{exc}",
            }
        ]
    checks = {
        "candidate_exists": candidate.is_dir(),
        "tree_scope_preserved": set(parent_hashes) == set(candidate_hashes),
        "python_syntax": not syntax_errors,
        "public_signatures_preserved": single._public_signatures(parent)
        == single._public_signatures(candidate),
        "changed_paths_within_bounded_python_scripts": len(changed_paths)
        <= MAX_CLOSURE_EDITS
        and all(path.startswith("scripts/") and path.endswith(".py") for path in changed_paths),
        "all_parameter_flow_obligations_resolved": not residual,
    }
    decision = ACCEPT_STRUCTURALLY if all(checks.values()) else "REVISE"
    return {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "decision": decision,
        "checks": checks,
        "changed_paths": changed_paths,
        "syntax_errors": syntax_errors,
        "residual_disconnects": residual,
        "parent_tree_hash": canonical_json_hash(parent_hashes),
        "candidate_tree_hash": canonical_json_hash(candidate_hashes),
        "hidden_semantic_correctness_checked": False,
        "claim_boundary": (
            "Acceptance establishes bounded AST edits, API preservation, and removal of the "
            "visible parameter-flow disconnects only."
        ),
    }


__all__ = [
    "PYTHON_CLOSURE_PATCH_TOOL",
    "analyze_candidate_closure",
    "apply_public_python_closure_patch",
    "build_public_python_parameter_closure",
    "build_same_package_wrong_closure",
    "validate_public_python_parameter_closure",
]
