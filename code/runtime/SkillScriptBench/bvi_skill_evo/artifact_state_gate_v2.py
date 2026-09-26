from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Iterable

from bvi_skill_evo import artifact_scope_ast_v1 as script_backend
from bvi_skill_evo.balanced_hybrid_ast_v316 import _tree_hash
from skillscriptbench.io_utils import (
    canonical_json_hash,
    copy_tree_clean,
    hash_tree,
)


SCHEMA_VERSION = "skillscriptbench-artifact-state-gate-v2"
METHOD_ID = "public_request_artifact_state_gate_v2"

DOC_SATISFIED = "SATISFIED"
DOC_VIOLATED = "VIOLATED"
DOC_UNKNOWN = "UNKNOWN"
SCRIPT_VIOLATED = "VIOLATED"
SCRIPT_NO_VIOLATION = "NO_DETECTED_VIOLATION"

ROUTE_PRESERVE = "preserve_all"
ROUTE_MARKDOWN = "markdown_only"
ROUTE_SCRIPTS = "scripts_only"
ROUTE_JOINT = "joint"
ROUTE_UNCERTAIN = "uncertain"


def _normalized_markdown(value: str) -> str:
    value = value.replace("\u2014", "-").replace("\u2013", "-")
    return re.sub(r"\s+", " ", value).strip().casefold()


def extract_required_use_case(request_text: str) -> str | None:
    match = re.search(
        r"^##[ \t]+Required Use Case[ \t]*$",
        request_text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if match is None:
        return None
    # The benchmark contract makes Required Use Case the terminal request
    # section. Its payload may itself begin with shell comments such as
    # ``# Optional: ...``; treating those lines as Markdown headings silently
    # drops valid executable guidance.
    section = request_text[match.end() :].strip()
    return section or None


def markdown_contract_status(request_text: str, skill_text: str) -> dict[str, Any]:
    required = extract_required_use_case(request_text)
    if required is None:
        return {
            "status": DOC_UNKNOWN,
            "reason": "required_use_case_section_missing",
            "required_use_case_present": False,
            "normalized_exact_match": False,
            "required_use_case_hash": None,
        }
    required_normalized = _normalized_markdown(required)
    skill_normalized = _normalized_markdown(skill_text)
    matched = bool(required_normalized and required_normalized in skill_normalized)
    return {
        "status": DOC_SATISFIED if matched else DOC_VIOLATED,
        "reason": (
            "required_use_case_present_in_skill"
            if matched
            else "required_use_case_absent_from_skill"
        ),
        "required_use_case_present": True,
        "normalized_exact_match": matched,
        "required_use_case_hash": canonical_json_hash(
            {"normalized_required_use_case": required_normalized}
        ),
    }


def _script_status(facts: dict[str, Any]) -> str:
    decision = str((facts.get("localization_decision") or {}).get("decision") or "")
    return SCRIPT_VIOLATED if decision == "PROPOSE" else SCRIPT_NO_VIOLATION


def _route(document_status: str, script_status: str) -> str:
    if document_status == DOC_UNKNOWN:
        return ROUTE_UNCERTAIN
    script_violation = script_status == SCRIPT_VIOLATED
    if document_status == DOC_SATISFIED and not script_violation:
        return ROUTE_PRESERVE
    if document_status == DOC_VIOLATED and not script_violation:
        return ROUTE_MARKDOWN
    if document_status == DOC_SATISFIED and script_violation:
        return ROUTE_SCRIPTS
    if document_status == DOC_VIOLATED and script_violation:
        return ROUTE_JOINT
    return ROUTE_UNCERTAIN


def _editable_script_paths(facts: dict[str, Any]) -> list[str]:
    return sorted(
        {
            str(node.get("path") or "")
            for node in facts.get("editable_nodes") or []
            if str(node.get("path") or "")
        }
    )


def build_artifact_state_report(
    parent_package: str | Path,
    candidate_package: str | Path,
    request_text: str,
) -> dict[str, Any]:
    parent = Path(parent_package).resolve()
    candidate = Path(candidate_package).resolve()
    parent_facts = script_backend.build_public_multilang_contract_set(parent, request_text)
    candidate_facts = script_backend.build_public_multilang_contract_set(candidate, request_text)
    script_backend.validate_public_multilang_contract_set(parent_facts, parent)
    script_backend.validate_public_multilang_contract_set(candidate_facts, candidate)
    parent_doc = markdown_contract_status(
        request_text, (parent / "SKILL.md").read_text(encoding="utf-8")
    )
    candidate_doc = markdown_contract_status(
        request_text, (candidate / "SKILL.md").read_text(encoding="utf-8")
    )
    parent_script_status = _script_status(parent_facts)
    candidate_script_status = _script_status(candidate_facts)
    route = _route(str(parent_doc["status"]), parent_script_status)
    editable_script_paths = _editable_script_paths(parent_facts)
    allowed_paths: list[str] = []
    if route in {ROUTE_MARKDOWN, ROUTE_JOINT}:
        allowed_paths.append("SKILL.md")
    if route in {ROUTE_SCRIPTS, ROUTE_JOINT}:
        allowed_paths.extend(editable_script_paths)
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "source_scope": "public_request_parent_package_and_candidate_package",
        "parent_tree_hash": _tree_hash(parent),
        "candidate_tree_hash": _tree_hash(candidate),
        "parent_document": parent_doc,
        "candidate_document": candidate_doc,
        "parent_script": {
            "status": parent_script_status,
            "facts_hash": parent_facts["facts_hash"],
            "decision": parent_facts["localization_decision"],
        },
        "candidate_script": {
            "status": candidate_script_status,
            "facts_hash": candidate_facts["facts_hash"],
            "decision": candidate_facts["localization_decision"],
        },
        "artifact_route": route,
        "allowed_paths": sorted(set(allowed_paths)),
        "editable_script_paths": editable_script_paths,
        "benchmark_bundled_facts_consumed": False,
        "hidden_artifacts_consumed": False,
        "task_verifier_consumed": False,
        "gold_or_oracle_consumed": False,
        "reward_consumed": False,
        "semantic_correctness_inferred": False,
        "claim_boundary": (
            "The gate infers artifact scope from a public Required Use Case and public "
            "request-to-script discrepancies. Absence of a detected script discrepancy is "
            "not treated as a general semantic correctness proof."
        ),
    }
    result["report_hash"] = canonical_json_hash(result)
    return result


