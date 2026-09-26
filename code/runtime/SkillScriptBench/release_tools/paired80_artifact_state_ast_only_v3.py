from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable

from bvi_skill_evo import artifact_scope_ast_v1 as script_backend
from bvi_skill_evo.artifact_state_gate_v2 import (
    METHOD_ID,
    ROUTE_PRESERVE,
    build_artifact_state_report,
    choose_public_projection,
)
from release_tools import paired80_artifact_state_replay_v2 as replay
from release_tools import paired_smoke_execution_v2 as provider
from skillscriptbench.io_utils import (
    canonical_json_hash,
    copy_tree_clean,
    read_json,
    sha256_file,
    write_json,
)


SCHEMA_VERSION = "skillscriptbench-paired80-artifact-state-ast-only-v3"
CONDITION = "artifact-state-ast-v3"
MODEL = "gpt-5.5"
BASE_URL = "https://api.openlux.ai/v1"
WORKERS = 6
TIMEOUT_SECONDS = 600
MAX_TRANSPORT_ATTEMPTS = 2
EXPECTED_CASES = 80
EXPECTED_REPEATS = 3
EXPECTED_ROWS = EXPECTED_CASES * EXPECTED_REPEATS
EXPECTED_MODEL_CALLS = 180


def _tree_hash(path: Path) -> str:
    return provider._tree_hash(path.resolve())


def _embedded_hash_valid(payload: dict[str, Any], field: str) -> bool:
    body = dict(payload)
    expected = str(body.pop(field, ""))
    return bool(expected) and canonical_json_hash(body) == expected


def _code_paths() -> dict[str, Path]:
    workspace = Path(__file__).resolve().parents[1]
    return {
        "bvi_skill_evo/artifact_scope_ast_v1.py": Path(script_backend.__file__).resolve(),
        "bvi_skill_evo/artifact_state_gate_v2.py": workspace
        / "bvi_skill_evo"
        / "artifact_state_gate_v2.py",
        "release_tools/paired80_artifact_state_ast_only_v3.py": Path(__file__).resolve(),
        "release_tools/paired_smoke_execution_v2.py": Path(provider.__file__).resolve(),
        "skillscriptbench/package_matrix_conditions_v64.py": workspace
        / "skillscriptbench"
        / "package_matrix_conditions_v64.py",
    }


def _public_registry(benchmark: Path) -> list[dict[str, Any]]:
    rows = provider._jsonl_read(benchmark / "public" / "registry.jsonl")
    rows.sort(key=lambda row: str(row["case_id"]))
    if len(rows) != EXPECTED_CASES:
        raise ValueError(f"artifact_state_registry_not_80:{len(rows)}")
    return rows


def _smoke_case_ids(registry: list[dict[str, Any]]) -> set[str]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in registry:
        groups[str(row["request_sha256"])].append(row)
    selected: set[str] = set()
    for language in ("javascript", "shell"):
        group = next(
            (
                values
                for _, values in sorted(groups.items())
                if len(values) == 4
                and {str(row.get("language") or "") for row in values} == {language}
            ),
            None,
        )
        if group is None:
            raise ValueError(f"artifact_state_smoke_group_missing:{language}")
        selected.update(str(row["case_id"]) for row in group)
    if len(selected) != 8:
        raise ValueError(f"artifact_state_smoke_case_count_not_8:{len(selected)}")
    return selected


def _source_primary_rows(source: Path) -> dict[tuple[int, str], dict[str, Any]]:
    path = source / "selected_primary" / "PRIMARY_SELECTION.json"
    payload = read_json(path)
    if not _embedded_hash_valid(payload, "selection_hash"):
        raise ValueError("artifact_state_source_primary_selection_invalid")
    rows = {
        (int(row["repeat_index"]), str(row["case_id"])): row
        for row in payload["rows"]
        if row["view"] == "package-view"
    }
    if len(rows) != EXPECTED_ROWS:
        raise ValueError(f"artifact_state_source_primary_rows_not_240:{len(rows)}")
    return rows


