from __future__ import annotations

import copy
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from bvi_skill_evo import python_span_patch_v237 as base
from skillscriptbench.io_utils import canonical_json_hash, copy_tree_clean, hash_tree


ACCEPT_STRUCTURALLY = base.ACCEPT_STRUCTURALLY
MAX_EDITS = base.MAX_EDITS
PYTHON_SPAN_PATCH_TOOL = copy.deepcopy(base.PYTHON_SPAN_PATCH_TOOL)
_EDIT_SCHEMA = PYTHON_SPAN_PATCH_TOOL["function"]["parameters"]["properties"]["edits"]["items"]
_EDIT_SCHEMA["required"] = [
    "operation",
    "path",
    "symbol",
    "observed_source",
    "replacement_source",
]
_EDIT_SCHEMA["properties"]["node_id"] = {"type": "string"}
_EDIT_SCHEMA["properties"]["node_sha256"] = {"type": "string"}
PYTHON_SPAN_PATCH_TOOL["function"]["description"] = (
    "Submit zero, one, or two bounded Python source-span replacements. AST-guided callers place a "
    "finding node_id in target_node_id (node_id is accepted as a mechanical alias). All conditions "
    "use the same parser, normalizer, syntax check, public-API check, and structural gate."
)


def _parse_response(content: str) -> tuple[dict[str, Any], list[str]]:
    raw = json.loads(content)
    if isinstance(raw, dict) and isinstance(raw.get("edits"), list):
        for edit in raw["edits"]:
            if not isinstance(edit, dict):
                continue
            if not edit.get("target_node_id") and edit.get("node_id"):
                edit["target_node_id"] = edit["node_id"]
            if not edit.get("expected_node_sha256") and edit.get("node_sha256"):
                edit["expected_node_sha256"] = edit["node_sha256"]
    parsed, ignored = base._parse_response(json.dumps(raw, ensure_ascii=False))
    alias_fields = []
    for index, edit in enumerate((raw or {}).get("edits", []) if isinstance(raw, dict) else []):
        if isinstance(edit, dict):
            if "node_id" in edit:
                alias_fields.append(f"edits[{index}].node_id_alias")
            if "node_sha256" in edit:
                alias_fields.append(f"edits[{index}].node_sha256_alias")
    return parsed, sorted(set(ignored + alias_fields))


def _bound_span(
    source: str,
    edit: dict[str, str],
    facts: dict[str, Any],
) -> tuple[int, int, str, dict[str, Any]]:
    matches = [
        row
        for row in facts.get("editable_nodes", [])
        if row.get("node_id") == edit["target_node_id"]
    ]
    if len(matches) != 1:
        raise ValueError(f"ast_node_binding_not_unique:{len(matches)}")
    node = matches[0]
    if node["path"] != edit["path"]:
        raise ValueError("ast_node_path_mismatch")
    if node["node_sha256"] != edit["expected_node_sha256"]:
        raise ValueError("ast_node_sha256_mismatch")
    node_start = base._line_col_offset(source, int(node["line"]), int(node["column"]))
    node_end = base._line_col_offset(source, int(node["end_line"]), int(node["end_column"]))
    node_source = source[node_start:node_end]
    if node_source != node["observed_source"]:
        raise ValueError("visible_ast_node_source_drift")
    if edit["observed_source"] == node_source:
        return (
            node_start,
            node_end,
            edit["replacement_source"],
            {"mode": "exact_visible_ast_node", "node_id": node["node_id"]},
        )
    occurrences = base._unbound_span(source, edit["observed_source"])
    start, end, _ = occurrences
    if not (start <= node_start < node_end <= end):
        raise ValueError("ast_context_span_does_not_contain_bound_node")
    return (
        start,
        end,
        edit["replacement_source"],
        {
            "mode": "exact_context_span_anchored_by_visible_ast_node",
            "node_id": node["node_id"],
        },
    )


def _minimal_difference(observed: str, replacement: str) -> tuple[str, str, str, str]:
    prefix = 0
    limit = min(len(observed), len(replacement))
    while prefix < limit and observed[prefix] == replacement[prefix]:
        prefix += 1
    suffix = 0
    remaining_observed = len(observed) - prefix
    remaining_replacement = len(replacement) - prefix
    while (
        suffix < remaining_observed
        and suffix < remaining_replacement
        and observed[len(observed) - suffix - 1] == replacement[len(replacement) - suffix - 1]
    ):
        suffix += 1
    observed_end = len(observed) - suffix if suffix else len(observed)
    replacement_end = len(replacement) - suffix if suffix else len(replacement)
    return (
        observed[:prefix],
        observed[prefix:observed_end],
        replacement[prefix:replacement_end],
        observed[observed_end:],
    )


def _anchor_lines(value: str, *, from_end: bool) -> list[str]:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    selected = lines[-3:] if from_end else lines[:3]
    return selected


def _anchors_match(source: str, start: int, end: int, prefix: str, suffix: str) -> bool:
    before = source[max(0, start - 1500) : start]
    after = source[end : min(len(source), end + 1500)]
    prefix_anchors = _anchor_lines(prefix, from_end=True)
    suffix_anchors = _anchor_lines(suffix, from_end=False)
    prefix_ok = not prefix_anchors or any(anchor in before for anchor in prefix_anchors)
    suffix_ok = not suffix_anchors or any(anchor in after for anchor in suffix_anchors)
    return prefix_ok and suffix_ok


