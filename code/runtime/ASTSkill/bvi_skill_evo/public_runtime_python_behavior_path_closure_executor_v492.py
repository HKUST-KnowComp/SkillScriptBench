from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from typing import Any

from bvi_skill_evo import public_runtime_python_behavior_path_closure_v489 as method
from bvi_skill_evo.python_span_patch_v246 import apply_python_span_patch


SCHEMA_VERSION = "4.92-public-runtime-python-behavior-path-executor-v1"
METHOD_ID = "public_runtime_python_behavior_path_closure_executor_v1"
ACCEPT_STRUCTURALLY = "ACCEPT_STRUCTURALLY"
REVISE = "REVISE"

PYTHON_NODE_PATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_skill_patch",
        "description": (
            "Repair one bounded Python behavior-path closure. Select every listed "
            "target_node_id exactly once. For BoolOpOperator use only 'and' or 'or'; "
            "for a control-transfer node provide exactly one complete Python statement."
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
            "bool_operator_replacement_domain": ["and", "or"],
            "control_transfer_replacement_is_one_python_statement": True,
            "semantic_replacement_completed_or_rewritten": False,
            "unknown_target_node_id_rejected": True,
        },
        "claim_boundary": facts.get("claim_boundary"),
    }


def _parse_payload(content: str) -> dict[str, Any]:
    payload = json.loads(content)
    if not isinstance(payload, dict) or set(payload) != {"edits", "summary"}:
        raise ValueError("behavior_path_response_schema_invalid")
    if not isinstance(payload["summary"], str):
        raise TypeError("behavior_path_summary_must_be_string")
    edits = payload["edits"]
    if not isinstance(edits, list) or len(edits) > method.MAX_CLOSURE_EDITS:
        raise ValueError("behavior_path_edits_outside_bound")
    return payload


def _one_safe_statement(value: str) -> ast.stmt:
    if len(value.encode("utf-8")) > 1024 or len(value.splitlines()) > 12:
        raise ValueError("behavior_path_statement_replacement_too_large")
    try:
        module = ast.parse(value)
    except SyntaxError as exc:
        raise ValueError("behavior_path_replacement_not_python_statement") from exc
    if len(module.body) != 1:
        raise ValueError("behavior_path_replacement_must_be_one_statement")
    statement = module.body[0]
    if isinstance(
        statement,
        (
            ast.FunctionDef,
            ast.AsyncFunctionDef,
            ast.ClassDef,
            ast.Import,
            ast.ImportFrom,
            ast.Global,
            ast.Nonlocal,
        ),
    ):
        raise ValueError("behavior_path_replacement_statement_unsafe")
    return statement


def _normalize_edits(
    payload: dict[str, Any], facts: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    registry = {str(row["node_id"]): row for row in facts.get("editable_nodes") or []}
    required = set(registry)
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    for index, raw in enumerate(payload["edits"]):
        if not isinstance(raw, dict) or set(raw) != {"target_node_id", "replacement"}:
            raise ValueError(f"behavior_path_compact_edit_schema_invalid:{index}")
        node_id = str(raw["target_node_id"])
        replacement = raw["replacement"]
        if node_id not in registry:
            raise ValueError("behavior_path_target_not_in_frozen_packet")
        if node_id in seen:
            raise ValueError("behavior_path_duplicate_target_node_id")
        if not isinstance(replacement, str) or not replacement.strip():
            raise ValueError("behavior_path_replacement_must_be_nonempty_string")
        replacement = replacement.strip()
        node = registry[node_id]
        if replacement == str(node["observed_source"]).strip():
            raise ValueError("behavior_path_noop_replacement")
        if node["node_type"] == "BoolOpOperator":
            if replacement not in {"and", "or"}:
                raise ValueError("behavior_path_boolean_replacement_outside_domain")
        else:
            _one_safe_statement(replacement)
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
                "node_type": node["node_type"],
                "locator_fields_completed_from_frozen_public_packet": True,
                "semantic_replacement_completed_or_rewritten": False,
            }
        )
    if seen != required:
        raise ValueError(
            f"behavior_path_atomic_target_set_required:missing={sorted(required-seen)}:"
            f"extra={sorted(seen-required)}"
        )
    return normalized, receipts


def apply_public_behavior_path_patch(
    content: str,
    source_package: str | Path,
    candidate_package: str | Path,
    *,
    frozen_facts: dict[str, Any],
) -> dict[str, Any]:
    source = Path(source_package).resolve()
    candidate = Path(candidate_package).resolve()
    method.validate_public_python_behavior_path_closure(frozen_facts, source)
    if frozen_facts["localization_decision"]["decision"] != "PROPOSE":
        raise ValueError("behavior_path_closure_abstained")
    payload = _parse_payload(content)
    edits, receipts = _normalize_edits(payload, frozen_facts)
    span_payload = {"summary": payload["summary"], "edits": edits}
    application = apply_python_span_patch(
        json.dumps(span_payload, sort_keys=True),
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
            "changed_paths_within_behavior_path_bound": len(changed_paths)
            <= method.MAX_CHANGED_PATHS,
            "all_frozen_behavior_path_obligations_resolved": not residual,
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
            "structural_gate": {
                "decision": decision,
                "checks": checks,
            },
            "behavior_path_gate": {
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
                "Acceptance establishes exact bounded edits, parse/API preservation, and "
                "closure of the frozen public behavior-path discrepancy only."
            ),
        }
    )
    return application


build_public_python_parameter_effect_set = (
    method.build_public_python_behavior_path_closure
)
build_same_package_wrong_node_set = (
    method.build_same_package_wrong_behavior_path_closure
)
validate_public_python_parameter_effect_set = (
    method.validate_public_python_behavior_path_closure
)
apply_public_node_bound_patch = apply_public_behavior_path_patch


__all__ = [
    "ACCEPT_STRUCTURALLY",
    "METHOD_ID",
    "MULTILANG_NODE_PATCH_TOOL",
    "PYTHON_NODE_PATCH_TOOL",
    "apply_public_behavior_path_patch",
    "apply_public_node_bound_patch",
    "build_public_python_parameter_effect_set",
    "build_same_package_wrong_node_set",
    "method",
    "prompt_packet_view",
    "validate_public_python_parameter_effect_set",
]
