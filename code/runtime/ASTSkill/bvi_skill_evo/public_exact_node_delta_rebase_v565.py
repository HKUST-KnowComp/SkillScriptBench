from __future__ import annotations

import difflib
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from bvi_skill_evo.contextual_unified_public_closure_executor_v517 import (
    _public_signatures,
    _syntax_findings,
)
from skillscriptbench.io_utils import (
    canonical_json_hash,
    copy_tree_clean,
    hash_tree,
)


SCHEMA_VERSION = "5.65-public-exact-node-delta-rebase-v1"
METHOD_ID = "public_exact_node_delta_rebase_v1"
ACCEPT_STRUCTURALLY = "ACCEPT_STRUCTURALLY"
ABSTAIN_TO_RAW = "ABSTAIN_TO_RAW"


def _tree_hash(root: Path) -> str:
    return canonical_json_hash(hash_tree(root))


def _find_subsequence(haystack: list[str], needle: list[str]) -> list[int]:
    if not needle or len(needle) > len(haystack):
        return []
    width = len(needle)
    return [
        index
        for index in range(len(haystack) - width + 1)
        if haystack[index : index + width] == needle
    ]


def _line_hunks(parent_source: str, candidate_source: str) -> list[dict[str, Any]]:
    parent_lines = parent_source.splitlines(keepends=True)
    candidate_lines = candidate_source.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(
        None,
        parent_lines,
        candidate_lines,
        autojunk=False,
    )
    hunks: list[dict[str, Any]] = []
    for tag, parent_start, parent_end, candidate_start, candidate_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        hunks.append(
            {
                "tag": tag,
                "parent_line_start": parent_start + 1,
                "parent_line_end": parent_end,
                "candidate_line_start": candidate_start + 1,
                "candidate_line_end": candidate_end,
                "parent_lines": parent_lines[parent_start:parent_end],
                "candidate_lines": candidate_lines[candidate_start:candidate_end],
            }
        )
    return hunks


def _edit_intersects_hunk(edit: dict[str, Any], hunk: dict[str, Any]) -> bool:
    edit_start = int(edit.get("line") or 0)
    edit_end = int(edit.get("end_line") or edit_start)
    hunk_start = int(hunk["parent_line_start"])
    hunk_end = int(hunk["parent_line_end"])
    return edit_start <= hunk_end and hunk_start <= edit_end


def _project_file(
    *,
    path: str,
    parent_source: str,
    raw_source: str,
    candidate_source: str,
    edits: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[str]]:
    # A unique line elsewhere in a changed file does not identify the original
    # parser-bound target. Until cross-snapshot node identity is available, only
    # project into an unchanged file (or recognize the entire completed delta).
    # Other proposal files remain untouched by this file-local restriction.
    if raw_source not in (parent_source, candidate_source):
        return raw_source, [], [f"{path}:changed_raw_file_requires_node_identity"]
    effective_edits: list[dict[str, Any]] = []
    parent_bytes = parent_source.encode("utf-8")
    for original in edits:
        edit = dict(original)
        if int(edit.get("line") or 0) <= 0:
            span = edit.get("byte_span") or {}
            start = int(span.get("start") or 0)
            end = int(span.get("end") or start)
            if not (0 <= start <= end <= len(parent_bytes)):
                findings = [f"{path}:invalid_byte_span_for_line_recovery"]
                return raw_source, [], findings
            edit["line"] = parent_bytes[:start].count(b"\n") + 1
            edit["end_line"] = parent_bytes[:end].count(b"\n") + 1
        effective_edits.append(edit)
    hunks = _line_hunks(parent_source, candidate_source)
    findings: list[str] = []
    if not hunks:
        findings.append(f"{path}:candidate_has_no_delta")
        return raw_source, [], findings
    for hunk in hunks:
        if not hunk["parent_lines"] or not hunk["candidate_lines"]:
            findings.append(f"{path}:non_replacement_hunk")
        if not any(_edit_intersects_hunk(edit, hunk) for edit in effective_edits):
            findings.append(f"{path}:hunk_outside_declared_exact_nodes")
    for edit in effective_edits:
        if not any(_edit_intersects_hunk(edit, hunk) for hunk in hunks):
            findings.append(f"{path}:declared_node_without_candidate_hunk")
    if findings:
        return raw_source, [], sorted(set(findings))

    projected_lines = candidate_source.splitlines(keepends=True)
    receipts: list[dict[str, Any]] = []
    for index, hunk in enumerate(hunks, start=1):
        parent_block = list(hunk["parent_lines"])
        candidate_block = list(hunk["candidate_lines"])
        if raw_source == candidate_source:
            # The complete candidate already establishes identity, even if the
            # old text also survives in a different function in that candidate.
            action = "already_applied_in_raw"
            projected_start = hunk["candidate_line_start"]
        else:
            # raw_source == parent_source: use the validated candidate directly,
            # never search identical lines in other functions for a target.
            action = "applied_parent_delta"
            projected_start = hunk["candidate_line_start"]
        receipts.append(
            {
                "hunk_index": index,
                "action": action,
                "parent_line_start": hunk["parent_line_start"],
                "parent_line_end": hunk["parent_line_end"],
                "projected_line_start": projected_start,
                "parent_block_hash": canonical_json_hash(parent_block),
                "candidate_block_hash": canonical_json_hash(candidate_block),
                "declared_target_node_ids": sorted(
                    {
                        str(edit["target_node_id"])
                        for edit in effective_edits
                        if _edit_intersects_hunk(edit, hunk)
                    }
                ),
            }
        )
    return "".join(projected_lines), receipts, sorted(set(findings))