def _revision_prompt(
    case_root: Path,
    source_package: Path,
    route_report: dict[str, Any],
    parent_facts: dict[str, Any],
    candidate_facts: dict[str, Any],
) -> str:
    request = (case_root / "REQUEST.md").read_text(encoding="utf-8")
    route = str(route_report["artifact_route"])
    allowed = list(route_report["allowed_paths"])
    return (
        "Revise one frozen executable Agent Skill proposal using only public structural evidence. "
        "The artifact-state route is already fixed from the public request and parent package. "
        "Do not edit outside ALLOWED_PATHS. Preserve all unrelated bytes and public behavior. "
        "Hidden tests, labels, mutation operators, oracle packages, rewards, and task-verifier "
        "feedback are unavailable.\n\n"
        f"ARTIFACT_ROUTE: {route}\n"
        + "ALLOWED_PATHS.json\n"
        + json.dumps(allowed, indent=2, ensure_ascii=True)
        + "\n\n"
        + provider._edit_protocol()
        + "\nFor executable edits, use only node_id values from CANDIDATE_PUBLIC_STRUCTURAL_FACTS. "
        "Documentation edits use an empty target_node_id. If the frozen proposal already "
        "satisfies an allowed axis, leave that axis unchanged.\n\nUSER_REQUEST.md\n"
        + request
        + "\n\nPUBLIC_ARTIFACT_STATE_REPORT.json\n"
        + json.dumps(route_report, indent=2, sort_keys=True, ensure_ascii=True)
        + "\n\nPARENT_PUBLIC_STRUCTURAL_FACTS.json\n"
        + json.dumps(parent_facts, indent=2, sort_keys=True, ensure_ascii=True)
        + "\n\nCANDIDATE_PUBLIC_STRUCTURAL_FACTS.json\n"
        + json.dumps(candidate_facts, indent=2, sort_keys=True, ensure_ascii=True)
        + "\n\nFROZEN_PROPOSAL_PACKAGE.json\n"
        + json.dumps(
            provider._package_payload(source_package, markdown_only=False),
            indent=2,
            ensure_ascii=True,
        )
        + "\n"
    )


