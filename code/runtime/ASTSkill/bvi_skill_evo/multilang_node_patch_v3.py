from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from bvi_skill_evo import multilang_node_patch as base
from bvi_skill_evo.multilang_node_patch_v2 import MULTILANG_NODE_PATCH_TOOL as V2_TOOL
from skillscriptbench.multilang_structural_v66 import enumerate_package_nodes


SCHEMA_VERSION = "bvi.multilang-node-patch.v3"
MULTILANG_NODE_PATCH_TOOL = deepcopy(V2_TOOL)
MULTILANG_NODE_PATCH_TOOL["function"]["description"] = (
    "Submit one bounded script edit for a visible executable Agent Skill package. "
    "AST-bound conditions submit an exact listed node. Other conditions may submit "
    "an exact parser node or one enclosing statement. The common editor resolves a "
    "unique smallest node, ignores formatting-only outer differences, and may default "
    "omitted protocol-only fields; it never invents or changes proposed semantics."
)

ACCEPT_STRUCTURALLY = base.ACCEPT_STRUCTURALLY
REJECT_STRUCTURALLY = base.REJECT_STRUCTURALLY
ABSTAIN = base.ABSTAIN

EDIT_FIELDS = {
    "path",
    "expected_file_sha256",
    "operation",
    "target_node_id",
    "expected_node_sha256",
    "symbol",
    "start_line",
    "start_column",
    "end_line",
    "end_column",
    "observed_source",
    "replacement",
}
PROTOCOL_DEFAULTS = {
    "operation": "replace_node",
    "target_node_id": "",
    "expected_node_sha256": "",
    "symbol": "",
    "start_column": 0,
    "end_column": 0,
}


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _complete_protocol_fields(edit: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(edit, dict):
        raise TypeError("edit_must_be_object")
    extras = set(edit) - EDIT_FIELDS
    if extras:
        raise ValueError(f"edit_schema_unknown_fields:{sorted(extras)}")
    completed = dict(edit)
    defaulted = []
    for field, value in PROTOCOL_DEFAULTS.items():
        if field not in completed:
            completed[field] = value
            defaulted.append(field)
    missing = EDIT_FIELDS - set(completed)
    if missing:
        raise ValueError(f"edit_schema_missing_semantic_fields:{sorted(missing)}")
    return completed, sorted(defaulted)


def _horizontal_ws_signature(value: str) -> str:
    rows = []
    for line in value.splitlines():
        compact = []
        in_space = False
        for char in line:
            if char in " \t":
                if not in_space:
                    compact.append(" ")
                in_space = True
            else:
                compact.append(char)
                in_space = False
        rows.append("".join(compact).strip())
    return "\n".join(rows)


def _prefix_end_ignoring_horizontal_ws(expected: str, proposed: str) -> int | None:
    i = 0
    j = 0
    while i < len(expected):
        if expected[i] in " \t":
            while i < len(expected) and expected[i] in " \t":
                i += 1
            if j >= len(proposed) or proposed[j] not in " \t":
                return None
            while j < len(proposed) and proposed[j] in " \t":
                j += 1
            continue
        if j >= len(proposed) or expected[i] != proposed[j]:
            return None
        i += 1
        j += 1
    return j


def _suffix_start_ignoring_horizontal_ws(expected: str, proposed: str) -> int | None:
    consumed = _prefix_end_ignoring_horizontal_ws(expected[::-1], proposed[::-1])
    return None if consumed is None else len(proposed) - consumed


def _source_line_windows(source: str, line_count: int) -> list[tuple[int, int, str, int, int]]:
    kept = source.splitlines(keepends=True)
    rows: list[tuple[int, int, str, int, int]] = []
    byte_offsets = [0]
    for line in kept:
        byte_offsets.append(byte_offsets[-1] + len(line.encode("utf-8")))
    for start in range(0, max(0, len(kept) - line_count + 1)):
        chunk = "".join(kept[start : start + line_count])
        chunk = chunk.removesuffix("\r\n").removesuffix("\n")
        end_byte = byte_offsets[start] + len(chunk.encode("utf-8"))
        rows.append((byte_offsets[start], end_byte, chunk, start + 1, start + line_count))
    return rows


def _locate_submitted_scope(
    source: str, edit: dict[str, Any]
) -> tuple[int, int, str, bool]:
    observed = str(edit["observed_source"])
    source_bytes = source.encode("utf-8")
    needle = observed.encode("utf-8")
    exact: list[tuple[int, int]] = []
    cursor = 0
    while needle:
        start = source_bytes.find(needle, cursor)
        if start < 0:
            break
        end = start + len(needle)
        start_line = source_bytes[:start].count(b"\n") + 1
        end_line = source_bytes[:end].count(b"\n") + 1
        if not (
            end_line < int(edit["start_line"])
            or int(edit["end_line"]) < start_line
        ):
            exact.append((start, end))
        cursor = start + 1
    if len(exact) == 1:
        return exact[0][0], exact[0][1], observed, False
    if len(exact) > 1:
        raise ValueError(f"statement_locator_not_unique:{len(exact)}")

    line_count = max(1, len(observed.splitlines()))
    signature = _horizontal_ws_signature(observed)
    approximate = [
        row
        for row in _source_line_windows(source, line_count)
        if not (
            row[4] < int(edit["start_line"])
            or int(edit["end_line"]) < row[3]
        )
        and _horizontal_ws_signature(row[2]) == signature
    ]
    if len(approximate) != 1:
        raise ValueError(f"whitespace_normalized_statement_locator_not_unique:{len(approximate)}")
    start, end, actual, _, _ = approximate[0]
    return start, end, actual, True


def _reject_function_scope_submission(
    edit: dict[str, Any], nodes: list[dict[str, Any]]
) -> None:
    submitted = _horizontal_ws_signature(str(edit["observed_source"]))
    for node in nodes:
        if (
            node["path"] == edit["path"]
            and node["role"] == "function_scope"
            and _horizontal_ws_signature(str(node["observed_source"])) == submitted
        ):
            raise ValueError("function_scope_not_editable")


def _resolve_smallest_node(
    edit: dict[str, Any], source_package: Path, nodes: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    _reject_function_scope_submission(edit, nodes)
    relative = str(edit["path"])
    source = (source_package / relative).read_text(encoding="utf-8")
    source_bytes = source.encode("utf-8")
    start, end, actual_scope, whitespace_scope_recovered = _locate_submitted_scope(
        source, edit
    )
    replacement = str(edit["replacement"])
    candidates: list[tuple[int, int, str, dict[str, Any], str, bool]] = []
    for node in nodes:
        if node["path"] != relative or node["role"] == "function_scope":
            continue
        node_start = int(node["byte_span"]["start"])
        node_end = int(node["byte_span"]["end"])
        if not (start <= node_start < node_end <= end):
            continue
        prefix = source_bytes[start:node_start].decode("utf-8")
        suffix = source_bytes[node_end:end].decode("utf-8")
        proposed_start = _prefix_end_ignoring_horizontal_ws(prefix, replacement)
        proposed_end = _suffix_start_ignoring_horizontal_ws(suffix, replacement)
        if proposed_start is None or proposed_end is None or proposed_start > proposed_end:
            continue
        proposed_node = replacement[proposed_start:proposed_end]
        if not proposed_node or proposed_node == node["observed_source"]:
            continue
        candidates.append(
            (
                node_end - node_start,
                node_start,
                str(node["site_id"]),
                node,
                proposed_node,
                _horizontal_ws_signature(prefix + proposed_node + suffix)
                != _horizontal_ws_signature(replacement),
            )
        )
    if not candidates:
        raise ValueError("statement_change_not_reducible_to_node")
    candidates.sort(key=lambda row: (row[0], row[1], row[2]))
    minimum_size = candidates[0][0]
    minimum_start = candidates[0][1]
    minimum = [
        row for row in candidates if row[0] == minimum_size and row[1] == minimum_start
    ]
    spans = {
        (int(row[3]["byte_span"]["start"]), int(row[3]["byte_span"]["end"]))
        for row in minimum
    }
    replacements = {row[4] for row in minimum}
    if len(spans) != 1 or len(replacements) != 1:
        raise ValueError("smallest_changed_node_not_unique")
    _, _, _, node, proposed_node, outer_formatting_discarded = minimum[0]
    span = node["span"]
    internal = {
        **edit,
        "operation": "replace_node",
        "target_node_id": node["site_id"],
        "expected_node_sha256": node["node_source_sha256"],
        "symbol": node["symbol"],
        "start_line": int(span["start_line"]),
        "start_column": int(span["start_column"]),
        "end_line": int(span["end_line"]),
        "end_column": int(span["end_column"]),
        "observed_source": node["observed_source"],
        "replacement": proposed_node,
    }
    receipt = {
        "mode": "condition_blind_unique_smallest_node_resolution",
        "semantic_fields_changed": False,
        "submitted_observed_sha256": _sha256_text(str(edit["observed_source"])),
        "submitted_replacement_sha256": _sha256_text(replacement),
        "actual_scope_sha256": _sha256_text(actual_scope),
        "resolved_node_id": node["site_id"],
        "resolved_node_source_sha256": node["node_source_sha256"],
        "resolved_node_role": node["role"],
        "resolved_span": span,
        "derived_replacement_sha256": _sha256_text(proposed_node),
        "whitespace_only_scope_recovered": whitespace_scope_recovered,
        "outer_formatting_not_applied": outer_formatting_discarded,
        "equivalent_role_count_at_resolved_span": len(minimum),
    }
    return internal, receipt, node


def apply_multilang_node_patch(
    content: str,
    source_package: str | Path,
    candidate_package: str | Path,
    *,
    visible_ast_facts: dict[str, Any] | None,
    require_ast_binding: bool,
) -> dict[str, Any]:
    source = Path(source_package)
    payload = base._parse_payload(content)
    if require_ast_binding:
        result = base.apply_multilang_node_patch(
            content,
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
            completed, defaulted = _complete_protocol_fields(submitted)
            if completed["target_node_id"] or completed["expected_node_sha256"]:
                raise ValueError("non_ast_condition_must_leave_node_binding_empty")
            internal, receipt, node = _resolve_smallest_node(completed, source, nodes)
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
        "protocol_defaulted_fields_by_edit": defaulted_fields,
        "receipts": receipts,
    }
    return result