def _changed_paths(left: Path, right: Path) -> list[str]:
    left_hashes = hash_tree(left)
    right_hashes = hash_tree(right)
    return sorted(
        path
        for path in set(left_hashes) | set(right_hashes)
        if left_hashes.get(path) != right_hashes.get(path)
    )


def rebase_exact_node_delta(
    *,
    parent_package: str | Path,
    raw_package: str | Path,
    candidate_package: str | Path,
    response_application: dict[str, Any],
    output_package: str | Path,
) -> dict[str, Any]:
    parent = Path(parent_package).resolve()
    raw = Path(raw_package).resolve()
    candidate = Path(candidate_package).resolve()
    output = Path(output_package).resolve()
    if output.exists():
        raise FileExistsError(output)

    edits = list(response_application.get("normalized_edits") or [])
    edits_by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edit in edits:
        edits_by_path[str(edit["path"])].append(edit)
    declared_paths = sorted(edits_by_path)
    candidate_changed_paths = _changed_paths(parent, candidate)
    findings: list[str] = []
    if response_application.get("structural_gate", {}).get("decision") != ACCEPT_STRUCTURALLY:
        findings.append("source_candidate_not_structurally_accepted")
    if not edits:
        findings.append("source_candidate_has_no_normalized_edits")
    if candidate_changed_paths != declared_paths:
        findings.append("candidate_changed_paths_do_not_match_declared_nodes")

    projected_sources: dict[str, str] = {}
    hunk_receipts: list[dict[str, Any]] = []
    for relative in declared_paths:
        parent_path = parent / relative
        raw_path = raw / relative
        candidate_path = candidate / relative
        try:
            parent_source = parent_path.read_text(encoding="utf-8")
            raw_source = raw_path.read_text(encoding="utf-8")
            candidate_source = candidate_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            findings.append(f"{relative}:source_read:{type(exc).__name__}:{exc}")
            continue
        projected, receipts, local_findings = _project_file(
            path=relative,
            parent_source=parent_source,
            raw_source=raw_source,
            candidate_source=candidate_source,
            edits=edits_by_path[relative],
        )
        projected_sources[relative] = projected
        hunk_receipts.extend({"path": relative, **receipt} for receipt in receipts)
        findings.extend(local_findings)

    copy_tree_clean(raw, output)
    if not findings:
        for relative, source in projected_sources.items():
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(source, encoding="utf-8")

    projected_changed_paths = _changed_paths(raw, output)
    syntax_findings = _syntax_findings(output, projected_changed_paths)
    signatures_preserved = _public_signatures(raw) == _public_signatures(output)
    checks = {
        "source_candidate_structurally_accepted": response_application.get(
            "structural_gate", {}
        ).get("decision")
        == ACCEPT_STRUCTURALLY,
        "candidate_delta_matches_declared_exact_nodes": (
            bool(edits) and candidate_changed_paths == declared_paths
        ),
        "all_delta_hunks_projected_or_already_applied": not findings,
        "projected_changes_within_declared_paths": set(projected_changed_paths)
        <= set(declared_paths),
        "raw_public_signatures_preserved": signatures_preserved,
        "projected_syntax_valid": not syntax_findings,
        "raw_is_projection_base": True,
    }
    decision = ACCEPT_STRUCTURALLY if all(checks.values()) else ABSTAIN_TO_RAW
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "decision": decision,
        "parent_tree_hash": _tree_hash(parent),
        "raw_tree_hash": _tree_hash(raw),
        "source_candidate_tree_hash": _tree_hash(candidate),
        "projected_tree_hash": _tree_hash(output),
        "projected_tree_differs_from_raw": _tree_hash(output) != _tree_hash(raw),
        "declared_exact_node_count": len(edits),
        "declared_changed_paths": declared_paths,
        "candidate_changed_paths": candidate_changed_paths,
        "projected_changed_paths": projected_changed_paths,
        "applied_hunk_count": sum(
            receipt["action"] == "applied_parent_delta" for receipt in hunk_receipts
        ),
        "already_applied_hunk_count": sum(
            receipt["action"] == "already_applied_in_raw" for receipt in hunk_receipts
        ),
        "hunk_receipts": hunk_receipts,
        "findings": sorted(set(findings)),
        "syntax_findings": syntax_findings,
        "checks": checks,
        "hidden_artifacts_consumed": False,
        "task_verifier_consumed": False,
        "gold_or_oracle_consumed": False,
        "reward_consumed": False,
        "semantic_correctness_inferred": False,
        "claim_boundary": (
            "This operation rebases a previously frozen exact-node candidate delta onto "
            "a public Raw package only when each affected file equals its parent or "
            "complete candidate. Other Raw files are preserved. Changed affected files "
            "are conservatively rejected; task-level semantic correctness is not established."
        ),
    }
    report["rebase_hash"] = canonical_json_hash(report)
    return report


__all__ = [
    "ABSTAIN_TO_RAW",
    "ACCEPT_STRUCTURALLY",
    "METHOD_ID",
    "SCHEMA_VERSION",
    "rebase_exact_node_delta",
]