def prepare_execution(
    source_experiment: Path, benchmark_root: Path, output_root: Path
) -> dict[str, Any]:
    source = source_experiment.resolve()
    benchmark = benchmark_root.resolve()
    output = output_root.resolve()
    if output.exists():
        raise FileExistsError(output)
    registry = _public_registry(benchmark)
    smoke_cases = _smoke_case_ids(registry)
    primary_rows = _source_primary_rows(source)
    source_primary_path = source / "selected_primary" / "PRIMARY_SELECTION.json"
    code_paths = _code_paths()
    if not all(path.is_file() for path in code_paths.values()):
        raise FileNotFoundError("artifact_state_ast_only_code_missing")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.building-", dir=output.parent))
    try:
        calls: list[dict[str, Any]] = []
        matrix: list[dict[str, Any]] = []
        for repeat_index in range(1, EXPECTED_REPEATS + 1):
            for registry_row in registry:
                case_id = str(registry_row["case_id"])
                source_case = benchmark / "public" / "cases" / case_id
                staged_case = temporary / "stage" / "public" / "cases" / case_id
                if repeat_index == 1:
                    provider._copy_case(source_case, staged_case)
                parent = staged_case / "package"
                source_row = primary_rows[(repeat_index, case_id)]
                source_primary = source / str(source_row["package_path"])
                if _tree_hash(source_primary) != source_row["package_tree_hash"]:
                    raise ValueError(f"artifact_state_source_primary_changed:{repeat_index}:{case_id}")
                staged_primary = (
                    temporary
                    / "stage"
                    / "source_primary"
                    / f"repeat-{repeat_index:02d}"
                    / case_id
                    / "package"
                )
                copy_tree_clean(source_primary, staged_primary)
                request_text = (staged_case / "REQUEST.md").read_text(encoding="utf-8")
                route_report = build_artifact_state_report(
                    parent, staged_primary, request_text
                )
                parent_facts = script_backend.build_public_multilang_contract_set(
                    parent, request_text
                )
                candidate_facts = script_backend.build_public_multilang_contract_set(
                    staged_primary, request_text
                )
                script_backend.validate_public_multilang_contract_set(parent_facts, parent)
                script_backend.validate_public_multilang_contract_set(
                    candidate_facts, staged_primary
                )
                evidence_root = (
                    temporary
                    / "stage"
                    / "evidence"
                    / f"repeat-{repeat_index:02d}"
                    / case_id
                )
                write_json(evidence_root / "ARTIFACT_STATE_REPORT.json", route_report)
                write_json(evidence_root / "PARENT_FACTS.json", parent_facts)
                write_json(evidence_root / "CANDIDATE_FACTS.json", candidate_facts)
                is_smoke = repeat_index == 1 and case_id in smoke_cases
                cohort = "smoke" if is_smoke else "formal"
                matrix_row: dict[str, Any] = {
                    "repeat_index": repeat_index,
                    "case_id": case_id,
                    "language": registry_row.get("language"),
                    "artifact_route": route_report["artifact_route"],
                    "allowed_paths": route_report["allowed_paths"],
                    "parent_package_path": parent.relative_to(temporary).as_posix(),
                    "parent_tree_hash": _tree_hash(parent),
                    "source_primary_path": staged_primary.relative_to(temporary).as_posix(),
                    "source_primary_tree_hash": _tree_hash(staged_primary),
                    "source_primary_run_record_hash": source_row["run_record_hash"],
                    "artifact_state_report_path": (
                        evidence_root / "ARTIFACT_STATE_REPORT.json"
                    ).relative_to(temporary).as_posix(),
                    "artifact_state_report_hash": route_report["report_hash"],
                    "candidate_facts_path": (
                        evidence_root / "CANDIDATE_FACTS.json"
                    ).relative_to(temporary).as_posix(),
                    "candidate_facts_hash": candidate_facts["facts_hash"],
                    "cohort": cohort,
                    "model_call_planned": route_report["artifact_route"]
                    != ROUTE_PRESERVE,
                }
                matrix.append(matrix_row)
                if route_report["artifact_route"] == ROUTE_PRESERVE:
                    continue
                trial_id = f"r{repeat_index:02d}--{case_id}--artifact-state-revision"
                prompt_path = (
                    temporary
                    / "stage"
                    / "prompts"
                    / cohort
                    / f"{trial_id}.txt"
                )
                prompt_path.parent.mkdir(parents=True, exist_ok=True)
                prompt_path.write_text(
                    _revision_prompt(
                        staged_case,
                        staged_primary,
                        route_report,
                        parent_facts,
                        candidate_facts,
                    ),
                    encoding="utf-8",
                )
                calls.append(
                    {
                        "trial_id": trial_id,
                        "case_id": case_id,
                        "repeat_index": repeat_index,
                        "view": "structural-revision",
                        "phase": "revision",
                        "cohort": cohort,
                        "artifact_route": route_report["artifact_route"],
                        "allowed_paths": route_report["allowed_paths"],
                        "prompt_path": prompt_path.relative_to(temporary).as_posix(),
                        "prompt_sha256": sha256_file(prompt_path),
                        "source_package_path": staged_primary.relative_to(temporary).as_posix(),
                        "source_package_tree_hash": _tree_hash(staged_primary),
                        "facts_path": (
                            evidence_root / "CANDIDATE_FACTS.json"
                        ).relative_to(temporary).as_posix(),
                        "facts_sha256": sha256_file(
                            evidence_root / "CANDIDATE_FACTS.json"
                        ),
                        "facts_hash": candidate_facts["facts_hash"],
                    }
                )

        calls.sort(key=lambda row: str(row["trial_id"]))
        matrix.sort(key=lambda row: (int(row["repeat_index"]), str(row["case_id"])))
        write_json(temporary / "stage" / "CASE_MATRIX.json", {"rows": matrix})
        write_json(temporary / "stage" / "REVISION_CALLS.json", {"calls": calls})
        smoke_count = sum(row["cohort"] == "smoke" for row in calls)
        formal_count = sum(row["cohort"] == "formal" for row in calls)
        if len(calls) != EXPECTED_MODEL_CALLS or smoke_count != 6 or formal_count != 174:
            raise ValueError(
                f"artifact_state_call_matrix_invalid:{len(calls)}:{smoke_count}:{formal_count}"
            )
        protocol: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": "frozen_before_model_calls",
            "scientific_role": "development_ast_only_model_time_validation",
            "condition": CONDITION,
            "method": METHOD_ID,
            "model": MODEL,
            "temperature": 0,
            "base_url": BASE_URL,
            "worker_count": WORKERS,
            "timeout_seconds": TIMEOUT_SECONDS,
            "max_transport_attempts_per_logical_call": MAX_TRANSPORT_ATTEMPTS,
            "case_count": EXPECTED_CASES,
            "repeat_count": EXPECTED_REPEATS,
            "final_candidate_rows": EXPECTED_ROWS,
            "planned_model_calls": EXPECTED_MODEL_CALLS,
            "smoke_model_calls": smoke_count,
            "formal_model_calls": formal_count,
            "clean_route_model_calls": 0,
            "source_experiment": source.name,
            "source_primary_selection_sha256": sha256_file(source_primary_path),
            "benchmark_public_registry_sha256": sha256_file(
                benchmark / "public" / "registry.jsonl"
            ),
            "case_matrix_sha256": sha256_file(temporary / "stage" / "CASE_MATRIX.json"),
            "revision_calls_sha256": sha256_file(
                temporary / "stage" / "REVISION_CALLS.json"
            ),
            "smoke_case_ids": sorted(smoke_cases),
            "hidden_outcomes_feed_back": False,
            "candidate_selection_uses_hidden": False,
            "model_calls": 0,
            "behavioral_evaluator_loaded": False,
            "hidden_artifacts_consumed": False,
            "credential_persisted": False,
            "code_sha256": {
                name: sha256_file(path) for name, path in sorted(code_paths.items())
            },
            "claim_boundary": (
                "This is a development rerun on Paired80. It isolates the new AST method by "
                "reusing frozen package-primary proposals and making no new baseline calls."
            ),
        }
        protocol["protocol_hash"] = canonical_json_hash(protocol)
        write_json(temporary / "stage" / "FROZEN_PROTOCOL.json", protocol)
        preflight = audit_execution(temporary, source, benchmark)
        write_json(temporary / "_audit" / "ZERO_CALL_PREFLIGHT.json", preflight)
        if preflight["status"] != "pass":
            raise ValueError(f"artifact_state_preflight_failed:{preflight['failed_checks']}")
        os.replace(temporary, output)
        return preflight
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def audit_execution(
    experiment_root: Path, source_experiment: Path, benchmark_root: Path
) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    source = source_experiment.resolve()
    benchmark = benchmark_root.resolve()
    protocol = read_json(experiment / "stage" / "FROZEN_PROTOCOL.json")
    calls = read_json(experiment / "stage" / "REVISION_CALLS.json")["calls"]
    matrix = read_json(experiment / "stage" / "CASE_MATRIX.json")["rows"]
    prompt_valid = True
    source_valid = True
    facts_valid = True
    route_valid = True
    for call in calls:
        prompt = experiment / str(call["prompt_path"])
        source_package = experiment / str(call["source_package_path"])
        facts_path = experiment / str(call["facts_path"])
        facts = read_json(facts_path)
        prompt_valid = prompt_valid and sha256_file(prompt) == call["prompt_sha256"]
        source_valid = source_valid and _tree_hash(source_package) == call[
            "source_package_tree_hash"
        ]
        facts_valid = facts_valid and sha256_file(facts_path) == call["facts_sha256"]
        try:
            script_backend.validate_public_multilang_contract_set(facts, source_package)
        except ValueError:
            facts_valid = False
        route_valid = route_valid and call["artifact_route"] != ROUTE_PRESERVE
    code_hashes = protocol.get("code_sha256") or {}
    visible_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (experiment / "stage").rglob("*")
        if path.is_file() and path.stat().st_size <= 2_000_000
    )
    checks = {
        "protocol_hash_valid": _embedded_hash_valid(protocol, "protocol_hash"),
        "source_primary_selection_unchanged": sha256_file(
            source / "selected_primary" / "PRIMARY_SELECTION.json"
        )
        == protocol["source_primary_selection_sha256"],
        "benchmark_registry_unchanged": sha256_file(
            benchmark / "public" / "registry.jsonl"
        )
        == protocol["benchmark_public_registry_sha256"],
        "matrix_sha256_valid": sha256_file(experiment / "stage" / "CASE_MATRIX.json")
        == protocol["case_matrix_sha256"],
        "calls_sha256_valid": sha256_file(
            experiment / "stage" / "REVISION_CALLS.json"
        )
        == protocol["revision_calls_sha256"],
        "case_matrix_240": len(matrix) == EXPECTED_ROWS,
        "call_count_180": len(calls) == EXPECTED_MODEL_CALLS,
        "smoke_calls_6": sum(row["cohort"] == "smoke" for row in calls) == 6,
        "formal_calls_174": sum(row["cohort"] == "formal" for row in calls) == 174,
        "trial_ids_unique": len({row["trial_id"] for row in calls})
        == EXPECTED_MODEL_CALLS,
        "prompt_hashes_valid": prompt_valid,
        "source_tree_hashes_valid": source_valid,
        "candidate_facts_valid": facts_valid,
        "no_preserve_route_model_call": route_valid,
        "model_exact_gpt55": protocol.get("model") == MODEL,
        "workers_exact_six": protocol.get("worker_count") == WORKERS,
        "code_hashes_current": code_hashes
        == {name: sha256_file(path) for name, path in sorted(_code_paths().items())},
        "model_calls_zero": protocol.get("model_calls") == 0,
        "hidden_not_loaded": protocol.get("behavioral_evaluator_loaded") is False
        and protocol.get("hidden_artifacts_consumed") is False,
        "no_private_directory": not (experiment / "_private").exists(),
        "no_high_entropy_secret": provider.HIGH_ENTROPY_SECRET.search(visible_text) is None,
        "credential_not_persisted": protocol.get("credential_persisted") is False,
    }
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "failed_checks": [name for name, value in checks.items() if not value],
        "check_count": len(checks),
        "passed_check_count": sum(bool(value) for value in checks.values()),
        "route_counts": dict(sorted(Counter(row["artifact_route"] for row in matrix).items())),
        "planned_model_calls": len(calls),
        "model_calls": 0,
        "behavioral_evaluator_loaded": False,
        "hidden_artifacts_consumed": False,
        "credential_persisted": False,
        "protocol_hash": protocol.get("protocol_hash"),
    }
    result["preflight_hash"] = canonical_json_hash(result)
    return result


