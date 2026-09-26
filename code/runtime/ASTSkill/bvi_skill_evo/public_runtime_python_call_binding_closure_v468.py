from __future__ import annotations

import ast
import copy
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from bvi_skill_evo import public_runtime_python_call_binding_closure_v466 as base
from skillscriptbench.io_utils import canonical_json_hash


SCHEMA_VERSION = "4.68-public-runtime-python-call-binding-closure-v3"
METHOD_ID = "public_runtime_python_call_binding_closure_v3"
FAMILY = base.FAMILY
MAX_CLOSURE_EDITS = base.MAX_CLOSURE_EDITS


def _annotation_key(node: ast.expr | None) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return "".join(node.value.split())
    try:
        return "".join(ast.unparse(node).split())
    except (AttributeError, ValueError):
        return ast.dump(node, annotate_fields=False, include_attributes=False)


def _parameter_annotations(function: dict[str, Any]) -> dict[str, str]:
    arguments = function["node"].args
    values = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
    if arguments.vararg is not None:
        values.append(arguments.vararg)
    if arguments.kwarg is not None:
        values.append(arguments.kwarg)
    return {value.arg: _annotation_key(value.annotation) for value in values}


def _binding_rows(package: Path) -> list[dict[str, Any]]:
    graph = base.base._build_public_graph(package)
    rows: list[dict[str, Any]] = []
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for function in graph["functions"]:
        by_symbol[str(function["symbol"])].append(function)
    for caller in graph["functions"]:
        caller_annotations = _parameter_annotations(caller)
        caller_rows, _ = base._binding_rows(graph, caller)
        for row in caller_rows:
            callee_matches = by_symbol.get(str(row["callee_symbol"]), [])
            callee_annotations = (
                _parameter_annotations(callee_matches[0])
                if len(callee_matches) == 1
                else {}
            )
            rows.append(
                {
                    **row,
                    "caller_formal_annotation": caller_annotations.get(
                        str(row["formal_parameter"]), ""
                    ),
                    "caller_actual_annotation": caller_annotations.get(
                        str(row["actual_parameter"]), ""
                    ),
                    "callee_formal_annotation": callee_annotations.get(
                        str(row["formal_parameter"]), ""
                    ),
                }
            )
    return rows


def _suppression_map(package: Path) -> dict[str, dict[str, Any]]:
    rows = _binding_rows(package)
    suppressions: dict[str, dict[str, Any]] = {}
    by_caller_callee: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    by_call: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_caller_callee[
            (row["path"], row["caller_symbol"], row["callee_symbol"])
        ].append(row)
        by_call[row["call_id"]].append(row)

    for row in rows:
        if row["is_identity"] or not row["formal_available_as_caller_parameter"]:
            continue
        callee_annotation = str(row["callee_formal_annotation"])
        actual_annotation = str(row["caller_actual_annotation"])
        same_name_annotation = str(row["caller_formal_annotation"])
        if (
            callee_annotation
            and actual_annotation
            and same_name_annotation
            and actual_annotation == callee_annotation
            and same_name_annotation != callee_annotation
        ):
            node_id = base.base._node_id(row["path"], row["node"])
            suppressions[node_id] = {
                "reason": "actual_parameter_type_matches_callee_while_same_name_parameter_does_not",
                "path": row["path"],
                "caller_symbol": row["caller_symbol"],
                "callee_symbol": row["callee_symbol"],
                "callee_formal_parameter": row["formal_parameter"],
                "observed_caller_parameter": row["actual_parameter"],
                "callee_annotation": callee_annotation,
                "actual_annotation": actual_annotation,
                "same_name_annotation": same_name_annotation,
            }

    for key, caller_rows in by_caller_callee.items():
        calls = {row["call_id"] for row in caller_rows}
        if len(calls) < 2:
            continue
        for call_id in calls:
            current = by_call[call_id]
            mismatches = [
                row
                for row in current
                if not row["is_identity"]
                and row["formal_available_as_caller_parameter"]
            ]
            for row in mismatches:
                reciprocal = next(
                    (
                        other
                        for other in mismatches
                        if other["formal_parameter"] == row["actual_parameter"]
                        and other["actual_parameter"] == row["formal_parameter"]
                    ),
                    None,
                )
                if reciprocal is None:
                    continue
                identity_alternative = any(
                    other["call_id"] != call_id
                    and other["formal_parameter"] == row["formal_parameter"]
                    and other["actual_parameter"] == row["formal_parameter"]
                    for other in caller_rows
                ) and any(
                    other["call_id"] != call_id
                    and other["formal_parameter"] == row["actual_parameter"]
                    and other["actual_parameter"] == row["actual_parameter"]
                    for other in caller_rows
                )
                if not identity_alternative:
                    continue
                for target in (row, reciprocal):
                    node_id = base.base._node_id(target["path"], target["node"])
                    suppressions[node_id] = {
                        "reason": "same_caller_callee_has_identity_and_mirrored_argument_branches",
                        "path": target["path"],
                        "caller_symbol": target["caller_symbol"],
                        "callee_symbol": target["callee_symbol"],
                        "callee_formal_parameter": target["formal_parameter"],
                        "observed_caller_parameter": target["actual_parameter"],
                        "identity_alternative_present": True,
                    }
    return dict(sorted(suppressions.items()))


