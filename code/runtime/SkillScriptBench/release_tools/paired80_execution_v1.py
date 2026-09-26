from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from bvi_skill_evo import artifact_scope_ast_v1 as ast_backend
from release_tools import paired_smoke_execution_v2 as base
from release_tools.artifact_state_release import _canonical_embedded_hash_valid
from release_tools.paired_smoke_execution_v3 import _normalized_apply
from skillscriptbench.io_utils import (
    canonical_json_hash,
    copy_tree_clean,
    read_json,
    sha256_file,
    write_json,
)


SCHEMA_VERSION = "skillscriptbench-paired80-execution-v1"
PROTOCOL_VERSION = "paired80_artifact_scope_v1"
MODEL = "gpt-5.5"
BASE_URL = "https://api.openlux.ai/v1"
WORKERS = 6
TIMEOUT = 600
MAX_ATTEMPTS = 4
PRIMARY_VIEWS = base.PRIMARY_VIEWS
REVISION_VIEWS = base.REVISION_VIEWS
FINAL_CONDITIONS = base.FINAL_CONDITIONS
MODE_CONFIG = {
    "smoke": {"base_count": 2, "case_count": 8, "repeat_count": 1},
    "formal": {"base_count": 20, "case_count": 80, "repeat_count": 3},
}


def _workspace() -> Path:
    return Path(__file__).resolve().parents[1]


def _tree_hash(root: Path) -> str:
    return base._tree_hash(root)


def _embedded_hash_valid(payload: dict[str, Any], field: str) -> bool:
    return base._embedded_hash_valid(payload, field)


def _registry_rows(benchmark_root: Path) -> list[dict[str, Any]]:
    rows = base._jsonl_read(benchmark_root / "public" / "registry.jsonl")
    rows.sort(key=lambda row: str(row["case_id"]))
    return rows


def _select_public_rows(benchmark_root: Path, mode: str) -> list[dict[str, Any]]:
    rows = _registry_rows(benchmark_root)
    if mode == "formal":
        if len(rows) != 80:
            raise ValueError(f"paired80_public_case_count_not_80:{len(rows)}")
        return rows
    if mode != "smoke":
        raise ValueError(f"unknown_mode:{mode}")
    by_request: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_request[str(row["request_sha256"])].append(row)
    groups = [
        sorted(group, key=lambda row: str(row["case_id"]))
        for _, group in sorted(by_request.items())
        if len(group) == 4
    ]
    selected: list[dict[str, Any]] = []
    for language in ("javascript", "shell"):
        group = next(
            (
                values
                for values in groups
                if {str(row.get("language") or "") for row in values} == {language}
                and not ({str(row["case_id"]) for row in values} & {str(row["case_id"]) for row in selected})
            ),
            None,
        )
        if group is None:
            raise ValueError(f"paired80_smoke_language_group_missing:{language}")
        selected.extend(group)
    selected.sort(key=lambda row: str(row["case_id"]))
    if len(selected) != 8:
        raise ValueError(f"paired80_smoke_case_count_not_8:{len(selected)}")
    return selected


def _code_paths() -> dict[str, Path]:
    workspace = _workspace()
    return {
        "release_tools/paired80_execution_v1.py": Path(__file__),
        "release_tools/paired80_hidden_evaluator_v1.py": workspace
        / "release_tools"
        / "paired80_hidden_evaluator_v1.py",
        "release_tools/paired_smoke_execution_v2.py": Path(base.__file__),
        "release_tools/paired_smoke_execution_v3.py": workspace
        / "release_tools"
        / "paired_smoke_execution_v3.py",
        "skillscriptbench/package_matrix_conditions_v64.py": workspace
        / "skillscriptbench"
        / "package_matrix_conditions_v64.py",
        "bvi_skill_evo/artifact_scope_ast_v1.py": Path(ast_backend.__file__),
    }


def _validate_smoke_analysis(path: Path) -> dict[str, Any]:
    payload = read_json(path.resolve())
    if (
        payload.get("status") != "complete"
        or payload.get("scientific_role") != "paired80_pipeline_smoke"
        or payload.get("case_count") != 8
        or not _embedded_hash_valid(payload, "analysis_hash")
    ):
        raise ValueError("paired80_valid_smoke_analysis_required")
    return payload


