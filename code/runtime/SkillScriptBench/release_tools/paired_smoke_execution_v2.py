from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from bvi_skill_evo import public_runtime_multilang_dual_v410 as ast_backend
from release_tools.artifact_state_release import _canonical_embedded_hash_valid
from release_tools.paired_smoke_protocol import audit_protocol as audit_v1_protocol
from skillscriptbench.io_utils import (
    canonical_json_hash,
    copy_tree_clean,
    hash_tree,
    read_json,
    sha256_bytes,
    sha256_file,
    write_json,
)
from skillscriptbench.package_matrix_conditions_v64 import (
    PATCH_TOOL,
    _call_patch_completion,
    _patch_arguments,
    parse_and_apply_package_edits,
    validate_candidate_package,
)


SCHEMA_VERSION = "skillscriptbench-paired-smoke-execution-v2"
PROTOCOL_VERSION = "paired_artifact_states_smoke_v2"
MODEL = "gpt-5.5"
BASE_URL = "https://api.openlux.ai/v1"
WORKERS = 6
TIMEOUT = 600
MAX_ATTEMPTS = 2
# Applies to new Markdown-only calls; existing frozen results are unchanged.
MARKDOWN_MAX_COMPLETION_TOKENS = 8192
PRIMARY_VIEWS = ("markdown-view", "package-view")
FINAL_CONDITIONS = (
    "no-evolution",
    "md-only-self-evolution",
    "raw-package-self-evolution",
    "ast-package-self-evolution",
)
REVISION_VIEWS = (
    "markdown-revision",
    "package-revision",
    "structural-revision",
)
MAX_VISIBLE_FILE_BYTES = 250_000
MAX_VISIBLE_PACKAGE_BYTES = 1_500_000
HIGH_ENTROPY_SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")


def _workspace() -> Path:
    return Path(__file__).resolve().parents[1]


def _embedded_hash_valid(payload: dict[str, Any], field: str) -> bool:
    expected = payload.get(field)
    body = dict(payload)
    body.pop(field, None)
    return isinstance(expected, str) and expected == canonical_json_hash(body)


def _tree_hash(root: Path) -> str:
    return canonical_json_hash(hash_tree(root))


def _tree_diff(left: Path, right: Path) -> list[str]:
    left_hashes = hash_tree(left)
    right_hashes = hash_tree(right)
    return sorted(
        path
        for path in set(left_hashes) | set(right_hashes)
        if left_hashes.get(path) != right_hashes.get(path)
    )