def validate_artifact_state_report(
    report: dict[str, Any],
    parent_package: str | Path,
    candidate_package: str | Path,
) -> str:
    body = dict(report)
    expected = str(body.pop("report_hash", ""))
    if not expected or canonical_json_hash(body) != expected:
        raise ValueError("artifact_state_report_hash_invalid")
    if report.get("method") != METHOD_ID:
        raise ValueError("artifact_state_method_invalid")
    if report.get("parent_tree_hash") != _tree_hash(Path(parent_package).resolve()):
        raise ValueError("artifact_state_parent_hash_invalid")
    if report.get("candidate_tree_hash") != _tree_hash(Path(candidate_package).resolve()):
        raise ValueError("artifact_state_candidate_hash_invalid")
    if any(
        report.get(field) is not False
        for field in (
            "benchmark_bundled_facts_consumed",
            "hidden_artifacts_consumed",
            "task_verifier_consumed",
            "gold_or_oracle_consumed",
            "reward_consumed",
        )
    ):
        raise ValueError("artifact_state_nonpublic_input_flag")
    return expected


def _copy_allowed_path(candidate: Path, destination: Path, relative: str) -> None:
    source = candidate / relative
    target = destination / relative
    if not source.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def project_candidate(
    parent_package: str | Path,
    candidate_package: str | Path,
    destination_package: str | Path,
    report: dict[str, Any],
) -> dict[str, Any]:
    parent = Path(parent_package).resolve()
    candidate = Path(candidate_package).resolve()
    destination = Path(destination_package).resolve()
    validate_artifact_state_report(report, parent, candidate)
    copy_tree_clean(parent, destination)
    for relative in report.get("allowed_paths") or []:
        _copy_allowed_path(candidate, destination, str(relative))
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "route": report["artifact_route"],
        "report_hash": report["report_hash"],
        "parent_tree_hash": _tree_hash(parent),
        "source_candidate_tree_hash": _tree_hash(candidate),
        "projected_tree_hash": _tree_hash(destination),
        "allowed_paths": list(report.get("allowed_paths") or []),
        "projected_file_hashes": hash_tree(destination),
        "hidden_artifacts_consumed": False,
        "task_verifier_consumed": False,
        "candidate_selection_uses_hidden": False,
    }
    result["projection_hash"] = canonical_json_hash(result)
    return result


