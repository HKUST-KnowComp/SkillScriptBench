from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import random
import shutil
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from bvi_skill_evo.proposal_first_ast_gate_v280 import build_application_failure_gate
from bvi_skill_evo.proposal_first_closure_gate_v292 import (
    ACCEPT,
    build_proposal_first_closure_report,
)
from bvi_skill_evo.python_span_patch_v246 import apply_python_span_patch
from bvi_skill_evo.universal_span_patch_v286 import apply_universal_span_patch
from skillscriptbench.d35_registry105_proposal_first_experiment_v288 import (
    BASE_URL,
    MODEL,
    _call_completion,
    _discover_package,
    _embedded_hash_valid,
    _package_payload,
)
from skillscriptbench.io_utils import (
    canonical_json_hash,
    copy_tree_clean,
    hash_tree,
    read_json,
    sha256_file,
    write_json,
)
from skillscriptbench.package_matrix_conditions_v65 import _patch_arguments, _utc_now


SCHEMA_VERSION = "2.93-d36-registry105-proposal-first-closure-v1"
SOURCE_EXPERIMENT = (
    "final_results/multi_view_structural_skill_evo/"
    "20260815_stage132_d35_registry105_v3"
)
CALL_CONDITIONS = (
    "generic-revision",
    "ast-map-revision",
    "ast-closure-revision",
)
CONDITIONS = (
    "no-evolution",
    "raw-one-shot",
    *CALL_CONDITIONS,
)
SMOKE_TASK_IDS = (
    "d16-railway-multiroute",
    "d24-gtars-bed-path-binding",
    "d25-tokenize-limit-effect",
    "d27-cot-analysis-dual-flow",
    "d28-js-sort-direction",
    "d31-dividend-event-cap-flow",
)
FACT_PRIORITY = (
    "WORKFLOW_AST_FACTS.json",
    "DOCUMENTED_PARAMETER_AST_FACTS.json",
    "MULTILANG_AST_FACTS.json",
    "PYTHON_AST_FACTS.json",
)
FORBIDDEN_PROMPT_MARKERS = (
    "_private/",
    "EVALUATION_SPEC",
    "EVALUATION_LABEL",
    "SOURCE_TEST_RESULT",
    "MUTATION_LABEL",
    "expected_output",
    "oracle_package",
    "gold_or_oracle_used",
    "task_verifier_feedback_used",
)
CODE_PATHS = (
    "skillscriptbench/d36_registry105_closure_experiment_v293.py",
    "skillscriptbench/d36_registry105_hidden_dispatch_v294.py",
    "skillscriptbench/d36_registry105_analysis_v295.py",
    "bvi_skill_evo/proposal_first_closure_gate_v292.py",
    "bvi_skill_evo/proposal_first_structural_gate_v287.py",
    "bvi_skill_evo/proposal_first_ast_gate_v280.py",
    "bvi_skill_evo/proposal_first_ast_gate_v283.py",
    "bvi_skill_evo/python_span_patch_v246.py",
    "bvi_skill_evo/python_span_patch_v241.py",
    "bvi_skill_evo/python_span_patch_v237.py",
    "bvi_skill_evo/universal_span_patch_v286.py",
    "skillscriptbench/d35_registry105_proposal_first_experiment_v288.py",
    "skillscriptbench/multilang_structural_v66.py",
    "skillscriptbench/io_utils.py",
)


def _workspace() -> Path:
    return Path(__file__).resolve().parents[1]


def _source() -> Path:
    return (_workspace() / SOURCE_EXPERIMENT).resolve()


def _code_hashes() -> dict[str, str]:
    missing = [path for path in CODE_PATHS if not (_workspace() / path).is_file()]
    if missing:
        raise FileNotFoundError(f"experiment_code_missing:{missing}")
    return {path: sha256_file(_workspace() / path) for path in CODE_PATHS}


def _select_visible_facts(source_case: Path) -> tuple[dict[str, Any] | None, str | None]:
    evolution = source_case / "evolution"
    for name in FACT_PRIORITY:
        path = evolution / name
        if path.is_file():
            return read_json(path), sha256_file(path)
    return None, None