def _jsonl_read(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _copy_case(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    for name in ("REQUEST.md", "TASK.json"):
        shutil.copy2(source / name, destination / name)
    copy_tree_clean(source / "package", destination / "package")


def _package_payload(package: Path, *, markdown_only: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    total_bytes = 0
    for path in sorted(value for value in package.rglob("*") if value.is_file()):
        relative = path.relative_to(package).as_posix()
        if markdown_only and path.suffix.lower() != ".md":
            continue
        data = path.read_bytes()
        if len(data) > MAX_VISIBLE_FILE_BYTES or total_bytes + len(data) > MAX_VISIBLE_PACKAGE_BYTES:
            rows.append(
                {
                    "path": relative,
                    "editable": False,
                    "encoding": "omitted-by-public-size-bound",
                    "sha256": sha256_bytes(data),
                    "numbered_content": "",
                }
            )
            continue
        total_bytes += len(data)
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            rows.append(
                {
                    "path": relative,
                    "editable": False,
                    "encoding": "binary",
                    "sha256": sha256_bytes(data),
                    "numbered_content": "",
                }
            )
            continue
        rows.append(
            {
                "path": relative,
                "editable": relative == "SKILL.md" or relative.startswith("scripts/"),
                "encoding": "utf-8",
                "sha256": sha256_bytes(data),
                "numbered_content": "\n".join(
                    f"{line_number}: {line}"
                    for line_number, line in enumerate(content.splitlines(), start=1)
                ),
            }
        )
    return rows


def _edit_protocol() -> str:
    return (
        "Call submit_skill_patch exactly once. Return zero to eight bounded line edits. "
        "Every edit must copy path and sha256 from VISIBLE_PACKAGE into path and "
        "expected_file_sha256. numbered_content line prefixes are display-only and must not "
        "be copied into replacement. replace_lines replaces the inclusive start_line..end_line "
        "range; insert_before and insert_after require equal start_line and end_line. Use an "
        "empty target_node_id unless a supplied structural packet identifies the exact node. "
        "Do not return a unified diff or complete files. Use zero edits when no change is needed."
    )


def _primary_prompt(case_root: Path, *, markdown_only: bool) -> str:
    request = (case_root / "REQUEST.md").read_text(encoding="utf-8")
    task = read_json(case_root / "TASK.json")
    scope = (
        "Only Markdown files are visible and editable; executable files are unavailable and immutable."
        if markdown_only
        else "The complete public package is visible; edit only SKILL.md or existing files under scripts/."
    )
    return (
        "Audit and repair one visible executable Agent Skill package. "
        "Do not assume a defect exists. Preserve unrelated behavior and public interfaces. "
        "Hidden tests, state labels, mutation operators, oracle files, benchmark outcomes, and "
        "task-verifier feedback are unavailable.\n\n"
        + scope
        + "\n\n"
        + _edit_protocol()
        + "\n\nPUBLIC_TASK.json\n"
        + json.dumps(task, indent=2, sort_keys=True, ensure_ascii=True)
        + "\n\nUSER_REQUEST.md\n"
        + request
        + "\n\nVISIBLE_PACKAGE.json\n"
        + json.dumps(
            _package_payload(case_root / "package", markdown_only=markdown_only),
            indent=2,
            ensure_ascii=True,
        )
        + "\n"
    )


def _generic_checks(package: Path, source: Path) -> dict[str, Any]:
    editable = [
        path.relative_to(package).as_posix()
        for path in package.rglob("*")
        if path.is_file()
        and (path.relative_to(package).as_posix() == "SKILL.md" or "scripts" in path.parts)
    ]
    syntax = validate_candidate_package(package, editable)
    changed = _tree_diff(source, package)
    allowed = all(path == "SKILL.md" or path.startswith("scripts/") for path in changed)
    return {
        "status": "pass" if syntax["status"] == "pass" and allowed else "fail",
        "syntax": syntax,
        "changed_paths": changed,
        "changed_paths_within_public_edit_scope": allowed,
        "task_correctness_assessed": False,
        "hidden_artifacts_consumed": False,
        "verifier_feedback_consumed": False,
    }


def _revision_prompt(
    case_root: Path,
    source_package: Path,
    generic_checks: dict[str, Any],
    *,
    view: str,
    facts: dict[str, Any] | None,
) -> str:
    request = (case_root / "REQUEST.md").read_text(encoding="utf-8")
    markdown_only = view == "markdown-revision"
    if markdown_only:
        scope = "Only Markdown remains visible and editable; scripts are unavailable and immutable."
    else:
        scope = "The complete frozen package proposal is visible; edit only SKILL.md or scripts/."
    structural = ""
    if facts is not None:
        structural = (
            "\n\nPUBLIC_RUNTIME_STRUCTURAL_PACKET.json\n"
            + json.dumps(facts, indent=2, sort_keys=True, ensure_ascii=True)
            + "\nThe packet constrains executable edits to listed nodes but does not reveal the correct semantics. "
            "For script edits, copy the packet node_id into target_node_id. Documentation edits use an empty target_node_id."
        )
    return (
        "Revise one frozen executable-skill package proposal. Inspect it again against the public "
        "request and the generic checks. Do not assume another edit is necessary. Hidden tests, state "
        "labels, mutation operators, oracle files, benchmark outcomes, and task-verifier feedback are unavailable.\n\n"
        + scope
        + "\n\n"
        + _edit_protocol()
        + "\n\nUSER_REQUEST.md\n"
        + request
        + "\n\nGENERIC_PUBLIC_CHECKS.json\n"
        + json.dumps(generic_checks, indent=2, sort_keys=True, ensure_ascii=True)
        + structural
        + "\n\nFROZEN_PROPOSAL_PACKAGE.json\n"
        + json.dumps(
            _package_payload(source_package, markdown_only=markdown_only),
            indent=2,
            ensure_ascii=True,
        )
        + "\n"
    )


def prepare_execution(
    workspace_root: Path,
    benchmark_root: Path,
    parent_protocol_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    workspace_root = workspace_root.resolve()
    benchmark_root = benchmark_root.resolve()
    parent_protocol_root = parent_protocol_root.resolve()
    output_root = output_root.resolve()
    if output_root.exists():
        raise FileExistsError(output_root)
    parent_audit = audit_v1_protocol(parent_protocol_root, benchmark_root)
    if parent_audit.get("status") != "pass" or not _embedded_hash_valid(
        parent_audit, "preflight_hash"
    ):
        raise ValueError("paired_smoke_v1_parent_invalid")
    parent_protocol = read_json(parent_protocol_root / "_audit" / "FROZEN_PROTOCOL.json")
    matrix = read_json(parent_protocol_root / "_audit" / "CASE_MATRIX.json")["rows"]
    if len(matrix) != 12:
        raise ValueError("paired_smoke_v2_case_count_not_12")

    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output_root.name}.building-", dir=output_root.parent)
    )
    try:
        stage = temporary / "stage"
        prompts = stage / "prompts" / "primary"
        public_cases = stage / "public" / "cases"
        audit_root = temporary / "_audit"
        prompts.mkdir(parents=True)
        public_cases.mkdir(parents=True)
        audit_root.mkdir(parents=True)
        calls: list[dict[str, Any]] = []
        for row in sorted(matrix, key=lambda item: item["case_id"]):
            case_id = str(row["case_id"])
            source_case = benchmark_root / "public" / "cases" / case_id
            staged_case = public_cases / case_id
            _copy_case(source_case, staged_case)
            for view, markdown_only in (("markdown-view", True), ("package-view", False)):
                trial_id = f"{case_id}--{view}--primary"
                prompt_path = prompts / f"{trial_id}.txt"
                prompt_path.write_text(
                    _primary_prompt(staged_case, markdown_only=markdown_only),
                    encoding="utf-8",
                )
                calls.append(
                    {
                        "trial_id": trial_id,
                        "case_id": case_id,
                        "view": view,
                        "phase": "primary",
                        "prompt_path": prompt_path.relative_to(temporary).as_posix(),
                        "prompt_sha256": sha256_file(prompt_path),
                        "source_package_path": (
                            staged_case / "package"
                        ).relative_to(temporary).as_posix(),
                        "source_package_tree_hash": _tree_hash(staged_case / "package"),
                    }
                )
        calls.sort(key=lambda item: item["trial_id"])
        write_json(stage / "PRIMARY_CALLS.json", {"calls": calls})
        code_paths = {
            "release_tools/paired_smoke_execution_v2.py": Path(__file__),
            "release_tools/paired_smoke_hidden_evaluator_v2.py": _workspace()
            / "release_tools"
            / "paired_smoke_hidden_evaluator_v2.py",
            "release_tools/paired_smoke_protocol.py": _workspace()
            / "release_tools"
            / "paired_smoke_protocol.py",
            "skillscriptbench/package_matrix_conditions_v64.py": _workspace()
            / "skillscriptbench"
            / "package_matrix_conditions_v64.py",
            "bvi_skill_evo/public_runtime_multilang_dual_v410.py": Path(
                ast_backend.__file__
            ),
        }
        protocol: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "status": "frozen_before_primary_model_calls",
            "scientific_role": "pipeline_smoke_not_paper_main_result",
            "model": MODEL,
            "temperature": 0,
            "base_url": BASE_URL,
            "worker_count": WORKERS,
            "timeout_seconds": TIMEOUT,
            "max_transport_attempts_per_logical_call": MAX_ATTEMPTS,
            "case_count": 12,
            "base_count": 3,
            "primary_logical_calls": 24,
            "revision_logical_calls": 36,
            "total_logical_calls": 60,
            "conditions": list(FINAL_CONDITIONS),
            "shared_package_primary": True,
            "raw_ast_share_frozen_primary_candidate": True,
            "hidden_outcomes_feed_back": False,
            "candidate_selection_uses_hidden": False,
            "parent_protocol_hash": parent_protocol["protocol_hash"],
            "parent_preflight_hash": parent_audit["preflight_hash"],
            "benchmark_preflight_hash": read_json(
                benchmark_root / "_audit" / "ZERO_MODEL_PREFLIGHT.json"
            )["preflight_hash"],
            "primary_calls_sha256": sha256_file(stage / "PRIMARY_CALLS.json"),
            "code_sha256": {
                name: sha256_file(path) for name, path in sorted(code_paths.items())
            },
            "model_calls": 0,
            "behavioral_evaluator_loaded": False,
            "credential_persisted": False,
        }
        protocol["protocol_hash"] = canonical_json_hash(protocol)
        write_json(stage / "FROZEN_PROTOCOL.json", protocol)
        preflight = audit_execution(temporary, benchmark_root, parent_protocol_root)
        write_json(audit_root / "ZERO_CALL_PREFLIGHT.json", preflight)
        if preflight["status"] != "pass":
            raise ValueError(f"paired_smoke_v2_preflight_failed:{preflight['failed_checks']}")
        os.replace(temporary, output_root)
        return preflight
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def audit_execution(
    experiment_root: Path, benchmark_root: Path, parent_protocol_root: Path
) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    protocol = read_json(experiment / "stage" / "FROZEN_PROTOCOL.json")
    calls = read_json(experiment / "stage" / "PRIMARY_CALLS.json")["calls"]
    parent_audit = audit_v1_protocol(parent_protocol_root.resolve(), benchmark_root.resolve())
    prompt_hashes = all(
        sha256_file(experiment / call["prompt_path"]) == call["prompt_sha256"]
        for call in calls
    )
    source_hashes = all(
        _tree_hash(experiment / call["source_package_path"])
        == call["source_package_tree_hash"]
        for call in calls
    )
    # Scan only provider-visible stage artifacts. Including prior audit reports would
    # make check names such as ``no_private_directory`` self-trigger on re-audit.
    visible_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (experiment / "stage").rglob("*")
        if path.is_file() and path.stat().st_size <= 2_000_000
    )
    code_hashes = protocol.get("code_sha256") or {}
    checks = {
        "protocol_hash_valid": _embedded_hash_valid(protocol, "protocol_hash"),
        "parent_protocol_valid": parent_audit.get("status") == "pass"
        and _embedded_hash_valid(parent_audit, "preflight_hash"),
        "case_count_exact": protocol.get("case_count") == 12,
        "primary_call_count_exact": len(calls) == 24,
        "two_primary_views_per_case": Counter(call["view"] for call in calls)
        == Counter({"markdown-view": 12, "package-view": 12}),
        "trial_ids_unique": len({call["trial_id"] for call in calls}) == 24,
        "prompt_hashes_valid": prompt_hashes,
        "source_tree_hashes_valid": source_hashes,
        "model_exact_gpt55": protocol.get("model") == MODEL,
        "workers_exact_six": protocol.get("worker_count") == WORKERS,
        "logical_call_budget_60": protocol.get("total_logical_calls") == 60,
        "shared_primary_frozen": protocol.get("shared_package_primary") is True,
        "hidden_not_loaded": protocol.get("behavioral_evaluator_loaded") is False,
        "model_calls_zero": protocol.get("model_calls") == 0,
        "no_private_directory": not (experiment / "_private").exists(),
        "no_private_path_token": "_private" not in visible_text,
        "no_hidden_label_token": "canonical_package_path" not in visible_text,
        "no_high_entropy_secret": HIGH_ENTROPY_SECRET.search(visible_text) is None,
        "execution_code_hash_valid": code_hashes.get(
            "release_tools/paired_smoke_execution_v2.py"
        )
        == sha256_file(Path(__file__)),
        "ast_backend_hash_valid": code_hashes.get(
            "bvi_skill_evo/public_runtime_multilang_dual_v410.py"
        )
        == sha256_file(Path(ast_backend.__file__)),
        "hidden_evaluator_hash_frozen": code_hashes.get(
            "release_tools/paired_smoke_hidden_evaluator_v2.py"
        )
        == sha256_file(
            _workspace() / "release_tools" / "paired_smoke_hidden_evaluator_v2.py"
        ),
    }
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "failed_checks": [name for name, value in checks.items() if not value],
        "check_count": len(checks),
        "passed_check_count": sum(bool(value) for value in checks.values()),
        "case_count": 12,
        "planned_logical_model_calls": 60,
        "model_calls": 0,
        "behavioral_evaluator_loaded": False,
        "credential_persisted": False,
        "protocol_hash": protocol.get("protocol_hash"),
    }
    result["preflight_hash"] = canonical_json_hash(result)
    return result


