from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from bvi_skill_evo.document_markers import BEGIN_MARKER, END_MARKER
from skillscriptbench.io_utils import canonical_json_hash


SCHEMA_VERSION = "skillscriptbench-package-contradiction-alignment-v1"
METHOD_ID = "public_package_llm_contradiction_alignment_v1"
MAX_EDITS = 2
MAX_EVIDENCE = 4


CONTRADICTION_ALIGNMENT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_package_contradiction_assessment",
        "description": (
            "Assess whether SKILL.md contradicts the public request or bundled scripts and, "
            "only when grounded, return minimal exact-span document edits."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "summary", "edits", "script_evidence"],
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["SATISFIED", "CONTRADICTED", "UNCERTAIN"],
                },
                "summary": {"type": "string"},
                "edits": {
                    "type": "array",
                    "maxItems": MAX_EDITS,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["old_text", "new_text", "reason"],
                        "properties": {
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                    },
                },
                "script_evidence": {
                    "type": "array",
                    "maxItems": MAX_EVIDENCE,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "quote", "supports"],
                        "properties": {
                            "path": {"type": "string"},
                            "quote": {"type": "string"},
                            "supports": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}


def build_prompt(request_text: str, skill_text: str, scripts: Mapping[str, str]) -> str:
    script_blocks = []
    for path, text in sorted(scripts.items()):
        script_blocks.append(f"\n--- FILE: {path} ---\n{text}\n--- END FILE: {path} ---")
    return (
        "Call submit_package_contradiction_assessment exactly once. You are checking one public "
        "executable skill package. Compare the public request, the complete SKILL.md, and every "
        "bundled script below. Decide whether SKILL.md contains a concrete operational statement "
        "that contradicts either the requested use case or actual script behavior. A missing "
        "explanation alone is not a contradiction. A normative warning such as 'do not X' is not "
        "evidence that the skill recommends X. Do not rewrite for style, brevity, completeness, or "
        "preference. Return SATISFIED when no concrete contradiction is visible. Return UNCERTAIN "
        "when script behavior cannot be determined. Return CONTRADICTED only when you can quote "
        "the exact unique SKILL.md span to replace and at least one exact script quote proving the "
        "conflict. Use at most two minimal edits. old_text must be copied byte-for-byte from "
        "SKILL.md. new_text must be the smallest correction and must not remove or weaken the "
        "Required Use Case. Script evidence path and quote must be copied exactly from the supplied "
        "scripts. Do not mention tests, hidden artifacts, verifier, oracle, reward, labels, or an "
        "expected answer.\n\nPUBLIC_REQUEST.md\n"
        + request_text
        + "\n\nSKILL.md\n"
        + skill_text
        + "\n\nBUNDLED_SCRIPTS\n"
        + "".join(script_blocks)
    )


def tool_arguments(response: Mapping[str, Any]) -> dict[str, Any]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("package_contradiction_response_choices_missing")
    calls = (choices[0].get("message") or {}).get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1:
        raise ValueError("package_contradiction_tool_call_count_invalid")
    function = calls[0].get("function") or {}
    if function.get("name") != "submit_package_contradiction_assessment":
        raise ValueError("package_contradiction_tool_name_invalid")
    arguments = function.get("arguments")
    payload = json.loads(arguments) if isinstance(arguments, str) else arguments
    if not isinstance(payload, dict):
        raise TypeError("package_contradiction_payload_invalid")
    return payload


def _clean_text(value: Any, field: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"package_contradiction_{field}_not_string")
    text = value.strip()
    if not minimum <= len(text) <= maximum:
        raise ValueError(f"package_contradiction_{field}_length_invalid")
    forbidden = ("hidden artifact", "task verifier", "gold answer", "oracle output", "reward signal", "benchmark label")
    if any(token in text.casefold() for token in forbidden):
        raise ValueError(f"package_contradiction_{field}_nonpublic_reference")
    return text