def _prompt_packet(report: dict[str, Any], *, include_visible_facts: bool) -> dict[str, Any]:
    packet = {
        "changed_nodes_before": report.get("changed_nodes_before"),
        "changed_nodes_after": report.get("changed_nodes_after"),
        "impact_closure": report.get("impact_closure"),
        "local_def_use_findings": report.get("local_def_use_findings"),
        "call_edges_added": report.get("call_edges_added"),
        "call_edges_removed": report.get("call_edges_removed"),
        "removed_public_declarations": report.get("removed_public_declarations"),
        "removed_private_implementation_declarations": report.get(
            "removed_private_implementation_declarations"
        ),
        "unresolved_name_findings": report.get("unresolved_name_findings"),
        "parse_failures": report.get("parse_failures"),
        "claim_boundary": report.get("claim_boundary"),
    }
    if include_visible_facts:
        packet["candidate_aware_visible_structural_facts"] = report.get(
            "candidate_aware_visible_structural_facts"
        )
    return packet


def _revision_prompt(
    task_text: str,
    current_package: Path,
    language: str,
    condition: str,
    report: dict[str, Any],
) -> str:
    extension = {
        "python": "Python files under scripts/",
        "javascript": "JavaScript files under scripts/",
        "typescript": "TypeScript files under scripts/",
        "shell": "Shell files under scripts/",
    }[language]
    instruction = (
        "Review a previously proposed repair to one reusable executable Agent Skill. Read the complete "
        "visible request, SKILL.md, and current scripts. Submit one incremental patch against the CURRENT "
        "CANDIDATE package. Zero edits keeps the current candidate. Use at most two unique exact replace_span "
        f"edits inside {extension}; preserve public interfaces, defaults, documentation, and unrelated behavior. "
        "Do not request tests, task verdicts, expected outputs, mutation labels, gold code, oracle behavior, "
        "rewards, or verifier feedback. Call submit_skill_patch exactly once and leave AST id fields empty."
    )
    sections = [
        instruction,
        "USER_REQUEST.md\n" + task_text,
        "CURRENT_VISIBLE_EXECUTABLE_SKILL_PACKAGE.json\n"
        + json.dumps(
            _package_payload(current_package, language),
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
        ),
    ]
    if condition == "ast-map-revision":
        sections.append(
            "PACKAGE_CHANGE_AND_IMPACT_MAP.json\n"
            + json.dumps(
                _prompt_packet(report, include_visible_facts=False),
                indent=2,
                sort_keys=True,
                ensure_ascii=True,
            )
        )
        sections.append(
            "The map is a package-local structural review aid, not a semantic verdict. Inspect related "
            "call sites without assuming every listed node requires a change."
        )
    elif condition == "ast-closure-revision":
        sections.append(
            "CANDIDATE_AWARE_STRUCTURAL_COMPLETENESS_REPORT.json\n"
            + json.dumps(
                _prompt_packet(report, include_visible_facts=True),
                indent=2,
                sort_keys=True,
                ensure_ascii=True,
            )
        )
        sections.append(
            "The report contains answer-free structural evidence derived from the visible package. A "
            "still-present site is only a review item, and a changed site is not proof of correctness. "
            "Use the full package and request to decide whether any incremental edit is justified."
        )
    return "\n\n".join(sections) + "\n"


def _validate_source() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    source = _source()
    plan = read_json(source / "stage" / "FROZEN_PLAN.json")
    selections = read_json(source / "selected" / "all" / "SELECTION_SUMMARY.json")
    origins = list(read_json(source / "stage" / "CASE_ORIGINS.json")["rows"])
    if not _embedded_hash_valid(plan, "plan_hash"):
        raise ValueError("source_plan_hash_invalid")
    if not _embedded_hash_valid(selections, "selection_hash"):
        raise ValueError("source_selection_hash_invalid")
    if plan.get("model") != MODEL or len(origins) != 105:
        raise ValueError("source_registry105_gpt55_required")
    for origin in origins:
        task_id = str(origin["task_id"])
        for condition in ("no-evolution", "raw-proposal"):
            selected = source / "selected" / "all" / condition / task_id
            freeze = read_json(selected / "SELECTION_FREEZE.json")
            if not _embedded_hash_valid(freeze, "selection_freeze_hash"):
                raise ValueError(f"source_selection_freeze_invalid:{task_id}:{condition}")
            if freeze.get("selected_tree_hash") != canonical_json_hash(
                hash_tree(selected / "package")
            ):
                raise ValueError(f"source_selection_tree_changed:{task_id}:{condition}")
    return plan, selections, origins