def _valid_zero_call(experiment: Path) -> dict[str, Any]:
    preflight = read_json(experiment / "_audit" / "ZERO_CALL_PREFLIGHT.json")
    protocol = read_json(experiment / "stage" / "FROZEN_PROTOCOL.json")
    if (
        preflight.get("status") != "pass"
        or not _embedded_hash_valid(preflight, "preflight_hash")
        or preflight.get("protocol_hash") != protocol.get("protocol_hash")
        or not _embedded_hash_valid(protocol, "protocol_hash")
    ):
        raise ValueError("paired_smoke_v2_valid_zero_call_preflight_required")
    return protocol


def _structural_nodes(facts: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if facts is None:
        return {}
    return {str(row["node_id"]): row for row in facts.get("editable_nodes", [])}


def _structural_gate(
    application: dict[str, Any], facts: dict[str, Any] | None, view: str
) -> dict[str, Any]:
    if view != "structural-revision":
        return {"status": "not_applicable", "pass": True}
    decision = (facts or {}).get("localization_decision", {}).get("decision")
    code_edits = [
        row for row in application.get("edits", []) if row.get("path") != "SKILL.md"
    ]
    if decision == "ABSTAIN":
        passed = not code_edits
        return {
            "status": "pass" if passed else "fail",
            "pass": passed,
            "decision": decision,
            "reason": "abstained_packet_prohibits_executable_edits",
            "code_edit_count": len(code_edits),
        }
    matched = all(
        row.get("target_node_id")
        and (row.get("node_binding") or {}).get("status") == "matched"
        for row in code_edits
    )
    return {
        "status": "pass" if matched else "fail",
        "pass": matched,
        "decision": decision,
        "reason": "all_executable_edits_must_bind_to_selected_runtime_nodes",
        "code_edit_count": len(code_edits),
    }


def _credential_absent(root: Path, api_key: str) -> bool:
    needle = api_key.encode("utf-8")
    return all(needle not in path.read_bytes() for path in root.rglob("*") if path.is_file())


def _run_one(
    experiment: Path,
    call: dict[str, Any],
    output_root: Path,
    *,
    api_key: str,
    protocol: dict[str, Any],
) -> dict[str, Any]:
    trial_id = str(call["trial_id"])
    final = output_root / trial_id
    if final.exists():
        raise FileExistsError(final)
    temporary = output_root / f".{trial_id}.partial-{uuid.uuid4().hex}"
    temporary.mkdir(parents=True)
    prompt_path = experiment / call["prompt_path"]
    source = experiment / call["source_package_path"]
    if sha256_file(prompt_path) != call["prompt_sha256"]:
        raise ValueError(f"prompt_changed:{trial_id}")
    if _tree_hash(source) != call["source_package_tree_hash"]:
        raise ValueError(f"source_changed:{trial_id}")
    started = time.monotonic()
    response, attempts, request_payload = _call_patch_completion(
        api_key=api_key,
        base_url=str(protocol["base_url"]),
        model=str(protocol["model"]),
        system_prompt=(
            "You audit and repair one visible Agent Skill package. Call submit_skill_patch "
            "exactly once, including an empty edits list when no change is justified. Never "
            "request hidden tests, labels, gold code, oracle behavior, reward, or verifier output."
        ),
        user_prompt=prompt_path.read_text(encoding="utf-8"),
        timeout=int(protocol["timeout_seconds"]),
        max_attempts=int(protocol["max_transport_attempts_per_logical_call"]),
        max_completion_tokens=(
            MARKDOWN_MAX_COMPLETION_TOKENS
            if call["view"] in {"markdown-view", "markdown-revision"}
            else None
        ),
    )
    if response is not None:
        write_json(temporary / "SEALED_PROVIDER_RESPONSE.json", response)
    parse_error: str | None = None
    response_mode: str | None = None
    content = ""
    if response is not None:
        try:
            content, response_mode = _patch_arguments(response)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            parse_error = f"{type(exc).__name__}:{exc}"
    (temporary / "RAW_TOOL_ARGUMENTS.txt").write_text(content, encoding="utf-8")
    application: dict[str, Any] | None = None
    application_error = parse_error
    facts: dict[str, Any] | None = None
    facts_path_value = call.get("facts_path")
    if facts_path_value:
        facts_path = experiment / str(facts_path_value)
        if sha256_file(facts_path) != call.get("facts_sha256"):
            raise ValueError(f"facts_changed:{trial_id}")
        facts = read_json(facts_path)
        ast_backend.validate_public_multilang_contract_set(facts, source)
    if response is not None and parse_error is None:
        try:
            application = parse_and_apply_package_edits(
                content,
                source,
                temporary / "candidate" / "package",
                structural_nodes=_structural_nodes(facts),
            )
            view = str(call["view"])
            changed_paths = list(application.get("changed_paths") or [])
            if view in {"markdown-view", "markdown-revision"} and any(
                path != "SKILL.md" for path in changed_paths
            ):
                raise ValueError("markdown_view_modified_non_markdown_file")
            gate = _structural_gate(application, facts, view)
            application["structural_gate"] = gate
            if not gate["pass"]:
                raise ValueError(f"structural_gate_failed:{gate['reason']}")
            application["generic_checks"] = _generic_checks(
                temporary / "candidate" / "package", source
            )
            write_json(temporary / "RESPONSE_APPLICATION.json", application)
        except Exception as exc:
            application_error = f"{type(exc).__name__}:{exc}"
            application = None
            shutil.rmtree(temporary / "candidate", ignore_errors=True)
    status = (
        "provider_unavailable"
        if response is None
        else "invalid_response"
        if application is None
        else "candidate_frozen"
    )
    candidate = temporary / "candidate" / "package"
    freeze: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "trial_id": trial_id,
        "case_id": call["case_id"],
        "view": call["view"],
        "phase": call["phase"],
        "status": status,
        "application_error": application_error,
        "protocol_hash": protocol["protocol_hash"],
        "prompt_sha256": sha256_file(prompt_path),
        "source_package_tree_hash": _tree_hash(source),
        "candidate_tree_hash": _tree_hash(candidate) if candidate.is_dir() else None,
        "response_sha256": (
            sha256_file(temporary / "SEALED_PROVIDER_RESPONSE.json")
            if response is not None
            else None
        ),
        "tool_arguments_sha256": sha256_file(temporary / "RAW_TOOL_ARGUMENTS.txt"),
        "application_sha256": (
            sha256_file(temporary / "RESPONSE_APPLICATION.json")
            if application is not None
            else None
        ),
        "behavioral_evaluator_loaded": False,
        "hidden_artifacts_consumed": False,
        "credential_persisted": False,
    }
    freeze["candidate_freeze_hash"] = canonical_json_hash(freeze)
    write_json(temporary / "CANDIDATE_FREEZE.json", freeze)
    usage = (response or {}).get("usage") or {}
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "trial_id": trial_id,
        "case_id": call["case_id"],
        "view": call["view"],
        "phase": call["phase"],
        "status": status,
        "failure_class": (
            "transport" if response is None else "response_or_application" if application is None else "none"
        ),
        "application_error": application_error,
        "model": (response or {}).get("model"),
        "response_id": (response or {}).get("id"),
        "response_mode": response_mode,
        "attempts": attempts,
        "actual_transport_attempt_count": len(attempts),
        "request_payload_hash": canonical_json_hash(request_payload),
        "requested_max_completion_tokens": request_payload.get("max_completion_tokens"),
        "candidate_freeze_hash": freeze["candidate_freeze_hash"],
        "usage": {
            key: int(usage.get(key) or 0)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "behavioral_evaluator_loaded": False,
        "hidden_artifacts_consumed": False,
        "credential_persisted": False,
    }
    record["run_record_hash"] = canonical_json_hash(record)
    write_json(temporary / "RUN_RECORD.json", record)
    if not _credential_absent(temporary, api_key):
        raise RuntimeError("api_credential_persisted")
    os.replace(temporary, final)
    return record