def validate_assessment(
    payload: Mapping[str, Any],
    *,
    request_text: str,
    skill_text: str,
    scripts: Mapping[str, str],
) -> dict[str, Any]:
    if set(payload) != {"status", "summary", "edits", "script_evidence"}:
        raise ValueError("package_contradiction_top_level_schema_invalid")
    status = payload["status"]
    if status not in {"SATISFIED", "CONTRADICTED", "UNCERTAIN"}:
        raise ValueError("package_contradiction_status_invalid")
    summary = _clean_text(payload["summary"], "summary", 8, 800)
    if not isinstance(payload["edits"], list) or len(payload["edits"]) > MAX_EDITS:
        raise ValueError("package_contradiction_edits_schema_invalid")
    if not isinstance(payload["script_evidence"], list) or len(payload["script_evidence"]) > MAX_EVIDENCE:
        raise ValueError("package_contradiction_evidence_schema_invalid")

    edits = []
    spans: list[tuple[int, int]] = []
    for value in payload["edits"]:
        row = dict(value)
        if set(row) != {"old_text", "new_text", "reason"}:
            raise ValueError("package_contradiction_edit_schema_invalid")
        old_text = _clean_text(row["old_text"], "old_text", 12, 4000)
        if skill_text.count(old_text) != 1:
            raise ValueError("package_contradiction_old_text_not_unique")
        if BEGIN_MARKER in old_text or END_MARKER in old_text:
            raise ValueError("package_contradiction_edit_overlaps_protected_marker")
        new_value = row["new_text"]
        if not isinstance(new_value, str) or len(new_value) > 4000:
            raise ValueError("package_contradiction_new_text_invalid")
        new_text = new_value.strip()
        if new_text == old_text:
            raise ValueError("package_contradiction_noop_edit")
        if BEGIN_MARKER in new_text or END_MARKER in new_text:
            raise ValueError("package_contradiction_new_text_contains_marker")
        reason = _clean_text(row["reason"], "reason", 8, 800)
        start = skill_text.index(old_text)
        end = start + len(old_text)
        if any(not (end <= prior_start or start >= prior_end) for prior_start, prior_end in spans):
            raise ValueError("package_contradiction_edits_overlap")
        spans.append((start, end))
        edits.append({"old_text": old_text, "new_text": new_text, "reason": reason, "start": start, "end": end})

    evidence = []
    for value in payload["script_evidence"]:
        row = dict(value)
        if set(row) != {"path", "quote", "supports"}:
            raise ValueError("package_contradiction_script_evidence_schema_invalid")
        path = str(row["path"])
        if path not in scripts or Path(path).is_absolute() or ".." in Path(path).parts:
            raise ValueError("package_contradiction_script_path_invalid")
        quote = _clean_text(row["quote"], "script_quote", 8, 2000)
        if scripts[path].count(quote) < 1:
            raise ValueError("package_contradiction_script_quote_not_grounded")
        supports = _clean_text(row["supports"], "script_support", 8, 800)
        evidence.append({"path": path, "quote": quote, "supports": supports})

    if status == "CONTRADICTED":
        if not edits or not evidence:
            raise ValueError("package_contradiction_grounded_edit_required")
    elif edits or evidence:
        raise ValueError("package_contradiction_nonedit_status_has_changes")

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "status": status,
        "summary": summary,
        "edits": edits,
        "script_evidence": evidence,
        "request_hash": canonical_json_hash({"request_text": request_text}),
        "skill_hash": canonical_json_hash({"skill_text": skill_text}),
        "scripts_hash": canonical_json_hash({"scripts": dict(sorted(scripts.items()))}),
        "source_scope": "public_request_skill_and_scripts_only",
        "llm_role": "semantic_contradiction_assessment_and_minimal_edit_proposal",
        "host_role": "exact_span_evidence_validation_and_bounded_application",
    }
    result["assessment_hash"] = canonical_json_hash(result)
    return result


def apply_validated_edits(skill_text: str, assessment: Mapping[str, Any]) -> str:
    if assessment.get("status") != "CONTRADICTED":
        return skill_text
    result = skill_text
    edits = sorted(assessment["edits"], key=lambda row: int(row["start"]), reverse=True)
    for row in edits:
        start = int(row["start"])
        end = int(row["end"])
        if result[start:end] != row["old_text"]:
            raise ValueError("package_contradiction_edit_source_changed")
        replacement = str(row["new_text"])
        result = result[:start] + replacement + result[end:]
    return result