def prepare_stage(
    experiment_root: str | Path,
    *,
    model: str = MODEL,
    base_url: str = BASE_URL,
    timeout: int = 600,
) -> dict[str, Any]:
    if model != MODEL:
        raise ValueError("d36_requires_exact_gpt_5_5")
    experiment = Path(experiment_root).resolve()
    if experiment.exists():
        raise FileExistsError(experiment)
    public = experiment / "stage" / "public"
    current = experiment / "stage" / "current_candidates"
    reports = experiment / "stage" / "closure_reports"
    prompts = experiment / "stage" / "revision_prompts"
    for path in (public, current, reports, prompts, experiment / "_audit"):
        path.mkdir(parents=True, exist_ok=True)

    source_plan, source_selections, origins = _validate_source()
    calls: list[dict[str, Any]] = []
    origin_rows: list[dict[str, Any]] = []
    source = _source()
    for origin in sorted(origins, key=lambda row: str(row["task_id"])):
        task_id = str(origin["task_id"])
        source_case = source / "stage" / "public" / "cases" / task_id
        target_case = public / "cases" / task_id
        copy_tree_clean(source_case / "task", target_case / "task")
        raw_package = source / "selected" / "all" / "raw-proposal" / task_id / "package"
        target_current = current / task_id / "package"
        copy_tree_clean(raw_package, target_current)
        _, parent = _discover_package(target_case, str(origin["skill_name"]))
        facts, facts_sha256 = _select_visible_facts(Path(origin["source_case"]))
        request_text = (target_case / "task" / "task.md").read_text(encoding="utf-8")
        report = build_proposal_first_closure_report(
            parent,
            target_current,
            request_text,
            visible_structural_facts=facts,
        )
        report_path = reports / f"{task_id}.json"
        write_json(report_path, report)
        for condition in CALL_CONDITIONS:
            prompt_path = prompts / f"{task_id}--{condition}--r2.txt"
            prompt_path.write_text(
                _revision_prompt(
                    request_text,
                    target_current,
                    str(origin["language"]),
                    condition,
                    report,
                ),
                encoding="utf-8",
            )
            calls.append(
                {
                    "trial_id": f"{task_id}--{condition}--r2",
                    "task_id": task_id,
                    "condition": condition,
                    "batch": str(origin["batch"]),
                    "language": str(origin["language"]),
                    "skill_name": str(origin["skill_name"]),
                    "prompt_path": str(prompt_path.relative_to(experiment)),
                    "prompt_sha256": sha256_file(prompt_path),
                    "prompt_bytes": prompt_path.stat().st_size,
                    "source_raw_tree_hash": canonical_json_hash(hash_tree(target_current)),
                    "source_report_hash": report["gate_hash"],
                }
            )
        origin_rows.append(
            {
                **origin,
                "visible_structural_facts_sha256": facts_sha256,
                "source_raw_tree_hash": canonical_json_hash(hash_tree(target_current)),
                "source_report_hash": report["gate_hash"],
            }
        )

    smoke = sorted(set(SMOKE_TASK_IDS))
    all_ids = sorted(str(row["task_id"]) for row in origin_rows)
    if not set(smoke) <= set(all_ids):
        raise ValueError("smoke_task_missing")
    splits = {
        "schema_version": SCHEMA_VERSION,
        "smoke": smoke,
        "formal": sorted(set(all_ids) - set(smoke)),
        "all": all_ids,
    }
    splits["splits_hash"] = canonical_json_hash(splits)
    write_json(experiment / "stage" / "SPLITS.json", splits)
    write_json(
        experiment / "stage" / "CASE_ORIGINS.json",
        {"schema_version": SCHEMA_VERSION, "rows": origin_rows},
    )
    random.Random("d36-registry105-closure-v293").shuffle(calls)
    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "frozen_before_equal_budget_revision_calls",
        "created_at": _utc_now(),
        "model": model,
        "base_url": base_url,
        "temperature": 0,
        "timeout": timeout,
        "max_attempts": 1,
        "case_count": 105,
        "call_count": len(calls),
        "call_conditions": list(CALL_CONDITIONS),
        "selection_conditions": list(CONDITIONS),
        "calls": calls,
        "source_experiment": str(source),
        "source_plan_hash": source_plan["plan_hash"],
        "source_selection_hash": source_selections["selection_hash"],
        "source_first_proposal_model_calls_per_task": 1,
        "revision_model_calls_per_method_per_task": 1,
        "effective_model_calls_per_revision_method": 2,
        "public_tree_hash": canonical_json_hash(hash_tree(public)),
        "current_candidates_tree_hash": canonical_json_hash(hash_tree(current)),
        "closure_reports_tree_hash": canonical_json_hash(hash_tree(reports)),
        "prompts_tree_hash": canonical_json_hash(hash_tree(prompts)),
        "origins_sha256": sha256_file(experiment / "stage" / "CASE_ORIGINS.json"),
        "splits_sha256": sha256_file(experiment / "stage" / "SPLITS.json"),
        "code_sha256": _code_hashes(),
        "candidate_frozen_before_hidden_evaluation": True,
        "hidden_evaluator_loaded_during_generation": False,
        "task_verifier_feedback_used": False,
        "retrospective_registry105_development_experiment": True,
        "runtime": {"python": platform.python_version(), "platform": platform.platform()},
        "credential_persisted": False,
    }
    if len(calls) != 105 * len(CALL_CONDITIONS):
        raise ValueError(f"expected_315_calls:{len(calls)}")
    plan["plan_hash"] = canonical_json_hash(plan)
    write_json(experiment / "stage" / "FROZEN_PLAN.json", plan)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "ready_for_zero_call_preflight",
        "case_count": 105,
        "call_count": len(calls),
        "smoke_call_count": len(smoke) * len(CALL_CONDITIONS),
        "formal_call_count": (105 - len(smoke)) * len(CALL_CONDITIONS),
        "plan_hash": plan["plan_hash"],
        "model_calls": 0,
    }
    result["prepare_hash"] = canonical_json_hash(result)
    write_json(experiment / "_audit" / "PREPARE_RECORD.json", result)
    return result


