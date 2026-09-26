from __future__ import annotations

from typing import Any, Mapping

from bvi_skill_evo import package_contradiction_alignment_v1 as v1
from skillscriptbench.io_utils import canonical_json_hash


SCHEMA_VERSION = "skillscriptbench-package-contradiction-alignment-v2"
METHOD_ID = "public_package_llm_contradiction_alignment_v2"
CONTRADICTION_ALIGNMENT_TOOL = v1.CONTRADICTION_ALIGNMENT_TOOL
tool_arguments = v1.tool_arguments
apply_validated_edits = v1.apply_validated_edits


def build_prompt(request_text: str, skill_text: str, scripts: Mapping[str, str]) -> str:
    return v1.build_prompt(request_text, skill_text, scripts) + (
        "\n\nV2_PROTOCOL_CLARIFICATIONS\n"
        "For SATISFIED or UNCERTAIN, return both edits and script_evidence as empty arrays. "
        "Whitespace, indentation, pretty-printing, JSON spacing, Markdown table alignment, and "
        "punctuation style alone are not operational contradictions. Do not propose an edit when "
        "old_text and new_text differ only in whitespace."
    )


def validate_assessment(
    payload: Mapping[str, Any],
    *,
    request_text: str,
    skill_text: str,
    scripts: Mapping[str, str],
) -> dict[str, Any]:
    normalized_payload = dict(payload)
    if normalized_payload.get("status") in {"SATISFIED", "UNCERTAIN"}:
        if normalized_payload.get("edits"):
            raise ValueError("package_contradiction_v2_nonedit_status_has_edits")
        normalized_payload["script_evidence"] = []
    result = v1.validate_assessment(
        normalized_payload,
        request_text=request_text,
        skill_text=skill_text,
        scripts=scripts,
    )
    for edit in result["edits"]:
        if "".join(str(edit["old_text"]).split()) == "".join(str(edit["new_text"]).split()):
            raise ValueError("package_contradiction_v2_whitespace_only_edit")
    result.pop("assessment_hash", None)
    result["schema_version"] = SCHEMA_VERSION
    result["method"] = METHOD_ID
    result["protocol_revision"] = (
        "nonedit evidence ignored; whitespace-only edits rejected"
    )
    result["assessment_hash"] = canonical_json_hash(result)
    return result