def _run_calls(
    experiment_root: Path, *, phase: str, api_key: str
) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    protocol = _valid_zero_call(experiment)
    if phase == "primary":
        calls_path = experiment / "stage" / "PRIMARY_CALLS.json"
    elif phase == "revision":
        calls_path = experiment / "stage" / "revisions" / "REVISION_CALLS.json"
        plan = read_json(experiment / "stage" / "revisions" / "FROZEN_REVISION_PLAN.json")
        if not _embedded_hash_valid(plan, "revision_plan_hash"):
            raise ValueError("revision_plan_invalid")
    else:
        raise ValueError(phase)
    calls = read_json(calls_path)["calls"]
    output = experiment / "runs" / phase
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(calls))) as executor:
        futures = {
            executor.submit(
                _run_one,
                experiment,
                call,
                output,
                api_key=api_key,
                protocol=protocol,
            ): call
            for call in calls
        }
        for future in as_completed(futures):
            records.append(future.result())
    records.sort(key=lambda row: row["trial_id"])
    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "phase": phase,
        "status": "complete",
        "logical_call_count": len(records),
        "provider_response_count": sum(bool(row["response_id"]) for row in records),
        "exact_model_count": sum(row["model"] == MODEL for row in records),
        "candidate_frozen_count": sum(row["status"] == "candidate_frozen" for row in records),
        "status_counts": dict(sorted(Counter(row["status"] for row in records).items())),
        "view_counts": dict(sorted(Counter(row["view"] for row in records).items())),
        "actual_transport_attempt_count": sum(row["actual_transport_attempt_count"] for row in records),
        "usage": {
            key: sum(int((row.get("usage") or {}).get(key) or 0) for row in records)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "behavioral_evaluator_loaded": False,
        "hidden_artifacts_consumed": False,
        "credential_persisted": False,
    }
    summary["run_hash"] = canonical_json_hash(summary)
    write_json(output / "BATCH_RUN_SUMMARY.json", summary)
    return summary