def _unbound_span(
    source: str, edit: dict[str, str]
) -> tuple[int, int, str, dict[str, Any]]:
    try:
        start, end, locator = base._unbound_span(source, edit["observed_source"])
        return start, end, edit["replacement_source"], locator
    except ValueError as exact_error:
        if not str(exact_error).endswith(":0"):
            raise
    prefix, old_middle, new_middle, suffix = _minimal_difference(
        edit["observed_source"], edit["replacement_source"]
    )
    if not old_middle or old_middle == new_middle or len(old_middle) > 4000:
        raise ValueError("proposal_difference_not_safely_reducible")
    starts: list[int] = []
    offset = 0
    while True:
        found = source.find(old_middle, offset)
        if found < 0:
            break
        end = found + len(old_middle)
        if _anchors_match(source, found, end, prefix, suffix):
            starts.append(found)
        offset = found + max(1, len(old_middle))
    if len(starts) != 1:
        raise ValueError(f"normalized_difference_not_unique:{len(starts)}")
    start = starts[0]
    return (
        start,
        start + len(old_middle),
        new_middle,
        {
            "mode": "proposal_difference_to_unique_anchored_source_span",
            "original_observed_sha256": canonical_json_hash(edit["observed_source"]),
            "normalized_observed_sha256": canonical_json_hash(old_middle),
        },
    )


def apply_python_span_patch(
    content: str,
    source_package: str | Path,
    candidate_package: str | Path,
    *,
    visible_ast_facts: dict[str, Any] | None,
    require_ast_binding: bool,
) -> dict[str, Any]:
    source_root = Path(source_package).resolve()
    candidate_root = Path(candidate_package).resolve()
    if require_ast_binding and not isinstance(visible_ast_facts, dict):
        raise ValueError("visible_ast_facts_required")
    parsed, ignored_fields = _parse_response(content)
    before_api = base._api_signatures(source_root)
    copy_tree_clean(source_root, candidate_root)
    grouped: dict[str, list[tuple[int, int, str, dict[str, str], dict[str, Any]]]] = defaultdict(list)
    receipts: list[dict[str, Any]] = []
    for edit in parsed["edits"]:
        target = base._safe_path(candidate_root, edit["path"])
        if not target.is_file():
            raise FileNotFoundError(edit["path"])
        source = target.read_text(encoding="utf-8")
        if require_ast_binding:
            start, end, replacement, locator = _bound_span(
                source, edit, visible_ast_facts or {}
            )
        else:
            if edit["target_node_id"] or edit["expected_node_sha256"]:
                ignored_fields.extend(["unbound_target_node_id", "unbound_expected_node_sha256"])
            start, end, replacement, locator = _unbound_span(source, edit)
        grouped[edit["path"]].append((start, end, replacement, edit, locator))

    for relative, edits in grouped.items():
        target = base._safe_path(candidate_root, relative)
        source = target.read_text(encoding="utf-8")
        ordered = sorted(edits, key=lambda row: (row[0], row[1]), reverse=True)
        for index, (start, end, replacement, edit, locator) in enumerate(ordered):
            if index + 1 < len(ordered) and ordered[index + 1][1] > start:
                raise ValueError("overlapping_edits_forbidden")
            observed = source[start:end]
            source = source[:start] + replacement + source[end:]
            receipts.append(
                {
                    "path": relative,
                    "symbol": edit["symbol"],
                    "locator": locator,
                    "applied_observed_sha256": canonical_json_hash(observed),
                    "applied_replacement_sha256": canonical_json_hash(replacement),
                }
            )
        import ast

        ast.parse(source, filename=relative)
        target.write_text(source, encoding="utf-8")

    after_api = base._api_signatures(candidate_root)
    if before_api != after_api:
        raise ValueError("public_api_signature_changed")
    source_hashes = hash_tree(source_root)
    candidate_hashes = hash_tree(candidate_root)
    changed_paths = sorted(
        path
        for path in set(source_hashes) | set(candidate_hashes)
        if source_hashes.get(path) != candidate_hashes.get(path)
    )
    if len(changed_paths) > MAX_EDITS or any(
        not path.endswith(".py") or "scripts/" not in path for path in changed_paths
    ):
        raise ValueError(f"changed_path_scope_invalid:{changed_paths}")
    gate = {
        "decision": ACCEPT_STRUCTURALLY,
        "checks": {
            "python_parse": True,
            "public_api_signatures_preserved": True,
            "changed_paths_within_scripts": True,
            "bounded_edit_count": len(parsed["edits"]) <= MAX_EDITS,
            "normalization_was_mechanical_only": True,
            "hidden_semantic_correctness_checked": False,
        },
        "claim_boundary": "Structural acceptance and mechanical normalization are not task correctness.",
    }
    return {
        "status": "candidate_materialized",
        "summary": parsed["summary"],
        "edit_count": len(parsed["edits"]),
        "changed_paths": changed_paths,
        "ignored_response_fields": sorted(set(ignored_fields)),
        "edit_receipts": receipts,
        "candidate_tree_hash": canonical_json_hash(candidate_hashes),
        "structural_gate": gate,
    }
