from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from bvi_skill_evo import multilang_node_patch as base
from skillscriptbench.multilang_structural_v66 import enumerate_package_nodes


SCHEMA_VERSION = "bvi.multilang-node-patch.v2"
MULTILANG_NODE_PATCH_TOOL = deepcopy(base.MULTILANG_NODE_PATCH_TOOL)
MULTILANG_NODE_PATCH_TOOL["function"]["description"] = (
    "Submit one bounded script edit for a visible executable Agent Skill package. "
    "AST-bound conditions submit an exact listed node. Other conditions may submit "
    "either an exact parser node or one enclosing statement that can be mechanically "
    "reduced to a unique smallest parser node without changing the proposed semantics."
)

ACCEPT_STRUCTURALLY = base.ACCEPT_STRUCTURALLY
REJECT_STRUCTURALLY = base.REJECT_STRUCTURALLY
ABSTAIN = base.ABSTAIN


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _line_span(source: bytes, start: int, end: int) -> tuple[int, int]:
    start_line = source[:start].count(b"\n") + 1
    end_line = source[:end].count(b"\n") + 1
    return start_line, end_line


def _all_occurrences(source: bytes, needle: bytes) -> list[tuple[int, int]]:
    if not needle:
        return []
    rows: list[tuple[int, int]] = []
    cursor = 0
    while True:
        start = source.find(needle, cursor)
        if start < 0:
            return rows
        rows.append((start, start + len(needle)))
        cursor = start + 1


def _overlaps_declared_lines(
    source: bytes,
    occurrence: tuple[int, int],
    edit: dict[str, Any],
) -> bool:
    actual_start, actual_end = _line_span(source, *occurrence)
    declared_start = int(edit["start_line"])
    declared_end = int(edit["end_line"])
    return not (actual_end < declared_start or declared_end < actual_start)


def _exact_node_matches(
    edit: dict[str, Any], nodes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        node
        for node in nodes
        if node["path"] == edit["path"]
        and node["observed_source"] == edit["observed_source"]
        and (not edit["symbol"] or node["symbol"] == edit["symbol"])
    ]


def _statement_to_node(
    edit: dict[str, Any], source_package: Path, nodes: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    relative = str(edit["path"])
    source = (source_package / relative).read_bytes()
    observed = str(edit["observed_source"])
    replacement = str(edit["replacement"])
    observed_bytes = observed.encode("utf-8")
    replacement_bytes = replacement.encode("utf-8")
    if len(observed_bytes) > 4096 or len(replacement_bytes) > 4096:
        raise ValueError("statement_normalization_scope_too_large")

    occurrences = [
        row
        for row in _all_occurrences(source, observed_bytes)
        if _overlaps_declared_lines(source, row, edit)
    ]
    if len(occurrences) != 1:
        raise ValueError(f"statement_locator_not_unique:{len(occurrences)}")
    statement_start, statement_end = occurrences[0]

    candidates: list[tuple[int, int, dict[str, Any], str]] = []
    for node in nodes:
        if node["path"] != relative or node["role"] == "function_scope":
            continue
        node_start = int(node["byte_span"]["start"])
        node_end = int(node["byte_span"]["end"])
        if not (statement_start <= node_start < node_end <= statement_end):
            continue
        prefix = source[statement_start:node_start]
        suffix = source[node_end:statement_end]
        if not replacement_bytes.startswith(prefix) or not replacement_bytes.endswith(suffix):
            continue
        replacement_end = len(replacement_bytes) - len(suffix) if suffix else len(replacement_bytes)
        proposed_node = replacement_bytes[len(prefix) : replacement_end]
        if not proposed_node or proposed_node == source[node_start:node_end]:
            continue
        if prefix + proposed_node + suffix != replacement_bytes:
            continue
        try:
            proposed_text = proposed_node.decode("utf-8")
        except UnicodeDecodeError:
            continue
        candidates.append((node_end - node_start, node_start, node, proposed_text))

    if not candidates:
        raise ValueError("statement_change_not_reducible_to_node")
    candidates.sort(key=lambda row: (row[0], row[1], str(row[2]["site_id"])))
    minimum_size = candidates[0][0]
    minimum = [row for row in candidates if row[0] == minimum_size]
    unique_spans = {
        (int(row[2]["byte_span"]["start"]), int(row[2]["byte_span"]["end"]))
        for row in minimum
    }
    if len(unique_spans) != 1:
        raise ValueError(f"smallest_changed_node_not_unique:{len(unique_spans)}")
    _, _, node, proposed_text = minimum[0]
    span = node["span"]
    normalized = {
        **edit,
        "target_node_id": "",
        "expected_node_sha256": "",
        "symbol": node["symbol"],
        "start_line": int(span["start_line"]),
        "start_column": 0,
        "end_line": int(span["end_line"]),
        "end_column": 0,
        "observed_source": node["observed_source"],
        "replacement": proposed_text,
    }
    receipt = {
        "mode": "enclosing_statement_to_unique_smallest_node",
        "semantic_fields_changed": False,
        "submitted_observed_sha256": _sha256_text(observed),
        "submitted_replacement_sha256": _sha256_text(replacement),
        "resolved_node_id": node["site_id"],
        "resolved_node_source_sha256": node["node_source_sha256"],
        "resolved_node_role": node["role"],
        "resolved_span": span,
        "derived_replacement_sha256": _sha256_text(proposed_text),
    }
    return normalized, receipt


def _normalize_non_ast_payload(
    payload: dict[str, Any], source_package: Path
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    nodes = enumerate_package_nodes(source_package, include_markdown=False)
    normalized = deepcopy(payload)
    receipts: list[dict[str, Any]] = []
    for index, edit in enumerate(normalized["edits"]):
        if edit["target_node_id"] or edit["expected_node_sha256"]:
            raise ValueError("non_ast_condition_must_leave_node_binding_empty")
        exact = _exact_node_matches(edit, nodes)
        if exact:
            receipts.append(
                {
                    "edit_index": index,
                    "mode": "exact_parser_node",
                    "semantic_fields_changed": False,
                    "submitted_observed_sha256": _sha256_text(edit["observed_source"]),
                    "submitted_replacement_sha256": _sha256_text(edit["replacement"]),
                }
            )
            continue
        normalized_edit, receipt = _statement_to_node(edit, source_package, nodes)
        normalized["edits"][index] = normalized_edit
        receipts.append({"edit_index": index, **receipt})
    return normalized, receipts


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
        normalized_payload = payload
        receipts = [
            {
                "edit_index": index,
                "mode": "exact_visible_ast_node",
                "semantic_fields_changed": False,
                "submitted_observed_sha256": _sha256_text(edit["observed_source"]),
                "submitted_replacement_sha256": _sha256_text(edit["replacement"]),
            }
            for index, edit in enumerate(payload["edits"])
        ]
    else:
        normalized_payload, receipts = _normalize_non_ast_payload(payload, source)

    result = base.apply_multilang_node_patch(
        json.dumps(normalized_payload, ensure_ascii=False),
        source,
        candidate_package,
        visible_ast_facts=visible_ast_facts,
        require_ast_binding=require_ast_binding,
    )
    result["schema_version"] = SCHEMA_VERSION
    result["protocol_normalization"] = {
        "condition_blind": True,
        "hidden_artifacts_used": False,
        "verifier_feedback_used": False,
        "semantic_proposal_rewritten": False,
        "receipts": receipts,
    }
    return result