def _validate_stage(experiment: Path) -> tuple[dict[str, Any], dict[str, bool]]:
    plan = read_json(experiment / "stage" / "FROZEN_PLAN.json")
    splits = read_json(experiment / "stage" / "SPLITS.json")
    source_plan, source_selections, _ = _validate_source()
    calls = list(plan.get("calls") or [])
    checks = {
        "plan_hash": _embedded_hash_valid(plan, "plan_hash"),
        "status": plan.get("status") == "frozen_before_equal_budget_revision_calls",
        "exact_model": plan.get("model") == MODEL,
        "single_attempt": plan.get("max_attempts") == 1,
        "case_count": plan.get("case_count") == 105,
        "call_count": len(calls) == plan.get("call_count") == 315,
        "condition_balance": Counter(row["condition"] for row in calls)
        == Counter({condition: 105 for condition in CALL_CONDITIONS}),
        "unique_trials": len({row["trial_id"] for row in calls}) == len(calls),
        "split_hash": _embedded_hash_valid(splits, "splits_hash"),
        "source_plan": plan.get("source_plan_hash") == source_plan.get("plan_hash"),
        "source_selections": plan.get("source_selection_hash")
        == source_selections.get("selection_hash"),
        "public_tree": canonical_json_hash(hash_tree(experiment / "stage" / "public"))
        == plan.get("public_tree_hash"),
        "current_tree": canonical_json_hash(
            hash_tree(experiment / "stage" / "current_candidates")
        )
        == plan.get("current_candidates_tree_hash"),
        "reports_tree": canonical_json_hash(
            hash_tree(experiment / "stage" / "closure_reports")
        )
        == plan.get("closure_reports_tree_hash"),
        "prompts_tree": canonical_json_hash(hash_tree(experiment / "stage" / "revision_prompts"))
        == plan.get("prompts_tree_hash"),
        "origins": sha256_file(experiment / "stage" / "CASE_ORIGINS.json")
        == plan.get("origins_sha256"),
        "splits": sha256_file(experiment / "stage" / "SPLITS.json")
        == plan.get("splits_sha256"),
        "code_hashes": all(
            (_workspace() / path).is_file()
            and sha256_file(_workspace() / path) == digest
            for path, digest in plan.get("code_sha256", {}).items()
        ),
        "private_not_copied": not any(
            path.name == "_private" for path in (experiment / "stage").rglob("*")
        ),
    }
    prompt_checks: list[bool] = []
    for call in calls:
        prompt = experiment / call["prompt_path"]
        text = prompt.read_text(encoding="utf-8")
        prompt_checks.append(
            sha256_file(prompt) == call["prompt_sha256"]
            and not any(marker in text for marker in FORBIDDEN_PROMPT_MARKERS)
            and call["task_id"] not in text
        )
    checks["prompt_isolation"] = all(prompt_checks)
    return plan, checks


