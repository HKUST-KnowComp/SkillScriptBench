from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any

from bvi_skill_evo import multilang_node_patch as base
from bvi_skill_evo import multilang_node_patch_v3 as v3
from bvi_skill_evo.multilang_node_patch_v4 import MULTILANG_NODE_PATCH_TOOL as V4_TOOL
from skillscriptbench.multilang_structural_v66 import enumerate_package_nodes
from skillscriptbench.structural_evolution_v65 import apply_structured_edits


SCHEMA_VERSION = "bvi.multilang-scalar-patch.v5"
SCALAR_NODE_TYPES = {"StringLiteral", "NumericLiteral", "BooleanLiteral"}
MULTILANG_NODE_PATCH_TOOL = deepcopy(V4_TOOL)
MULTILANG_NODE_PATCH_TOOL["function"]["description"] = (
    "Submit at most two bounded scalar-node script edits for one visible executable "
    "Agent Skill package. AST-bound conditions submit exact listed scalar nodes. Other "
    "conditions may submit exact scalar nodes or enclosing statements that the common "
    "editor can reduce to unique smallest scalar nodes without changing proposed semantics."
)
MULTILANG_NODE_PATCH_TOOL["function"]["parameters"]["properties"]["edits"][
    "maxItems"
] = 2

ACCEPT_STRUCTURALLY = base.ACCEPT_STRUCTURALLY
REJECT_STRUCTURALLY = base.REJECT_STRUCTURALLY
ABSTAIN = base.ABSTAIN


def _parse_payload(content: str) -> tuple[dict[str, Any], list[str]]:
    duplicate_fields: list[str] = []

    def object_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                if result[key] != value:
                    raise ValueError(f"conflicting_duplicate_json_key:{key}")
                duplicate_fields.append(key)
                continue
            result[key] = value
        return result

    payload = json.loads(content, object_pairs_hook=object_hook)
    if not isinstance(payload, dict) or set(payload) != {"edits", "summary"}:
        raise ValueError("response_schema_invalid")
    if not isinstance(payload["summary"], str):
        raise TypeError("summary_must_be_string")
    if not isinstance(payload["edits"], list) or len(payload["edits"]) > 2:
        raise ValueError("edits_must_contain_zero_to_two_items")
    return payload, sorted(duplicate_fields)


def _validate_submission(edit: dict[str, Any], source: Path) -> dict[str, Any]:
    completed, defaulted = v3._complete_protocol_fields(edit)
    relative = str(completed["path"])
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or not relative.startswith("scripts/")
    ):
        raise ValueError("edit_outside_scripts")
    file = source / pure
    if not file.is_file() or base.sha256_file(file) != completed["expected_file_sha256"]:
        raise ValueError("edit_file_hash_mismatch")
    if completed["operation"] != "replace_node":
        raise ValueError("edit_operation_invalid")
    for field in ("observed_source", "replacement"):
        value = completed[field]
        if not isinstance(value, str) or len(value.encode("utf-8")) > 4096:
            raise ValueError(f"edit_{field}_invalid")
    return {"edit": completed, "defaulted": defaulted}


def _require_scalar(node: dict[str, Any]) -> None:
    if node.get("node_type") not in SCALAR_NODE_TYPES:
        raise ValueError(f"non_scalar_node_not_editable:{node.get('node_type')}")