def public_projection_score(
    parent_package: str | Path,
    projected_package: str | Path,
    request_text: str,
    route: str,
) -> tuple[int, int, int]:
    parent = Path(parent_package).resolve()
    projected = Path(projected_package).resolve()
    doc = markdown_contract_status(
        request_text, (projected / "SKILL.md").read_text(encoding="utf-8")
    )
    script = script_backend.build_public_multilang_contract_set(projected, request_text)
    script_backend.validate_public_multilang_contract_set(script, projected)
    doc_ok = doc["status"] == DOC_SATISFIED
    script_ok = script["localization_decision"]["decision"] == "ABSTAIN"
    parent_hashes = hash_tree(parent)
    projected_hashes = hash_tree(projected)
    changed_count = sum(
        parent_hashes.get(path) != projected_hashes.get(path)
        for path in set(parent_hashes) | set(projected_hashes)
    )
    preserve_bonus = int(route == ROUTE_PRESERVE and changed_count == 0)
    return (int(doc_ok) + int(script_ok), preserve_bonus, -changed_count)


def choose_public_projection(
    parent_package: str | Path,
    candidates: Iterable[tuple[str, str | Path]],
    destination_package: str | Path,
    request_text: str,
) -> dict[str, Any]:
    parent = Path(parent_package).resolve()
    destination = Path(destination_package).resolve()
    options: list[dict[str, Any]] = []
    temporary_root = destination.parent / f".{destination.name}.options"
    if temporary_root.exists():
        shutil.rmtree(temporary_root)
    temporary_root.mkdir(parents=True)
    try:
        for priority, (name, candidate_value) in enumerate(candidates):
            candidate = Path(candidate_value).resolve()
            report = build_artifact_state_report(parent, candidate, request_text)
            option_path = temporary_root / f"{priority:02d}-{name}" / "package"
            projection = project_candidate(parent, candidate, option_path, report)
            score = public_projection_score(
                parent, option_path, request_text, str(report["artifact_route"])
            )
            options.append(
                {
                    "name": name,
                    "priority": priority,
                    "candidate_tree_hash": _tree_hash(candidate),
                    "report": report,
                    "projection": projection,
                    "score": list(score),
                    "option_path": option_path,
                }
            )
        if not options:
            raise ValueError("artifact_state_no_projection_candidates")
        selected = max(
            options,
            key=lambda row: (
                tuple(int(value) for value in row["score"]),
                -int(row["priority"]),
            ),
        )
        copy_tree_clean(selected["option_path"], destination)
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "method": METHOD_ID,
            "selected_source": selected["name"],
            "selected_score": selected["score"],
            "selected_route": selected["report"]["artifact_route"],
            "selected_report_hash": selected["report"]["report_hash"],
            "selected_projection_hash": selected["projection"]["projection_hash"],
            "parent_tree_hash": _tree_hash(parent),
            "candidate_tree_hash": _tree_hash(destination),
            "options": [
                {
                    "name": row["name"],
                    "priority": row["priority"],
                    "candidate_tree_hash": row["candidate_tree_hash"],
                    "report_hash": row["report"]["report_hash"],
                    "projection_hash": row["projection"]["projection_hash"],
                    "route": row["report"]["artifact_route"],
                    "score": row["score"],
                }
                for row in options
            ],
            "hidden_artifacts_consumed": False,
            "task_verifier_consumed": False,
            "candidate_selection_uses_hidden": False,
        }
        result["selection_hash"] = canonical_json_hash(result)
        return result
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)