def _valid_preflight(experiment: Path) -> dict[str, Any]:
    protocol = read_json(experiment / "stage" / "FROZEN_PROTOCOL.json")
    preflight = read_json(experiment / "_audit" / "ZERO_CALL_PREFLIGHT.json")
    if (
        preflight.get("status") != "pass"
        or not _embedded_hash_valid(preflight, "preflight_hash")
        or not _embedded_hash_valid(protocol, "protocol_hash")
        or preflight.get("protocol_hash") != protocol.get("protocol_hash")
    ):
        raise ValueError("artifact_state_valid_preflight_required")
    return protocol


def run_calls(experiment_root: Path, *, cohort: str, api_key: str) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    protocol = _valid_preflight(experiment)
    if cohort == "formal":
        authorization = read_json(experiment / "stage" / "FORMAL_AUTHORIZATION.json")
        if not _embedded_hash_valid(authorization, "authorization_hash"):
            raise ValueError("artifact_state_formal_authorization_invalid")
    calls = [
        row
        for row in read_json(experiment / "stage" / "REVISION_CALLS.json")["calls"]
        if row["cohort"] == cohort
    ]
    expected = int(protocol[f"{cohort}_model_calls"])
    if len(calls) != expected:
        raise ValueError(f"artifact_state_{cohort}_call_count_mismatch")
    output = experiment / "runs" / cohort
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    records: list[dict[str, Any]] = []
    original_backend = provider.ast_backend
    provider.ast_backend = script_backend
    try:
        with ThreadPoolExecutor(max_workers=min(WORKERS, len(calls))) as executor:
            futures = {
                executor.submit(
                    provider._run_one,
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
    finally:
        provider.ast_backend = original_backend
    records.sort(key=lambda row: str(row["trial_id"]))
    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete",
        "cohort": cohort,
        "logical_call_count": len(records),
        "provider_response_count": sum(bool(row["response_id"]) for row in records),
        "exact_model_count": sum(row["model"] == MODEL for row in records),
        "candidate_frozen_count": sum(row["status"] == "candidate_frozen" for row in records),
        "status_counts": dict(sorted(Counter(row["status"] for row in records).items())),
        "actual_transport_attempt_count": sum(
            row["actual_transport_attempt_count"] for row in records
        ),
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


def authorize_formal(experiment_root: Path) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    protocol = _valid_preflight(experiment)
    smoke_path = experiment / "runs" / "smoke" / "BATCH_RUN_SUMMARY.json"
    smoke = read_json(smoke_path)
    checks = {
        "smoke_hash_valid": _embedded_hash_valid(smoke, "run_hash"),
        "smoke_calls_exact": smoke.get("logical_call_count")
        == protocol.get("smoke_model_calls"),
        "smoke_provider_responses_exact": smoke.get("provider_response_count")
        == protocol.get("smoke_model_calls"),
        "smoke_model_exact": smoke.get("exact_model_count")
        == protocol.get("smoke_model_calls"),
        "hidden_not_loaded": smoke.get("behavioral_evaluator_loaded") is False
        and smoke.get("hidden_artifacts_consumed") is False,
        "formal_not_started": not (experiment / "runs" / "formal").exists(),
    }
    authorization: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "authorized" if all(checks.values()) else "rejected",
        "checks": checks,
        "failed_checks": [name for name, value in checks.items() if not value],
        "protocol_hash": protocol["protocol_hash"],
        "smoke_summary_sha256": sha256_file(smoke_path),
        "smoke_run_hash": smoke.get("run_hash"),
        "formal_model_calls": protocol["formal_model_calls"],
        "behavioral_evaluator_loaded": False,
        "hidden_artifacts_consumed": False,
    }
    authorization["authorization_hash"] = canonical_json_hash(authorization)
    if authorization["status"] != "authorized":
        raise ValueError(
            f"artifact_state_formal_authorization_rejected:{authorization['failed_checks']}"
        )
    write_json(experiment / "stage" / "FORMAL_AUTHORIZATION.json", authorization)
    return authorization


def _complete_summary(experiment: Path, cohort: str, expected: int) -> dict[str, Any]:
    summary = read_json(experiment / "runs" / cohort / "BATCH_RUN_SUMMARY.json")
    if (
        not _embedded_hash_valid(summary, "run_hash")
        or summary.get("logical_call_count") != expected
        or summary.get("provider_response_count") != expected
        or summary.get("exact_model_count") != expected
    ):
        raise ValueError(f"artifact_state_{cohort}_run_incomplete")
    return summary


def _call_by_key(experiment: Path) -> dict[tuple[int, str], dict[str, Any]]:
    return {
        (int(row["repeat_index"]), str(row["case_id"])): row
        for row in read_json(experiment / "stage" / "REVISION_CALLS.json")["calls"]
    }


def freeze_final_candidates(experiment_root: Path) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    protocol = _valid_preflight(experiment)
    _complete_summary(experiment, "smoke", int(protocol["smoke_model_calls"]))
    _complete_summary(experiment, "formal", int(protocol["formal_model_calls"]))
    matrix = read_json(experiment / "stage" / "CASE_MATRIX.json")["rows"]
    calls = _call_by_key(experiment)
    selected_root = experiment / "selected"
    if selected_root.exists():
        raise FileExistsError(selected_root)
    rows: list[dict[str, Any]] = []
    for matrix_row in matrix:
        repeat_index = int(matrix_row["repeat_index"])
        case_id = str(matrix_row["case_id"])
        parent = experiment / str(matrix_row["parent_package_path"])
        primary = experiment / str(matrix_row["source_primary_path"])
        request_text = (
            experiment / "stage" / "public" / "cases" / case_id / "REQUEST.md"
        ).read_text(encoding="utf-8")
        run_record_hash: str | None = None
        if matrix_row["artifact_route"] == ROUTE_PRESERVE:
            candidates: Iterable[tuple[str, Path]] = (("public-parent", parent),)
            origin = "public_parent_abstain"
        else:
            call = calls[(repeat_index, case_id)]
            cohort = str(call["cohort"])
            trial = experiment / "runs" / cohort / str(call["trial_id"])
            record = read_json(trial / "RUN_RECORD.json")
            run_record_hash = record["run_record_hash"]
            model_candidate = trial / "candidate" / "package"
            if record["status"] == "candidate_frozen" and model_candidate.is_dir():
                candidates = (
                    ("artifact-state-revision", model_candidate),
                    ("frozen-package-primary", primary),
                )
                origin = "public_projection_after_model_revision"
            else:
                candidates = (("frozen-package-primary", primary),)
                origin = "public_projection_after_invalid_revision"
        destination_root = (
            selected_root
            / f"repeat-{repeat_index:02d}"
            / CONDITION
            / case_id
        )
        package = destination_root / "package"
        projection = choose_public_projection(
            parent, candidates, package, request_text
        )
        freeze: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "repeat_index": repeat_index,
            "case_id": case_id,
            "condition": CONDITION,
            "method": METHOD_ID,
            "origin": origin,
            "artifact_route": projection["selected_route"],
            "selected_source": projection["selected_source"],
            "projection_selection_hash": projection["selection_hash"],
            "protocol_hash": protocol["protocol_hash"],
            "parent_tree_hash": _tree_hash(parent),
            "source_primary_tree_hash": _tree_hash(primary),
            "candidate_tree_hash": _tree_hash(package),
            "run_record_hash": run_record_hash,
            "package_path": package.relative_to(experiment).as_posix(),
            "behavioral_evaluator_loaded": False,
            "hidden_artifacts_consumed": False,
            "candidate_selection_uses_hidden": False,
            "credential_persisted": False,
        }
        freeze["final_candidate_freeze_hash"] = canonical_json_hash(freeze)
        write_json(destination_root / "ARTIFACT_STATE_SELECTION.json", projection)
        write_json(destination_root / "FINAL_CANDIDATE_FREEZE.json", freeze)
        rows.append(freeze)
    rows.sort(key=lambda row: (int(row["repeat_index"]), str(row["case_id"])))
    selection: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "all_ast_candidates_frozen_before_hidden_evaluation",
        "condition": CONDITION,
        "method": METHOD_ID,
        "row_count": len(rows),
        "case_count": EXPECTED_CASES,
        "repeat_count": EXPECTED_REPEATS,
        "rows": rows,
        "model_response_count": EXPECTED_MODEL_CALLS,
        "behavioral_evaluator_loaded": False,
        "hidden_artifacts_consumed": False,
        "candidate_selection_uses_hidden": False,
        "credential_persisted": False,
    }
    selection["selection_hash"] = canonical_json_hash(selection)
    write_json(selected_root / "CANDIDATE_SELECTION.json", selection)
    audit = prehidden_audit(experiment)
    write_json(experiment / "_audit" / "PREHIDDEN_AUDIT.json", audit)
    if audit["status"] != "pass":
        raise ValueError(f"artifact_state_prehidden_failed:{audit['failed_checks']}")
    return audit