def _run_record(experiment: Path, phase: str, trial_id: str) -> dict[str, Any]:
    return read_json(experiment / "runs" / phase / trial_id / "RUN_RECORD.json")


def _candidate_or_source(
    experiment: Path, phase: str, call: dict[str, Any]
) -> tuple[Path, str]:
    record = _run_record(experiment, phase, call["trial_id"])
    candidate = experiment / "runs" / phase / call["trial_id"] / "candidate" / "package"
    if record["status"] == "candidate_frozen" and candidate.is_dir():
        return candidate, "model_candidate"
    return experiment / call["source_package_path"], "public_fallback_after_invalid_candidate"


def freeze_primary_and_prepare_revisions(experiment_root: Path) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    protocol = _valid_zero_call(experiment)
    primary_summary = read_json(experiment / "runs" / "primary" / "BATCH_RUN_SUMMARY.json")
    if (
        not _embedded_hash_valid(primary_summary, "run_hash")
        or primary_summary.get("logical_call_count") != 24
        or primary_summary.get("provider_response_count") != 24
        or primary_summary.get("exact_model_count") != 24
    ):
        raise ValueError("primary_model_run_incomplete")
    calls = read_json(experiment / "stage" / "PRIMARY_CALLS.json")["calls"]
    by_case: dict[str, dict[str, dict[str, Any]]] = {}
    for call in calls:
        by_case.setdefault(call["case_id"], {})[call["view"]] = call
    selected_root = experiment / "selected_primary"
    if selected_root.exists():
        raise FileExistsError(selected_root)
    selected_rows: list[dict[str, Any]] = []
    for case_id, views in sorted(by_case.items()):
        for view in PRIMARY_VIEWS:
            call = views[view]
            source, origin = _candidate_or_source(experiment, "primary", call)
            destination = selected_root / case_id / view / "package"
            destination.parent.mkdir(parents=True, exist_ok=True)
            copy_tree_clean(source, destination)
            selected_rows.append(
                {
                    "case_id": case_id,
                    "view": view,
                    "trial_id": call["trial_id"],
                    "origin": origin,
                    "package_path": destination.relative_to(experiment).as_posix(),
                    "package_tree_hash": _tree_hash(destination),
                    "run_record_hash": _run_record(
                        experiment, "primary", call["trial_id"]
                    )["run_record_hash"],
                }
            )
    primary_selection = {
        "schema_version": SCHEMA_VERSION,
        "row_count": len(selected_rows),
        "rows": selected_rows,
        "hidden_artifacts_consumed": False,
        "behavioral_evaluator_loaded": False,
    }
    primary_selection["selection_hash"] = canonical_json_hash(primary_selection)
    write_json(selected_root / "PRIMARY_SELECTION.json", primary_selection)

    revision_root = experiment / "stage" / "revisions"
    if revision_root.exists():
        raise FileExistsError(revision_root)
    prompts_root = revision_root / "prompts"
    facts_root = revision_root / "facts"
    prompts_root.mkdir(parents=True)
    facts_root.mkdir(parents=True)
    calls_out: list[dict[str, Any]] = []
    for case_id in sorted(by_case):
        case_root = experiment / "stage" / "public" / "cases" / case_id
        source_paths = {
            "markdown-revision": selected_root / case_id / "markdown-view" / "package",
            "package-revision": selected_root / case_id / "package-view" / "package",
            "structural-revision": selected_root / case_id / "package-view" / "package",
        }
        facts = ast_backend.build_public_multilang_contract_set(
            source_paths["structural-revision"],
            (case_root / "REQUEST.md").read_text(encoding="utf-8"),
        )
        ast_backend.validate_public_multilang_contract_set(
            facts, source_paths["structural-revision"]
        )
        facts_path = facts_root / f"{case_id}.json"
        write_json(facts_path, facts)
        for view in REVISION_VIEWS:
            source = source_paths[view]
            parent_source = (
                experiment
                / "stage"
                / "public"
                / "cases"
                / case_id
                / "package"
            )
            checks = _generic_checks(source, parent_source)
            supplied_facts = facts if view == "structural-revision" else None
            prompt_path = prompts_root / f"{case_id}--{view}.txt"
            prompt_path.write_text(
                _revision_prompt(
                    case_root,
                    source,
                    checks,
                    view=view,
                    facts=supplied_facts,
                ),
                encoding="utf-8",
            )
            call: dict[str, Any] = {
                "trial_id": f"{case_id}--{view}",
                "case_id": case_id,
                "view": view,
                "phase": "revision",
                "prompt_path": prompt_path.relative_to(experiment).as_posix(),
                "prompt_sha256": sha256_file(prompt_path),
                "source_package_path": source.relative_to(experiment).as_posix(),
                "source_package_tree_hash": _tree_hash(source),
            }
            if supplied_facts is not None:
                call.update(
                    {
                        "facts_path": facts_path.relative_to(experiment).as_posix(),
                        "facts_sha256": sha256_file(facts_path),
                        "facts_hash": facts["facts_hash"],
                    }
                )
            calls_out.append(call)
    calls_out.sort(key=lambda row: row["trial_id"])
    write_json(revision_root / "REVISION_CALLS.json", {"calls": calls_out})
    decisions = Counter(
        read_json(path)["localization_decision"]["decision"]
        for path in facts_root.glob("*.json")
    )
    revision_plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "frozen_before_revision_model_calls",
        "protocol_hash": protocol["protocol_hash"],
        "primary_summary_sha256": sha256_file(
            experiment / "runs" / "primary" / "BATCH_RUN_SUMMARY.json"
        ),
        "primary_selection_sha256": sha256_file(
            selected_root / "PRIMARY_SELECTION.json"
        ),
        "revision_calls_sha256": sha256_file(revision_root / "REVISION_CALLS.json"),
        "revision_logical_call_count": len(calls_out),
        "view_counts": dict(sorted(Counter(row["view"] for row in calls_out).items())),
        "structural_decision_counts": dict(sorted(decisions.items())),
        "model_calls_so_far": 24,
        "behavioral_evaluator_loaded": False,
        "hidden_artifacts_consumed": False,
        "credential_persisted": False,
    }
    revision_plan["revision_plan_hash"] = canonical_json_hash(revision_plan)
    write_json(revision_root / "FROZEN_REVISION_PLAN.json", revision_plan)
    preflight = audit_revisions(experiment)
    write_json(experiment / "_audit" / "REVISION_ZERO_CALL_PREFLIGHT.json", preflight)
    if preflight["status"] != "pass":
        raise ValueError(f"revision_preflight_failed:{preflight['failed_checks']}")
    return preflight