def _convert_to_base(facts: dict[str, Any]) -> dict[str, Any]:
    converted = copy.deepcopy(facts)
    converted.pop("facts_hash", None)
    converted["schema_version"] = base.SCHEMA_VERSION
    converted["method"] = base.METHOD_ID
    converted["facts_hash"] = canonical_json_hash(converted)
    return converted


def _convert_from_base(facts: dict[str, Any]) -> dict[str, Any]:
    converted = copy.deepcopy(facts)
    converted.pop("facts_hash", None)
    converted["schema_version"] = SCHEMA_VERSION
    converted["method"] = METHOD_ID
    converted["facts_hash"] = canonical_json_hash(converted)
    return converted


def build_public_python_call_binding_closure(
    package_root: str | Path, request_text: str = ""
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    result = base.build_public_python_call_binding_closure(package, request_text)
    result.pop("facts_hash", None)
    result["schema_version"] = SCHEMA_VERSION
    result["method"] = METHOD_ID
    suppressions = _suppression_map(package)
    original_editable = list(result.get("editable_nodes") or [])
    original_obligations = list(result.get("obligations") or [])
    suppressed_ids = set(suppressions)
    editable = [
        row for row in original_editable if str(row["node_id"]) not in suppressed_ids
    ]
    obligations = [
        row
        for row in original_obligations
        if str(row["editable_node_id"]) not in suppressed_ids
    ]
    result["editable_nodes"] = editable
    result["obligations"] = obligations
    result["ambiguity_suppressions"] = [
        {"node_id": node_id, **value} for node_id, value in suppressions.items()
    ]
    decision = result["localization_decision"]
    decision["suppressed_candidate_count"] = len(original_editable) - len(editable)
    decision["candidate_count_before_bounds"] = max(
        0,
        int(decision.get("candidate_count_before_bounds") or 0)
        - decision["suppressed_candidate_count"],
    )
    decision["selected_obligation_count"] = len(obligations)
    decision["editable_node_count"] = len(editable)
    if original_editable and not editable:
        decision.update(
            {
                "decision": "ABSTAIN",
                "selected_family": None,
                "confidence": 0.0,
                "caller_symbol_count": 0,
                "changed_path_upper_bound": 0,
                "abstain_reason": "structural_transform_ambiguity",
            }
        )
    elif editable:
        decision["caller_symbol_count"] = len(
            {(row["path"], row["caller_symbol"]) for row in obligations}
        )
        decision["changed_path_upper_bound"] = len(
            {str(row["path"]) for row in editable}
        )
    result["evidence_counts"] = dict(
        sorted(
            Counter(
                evidence
                for row in obligations
                for evidence in row.get("evidence") or []
            ).items()
        )
    )
    result["edit_contract"]["ambiguity_suppression_required"] = True
    result["claim_boundary"] = (
        "The packet reports a bounded package-local caller/callee binding anomaly after "
        "suppressing type-supported remappings and explicit identity/mirrored branches. "
        "It does not certify the intended runtime semantics."
    )
    result["facts_hash"] = canonical_json_hash(result)
    return result


def validate_public_python_call_binding_closure(
    facts: dict[str, Any], package_root: str | Path
) -> str:
    body = dict(facts)
    expected = str(body.pop("facts_hash", ""))
    if not expected or canonical_json_hash(body) != expected:
        raise ValueError("call_binding_closure_v3_facts_hash_invalid")
    if facts.get("schema_version") != SCHEMA_VERSION or facts.get("method") != METHOD_ID:
        raise ValueError("call_binding_closure_v3_method_invalid")
    base.validate_public_python_call_binding_closure(
        _convert_to_base(facts), package_root
    )
    return expected


def build_same_package_wrong_call_binding_closure(
    package_root: str | Path, real_facts: dict[str, Any]
) -> dict[str, Any]:
    validate_public_python_call_binding_closure(real_facts, package_root)
    sham = base.build_same_package_wrong_call_binding_closure(
        package_root, _convert_to_base(real_facts)
    )
    return _convert_from_base(sham)


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