def prehidden_audit(experiment_root: Path) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    protocol = _valid_preflight(experiment)
    selection = read_json(experiment / "selected" / "CANDIDATE_SELECTION.json")
    rows = list(selection.get("rows") or [])
    trees_valid = True
    freezes_valid = True
    for row in rows:
        package = experiment / str(row["package_path"])
        freeze = read_json(package.parent / "FINAL_CANDIDATE_FREEZE.json")
        trees_valid = trees_valid and _tree_hash(package) == row["candidate_tree_hash"]
        freezes_valid = freezes_valid and _embedded_hash_valid(
            freeze, "final_candidate_freeze_hash"
        )
    smoke = _complete_summary(experiment, "smoke", int(protocol["smoke_model_calls"]))
    formal = _complete_summary(experiment, "formal", int(protocol["formal_model_calls"]))
    visible_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in experiment.rglob("*")
        if path.is_file() and path.stat().st_size <= 2_000_000
    )
    checks = {
        "selection_hash_valid": _embedded_hash_valid(selection, "selection_hash"),
        "candidate_rows_240": len(rows) == EXPECTED_ROWS,
        "candidate_identity_unique": len(
            {(row["repeat_index"], row["case_id"], row["condition"]) for row in rows}
        )
        == EXPECTED_ROWS,
        "candidate_trees_valid": trees_valid,
        "candidate_freezes_valid": freezes_valid,
        "condition_exact": set(row["condition"] for row in rows) == {CONDITION},
        "smoke_complete": smoke["provider_response_count"] == protocol["smoke_model_calls"],
        "formal_complete": formal["provider_response_count"] == protocol["formal_model_calls"],
        "all_models_exact": smoke["exact_model_count"] + formal["exact_model_count"]
        == EXPECTED_MODEL_CALLS,
        "hidden_flags_false": selection.get("behavioral_evaluator_loaded") is False
        and selection.get("hidden_artifacts_consumed") is False,
        "selection_hidden_independent": selection.get("candidate_selection_uses_hidden")
        is False,
        "no_private_directory": not (experiment / "_private").exists(),
        "no_high_entropy_secret": provider.HIGH_ENTROPY_SECRET.search(visible_text) is None,
        "credential_not_persisted": selection.get("credential_persisted") is False,
    }
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "failed_checks": [name for name, value in checks.items() if not value],
        "check_count": len(checks),
        "passed_check_count": sum(bool(value) for value in checks.values()),
        "candidate_row_count": len(rows),
        "route_counts": dict(sorted(Counter(row["artifact_route"] for row in rows).items())),
        "selected_source_counts": dict(
            sorted(Counter(row["selected_source"] for row in rows).items())
        ),
        "model_response_count": EXPECTED_MODEL_CALLS,
        "behavioral_evaluator_loaded": False,
        "hidden_artifacts_consumed": False,
        "credential_persisted": False,
    }
    result["audit_hash"] = canonical_json_hash(result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--source-experiment", type=Path, required=True)
    prepare.add_argument("--benchmark-root", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    audit = commands.add_parser("preflight")
    audit.add_argument("--experiment-root", type=Path, required=True)
    audit.add_argument("--source-experiment", type=Path, required=True)
    audit.add_argument("--benchmark-root", type=Path, required=True)
    for name in ("run-smoke", "run-formal"):
        command = commands.add_parser(name)
        command.add_argument("--experiment-root", type=Path, required=True)
    authorize = commands.add_parser("authorize-formal")
    authorize.add_argument("--experiment-root", type=Path, required=True)
    freeze = commands.add_parser("freeze-final")
    freeze.add_argument("--experiment-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "prepare":
        result = prepare_execution(
            args.source_experiment, args.benchmark_root, args.output
        )
    elif args.command == "preflight":
        result = audit_execution(
            args.experiment_root, args.source_experiment, args.benchmark_root
        )
    elif args.command == "run-smoke":
        result = run_calls(
            args.experiment_root,
            cohort="smoke",
            api_key=getpass.getpass("OpenLux API key: "),
        )
    elif args.command == "authorize-formal":
        result = authorize_formal(args.experiment_root)
    elif args.command == "run-formal":
        result = run_calls(
            args.experiment_root,
            cohort="formal",
            api_key=getpass.getpass("OpenLux API key: "),
        )
    else:
        result = freeze_final_candidates(args.experiment_root)
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