def audit_revisions(experiment_root: Path) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    plan = read_json(experiment / "stage" / "revisions" / "FROZEN_REVISION_PLAN.json")
    calls = read_json(experiment / "stage" / "revisions" / "REVISION_CALLS.json")["calls"]
    prompt_hashes = all(
        sha256_file(experiment / call["prompt_path"]) == call["prompt_sha256"]
        for call in calls
    )
    source_hashes = all(
        _tree_hash(experiment / call["source_package_path"])
        == call["source_package_tree_hash"]
        for call in calls
    )
    facts_valid = True
    for call in calls:
        if call["view"] != "structural-revision":
            continue
        facts = read_json(experiment / call["facts_path"])
        try:
            ast_backend.validate_public_multilang_contract_set(
                facts, experiment / call["source_package_path"]
            )
        except ValueError:
            facts_valid = False
    checks = {
        "revision_plan_hash_valid": _embedded_hash_valid(plan, "revision_plan_hash"),
        "revision_call_count_exact": len(calls) == 36,
        "three_views_balanced": Counter(row["view"] for row in calls)
        == Counter({view: 12 for view in REVISION_VIEWS}),
        "trial_ids_unique": len({row["trial_id"] for row in calls}) == 36,
        "prompt_hashes_valid": prompt_hashes,
        "source_tree_hashes_valid": source_hashes,
        "runtime_facts_validate": facts_valid,
        "facts_only_on_structural_view": all(
            ("facts_path" in row) == (row["view"] == "structural-revision")
            for row in calls
        ),
        "primary_model_calls_exact": plan.get("model_calls_so_far") == 24,
        "hidden_not_loaded": plan.get("behavioral_evaluator_loaded") is False,
        "credential_not_persisted": plan.get("credential_persisted") is False,
    }
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "failed_checks": [name for name, value in checks.items() if not value],
        "check_count": len(checks),
        "passed_check_count": sum(bool(value) for value in checks.values()),
        "revision_logical_call_count": len(calls),
        "model_calls": 0,
        "behavioral_evaluator_loaded": False,
        "hidden_artifacts_consumed": False,
    }
    result["preflight_hash"] = canonical_json_hash(result)
    return result


