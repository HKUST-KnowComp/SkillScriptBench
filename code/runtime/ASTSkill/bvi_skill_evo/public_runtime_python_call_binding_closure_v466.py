from __future__ import annotations

import ast
import copy
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from bvi_skill_evo import public_runtime_python_call_binding_closure_v464 as base
from bvi_skill_evo.balanced_hybrid_ast_v316 import _assert_public_package, _tree_hash
from skillscriptbench.io_utils import canonical_json_hash, sha256_file


SCHEMA_VERSION = "4.66-public-runtime-python-call-binding-closure-v2"
METHOD_ID = "public_runtime_python_call_binding_closure_v2"
FAMILY = base.FAMILY
MAX_CLOSURE_EDITS = base.MAX_CLOSURE_EDITS
MAX_CALLER_SYMBOLS = 4
MAX_CHANGED_PATHS = base.MAX_CHANGED_PATHS
MIN_CONFIDENCE = base.MIN_CONFIDENCE
EVIDENCE_CONFIDENCE = {
    **base.EVIDENCE_CONFIDENCE,
    "repeated_target_formal_across_callers": 0.975,
}


def _binding_rows(
    graph: dict[str, Any], caller: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    abstentions: list[dict[str, Any]] = []
    caller_parameters = set(caller["parameters"])
    source = caller["file_record"]["source"]
    parents = caller["file_record"]["parents"]
    for call in base._calls_owned_by(caller):
        callee, reason = base._resolve_call(graph, caller, call)
        if reason:
            abstentions.append(
                {
                    "path": caller["path"],
                    "caller_symbol": caller["symbol"],
                    "line": int(call.lineno),
                    "reason": reason,
                }
            )
        if callee is None:
            continue
        mapped: list[tuple[str, ast.expr, str]] = []
        if not any(isinstance(value, ast.Starred) for value in call.args):
            for index, value in enumerate(call.args):
                if index < len(callee["positional"]):
                    mapped.append((callee["positional"][index], value, f"positional:{index}"))
        callee_parameters = set(callee["parameters"])
        for keyword in call.keywords:
            if keyword.arg is not None and keyword.arg in callee_parameters:
                mapped.append((keyword.arg, keyword.value, f"keyword:{keyword.arg}"))
        call_id = (
            f"{caller['path']}::{caller['symbol']}::"
            f"{int(call.lineno)}:{int(call.col_offset)}::{callee['symbol']}"
        )
        call_source = ast.get_source_segment(source, call) or ""
        for formal, value, slot in mapped:
            if not isinstance(value, ast.Name) or not isinstance(value.ctx, ast.Load):
                continue
            actual = value.id
            if actual not in caller_parameters:
                continue
            parent = parents.get(value)
            rows.append(
                {
                    "path": caller["path"],
                    "caller_symbol": caller["symbol"],
                    "callee_symbol": callee["symbol"],
                    "call_id": call_id,
                    "call_line": int(call.lineno),
                    "call_source": call_source,
                    "formal_parameter": formal,
                    "actual_parameter": actual,
                    "formal_available_as_caller_parameter": formal in caller_parameters,
                    "argument_slot": slot,
                    "is_identity": formal == actual,
                    "node": value,
                    "node_role": (
                        f"keyword:{parent.arg}"
                        if isinstance(parent, ast.keyword)
                        else slot
                    ),
                    "caller_parameter_count": len(caller_parameters),
                }
            )
    return rows, abstentions


def _evidence_for_bindings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_call: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_caller: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    eligible_mismatches: list[dict[str, Any]] = []
    for row in rows:
        by_call[row["call_id"]].append(row)
        by_caller[(row["path"], row["caller_symbol"])].append(row)
        if not row["is_identity"] and row["formal_available_as_caller_parameter"]:
            eligible_mismatches.append(row)

    by_target_formal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in eligible_mismatches:
        by_target_formal[row["formal_parameter"]].append(row)

    selected: list[dict[str, Any]] = []
    for row in eligible_mismatches:
        evidence: set[str] = set()
        same_call = by_call[row["call_id"]]
        same_caller = by_caller[(row["path"], row["caller_symbol"])]
        if any(
            other["formal_parameter"] == row["actual_parameter"]
            and other["actual_parameter"] == row["formal_parameter"]
            for other in same_call
        ):
            evidence.add("reciprocal_same_call")
        if any(
            other["actual_parameter"] == row["actual_parameter"]
            and other["formal_parameter"] != row["formal_parameter"]
            for other in same_call
        ):
            evidence.add("duplicate_displacement")
        if any(
            other["formal_parameter"] == row["actual_parameter"]
            and other["actual_parameter"] == row["formal_parameter"]
            and other["call_id"] != row["call_id"]
            for other in same_caller
        ):
            evidence.add("reciprocal_across_calls")
        if sum(
            other["formal_parameter"] == row["formal_parameter"]
            and other["actual_parameter"] == row["actual_parameter"]
            for other in same_caller
        ) >= 2:
            evidence.add("repeated_pair")
        call_mismatches = [
            other
            for other in same_call
            if not other["is_identity"]
            and other["formal_available_as_caller_parameter"]
        ]
        call_anchors = [other for other in same_call if other["is_identity"]]
        if len(call_mismatches) == 1 and call_anchors:
            evidence.add("anchored_single_outlier")
        same_target = by_target_formal[row["formal_parameter"]]
        if (
            len({(other["path"], other["caller_symbol"]) for other in same_target}) >= 2
            and len({other["actual_parameter"] for other in same_target}) >= 2
        ):
            evidence.add("repeated_target_formal_across_callers")
        if not evidence:
            continue
        confidence = max(EVIDENCE_CONFIDENCE[value] for value in evidence)
        if confidence < MIN_CONFIDENCE:
            continue
        selected.append({**row, "evidence": sorted(evidence), "confidence": confidence})

    unique: dict[str, dict[str, Any]] = {}
    for row in selected:
        node_id = base._node_id(row["path"], row["node"])
        current = unique.get(node_id)
        if current is None or float(row["confidence"]) > float(current["confidence"]):
            unique[node_id] = row
    return sorted(
        unique.values(),
        key=lambda row: (
            row["path"],
            int(row["node"].lineno),
            int(row["node"].col_offset),
        ),
    )


def _derive_candidates(package: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    graph = base._build_public_graph(package)
    binding_rows: list[dict[str, Any]] = []
    abstentions: list[dict[str, Any]] = []
    for caller in graph["functions"]:
        rows, skipped = _binding_rows(graph, caller)
        binding_rows.extend(rows)
        abstentions.extend(skipped)
    selected = _evidence_for_bindings(binding_rows)
    diagnostics = {
        "python_file_count": len(graph["files"]),
        "function_count": len(graph["functions"]),
        "resolved_caller_parameter_binding_count": len(binding_rows),
        "formal_available_binding_count": sum(
            row["formal_available_as_caller_parameter"] for row in binding_rows
        ),
        "identity_binding_count": sum(row["is_identity"] for row in binding_rows),
        "mismatch_binding_count": sum(not row["is_identity"] for row in binding_rows),
        "high_confidence_candidate_count": len(selected),
        "resolution_abstentions": abstentions[:32],
        "resolution_abstention_count": len(abstentions),
    }
    return selected, diagnostics


def build_public_python_call_binding_closure(
    package_root: str | Path, request_text: str = ""
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    _assert_public_package(package)
    candidates, diagnostics = _derive_candidates(package)
    caller_count = len(
        {(str(row["path"]), str(row["caller_symbol"])) for row in candidates}
    )
    path_count = len({str(row["path"]) for row in candidates})
    abstain_reason: str | None = None
    if not candidates:
        abstain_reason = "no_high_confidence_call_binding_closure"
    elif len(candidates) > MAX_CLOSURE_EDITS:
        abstain_reason = "call_binding_closure_exceeds_node_bound"
    elif caller_count > MAX_CALLER_SYMBOLS:
        abstain_reason = "call_binding_closure_exceeds_caller_bound"
    elif path_count > MAX_CHANGED_PATHS:
        abstain_reason = "call_binding_closure_exceeds_path_bound"
    selected = candidates if abstain_reason is None else []
    editable = [base._editable_node(package, row) for row in selected]
    obligations = [
        base._public_obligation(row, node)
        for row, node in zip(selected, editable, strict=True)
    ]
    decision = "PROPOSE" if editable else "ABSTAIN"
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "source_scope": "public_parent_python_scripts_only",
        "package_tree_hash": _tree_hash(package),
        "localization_decision": {
            "decision": decision,
            "selected_family": FAMILY if editable else None,
            "confidence": min(
                (float(row["confidence"]) for row in selected), default=0.0
            ),
            "candidate_count_before_bounds": len(candidates),
            "selected_obligation_count": len(obligations),
            "editable_node_count": len(editable),
            "caller_symbol_count": caller_count if editable else 0,
            "changed_path_upper_bound": path_count if editable else 0,
            "abstain_reason": abstain_reason,
        },
        "obligations": obligations,
        "editable_nodes": editable,
        "diagnostics": diagnostics,
        "evidence_counts": dict(
            sorted(
                Counter(
                    evidence
                    for row in selected
                    for evidence in row["evidence"]
                ).items()
            )
        ),
        "edit_contract": {
            "maximum_edits": MAX_CLOSURE_EDITS,
            "maximum_caller_symbols": MAX_CALLER_SYMBOLS,
            "maximum_changed_paths": MAX_CHANGED_PATHS,
            "required_edit_count": len(editable),
            "atomic_closure_required": len(editable) > 1,
            "exact_name_node_binding_required": True,
            "outside_selected_nodes_preserved_by_construction": True,
            "public_signatures_must_be_preserved": True,
            "residual_call_binding_check_required": True,
            "semantic_correctness_inferred": False,
        },
        "request_text_consumed": False,
        "skill_markdown_consumed": False,
        "benchmark_bundled_facts_consumed": False,
        "hidden_artifacts_consumed": False,
        "task_verifier_consumed": False,
        "gold_or_oracle_consumed": False,
        "reward_consumed": False,
        "semantic_correctness_inferred": False,
        "claim_boundary": (
            "The packet reports a bounded package-local caller/callee binding anomaly. "
            "It does not certify the intended runtime semantics."
        ),
    }
    result["facts_hash"] = canonical_json_hash(result)
    return result


def validate_public_python_call_binding_closure(
    facts: dict[str, Any], package_root: str | Path
) -> str:
    body = dict(facts)
    expected = str(body.pop("facts_hash", ""))
    if not expected or canonical_json_hash(body) != expected:
        raise ValueError("call_binding_closure_v2_facts_hash_invalid")
    if facts.get("schema_version") != SCHEMA_VERSION or facts.get("method") != METHOD_ID:
        raise ValueError("call_binding_closure_v2_method_invalid")
    if any(
        facts.get(field) is not False
        for field in (
            "benchmark_bundled_facts_consumed",
            "hidden_artifacts_consumed",
            "task_verifier_consumed",
            "gold_or_oracle_consumed",
            "reward_consumed",
            "semantic_correctness_inferred",
        )
    ):
        raise ValueError("call_binding_closure_v2_scope_invalid")
    package = Path(package_root).resolve()
    _assert_public_package(package)
    if facts.get("package_tree_hash") != _tree_hash(package):
        raise ValueError("call_binding_closure_v2_package_changed")
    registry = {str(row["node_id"]): row for row in base._all_name_nodes(package)}
    editable = list(facts.get("editable_nodes") or [])
    obligations = list(facts.get("obligations") or [])
    if len(editable) > MAX_CLOSURE_EDITS or len(editable) != len(obligations):
        raise ValueError("call_binding_closure_v2_editable_shape_invalid")
    if len({str(row.get("node_id")) for row in editable}) != len(editable):
        raise ValueError("call_binding_closure_v2_duplicate_node")
    for row in editable:
        current = registry.get(str(row.get("node_id")))
        if current is None or current.get("node_type") != "Name":
            raise ValueError("call_binding_closure_v2_unknown_name_node")
        if str(row.get("node_sha256")) != str(current.get("node_sha256")):
            raise ValueError("call_binding_closure_v2_node_hash_mismatch")
        if str(row.get("file_sha256")) != sha256_file(package / str(row["path"])):
            raise ValueError("call_binding_closure_v2_file_hash_mismatch")
    decision = str((facts.get("localization_decision") or {}).get("decision"))
    if decision == "PROPOSE" and not editable:
        raise ValueError("call_binding_closure_v2_empty_proposal")
    if decision == "ABSTAIN" and (editable or obligations):
        raise ValueError("call_binding_closure_v2_abstain_has_editable_content")
    return expected


def build_same_package_wrong_call_binding_closure(
    package_root: str | Path, real_facts: dict[str, Any]
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    validate_public_python_call_binding_closure(real_facts, package)
    result = copy.deepcopy(real_facts)
    result.pop("facts_hash", None)
    real_nodes = list(real_facts.get("editable_nodes") or [])
    if not real_nodes:
        result["control_metadata"] = {
            "control": "same-package-wrong-call-binding-closure-v2",
            "real_node_overlap_count": 0,
            "same_editable_node_count": True,
        }
        result["facts_hash"] = canonical_json_hash(result)
        return result
    registry = base._all_name_nodes(package)
    forbidden = {str(row["node_id"]) for row in real_nodes}
    selected: list[dict[str, Any]] = []
    tiers: list[str] = []
    for real in real_nodes:
        used = {str(row["node_id"]) for row in selected}
        candidates = [
            row for row in registry if str(row["node_id"]) not in forbidden | used
        ]
        same_symbol_role = [
            row
            for row in candidates
            if row.get("symbol") == real.get("symbol")
            and row.get("role") == real.get("role")
        ]
        same_symbol = [
            row for row in candidates if row.get("symbol") == real.get("symbol")
        ]
        same_path = [row for row in candidates if row.get("path") == real.get("path")]
        if same_symbol_role:
            pool, tier = same_symbol_role, "same_symbol_and_argument_role"
        elif same_symbol:
            pool, tier = same_symbol, "same_symbol"
        elif same_path:
            pool, tier = same_path, "same_path"
        else:
            pool, tier = candidates, "same_package"
        pool.sort(
            key=lambda row: (
                abs(int(row["line"]) - int(real["line"])),
                abs(
                    len(str(row["observed_source"]))
                    - len(str(real["observed_source"]))
                ),
                str(row["node_id"]),
            )
        )
        if not pool:
            raise ValueError("call_binding_closure_v2_sham_decoy_unavailable")
        selected.append(copy.deepcopy(pool[0]))
        tiers.append(tier)
    result["editable_nodes"] = selected
    result["control_metadata"] = {
        "control": "same-package-wrong-call-binding-closure-v2",
        "construction": "deterministic_shape_matched_package_local_name_derangement",
        "real_node_overlap_count": len(
            forbidden & {str(row["node_id"]) for row in selected}
        ),
        "same_editable_node_count": len(selected) == len(real_nodes),
        "shape_matching_tiers": tiers,
        "hidden_or_verifier_feedback_used": False,
    }
    result["facts_hash"] = canonical_json_hash(result)
    validate_public_python_call_binding_closure(result, package)
    return result


def residual_obligations(
    candidate_package: str | Path, frozen_facts: dict[str, Any]
) -> list[dict[str, Any]]:
    current = build_public_python_call_binding_closure(candidate_package)
    target_keys = {
        (
            str(row["path"]),
            str(row["caller_symbol"]),
            str(row["callee_symbol"]),
            str(row["callee_formal_parameter"]),
        )
        for row in frozen_facts.get("obligations") or []
    }
    return [
        row
        for row in current.get("obligations") or []
        if (
            str(row["path"]),
            str(row["caller_symbol"]),
            str(row["callee_symbol"]),
            str(row["callee_formal_parameter"]),
        )
        in target_keys
    ]


__all__ = [
    "FAMILY",
    "MAX_CLOSURE_EDITS",
    "METHOD_ID",
    "build_public_python_call_binding_closure",
    "build_same_package_wrong_call_binding_closure",
    "residual_obligations",
    "validate_public_python_call_binding_closure",
]
