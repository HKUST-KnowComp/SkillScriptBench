from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from bvi_skill_evo import multilang_node_patch as base
from bvi_skill_evo import multilang_node_patch_v3 as v3
from skillscriptbench.multilang_structural_v66 import enumerate_package_nodes


SCHEMA_VERSION = "bvi.multilang-node-patch.v4"
MULTILANG_NODE_PATCH_TOOL = deepcopy(v3.MULTILANG_NODE_PATCH_TOOL)
MULTILANG_NODE_PATCH_TOOL["function"]["description"] += (
    " Identical duplicate JSON fields are collapsed as transport redundancy; "
    "conflicting duplicates are rejected."
)

ACCEPT_STRUCTURALLY = base.ACCEPT_STRUCTURALLY
REJECT_STRUCTURALLY = base.REJECT_STRUCTURALLY
ABSTAIN = base.ABSTAIN


def _parse_payload(content: str) -> tuple[dict[str, Any], list[str]]:
    duplicates: list[str] = []

    def object_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                if result[key] != value:
                    raise ValueError(f"conflicting_duplicate_json_key:{key}")
                duplicates.append(key)
                continue
            result[key] = value
        return result

    value = json.loads(content, object_pairs_hook=object_hook)
    if not isinstance(value, dict) or set(value) != {"edits", "summary"}:
        raise ValueError("response_schema_invalid")
    if not isinstance(value["summary"], str):
        raise TypeError("summary_must_be_string")
    if not isinstance(value["edits"], list) or len(value["edits"]) > 1:
        raise ValueError("edits_must_contain_zero_or_one_item")
    return value, sorted(duplicates)


def apply_multilang_node_patch(
    content: str,
    source_package: str | Path,
    candidate_package: str | Path,
    *,
    visible_ast_facts: dict[str, Any] | None,
    require_ast_binding: bool,
) -> dict[str, Any]:
    source = Path(source_package)
    payload, identical_duplicates = _parse_payload(content)
    if require_ast_binding:
        result = base.apply_multilang_node_patch(
            json.dumps(payload, ensure_ascii=False),
            source,
            candidate_package,
            visible_ast_facts=visible_ast_facts,
            require_ast_binding=True,
        )
        receipts = [
            {
                "edit_index": index,
                "mode": "exact_visible_ast_node",
                "semantic_fields_changed": False,
            }
            for index, _ in enumerate(payload["edits"])
        ]
        defaulted_fields: list[list[str]] = [[] for _ in payload["edits"]]
    else:
        nodes = enumerate_package_nodes(source, include_markdown=False)
        internal_payload = deepcopy(payload)
        receipts = []
        defaulted_fields = []
        allowed_nodes = []
        for index, submitted in enumerate(payload["edits"]):
            completed, defaulted = v3._complete_protocol_fields(submitted)
            if completed["target_node_id"] or completed["expected_node_sha256"]:
                raise ValueError("non_ast_condition_must_leave_node_binding_empty")
            internal, receipt, node = v3._resolve_smallest_node(completed, source, nodes)
            internal_payload["edits"][index] = internal
            receipts.append({"edit_index": index, **receipt})
            defaulted_fields.append(defaulted)
            allowed_nodes.append(
                {
                    "node_id": node["site_id"],
                    "source_sha256": node["node_source_sha256"],
                }
            )
        result = base.apply_multilang_node_patch(
            json.dumps(internal_payload, ensure_ascii=False),
            source,
            candidate_package,
            visible_ast_facts={"editable_nodes": allowed_nodes},
            require_ast_binding=True,
        )
        result["structural_gate"]["ast_binding_required"] = False
        result["structural_gate"]["binding_origin"] = (
            "condition_blind_editor_resolution_after_submission"
        )

    result["schema_version"] = SCHEMA_VERSION
    result["protocol_normalization"] = {
        "condition_blind": True,
        "hidden_artifacts_used": False,
        "verifier_feedback_used": False,
        "semantic_proposal_rewritten": False,
        "identical_duplicate_json_fields_collapsed": identical_duplicates,
        "conflicting_duplicate_json_fields_allowed": False,
        "protocol_defaulted_fields_by_edit": defaulted_fields,
        "receipts": receipts,
    }
    return result