def apply_multilang_node_patch(
    content: str,
    source_package: str | Path,
    candidate_package: str | Path,
    *,
    visible_ast_facts: dict[str, Any] | None,
    require_ast_binding: bool,
) -> dict[str, Any]:
    source = Path(source_package)
    payload, identical_json_duplicates = _parse_payload(content)
    nodes = enumerate_package_nodes(source, include_markdown=False)
    registry = {str(node["site_id"]): node for node in nodes}
    allowed_ids = {
        str(row["node_id"])
        for row in (visible_ast_facts or {}).get("editable_nodes", [])
    }
    internal_edits: list[dict[str, Any]] = []
    normalizations: list[dict[str, Any]] = []
    allowed_nodes: list[dict[str, str]] = []
    physical_edits: dict[tuple[str, int, int], str] = {}
    collapsed_physical_duplicates: list[int] = []

    for index, submitted in enumerate(payload["edits"]):
        validated = _validate_submission(submitted, source)
        completed = validated["edit"]
        if require_ast_binding:
            node = base._resolve_node(
                completed,
                registry,
                allowed_node_ids=allowed_ids,
                require_ast_binding=True,
            )
            internal = completed
            receipt = {
                "mode": "exact_visible_ast_scalar_node",
                "semantic_fields_changed": False,
            }
        else:
            if completed["target_node_id"] or completed["expected_node_sha256"]:
                raise ValueError("non_ast_condition_must_leave_node_binding_empty")
            internal, receipt, node = v3._resolve_smallest_node(
                completed, source, nodes
            )
            allowed_nodes.append(
                {
                    "node_id": str(node["site_id"]),
                    "source_sha256": str(node["node_source_sha256"]),
                }
            )
        _require_scalar(node)
        key = (
            str(node["path"]),
            int(node["byte_span"]["start"]),
            int(node["byte_span"]["end"]),
        )
        replacement = str(internal["replacement"])
        if key in physical_edits:
            if physical_edits[key] != replacement:
                raise ValueError("conflicting_duplicate_physical_node_edit")
            collapsed_physical_duplicates.append(index)
            continue
        physical_edits[key] = replacement
        internal_edits.append(internal)
        normalizations.append(
            {
                "edit_index": index,
                **receipt,
                "protocol_defaulted_fields": validated["defaulted"],
                "resolved_node_id": str(node["site_id"]),
                "resolved_node_type": str(node["node_type"]),
            }
        )

    effective_facts = (
        visible_ast_facts
        if require_ast_binding
        else {"editable_nodes": allowed_nodes}
    )
    effective_allowed_ids = {
        str(row["node_id"])
        for row in (effective_facts or {}).get("editable_nodes", [])
    }
    normalized: list[dict[str, Any]] = []
    for edit in internal_edits:
        node = base._resolve_node(
            edit,
            registry,
            allowed_node_ids=effective_allowed_ids,
            require_ast_binding=True,
        )
        _require_scalar(node)
        normalized.append(
            {
                "path": str(edit["path"]),
                "expected_file_sha256": str(edit["expected_file_sha256"]),
                "operation": "replace_node",
                "target_node_id": str(node["site_id"]),
                "expected_node_sha256": str(node["node_source_sha256"]),
                "symbol": str(node.get("symbol") or ""),
                "start_line": 0,
                "end_line": 0,
                "replacement": str(edit["replacement"]),
            }
        )

    application = apply_structured_edits(
        normalized,
        source,
        Path(candidate_package),
        node_registry=registry,
        allowed_edit_paths={str(edit["path"]) for edit in internal_edits},
    )
    syntax = [
        base._validate_changed_script(Path(candidate_package) / path, path)
        for path in application["changed_paths"]
    ]
    decision = (
        ABSTAIN
        if not application["changed_paths"]
        else ACCEPT_STRUCTURALLY
        if 1 <= application["edit_count"] <= 2
        and len(application["changed_paths"]) == 1
        else REJECT_STRUCTURALLY
    )
    return {
        "schema_version": SCHEMA_VERSION,
        **application,
        "summary": payload["summary"],
        "syntax": syntax,
        "structural_gate": {
            "decision": decision,
            "semantic_correctness": "unknown",
            "outside_selected_nodes_preserved_by_construction": True,
            "ast_binding_required": require_ast_binding,
            "binding_origin": (
                "visible_ast_facts"
                if require_ast_binding
                else "condition_blind_editor_resolution_after_submission"
            ),
        },
        "protocol_normalization": {
            "condition_blind": True,
            "hidden_artifacts_used": False,
            "verifier_feedback_used": False,
            "semantic_proposal_rewritten": False,
            "identical_duplicate_json_fields_collapsed": identical_json_duplicates,
            "identical_duplicate_physical_edits_collapsed": collapsed_physical_duplicates,
            "receipts": normalizations,
        },
    }