def zero_call_preflight(experiment_root: str | Path) -> dict[str, Any]:
    experiment = Path(experiment_root).resolve()
    plan, checks = _validate_stage(experiment)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if all(checks.values()) else "fail",
        "created_at": _utc_now(),
        "checks": checks,
        "case_count": plan["case_count"],
        "call_count": plan["call_count"],
        "model_calls": 0,
    }
    result["preflight_hash"] = canonical_json_hash(result)
    write_json(experiment / "_audit" / "ZERO_CALL_PREFLIGHT.json", result)
    if result["status"] != "pass":
        raise ValueError(
            "zero_call_preflight_failed:"
            + ",".join(name for name, passed in checks.items() if not passed)
        )
    return result


def _run_one(
    experiment: Path,
    call: dict[str, Any],
    output: Path,
    *,
    api_key: str,
    plan: dict[str, Any],
) -> dict[str, Any]:
    final = output / call["trial_id"]
    temporary = output / f".{call['trial_id']}.partial-{uuid.uuid4().hex}"
    temporary.mkdir(parents=True)
    prompt_path = experiment / call["prompt_path"]
    started = time.perf_counter()
    response, attempts, request_payload = _call_completion(
        api_key=api_key,
        plan=plan,
        prompt=prompt_path.read_text(encoding="utf-8"),
        language=call["language"],
    )
    if response is not None:
        write_json(temporary / "provider_response.json", response)
    parse_error = None
    transport_mode = None
    try:
        content, transport_mode = _patch_arguments(response) if response is not None else ("", None)
    except (KeyError, TypeError, ValueError) as exc:
        content = ""
        parse_error = f"{type(exc).__name__}:{exc}"
    (temporary / "raw_response.txt").write_text(content, encoding="utf-8")

    current = experiment / "stage" / "current_candidates" / call["task_id"] / "package"
    case = experiment / "stage" / "public" / "cases" / call["task_id"]
    _, parent = _discover_package(case, call["skill_name"])
    application = None
    application_error = parse_error
    if response is not None and parse_error is None:
        try:
            if call["language"] == "python":
                application = apply_python_span_patch(
                    content,
                    current,
                    temporary / "candidate" / "package",
                    visible_ast_facts=None,
                    require_ast_binding=False,
                )
            else:
                application = apply_universal_span_patch(
                    content, current, temporary / "candidate" / "package"
                )
            write_json(temporary / "RESPONSE_APPLICATION.json", application)
        except Exception as exc:
            application_error = f"{type(exc).__name__}:{exc}"
            shutil.rmtree(temporary / "candidate", ignore_errors=True)
    candidate = temporary / "candidate" / "package"
    if application is not None and candidate.is_dir():
        visible = read_json(experiment / "stage" / "closure_reports" / f"{call['task_id']}.json").get(
            "candidate_aware_visible_structural_facts", {}
        ).get("facts")
        gate = build_proposal_first_closure_report(
            parent,
            candidate,
            (case / "task" / "task.md").read_text(encoding="utf-8"),
            visible_structural_facts=visible,
        )
    else:
        gate = build_application_failure_gate(application_error or "provider_unavailable")
    write_json(temporary / "POSTHOC_CLOSURE_GATE.json", gate)
    status = (
        "provider_unavailable_no_candidate"
        if response is None
        else "invalid_response_frozen"
        if application is None
        else "candidate_frozen"
    )
    freeze = {
        "schema_version": SCHEMA_VERSION,
        "trial_id": call["trial_id"],
        "task_id": call["task_id"],
        "condition": call["condition"],
        "language": call["language"],
        "status": status,
        "created_at": _utc_now(),
        "plan_hash": plan["plan_hash"],
        "prompt_sha256": sha256_file(prompt_path),
        "provider_response_sha256": sha256_file(temporary / "provider_response.json")
        if response
        else None,
        "raw_response_sha256": sha256_file(temporary / "raw_response.txt"),
        "application_sha256": sha256_file(temporary / "RESPONSE_APPLICATION.json")
        if application
        else None,
        "gate_sha256": sha256_file(temporary / "POSTHOC_CLOSURE_GATE.json"),
        "candidate_tree_hash": canonical_json_hash(hash_tree(candidate))
        if candidate.is_dir()
        else None,
        "gate_decision": gate["decision"],
        "application_error": application_error,
        "task_verifier_feedback_used": False,
        "hidden_evaluator_loaded_during_generation": False,
        "change_map_used": call["condition"] in {"ast-map-revision", "ast-closure-revision"},
        "visible_structural_facts_used": call["condition"] == "ast-closure-revision",
    }
    freeze["candidate_freeze_hash"] = canonical_json_hash(freeze)
    write_json(temporary / "CANDIDATE_FREEZE.json", freeze)
    usage = (response or {}).get("usage") or {}
    record = {
        "schema_version": SCHEMA_VERSION,
        "trial_id": call["trial_id"],
        "task_id": call["task_id"],
        "condition": call["condition"],
        "status": status,
        "response_id": (response or {}).get("id"),
        "transport_mode": transport_mode,
        "transport_attempts": attempts,
        "request_sha256": canonical_json_hash(request_payload),
        "candidate_freeze_hash": freeze["candidate_freeze_hash"],
        "gate_decision": gate["decision"],
        "usage": {
            key: int(usage.get(key) or 0)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "credential_persisted": False,
    }
    record["run_record_hash"] = canonical_json_hash(record)
    write_json(temporary / "RUN_RECORD.json", record)
    temporary.rename(final)
    return record


def run_calls(
    experiment_root: str | Path,
    *,
    split: str,
    api_key: str,
    workers: int = 6,
) -> dict[str, Any]:
    experiment = Path(experiment_root).resolve()
    preflight = read_json(experiment / "_audit" / "ZERO_CALL_PREFLIGHT.json")
    if preflight.get("status") != "pass" or not _embedded_hash_valid(
        preflight, "preflight_hash"
    ):
        raise ValueError("valid_zero_call_preflight_required")
    plan, checks = _validate_stage(experiment)
    if not all(checks.values()):
        raise ValueError("stage_changed_after_preflight")
    task_ids = set(read_json(experiment / "stage" / "SPLITS.json")[split])
    calls = [row for row in plan["calls"] if row["task_id"] in task_ids]
    output = experiment / "runs" / split
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(calls)))) as executor:
        futures = {
            executor.submit(
                _run_one, experiment, call, output, api_key=api_key, plan=plan
            ): call["trial_id"]
            for call in calls
        }
        for future in as_completed(futures):
            rows.append(future.result())
    rows.sort(key=lambda row: row["trial_id"])
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete",
        "split": split,
        "trial_count": len(rows),
        "completed_model_response_count": sum(row["response_id"] is not None for row in rows),
        "condition_counts": dict(Counter(row["condition"] for row in rows)),
        "status_counts": dict(Counter(row["status"] for row in rows)),
        "gate_decision_counts": dict(Counter(row["gate_decision"] for row in rows)),
        "usage": {
            key: sum(row["usage"][key] for row in rows)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "hidden_evaluator_loaded": False,
        "task_verifier_feedback_used": False,
        "rows": rows,
    }
    summary["run_hash"] = canonical_json_hash(summary)
    write_json(output / "BATCH_RUN_SUMMARY.json", summary)
    return summary