def _select_revision_or_primary(
    experiment: Path,
    case_id: str,
    revision_view: str,
    primary_view: str,
) -> tuple[Path, str, str]:
    trial_id = f"{case_id}--{revision_view}"
    record = _run_record(experiment, "revision", trial_id)
    candidate = experiment / "runs" / "revision" / trial_id / "candidate" / "package"
    if record["status"] == "candidate_frozen" and candidate.is_dir():
        return candidate, "revision_candidate", record["run_record_hash"]
    primary = experiment / "selected_primary" / case_id / primary_view / "package"
    return primary, "primary_fallback_after_invalid_revision", record["run_record_hash"]


def freeze_final_candidates(experiment_root: Path) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    protocol = _valid_zero_call(experiment)
    revision_summary = read_json(experiment / "runs" / "revision" / "BATCH_RUN_SUMMARY.json")
    if (
        not _embedded_hash_valid(revision_summary, "run_hash")
        or revision_summary.get("logical_call_count") != 36
        or revision_summary.get("provider_response_count") != 36
        or revision_summary.get("exact_model_count") != 36
    ):
        raise ValueError("revision_model_run_incomplete")
    case_ids = sorted(
        path.name for path in (experiment / "stage" / "public" / "cases").iterdir()
    )
    selected = experiment / "selected"
    if selected.exists():
        raise FileExistsError(selected)
    rows: list[dict[str, Any]] = []
    mapping = {
        "md-only-self-evolution": ("markdown-revision", "markdown-view"),
        "raw-package-self-evolution": ("package-revision", "package-view"),
        "ast-package-self-evolution": ("structural-revision", "package-view"),
    }
    for case_id in case_ids:
        parent = experiment / "stage" / "public" / "cases" / case_id / "package"
        for condition in FINAL_CONDITIONS:
            run_record_hash: str | None = None
            if condition == "no-evolution":
                source = parent
                origin = "public_parent"
            else:
                revision_view, primary_view = mapping[condition]
                source, origin, run_record_hash = _select_revision_or_primary(
                    experiment, case_id, revision_view, primary_view
                )
            destination = selected / condition / case_id / "package"
            destination.parent.mkdir(parents=True, exist_ok=True)
            copy_tree_clean(source, destination)
            freeze: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "case_id": case_id,
                "condition": condition,
                "origin": origin,
                "protocol_hash": protocol["protocol_hash"],
                "parent_tree_hash": _tree_hash(parent),
                "candidate_tree_hash": _tree_hash(destination),
                "run_record_hash": run_record_hash,
                "behavioral_evaluator_loaded": False,
                "hidden_artifacts_consumed": False,
                "candidate_selection_uses_hidden": False,
                "credential_persisted": False,
            }
            freeze["final_candidate_freeze_hash"] = canonical_json_hash(freeze)
            write_json(destination.parent / "FINAL_CANDIDATE_FREEZE.json", freeze)
            rows.append(
                {
                    **freeze,
                    "package_path": destination.relative_to(experiment).as_posix(),
                }
            )
    selection: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "all_candidates_frozen_before_hidden_evaluation",
        "row_count": len(rows),
        "case_count": len(case_ids),
        "condition_counts": dict(sorted(Counter(row["condition"] for row in rows).items())),
        "rows": rows,
        "model_response_count": 60,
        "behavioral_evaluator_loaded": False,
        "hidden_artifacts_consumed": False,
        "candidate_selection_uses_hidden": False,
        "credential_persisted": False,
    }
    selection["selection_hash"] = canonical_json_hash(selection)
    write_json(selected / "CANDIDATE_SELECTION.json", selection)
    audit = prehidden_audit(experiment)
    write_json(experiment / "_audit" / "PREHIDDEN_AUDIT.json", audit)
    if audit["status"] != "pass":
        raise ValueError(f"prehidden_audit_failed:{audit['failed_checks']}")
    return audit