def prepare_execution(
    benchmark_root: Path,
    output_root: Path,
    *,
    mode: str,
    smoke_analysis_path: Path | None = None,
) -> dict[str, Any]:
    benchmark = benchmark_root.resolve()
    output = output_root.resolve()
    if output.exists():
        raise FileExistsError(output)
    config = MODE_CONFIG[mode]
    benchmark_preflight = read_json(benchmark / "_audit" / "ZERO_MODEL_PREFLIGHT.json")
    if benchmark_preflight.get("status") != "pass" or not _canonical_embedded_hash_valid(
        benchmark_preflight, "preflight_hash"
    ):
        raise ValueError("paired80_benchmark_preflight_invalid")
    smoke_authorization = None
    if mode == "formal":
        if smoke_analysis_path is None:
            raise ValueError("paired80_formal_requires_smoke_analysis")
        smoke = _validate_smoke_analysis(smoke_analysis_path)
        smoke_authorization = {
            "analysis_path_name": smoke_analysis_path.name,
            "analysis_sha256": sha256_file(smoke_analysis_path.resolve()),
            "analysis_hash": smoke["analysis_hash"],
        }

    selected_rows = _select_public_rows(benchmark, mode)
    if len(selected_rows) != config["case_count"]:
        raise ValueError("paired80_selected_case_count_mismatch")
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.building-", dir=output.parent))
    try:
        stage = temporary / "stage"
        public_cases = stage / "public" / "cases"
        prompts = stage / "prompts" / "primary"
        audit_root = temporary / "_audit"
        public_cases.mkdir(parents=True)
        prompts.mkdir(parents=True)
        audit_root.mkdir(parents=True)
        calls: list[dict[str, Any]] = []
        case_rows: list[dict[str, Any]] = []
        for row in selected_rows:
            case_id = str(row["case_id"])
            source_case = benchmark / "public" / "cases" / case_id
            staged_case = public_cases / case_id
            base._copy_case(source_case, staged_case)
            case_rows.append(
                {
                    "case_id": case_id,
                    "language": row.get("language"),
                    "request_sha256": row["request_sha256"],
                    "source_package_tree_hash": _tree_hash(staged_case / "package"),
                }
            )
            for repeat_index in range(1, int(config["repeat_count"]) + 1):
                for view, markdown_only in (("markdown-view", True), ("package-view", False)):
                    trial_id = f"r{repeat_index:02d}--{case_id}--{view}--primary"
                    prompt_path = prompts / f"{trial_id}.txt"
                    prompt_path.write_text(
                        base._primary_prompt(staged_case, markdown_only=markdown_only),
                        encoding="utf-8",
                    )
                    calls.append(
                        {
                            "trial_id": trial_id,
                            "case_id": case_id,
                            "repeat_index": repeat_index,
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
        calls.sort(key=lambda row: str(row["trial_id"]))
        case_rows.sort(key=lambda row: str(row["case_id"]))
        write_json(stage / "CASE_MATRIX.json", {"rows": case_rows})
        write_json(stage / "PRIMARY_CALLS.json", {"calls": calls})
        primary_count = len(calls)
        revision_count = int(config["case_count"]) * int(config["repeat_count"]) * 3
        code_paths = _code_paths()
        if not all(path.is_file() for path in code_paths.values()):
            missing = [name for name, path in code_paths.items() if not path.is_file()]
            raise FileNotFoundError(f"paired80_protocol_code_missing:{missing}")
        protocol: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "status": "frozen_before_primary_model_calls",
            "scientific_role": (
                "paired80_pipeline_smoke" if mode == "smoke" else "paired80_formal_main_experiment"
            ),
            "mode": mode,
            "model": MODEL,
            "temperature": 0,
            "base_url": BASE_URL,
            "worker_count": WORKERS,
            "timeout_seconds": TIMEOUT,
            "max_transport_attempts_per_logical_call": MAX_ATTEMPTS,
            "base_count": config["base_count"],
            "case_count": config["case_count"],
            "repeat_count": config["repeat_count"],
            "primary_logical_calls": primary_count,
            "revision_logical_calls": revision_count,
            "total_logical_calls": primary_count + revision_count,
            "final_candidate_rows": int(config["case_count"])
            * int(config["repeat_count"])
            * len(FINAL_CONDITIONS),
            "conditions": list(FINAL_CONDITIONS),
            "shared_package_primary": True,
            "raw_ast_share_frozen_primary_candidate": True,
            "calls_per_condition_per_case_repeat": 2,
            "no_evolution_model_calls": 0,
            "hidden_outcomes_feed_back": False,
            "candidate_selection_uses_hidden": False,
            "runtime_ast_backend": ast_backend.METHOD_ID,
            "benchmark_preflight_hash": benchmark_preflight["preflight_hash"],
            "benchmark_public_registry_sha256": sha256_file(
                benchmark / "public" / "registry.jsonl"
            ),
            "case_matrix_sha256": sha256_file(stage / "CASE_MATRIX.json"),
            "primary_calls_sha256": sha256_file(stage / "PRIMARY_CALLS.json"),
            "smoke_authorization": smoke_authorization,
            "transport_schema_normalization": {
                "target_node_id_missing": "empty_string",
                "start_line_missing_with_end_line": "copy_end_line",
                "end_line_missing_with_start_line": "copy_start_line",
                "semantic_fields_inferred": False,
            },
            "code_sha256": {
                name: sha256_file(path) for name, path in sorted(code_paths.items())
            },
            "model_calls": 0,
            "behavioral_evaluator_loaded": False,
            "credential_persisted": False,
        }
        protocol["protocol_hash"] = canonical_json_hash(protocol)
        write_json(stage / "FROZEN_PROTOCOL.json", protocol)
        report = audit_execution(temporary, benchmark)
        write_json(audit_root / "ZERO_CALL_PREFLIGHT.json", report)
        if report["status"] != "pass":
            raise ValueError(f"paired80_zero_call_failed:{report['failed_checks']}")
        os.replace(temporary, output)
        return report
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def audit_execution(experiment_root: Path, benchmark_root: Path) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    benchmark = benchmark_root.resolve()
    protocol = read_json(experiment / "stage" / "FROZEN_PROTOCOL.json")
    calls = read_json(experiment / "stage" / "PRIMARY_CALLS.json")["calls"]
    matrix = read_json(experiment / "stage" / "CASE_MATRIX.json")["rows"]
    config = MODE_CONFIG[str(protocol["mode"])]
    expected_primary = int(config["case_count"]) * int(config["repeat_count"]) * 2
    prompt_exact = True
    source_exact = True
    for call in calls:
        case_root = experiment / "stage" / "public" / "cases" / str(call["case_id"])
        expected_prompt = base._primary_prompt(
            case_root, markdown_only=call["view"] == "markdown-view"
        )
        prompt_path = experiment / str(call["prompt_path"])
        prompt_exact = prompt_exact and prompt_path.read_text(encoding="utf-8") == expected_prompt
        prompt_exact = prompt_exact and sha256_file(prompt_path) == call["prompt_sha256"]
        source_exact = source_exact and _tree_hash(
            experiment / str(call["source_package_path"])
        ) == call["source_package_tree_hash"]

    ast_valid = True
    ast_decisions: Counter[str] = Counter()
    for row in matrix:
        case = experiment / "stage" / "public" / "cases" / str(row["case_id"])
        try:
            facts = ast_backend.build_public_multilang_contract_set(
                case / "package", (case / "REQUEST.md").read_text(encoding="utf-8")
            )
            ast_backend.validate_public_multilang_contract_set(facts, case / "package")
            ast_valid = ast_valid and all(
                facts.get(field) is False
                for field in (
                    "benchmark_bundled_facts_consumed",
                    "hidden_artifacts_consumed",
                    "task_verifier_consumed",
                    "gold_or_oracle_consumed",
                    "reward_consumed",
                )
            )
            ast_decisions[str(facts["localization_decision"]["decision"])] += 1
        except Exception:
            ast_valid = False

    stage_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (experiment / "stage").rglob("*")
        if path.is_file() and path.stat().st_size <= 2_000_000
    )
    code_hashes = protocol.get("code_sha256") or {}
    checks = {
        "protocol_hash_valid": _embedded_hash_valid(protocol, "protocol_hash"),
        "benchmark_preflight_valid": (
            lambda payload: payload.get("status") == "pass"
            and _canonical_embedded_hash_valid(payload, "preflight_hash")
            and payload.get("preflight_hash") == protocol.get("benchmark_preflight_hash")
        )(read_json(benchmark / "_audit" / "ZERO_MODEL_PREFLIGHT.json")),
        "case_count_exact": len(matrix) == int(config["case_count"]),
        "primary_call_count_exact": len(calls) == expected_primary,
        "two_primary_views_balanced": Counter(call["view"] for call in calls)
        == Counter({view: expected_primary // 2 for view in PRIMARY_VIEWS}),
        "repeat_indices_exact": {int(call["repeat_index"]) for call in calls}
        == set(range(1, int(config["repeat_count"]) + 1)),
        "trial_ids_unique": len({call["trial_id"] for call in calls}) == len(calls),
        "prompt_content_and_hashes_exact": prompt_exact,
        "source_tree_hashes_exact": source_exact,
        "public_runtime_ast_valid": ast_valid,
        "formal_parent_ast_balance_40_40": protocol["mode"] != "formal"
        or ast_decisions == Counter({"ABSTAIN": 40, "PROPOSE": 40}),
        "model_exact_gpt55": protocol.get("model") == MODEL,
        "workers_exact_six": protocol.get("worker_count") == WORKERS,
        "logical_call_budget_exact": protocol.get("total_logical_calls")
        == int(config["case_count"]) * int(config["repeat_count"]) * 5,
        "shared_primary_frozen": protocol.get("shared_package_primary") is True,
        "hidden_not_loaded": protocol.get("behavioral_evaluator_loaded") is False,
        "model_calls_zero": protocol.get("model_calls") == 0,
        "stage_has_no_private_directory": not (experiment / "stage" / "_private").exists(),
        "stage_has_no_private_path_token": "_private" not in stage_text,
        "stage_has_no_hidden_label_token": "canonical_package_path" not in stage_text,
        "stage_has_no_high_entropy_secret": base.HIGH_ENTROPY_SECRET.search(stage_text) is None,
        "driver_hash_frozen": code_hashes.get("release_tools/paired80_execution_v1.py")
        == sha256_file(Path(__file__)),
        "ast_backend_hash_frozen": code_hashes.get("bvi_skill_evo/artifact_scope_ast_v1.py")
        == sha256_file(Path(ast_backend.__file__)),
        "hidden_evaluator_hash_frozen": code_hashes.get(
            "release_tools/paired80_hidden_evaluator_v1.py"
        )
        == sha256_file(Path(__file__).with_name("paired80_hidden_evaluator_v1.py")),
        "formal_smoke_authorized": protocol["mode"] != "formal"
        or isinstance(protocol.get("smoke_authorization"), dict),
    }
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "failed_checks": [name for name, value in checks.items() if not value],
        "check_count": len(checks),
        "passed_check_count": sum(bool(value) for value in checks.values()),
        "mode": protocol["mode"],
        "case_count": len(matrix),
        "repeat_count": config["repeat_count"],
        "planned_logical_model_calls": protocol["total_logical_calls"],
        "parent_ast_decision_counts": dict(sorted(ast_decisions.items())),
        "model_calls": 0,
        "behavioral_evaluator_loaded": False,
        "credential_persisted": False,
        "protocol_hash": protocol["protocol_hash"],
    }
    report["preflight_hash"] = canonical_json_hash(report)
    return report


def _valid_zero_call(experiment: Path) -> dict[str, Any]:
    protocol = read_json(experiment / "stage" / "FROZEN_PROTOCOL.json")
    preflight = read_json(experiment / "_audit" / "ZERO_CALL_PREFLIGHT.json")
    if (
        preflight.get("status") != "pass"
        or not _embedded_hash_valid(preflight, "preflight_hash")
        or not _embedded_hash_valid(protocol, "protocol_hash")
        or preflight.get("protocol_hash") != protocol.get("protocol_hash")
        or (protocol.get("code_sha256") or {}).get(
            "release_tools/paired80_execution_v1.py"
        )
        != sha256_file(Path(__file__))
        or (protocol.get("code_sha256") or {}).get(
            "bvi_skill_evo/artifact_scope_ast_v1.py"
        )
        != sha256_file(Path(ast_backend.__file__))
    ):
        raise ValueError("paired80_valid_zero_call_preflight_required")
    return protocol


def run_calls(experiment_root: Path, *, phase: str, api_key: str) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    protocol = _valid_zero_call(experiment)
    originals = {
        "ast_backend": base.ast_backend,
        "parse": base.parse_and_apply_package_edits,
        "workers": base.WORKERS,
        "model": base.MODEL,
    }
    base.ast_backend = ast_backend
    base.parse_and_apply_package_edits = _normalized_apply
    base.WORKERS = int(protocol["worker_count"])
    base.MODEL = str(protocol["model"])
    try:
        return base._run_calls(experiment, phase=phase, api_key=api_key)
    finally:
        base.ast_backend = originals["ast_backend"]
        base.parse_and_apply_package_edits = originals["parse"]
        base.WORKERS = originals["workers"]
        base.MODEL = originals["model"]


def _complete_run_summary(
    experiment: Path, *, phase: str, expected_count: int
) -> dict[str, Any]:
    summary = read_json(experiment / "runs" / phase / "BATCH_RUN_SUMMARY.json")
    if (
        not _embedded_hash_valid(summary, "run_hash")
        or summary.get("logical_call_count") != expected_count
        or summary.get("provider_response_count") != expected_count
        or summary.get("exact_model_count") != expected_count
    ):
        raise ValueError(f"paired80_{phase}_model_run_incomplete")
    return summary


def prepare_revisions(experiment_root: Path) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    protocol = _valid_zero_call(experiment)
    expected_primary = int(protocol["primary_logical_calls"])
    _complete_run_summary(experiment, phase="primary", expected_count=expected_primary)
    calls = read_json(experiment / "stage" / "PRIMARY_CALLS.json")["calls"]
    by_key: dict[tuple[int, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for call in calls:
        by_key[(int(call["repeat_index"]), str(call["case_id"]))][str(call["view"])] = call

    selected_root = experiment / "selected_primary"
    if selected_root.exists():
        raise FileExistsError(selected_root)
    selected_rows: list[dict[str, Any]] = []
    for (repeat_index, case_id), views in sorted(by_key.items()):
        for view in PRIMARY_VIEWS:
            call = views[view]
            source, origin = base._candidate_or_source(experiment, "primary", call)
            destination = (
                selected_root
                / f"repeat-{repeat_index:02d}"
                / case_id
                / view
                / "package"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            copy_tree_clean(source, destination)
            selected_rows.append(
                {
                    "repeat_index": repeat_index,
                    "case_id": case_id,
                    "view": view,
                    "trial_id": call["trial_id"],
                    "origin": origin,
                    "package_path": destination.relative_to(experiment).as_posix(),
                    "package_tree_hash": _tree_hash(destination),
                    "run_record_hash": base._run_record(
                        experiment, "primary", call["trial_id"]
                    )["run_record_hash"],
                }
            )
    primary_selection: dict[str, Any] = {
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
    for (repeat_index, case_id), _ in sorted(by_key.items()):
        case_root = experiment / "stage" / "public" / "cases" / case_id
        repeat_root = selected_root / f"repeat-{repeat_index:02d}" / case_id
        source_paths = {
            "markdown-revision": repeat_root / "markdown-view" / "package",
            "package-revision": repeat_root / "package-view" / "package",
            "structural-revision": repeat_root / "package-view" / "package",
        }
        facts = ast_backend.build_public_multilang_contract_set(
            source_paths["structural-revision"],
            (case_root / "REQUEST.md").read_text(encoding="utf-8"),
        )
        ast_backend.validate_public_multilang_contract_set(
            facts, source_paths["structural-revision"]
        )
        facts_directory = facts_root / f"repeat-{repeat_index:02d}"
        facts_directory.mkdir(exist_ok=True)
        facts_path = facts_directory / f"{case_id}.json"
        write_json(facts_path, facts)
        parent_source = case_root / "package"
        for view in REVISION_VIEWS:
            source = source_paths[view]
            checks = base._generic_checks(source, parent_source)
            supplied_facts = facts if view == "structural-revision" else None
            trial_id = f"r{repeat_index:02d}--{case_id}--{view}"
            prompt_directory = prompts_root / f"repeat-{repeat_index:02d}"
            prompt_directory.mkdir(exist_ok=True)
            prompt_path = prompt_directory / f"{case_id}--{view}.txt"
            prompt_path.write_text(
                base._revision_prompt(
                    case_root,
                    source,
                    checks,
                    view=view,
                    facts=supplied_facts,
                ),
                encoding="utf-8",
            )
            call: dict[str, Any] = {
                "trial_id": trial_id,
                "case_id": case_id,
                "repeat_index": repeat_index,
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
    calls_out.sort(key=lambda row: str(row["trial_id"]))
    write_json(revision_root / "REVISION_CALLS.json", {"calls": calls_out})
    decisions = Counter(
        read_json(path)["localization_decision"]["decision"]
        for path in facts_root.rglob("*.json")
    )
    plan: dict[str, Any] = {
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
        "model_calls_so_far": expected_primary,
        "behavioral_evaluator_loaded": False,
        "hidden_artifacts_consumed": False,
        "credential_persisted": False,
    }
    plan["revision_plan_hash"] = canonical_json_hash(plan)
    write_json(revision_root / "FROZEN_REVISION_PLAN.json", plan)
    report = audit_revisions(experiment)
    write_json(experiment / "_audit" / "REVISION_ZERO_CALL_PREFLIGHT.json", report)
    if report["status"] != "pass":
        raise ValueError(f"paired80_revision_preflight_failed:{report['failed_checks']}")
    return report


def audit_revisions(experiment_root: Path) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    protocol = _valid_zero_call(experiment)
    plan = read_json(experiment / "stage" / "revisions" / "FROZEN_REVISION_PLAN.json")
    calls = read_json(experiment / "stage" / "revisions" / "REVISION_CALLS.json")["calls"]
    expected = int(protocol["revision_logical_calls"])
    prompt_exact = True
    source_exact = True
    facts_valid = True
    for call in calls:
        source = experiment / str(call["source_package_path"])
        case_root = experiment / "stage" / "public" / "cases" / str(call["case_id"])
        parent = case_root / "package"
        facts = None
        if call["view"] == "structural-revision":
            facts = read_json(experiment / str(call["facts_path"]))
            try:
                ast_backend.validate_public_multilang_contract_set(facts, source)
            except ValueError:
                facts_valid = False
        expected_prompt = base._revision_prompt(
            case_root,
            source,
            base._generic_checks(source, parent),
            view=str(call["view"]),
            facts=facts,
        )
        prompt_path = experiment / str(call["prompt_path"])
        prompt_exact = prompt_exact and prompt_path.read_text(encoding="utf-8") == expected_prompt
        prompt_exact = prompt_exact and sha256_file(prompt_path) == call["prompt_sha256"]
        source_exact = source_exact and _tree_hash(source) == call["source_package_tree_hash"]
    checks = {
        "revision_plan_hash_valid": _embedded_hash_valid(plan, "revision_plan_hash"),
        "revision_call_count_exact": len(calls) == expected,
        "three_views_balanced": Counter(row["view"] for row in calls)
        == Counter({view: expected // 3 for view in REVISION_VIEWS}),
        "trial_ids_unique": len({row["trial_id"] for row in calls}) == expected,
        "prompt_content_and_hashes_exact": prompt_exact,
        "source_tree_hashes_exact": source_exact,
        "runtime_facts_validate": facts_valid,
        "facts_only_on_structural_view": all(
            ("facts_path" in row) == (row["view"] == "structural-revision")
            for row in calls
        ),
        "primary_model_calls_exact": plan.get("model_calls_so_far")
        == protocol.get("primary_logical_calls"),
        "hidden_not_loaded": plan.get("behavioral_evaluator_loaded") is False,
        "credential_not_persisted": plan.get("credential_persisted") is False,
    }
    report: dict[str, Any] = {
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
    report["preflight_hash"] = canonical_json_hash(report)
    return report


def _select_revision_or_primary(
    experiment: Path,
    *,
    repeat_index: int,
    case_id: str,
    revision_view: str,
    primary_view: str,
) -> tuple[Path, str, str]:
    trial_id = f"r{repeat_index:02d}--{case_id}--{revision_view}"
    record = base._run_record(experiment, "revision", trial_id)
    candidate = experiment / "runs" / "revision" / trial_id / "candidate" / "package"
    if record["status"] == "candidate_frozen" and candidate.is_dir():
        return candidate, "revision_candidate", str(record["run_record_hash"])
    primary = (
        experiment
        / "selected_primary"
        / f"repeat-{repeat_index:02d}"
        / case_id
        / primary_view
        / "package"
    )
    return primary, "primary_fallback_after_invalid_revision", str(record["run_record_hash"])


def freeze_final_candidates(experiment_root: Path) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    protocol = _valid_zero_call(experiment)
    expected_revision = int(protocol["revision_logical_calls"])
    _complete_run_summary(experiment, phase="revision", expected_count=expected_revision)
    case_ids = sorted(
        path.name for path in (experiment / "stage" / "public" / "cases").iterdir()
    )
    selected = experiment / "selected"
    if selected.exists():
        raise FileExistsError(selected)
    mapping = {
        "md-only-self-evolution": ("markdown-revision", "markdown-view"),
        "raw-package-self-evolution": ("package-revision", "package-view"),
        "ast-package-self-evolution": ("structural-revision", "package-view"),
    }
    rows: list[dict[str, Any]] = []
    for repeat_index in range(1, int(protocol["repeat_count"]) + 1):
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
                        experiment,
                        repeat_index=repeat_index,
                        case_id=case_id,
                        revision_view=revision_view,
                        primary_view=primary_view,
                    )
                destination = (
                    selected
                    / f"repeat-{repeat_index:02d}"
                    / condition
                    / case_id
                    / "package"
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                copy_tree_clean(source, destination)
                freeze: dict[str, Any] = {
                    "schema_version": SCHEMA_VERSION,
                    "repeat_index": repeat_index,
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
        "repeat_count": protocol["repeat_count"],
        "condition_counts": dict(sorted(Counter(row["condition"] for row in rows).items())),
        "rows": rows,
        "model_response_count": int(protocol["total_logical_calls"]),
        "behavioral_evaluator_loaded": False,
        "hidden_artifacts_consumed": False,
        "candidate_selection_uses_hidden": False,
        "credential_persisted": False,
    }
    selection["selection_hash"] = canonical_json_hash(selection)
    write_json(selected / "CANDIDATE_SELECTION.json", selection)
    report = prehidden_audit(experiment)
    write_json(experiment / "_audit" / "PREHIDDEN_AUDIT.json", report)
    if report["status"] != "pass":
        raise ValueError(f"paired80_prehidden_failed:{report['failed_checks']}")
    return report


def prehidden_audit(experiment_root: Path) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    protocol = _valid_zero_call(experiment)
    selection = read_json(experiment / "selected" / "CANDIDATE_SELECTION.json")
    rows = list(selection.get("rows") or [])
    freezes_valid = True
    tree_hashes_valid = True
    for row in rows:
        package = experiment / str(row["package_path"])
        freeze = read_json(package.parent / "FINAL_CANDIDATE_FREEZE.json")
        freezes_valid = freezes_valid and _embedded_hash_valid(
            freeze, "final_candidate_freeze_hash"
        )
        tree_hashes_valid = tree_hashes_valid and _tree_hash(package) == row[
            "candidate_tree_hash"
        ]
    high_entropy_hits: list[str] = []
    for path in experiment.rglob("*"):
        if not path.is_file() or path.stat().st_size > 5_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if base.HIGH_ENTROPY_SECRET.search(text):
            high_entropy_hits.append(path.relative_to(experiment).as_posix())
    primary = read_json(experiment / "runs" / "primary" / "BATCH_RUN_SUMMARY.json")
    revision = read_json(experiment / "runs" / "revision" / "BATCH_RUN_SUMMARY.json")
    expected_rows = int(protocol["final_candidate_rows"])
    expected_per_condition = int(protocol["case_count"]) * int(protocol["repeat_count"])
    checks = {
        "protocol_hash_valid": _embedded_hash_valid(protocol, "protocol_hash"),
        "selection_hash_valid": _embedded_hash_valid(selection, "selection_hash"),
        "candidate_rows_exact": len(rows) == expected_rows,
        "case_count_exact": len({row["case_id"] for row in rows})
        == int(protocol["case_count"]),
        "repeat_count_exact": len({int(row["repeat_index"]) for row in rows})
        == int(protocol["repeat_count"]),
        "condition_matrix_exact": Counter(row["condition"] for row in rows)
        == Counter({condition: expected_per_condition for condition in FINAL_CONDITIONS}),
        "candidate_identity_unique": len(
            {
                (int(row["repeat_index"]), row["case_id"], row["condition"])
                for row in rows
            }
        )
        == expected_rows,
        "final_freezes_valid": freezes_valid,
        "candidate_tree_hashes_valid": tree_hashes_valid,
        "primary_logical_calls_exact": primary.get("logical_call_count")
        == int(protocol["primary_logical_calls"]),
        "revision_logical_calls_exact": revision.get("logical_call_count")
        == int(protocol["revision_logical_calls"]),
        "provider_responses_exact": primary.get("provider_response_count")
        == int(protocol["primary_logical_calls"])
        and revision.get("provider_response_count")
        == int(protocol["revision_logical_calls"]),
        "provider_model_exact": primary.get("exact_model_count")
        == int(protocol["primary_logical_calls"])
        and revision.get("exact_model_count")
        == int(protocol["revision_logical_calls"]),
        "hidden_flags_false": all(
            row.get("behavioral_evaluator_loaded") is False
            and row.get("hidden_artifacts_consumed") is False
            for row in rows
        ),
        "selection_hidden_independent": selection.get("candidate_selection_uses_hidden")
        is False,
        "experiment_has_no_private_directory": not (experiment / "_private").exists(),
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
        "logical_model_response_count": int(protocol["total_logical_calls"]),
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
    prepare.add_argument("--benchmark-root", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--mode", choices=sorted(MODE_CONFIG), required=True)
    prepare.add_argument("--smoke-analysis", type=Path)
    audit = commands.add_parser("preflight")
    audit.add_argument("--experiment-root", type=Path, required=True)
    audit.add_argument("--benchmark-root", type=Path, required=True)
    for name in ("run-primary", "prepare-revisions", "run-revisions", "freeze-final"):
        command = commands.add_parser(name)
        command.add_argument("--experiment-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "prepare":
        result = prepare_execution(
            args.benchmark_root,
            args.output,
            mode=args.mode,
            smoke_analysis_path=args.smoke_analysis,
        )
    elif args.command == "preflight":
        result = audit_execution(args.experiment_root, args.benchmark_root)
    elif args.command == "run-primary":
        result = run_calls(
            args.experiment_root,
            phase="primary",
            api_key=getpass.getpass("OpenLux API key: "),
        )
    elif args.command == "prepare-revisions":
        result = prepare_revisions(args.experiment_root)
    elif args.command == "run-revisions":
        result = run_calls(
            args.experiment_root,
            phase="revision",
            api_key=getpass.getpass("OpenLux API key: "),
        )
    else:
        result = freeze_final_candidates(args.experiment_root)
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