def _write_selection(
    experiment: Path,
    condition: str,
    task_id: str,
    package: Path,
    *,
    reason: str,
    source_hash: str | None,
    model_calls: int,
) -> dict[str, Any]:
    destination = experiment / "selected" / "all" / condition / task_id
    copy_tree_clean(package, destination / "package")
    freeze = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "condition": condition,
        "split": "all",
        "reason": reason,
        "source_candidate_freeze_hash": source_hash,
        "model_calls": model_calls,
        "selected_tree_hash": canonical_json_hash(hash_tree(destination / "package")),
        "candidate_frozen_before_hidden_evaluation": True,
        "hidden_feedback_used_for_selection": False,
        "task_verifier_feedback_used": False,
    }
    freeze["selection_freeze_hash"] = canonical_json_hash(freeze)
    write_json(destination / "SELECTION_FREEZE.json", freeze)
    return freeze


def freeze_selections(experiment_root: str | Path) -> dict[str, Any]:
    experiment = Path(experiment_root).resolve()
    plan, checks = _validate_stage(experiment)
    if not all(checks.values()):
        raise ValueError("stage_changed_before_selection")
    splits = read_json(experiment / "stage" / "SPLITS.json")
    rows_by_key: dict[tuple[str, str], Path] = {}
    for split in ("smoke", "formal"):
        summary = read_json(experiment / "runs" / split / "BATCH_RUN_SUMMARY.json")
        if not _embedded_hash_valid(summary, "run_hash"):
            raise ValueError(f"run_hash_invalid:{split}")
        for row in summary["rows"]:
            rows_by_key[(row["task_id"], row["condition"])] = (
                experiment / "runs" / split / row["trial_id"]
            )

    origins = {
        str(row["task_id"]): row
        for row in read_json(experiment / "stage" / "CASE_ORIGINS.json")["rows"]
    }
    source = _source()
    selection_rows: list[dict[str, Any]] = []
    for task_id in splits["all"]:
        origin = origins[task_id]
        case = experiment / "stage" / "public" / "cases" / task_id
        _, parent = _discover_package(case, str(origin["skill_name"]))
        raw = experiment / "stage" / "current_candidates" / task_id / "package"
        source_raw_freeze = read_json(
            source / "selected" / "all" / "raw-proposal" / task_id / "SELECTION_FREEZE.json"
        )
        raw_report = read_json(experiment / "stage" / "closure_reports" / f"{task_id}.json")
        safe_fallback = raw if raw_report.get("decision") == ACCEPT else parent
        selected: dict[str, tuple[Path, str, str | None, int]] = {
            "no-evolution": (parent, "frozen_parent_package", None, 0),
            "raw-one-shot": (
                raw,
                "imported_frozen_first_proposal",
                source_raw_freeze["selection_freeze_hash"],
                1,
            ),
        }
        for condition in CALL_CONDITIONS:
            run_root = rows_by_key[(task_id, condition)]
            freeze = read_json(run_root / "CANDIDATE_FREEZE.json")
            candidate = run_root / "candidate" / "package"
            if candidate.is_dir() and freeze.get("gate_decision") == ACCEPT:
                selected[condition] = (
                    candidate,
                    "equal_budget_revision_gate_accepted",
                    freeze["candidate_freeze_hash"],
                    2,
                )
            else:
                selected[condition] = (
                    safe_fallback,
                    "revision_rejected_fallback_first_proposal"
                    if safe_fallback == raw
                    else "revision_rejected_fallback_parent",
                    freeze["candidate_freeze_hash"],
                    2,
                )
        for condition in CONDITIONS:
            package, reason, source_hash, model_calls = selected[condition]
            frozen = _write_selection(
                experiment,
                condition,
                task_id,
                package,
                reason=reason,
                source_hash=source_hash,
                model_calls=model_calls,
            )
            selection_rows.append(
                {
                    "task_id": task_id,
                    "condition": condition,
                    "batch": str(origin["batch"]),
                    "reason": reason,
                    "selection_freeze_hash": frozen["selection_freeze_hash"],
                }
            )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "all_registry105_candidates_frozen_before_hidden_evaluation",
        "task_count": 105,
        "row_count": len(selection_rows),
        "conditions": list(CONDITIONS),
        "condition_counts": dict(Counter(row["condition"] for row in selection_rows)),
        "candidate_frozen_before_hidden_evaluation": True,
        "hidden_results_used_for_selection": False,
        "retrospective_registry105_development_experiment": True,
        "rows": selection_rows,
    }
    summary["selection_hash"] = canonical_json_hash(summary)
    write_json(experiment / "selected" / "all" / "SELECTION_SUMMARY.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--experiment-root", type=Path, required=True)
    prepare.add_argument("--model", default=MODEL)
    prepare.add_argument("--base-url", default=BASE_URL)
    prepare.add_argument("--timeout", type=int, default=600)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--experiment-root", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--experiment-root", type=Path, required=True)
    run.add_argument("--split", choices=("smoke", "formal"), required=True)
    run.add_argument("--workers", type=int, default=6)
    freeze = commands.add_parser("freeze-selections")
    freeze.add_argument("--experiment-root", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = prepare_stage(
            args.experiment_root,
            model=args.model,
            base_url=args.base_url,
            timeout=args.timeout,
        )
    elif args.command == "preflight":
        result = zero_call_preflight(args.experiment_root)
    elif args.command == "freeze-selections":
        result = freeze_selections(args.experiment_root)
    else:
        api_key = os.environ.get("OPENAI_API_KEY") or getpass.getpass("OpenLux API key: ")
        if not api_key:
            raise ValueError("api_key_required")
        result = run_calls(
            args.experiment_root,
            split=args.split,
            api_key=api_key,
            workers=args.workers,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