def prehidden_audit(experiment_root: Path) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    protocol = _valid_zero_call(experiment)
    selection = read_json(experiment / "selected" / "CANDIDATE_SELECTION.json")
    rows = selection.get("rows") or []
    freezes_valid = True
    tree_hashes_valid = True
    for row in rows:
        package = experiment / row["package_path"]
        freeze_path = package.parent / "FINAL_CANDIDATE_FREEZE.json"
        freeze = read_json(freeze_path)
        freezes_valid = freezes_valid and _embedded_hash_valid(
            freeze, "final_candidate_freeze_hash"
        )
        tree_hashes_valid = tree_hashes_valid and _tree_hash(package) == row[
            "candidate_tree_hash"
        ]
    text_paths = [
        path
        for path in experiment.rglob("*")
        if path.is_file() and path.stat().st_size <= 5_000_000
    ]
    high_entropy_hits = []
    for path in text_paths:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if HIGH_ENTROPY_SECRET.search(text):
            high_entropy_hits.append(path.relative_to(experiment).as_posix())
    primary = read_json(experiment / "runs" / "primary" / "BATCH_RUN_SUMMARY.json")
    revision = read_json(experiment / "runs" / "revision" / "BATCH_RUN_SUMMARY.json")
    checks = {
        "protocol_hash_valid": _embedded_hash_valid(protocol, "protocol_hash"),
        "selection_hash_valid": _embedded_hash_valid(selection, "selection_hash"),
        "candidate_rows_exact_48": len(rows) == 48,
        "case_count_exact_12": len({row["case_id"] for row in rows}) == 12,
        "condition_matrix_exact": Counter(row["condition"] for row in rows)
        == Counter({condition: 12 for condition in FINAL_CONDITIONS}),
        "candidate_identity_unique": len(
            {(row["case_id"], row["condition"]) for row in rows}
        )
        == 48,
        "final_freezes_valid": freezes_valid,
        "candidate_tree_hashes_valid": tree_hashes_valid,
        "primary_logical_calls_exact_24": primary.get("logical_call_count") == 24,
        "revision_logical_calls_exact_36": revision.get("logical_call_count") == 36,
        "provider_responses_exact_60": primary.get("provider_response_count") == 24
        and revision.get("provider_response_count") == 36,
        "provider_model_exact_60": primary.get("exact_model_count") == 24
        and revision.get("exact_model_count") == 36,
        "hidden_flags_false": all(
            row.get("behavioral_evaluator_loaded") is False
            and row.get("hidden_artifacts_consumed") is False
            for row in rows
        ),
        "selection_hidden_independent": selection.get("candidate_selection_uses_hidden")
        is False,
        "no_private_directory": not (experiment / "_private").exists(),
        "no_high_entropy_secret": not high_entropy_hits,
        "credential_not_persisted": selection.get("credential_persisted") is False,
    }
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "failed_checks": [name for name, value in checks.items() if not value],
        "check_count": len(checks),
        "passed_check_count": sum(bool(value) for value in checks.values()),
        "candidate_row_count": len(rows),
        "logical_model_response_count": 60,
        "high_entropy_secret_hits": high_entropy_hits,
        "behavioral_evaluator_loaded": False,
        "hidden_artifacts_consumed": False,
        "credential_persisted": False,
    }
    report["audit_hash"] = canonical_json_hash(report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--workspace-root", type=Path, default=_workspace())
    prepare.add_argument("--benchmark-root", type=Path, required=True)
    prepare.add_argument("--parent-protocol-root", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    audit = commands.add_parser("preflight")
    audit.add_argument("--experiment-root", type=Path, required=True)
    audit.add_argument("--benchmark-root", type=Path, required=True)
    audit.add_argument("--parent-protocol-root", type=Path, required=True)
    for name in ("run-primary", "run-revisions"):
        command = commands.add_parser(name)
        command.add_argument("--experiment-root", type=Path, required=True)
    freeze_primary = commands.add_parser("prepare-revisions")
    freeze_primary.add_argument("--experiment-root", type=Path, required=True)
    freeze_final = commands.add_parser("freeze-final")
    freeze_final.add_argument("--experiment-root", type=Path, required=True)
    prehidden = commands.add_parser("prehidden")
    prehidden.add_argument("--experiment-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "prepare":
        result = prepare_execution(
            args.workspace_root,
            args.benchmark_root,
            args.parent_protocol_root,
            args.output,
        )
    elif args.command == "preflight":
        result = audit_execution(
            args.experiment_root, args.benchmark_root, args.parent_protocol_root
        )
    elif args.command == "run-primary":
        api_key = getpass.getpass("OpenLux API key: ")
        result = _run_calls(args.experiment_root, phase="primary", api_key=api_key)
    elif args.command == "prepare-revisions":
        result = freeze_primary_and_prepare_revisions(args.experiment_root)
    elif args.command == "run-revisions":
        api_key = getpass.getpass("OpenLux API key: ")
        result = _run_calls(args.experiment_root, phase="revision", api_key=api_key)
    elif args.command == "freeze-final":
        result = freeze_final_candidates(args.experiment_root)
    else:
        result = prehidden_audit(args.experiment_root)
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
