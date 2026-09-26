from __future__ import annotations

from collections.abc import Callable
from typing import Any

from skillscriptbench.io_utils import sha256_bytes


ParsedJson = Callable[[str], Any]
CandidateValidator = Callable[[str], None]


def exact_replacement_instruction(script_name: str) -> str:
    return (
        f"Do not return the complete {script_name}. Return strict JSON with exactly two keys: "
        '{"edits":[{"old":"exact unique source substring","new":"replacement"}],'
        '"summary":"brief repair description"}. Use 1 to 4 minimal exact replacements. '
        "Each old substring must occur exactly once in the supplied script and must preserve "
        "enough indentation and surrounding syntax to apply unambiguously."
    )


def parse_and_apply_exact_replacements(
    content: str,
    source: str,
    *,
    parse_json: ParsedJson,
    validate_candidate: CandidateValidator,
    maximum_edits: int = 4,
    maximum_single_edit_bytes: int = 8_000,
    maximum_total_edit_span_bytes: int = 12_000,
) -> tuple[str, str, dict[str, Any]]:
    parsed = parse_json(content)
    if not isinstance(parsed, dict) or set(parsed) != {"edits", "summary"}:
        raise ValueError("response must contain exactly edits and summary")
    edits = parsed["edits"]
    summary = parsed["summary"]
    if not isinstance(edits, list) or not 1 <= len(edits) <= maximum_edits:
        raise ValueError(f"edits must contain between one and {maximum_edits} replacements")
    if not isinstance(summary, str):
        raise TypeError("summary must be a string")

    mutant_sha256 = sha256_bytes(source.encode("utf-8"))
    receipts: list[dict[str, Any]] = []
    changed_bytes = 0
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict) or set(edit) != {"old", "new"}:
            raise ValueError(f"edit_{index}_must_contain_exactly_old_and_new")
        old = edit["old"]
        new = edit["new"]
        if not isinstance(old, str) or not isinstance(new, str):
            raise TypeError(f"edit_{index}_old_and_new_must_be_strings")
        if not old or old == new:
            raise ValueError(f"edit_{index}_must_change_a_nonempty_substring")
        old_bytes = old.encode("utf-8")
        new_bytes = new.encode("utf-8")
        if max(len(old_bytes), len(new_bytes)) > maximum_single_edit_bytes:
            raise ValueError(
                f"edit_{index}_exceeds_{maximum_single_edit_bytes}_byte_limit"
            )
        occurrences = source.count(old)
        if occurrences != 1:
            raise ValueError(f"edit_{index}_old_occurrence_count:{occurrences}")
        changed_bytes += max(len(old_bytes), len(new_bytes))
        if changed_bytes > maximum_total_edit_span_bytes:
            raise ValueError(
                f"total_edit_span_exceeds_{maximum_total_edit_span_bytes}_byte_limit"
            )
        source = source.replace(old, new, 1)
        receipts.append(
            {
                "index": index,
                "old_byte_count": len(old_bytes),
                "new_byte_count": len(new_bytes),
                "old_sha256": sha256_bytes(old_bytes),
                "new_sha256": sha256_bytes(new_bytes),
            }
        )

    validate_candidate(source)
    return source, summary, {
        "response_mode": "exact_replacement_edits_v1",
        "edit_count": len(receipts),
        "total_edit_span_upper_bound_bytes": changed_bytes,
        "mutant_sha256": mutant_sha256,
        "candidate_sha256": sha256_bytes(source.encode("utf-8")),
        "edits": receipts,
    }
