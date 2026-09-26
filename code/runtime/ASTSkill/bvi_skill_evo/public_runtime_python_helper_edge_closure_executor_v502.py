from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from typing import Any

from bvi_skill_evo import public_runtime_python_helper_edge_closure_v498 as method
from bvi_skill_evo.python_span_patch_v246 import apply_python_span_patch


SCHEMA_VERSION = "5.02-public-runtime-python-helper-edge-executor-v1"
METHOD_ID = "public_runtime_python_helper_edge_closure_executor_v1"
ACCEPT_STRUCTURALLY = "ACCEPT_STRUCTURALLY"
REVISE = "REVISE"

PYTHON_NODE_PATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_skill_patch",
        "description": (
            "Repair one bounded package-local helper-edge closure. Select every listed "
            "target_node_id exactly once and replace the raw expression with a direct call "
            "to the expected helper, preserving the observed raw value as its sole argument."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["edits", "summary"],
            "properties": {
                "edits": {
                    "type": "array",
                    "maxItems": method.MAX_CLOSURE_EDITS,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["target_node_id", "replacement"],
                        "properties": {
                            "target_node_id": {"type": "string"},
                            "replacement": {"type": "string"},
                        },
                    },
                },
                "summary": {"type": "string"},
            },
        },
    },
}
MULTILANG_NODE_PATCH_TOOL = copy.deepcopy(PYTHON_NODE_PATCH_TOOL)


def prompt_packet_view(facts: dict[str, Any]) -> dict[str, Any]:
    return {
        "method": facts.get("method"),
        "family": method.FAMILY,
        "subfamily": method.SUBFAMILY,
        "localization_decision": facts.get("localization_decision"),
        "obligations": copy.deepcopy(facts.get("obligations") or []),
        "editable_nodes": [
            {
                "target_node_id": row.get("node_id"),
                "path": row.get("path"),
                "language": row.get("language"),
                "node_type": row.get("node_type"),
                "role": row.get("role"),
                "symbol": row.get("symbol"),
                "line": row.get("line"),
                "column": row.get("column"),
                "observed_source": row.get("observed_source"),
                "public_node_facts": copy.deepcopy(row.get("facts") or {}),
            }
            for row in facts.get("editable_nodes") or []
        ],
        "edit_contract": facts.get("edit_contract"),
        "locator_protocol": {
            "model_submits": ["target_node_id", "replacement"],
            "framework_completes_from_frozen_packet": [
                "path",
                "file_sha256",
                "node_sha256",
                "symbol",
                "source_span",
                "observed_source",
            ],
            "all_listed_target_node_ids_required": True,
            "replacement_is_direct_expected_helper_call": True,
            "observed_raw_value_is_preserved_as_only_argument": True,
            "semantic_replacement_completed_or_rewritten": False,
            "unknown_target_node_id_rejected": True,
        },
        "claim_boundary": facts.get("claim_boundary"),
    }


def _parse_payload(content: str) -> dict[str, Any]:
    payload = json.loads(content)
    if not isinstance(payload, dict) or set(payload) != {"edits", "summary"}:
        raise ValueError("helper_edge_response_schema_invalid")
    if not isinstance(payload["summary"], str):
        raise TypeError("helper_edge_summary_must_be_string")
    edits = payload["edits"]
    if not isinstance(edits, list) or len(edits) > method.MAX_CLOSURE_EDITS:
        raise ValueError("helper_edge_edits_outside_bound")
    return payload


def _expression(value: str) -> ast.expr:
    if len(value.encode("utf-8")) > 1024 or "\n" in value:
        raise ValueError("helper_edge_replacement_too_large")
    try:
        return ast.parse(value, mode="eval").body
    except SyntaxError as exc:
        raise ValueError("helper_edge_replacement_not_expression") from exc


def _validate_direct_call(value: str, obligation: dict[str, Any]) -> None:
    expression = _expression(value)
    expected_argument = _expression(str(obligation["expected_argument_source"]))
    if not (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Name)
        and expression.func.id == str(obligation["expected_helper_symbol"])
        and len(expression.args) == 1
        and not expression.keywords
        and ast.dump(expression.args[0], include_attributes=False)
        == ast.dump(expected_argument, include_attributes=False)
    ):
        raise ValueError("helper_edge_replacement_not_expected_direct_call")


def _normalize_edits(
    payload: dict[str, Any], facts: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    registry = {str(row["node_id"]): row for row in facts.get("editable_nodes") or []}
    obligations = {
        str(row["editable_node_id"]): row for row in facts.get("obligations") or []
    }
    required = set(registry)
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    for index, raw in enumerate(payload["edits"]):
        if not isinstance(raw, dict) or set(raw) != {"target_node_id", "replacement"}:
            raise ValueError(f"helper_edge_compact_edit_schema_invalid:{index}")
        node_id = str(raw["target_node_id"])
        replacement = raw["replacement"]
        if node_id not in registry or node_id not in obligations:
            raise ValueError("helper_edge_target_not_in_frozen_packet")
        if node_id in seen:
            raise ValueError("helper_edge_duplicate_target_node_id")
        if not isinstance(replacement, str) or not replacement.strip():
            raise ValueError("helper_edge_replacement_must_be_nonempty_string")
        replacement = replacement.strip()
        node = registry[node_id]
        if replacement == str(node["observed_source"]).strip():
            raise ValueError("helper_edge_noop_replacement")
        _validate_direct_call(replacement, obligations[node_id])
        seen.add(node_id)
        normalized.append(
            {
                "operation": "replace_node",
                "path": str(node["path"]),
                "symbol": str(node["symbol"]),
                "observed_source": str(node["observed_source"]),
                "replacement_source": replacement,
                "target_node_id": node_id,
                "expected_node_sha256": str(node["node_sha256"]),
            }
        )
        receipts.append(
            {
                "edit_index": index,
                "target_node_id": node_id,
                "expected_helper_symbol": obligations[node_id]["expected_helper_symbol"],
                "expected_argument_source": obligations[node_id]["expected_argument_source"],
                "direct_call_shape_validated": True,
                "locator_fields_completed_from_frozen_public_packet": True,
                "semantic_replacement_completed_or_rewritten": False,
            }
        )
    if seen != required:
        raise ValueError(
            f"helper_edge_atomic_target_set_required:missing={sorted(required-seen)}:"
            f"extra={sorted(seen-required)}"
        )
    return normalized, receipts


def apply_public_helper_edge_patch(
    content: str,
    source_package: str | Path,
    candidate_package: str | Path,
    *,
    frozen_facts: dict[str, Any],
) -> dict[str, Any]:
    source = Path(source_package).resolve()
    candidate = Path(candidate_package).resolve()
    method.validate_public_python_helper_edge_closure(frozen_facts, source)
    if frozen_facts["localization_decision"]["decision"] != "PROPOSE":
        raise ValueError("helper_edge_closure_abstained")
    payload = _parse_payload(content)
    edits, receipts = _normalize_edits(payload, frozen_facts)
    application = apply_python_span_patch(
        json.dumps({"summary": payload["summary"], "edits": edits}, sort_keys=True),
        source,
        candidate,
        visible_ast_facts=frozen_facts,
        require_ast_binding=True,
    )
    residual = method.residual_obligations(candidate, frozen_facts)
    changed_paths = list(application.get("changed_paths") or [])
    checks = dict((application.get("structural_gate") or {}).get("checks") or {})
    checks.pop("hidden_semantic_correctness_checked", None)
    checks.update(
        {
            "all_required_node_ids_submitted": len(edits)
            == len(frozen_facts.get("editable_nodes") or []),
            "all_replacements_direct_expected_helper_calls": all(
                receipt["direct_call_shape_validated"] for receipt in receipts
            ),
            "changed_paths_within_helper_edge_bound": len(changed_paths)
            <= method.MAX_CHANGED_PATHS,
            "all_frozen_helper_edge_obligations_resolved": not residual,
        }
    )
    decision = ACCEPT_STRUCTURALLY if all(checks.values()) else REVISE
    application.update(
        {
            "schema_version": SCHEMA_VERSION,
            "method": METHOD_ID,
            "protocol_normalization": {
                "compact_public_node_locator_receipts": receipts,
                "semantic_replacement_completed_or_rewritten": False,
            },
            "structural_gate": {"decision": decision, "checks": checks},
            "helper_edge_gate": {
                "decision": decision,
                "residual_obligation_count": len(residual),
                "residual_obligations": residual,
                "hidden_semantic_correctness_checked": False,
            },
            "hidden_artifacts_consumed": False,
            "task_verifier_consumed": False,
            "gold_or_oracle_consumed": False,
            "reward_consumed": False,
            "semantic_correctness_inferred": False,
            "claim_boundary": (
                "Acceptance establishes exact expression edits, direct package-local helper calls, "
                "raw-argument preservation, parse/API safety, and closure of frozen public obligations only."
            ),
        }
    )
    return application


build_public_python_parameter_effect_set = method.build_public_python_helper_edge_closure
build_same_package_wrong_node_set = method.build_same_package_wrong_helper_edge_closure
validate_public_python_parameter_effect_set = method.validate_public_python_helper_edge_closure
apply_public_node_bound_patch = apply_public_helper_edge_patch


__all__ = [
    "ACCEPT_STRUCTURALLY",
    "METHOD_ID",
    "MULTILANG_NODE_PATCH_TOOL",
    "PYTHON_NODE_PATCH_TOOL",
    "apply_public_helper_edge_patch",
    "apply_public_node_bound_patch",
    "build_public_python_parameter_effect_set",
    "build_same_package_wrong_node_set",
    "method",
    "prompt_packet_view",
    "validate_public_python_parameter_effect_set",
]
