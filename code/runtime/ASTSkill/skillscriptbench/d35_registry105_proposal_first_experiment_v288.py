from __future__ import annotations

import argparse
import getpass
import http.client
import json
import platform
import random
import shutil
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from bvi_skill_evo.proposal_first_ast_gate_v280 import build_application_failure_gate
from bvi_skill_evo.proposal_first_structural_gate_v287 import (
    ACCEPT,
    build_posthoc_structural_gate,
)
from bvi_skill_evo.python_span_patch_v246 import (
    PYTHON_SPAN_PATCH_TOOL,
    apply_python_span_patch,
)
from bvi_skill_evo.universal_span_patch_v286 import (
    SCRIPT_SUFFIXES,
    UNIVERSAL_SPAN_PATCH_TOOL,
    apply_universal_span_patch,
)
from skillscriptbench.io_utils import (
    canonical_json_hash,
    copy_tree_clean,
    hash_tree,
    read_json,
    sha256_bytes,
    sha256_file,
    write_json,
)
from skillscriptbench.package_matrix_conditions_v65 import _patch_arguments, _utc_now


SCHEMA_VERSION = "2.88-d35-registry105-proposal-first-structural-gate-v1"
MODEL = "gpt-5.5"
BASE_URL = "https://api.openlux.ai/v1"
CONDITIONS = ("no-evolution", "raw-proposal", "proposal-first-ast")
REGISTRY = (
    "final_results/multi_view_structural_skill_evo/"
    "20260815_stage121_cumulative_registry105_v1/CUMULATIVE_REGISTRY.json"
)
D34_SOURCE = (
    "final_results/multi_view_structural_skill_evo/"
    "20260815_stage126_d34_binding_corrected_v1"
)
SOURCE_EXPERIMENTS = {
    "D22": "final_results/multi_view_structural_skill_evo/20260814_stage69_d22_verifier_granularity",
    "D24": "final_results/multi_view_structural_skill_evo/20260814_stage73_d24_batch1_recovery",
    "D25": "final_results/multi_view_structural_skill_evo/20260814_stage76_d25_batch2_normalized",
    "D26": "final_results/multi_view_structural_skill_evo/20260814_stage79_d26_batch3_protocol_v2",
    "D27": "final_results/multi_view_structural_skill_evo/20260814_stage82_d27_batch4_multi_contract",
    "D28": "final_results/multi_view_structural_skill_evo/20260815_stage88_d28_batch5_multilang_v4",
    "D29": "final_results/multi_view_structural_skill_evo/20260815_stage92_d29_batch6_formal_v1",
    "D30": "final_results/multi_view_structural_skill_evo/20260815_stage99_d30_batch7_protocol_recovery_v1",
    "D31": "final_results/multi_view_structural_skill_evo/20260815_stage106_d31_batch8_compact_ast_v1",
    "D32": "final_results/multi_view_structural_skill_evo/20260815_stage113_d32_batch9_heldout_v1",
    "D33": "final_results/multi_view_structural_skill_evo/20260815_stage119_d33_batch10_residual_v3",
}
IMPORTED_BATCHES = {"D31", "D32", "D33"}
SMOKE_TASK_IDS = ("d25-tokenize-limit-effect", "d28-js-mp4-extension-filter")
FORBIDDEN_PROMPT_MARKERS = (
    "_private/",
    "EVALUATION_SPEC",
    "TASK_VERIFIER_FEEDBACK",
    "VISIBLE_PYTHON_AST_FINDINGS",
    "PYTHON_AST_FACTS",
    "WORKFLOW_AST_FACTS",
    "MUTATION_LABEL",
    "SOURCE_TEST_RESULT",
    "source_tests/",
    "oracle_package",
    '"path": "scripts/tests/',
)
CODE_PATHS = (
    "skillscriptbench/d35_registry105_proposal_first_experiment_v288.py",
    "bvi_skill_evo/proposal_first_structural_gate_v287.py",
    "bvi_skill_evo/universal_span_patch_v286.py",
    "bvi_skill_evo/proposal_first_ast_gate_v280.py",
    "bvi_skill_evo/proposal_first_ast_gate_v283.py",
    "bvi_skill_evo/python_span_patch_v246.py",
    "bvi_skill_evo/python_span_patch_v241.py",
    "bvi_skill_evo/python_span_patch_v237.py",
    "skillscriptbench/multilang_structural_v66.py",
    "skillscriptbench/js_parser/extract_structural_nodes_v66.mjs",
    "skillscriptbench/io_utils.py",
)
PROTOCOL_TEXT = """# Proposal-first verifier-independent executable-skill evolution

Generate one repair proposal from the complete visible skill package without pre-ranked code
locations. A package-derived structural safety gate may map the resulting diff to concrete language
AST nodes, inspect local caller/callee impact, preserve public interfaces, and request at most one
bounded revision for structural invalidity. The proposal and gate receive no task verdict, source
test, expected behavior trace, reward, mutation label, gold source, oracle, or hidden evaluation
result. All candidate selections are frozen before behavioral evaluation.
"""


def _workspace() -> Path:
    return Path(__file__).resolve().parents[1]


def _embedded_hash_valid(payload: dict[str, Any], field: str) -> bool:
    expected = str(payload.get(field) or "")
    body = dict(payload)
    body.pop(field, None)
    return bool(expected) and canonical_json_hash(body) == expected


def _code_hashes() -> dict[str, str]:
    workspace = _workspace()
    missing = [path for path in CODE_PATHS if not (workspace / path).is_file()]
    if missing:
        raise FileNotFoundError(f"experiment_code_missing:{missing}")
    return {path: sha256_file(workspace / path) for path in CODE_PATHS}


def _discover_package(case: Path, expected_name: str | None = None) -> tuple[str, Path]:
    skills = case / "task" / "environment" / "skills"
    if expected_name and (skills / expected_name / "SKILL.md").is_file():
        return expected_name, skills / expected_name
    matches = sorted(path.parent for path in skills.glob("*/SKILL.md"))
    if len(matches) != 1:
        raise ValueError(f"expected_one_skill_package:{case.name}:{len(matches)}")
    return matches[0].name, matches[0]


def _package_payload(package: Path, language: str) -> list[dict[str, Any]]:
    editable_suffixes = {
        "python": {".py"},
        "javascript": {".js", ".mjs", ".cjs"},
        "typescript": {".ts"},
        "shell": {".sh", ".bash"},
    }[language]
    rows: list[dict[str, Any]] = []
    for path in sorted(value for value in package.rglob("*") if value.is_file()):
        relative = path.relative_to(package).as_posix()
        parts = Path(relative).parts
        if relative != "SKILL.md" and not relative.startswith("scripts/"):
            continue
        if (
            "tests" in parts
            or "test" in parts
            or path.name.startswith("test_")
            or ".test." in path.name
            or "_test." in path.name
            or path.suffix == ".pyc"
        ):
            continue
        data = path.read_bytes()
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            rows.append(
                {
                    "path": relative,
                    "editable": False,
                    "encoding": "binary-omitted",
                    "sha256": sha256_bytes(data),
                }
            )
            continue
        rows.append(
            {
                "path": relative,
                "editable": relative.startswith("scripts/")
                and path.suffix.lower() in editable_suffixes,
                "encoding": "utf-8",
                "sha256": sha256_bytes(data),
                "numbered_content": "\n".join(
                    f"{index}: {line}"
                    for index, line in enumerate(content.splitlines(), start=1)
                ),
            }
        )
    return rows


def _proposal_prompt(case: Path, package: Path, language: str) -> str:
    task = (case / "task" / "task.md").read_text(encoding="utf-8")
    protocol = (case / "evolution" / "PROTOCOL.md").read_text(encoding="utf-8")
    extension = {
        "python": "scripts/*.py",
        "javascript": "JavaScript files under scripts/",
        "typescript": "TypeScript files under scripts/",
        "shell": "Shell files under scripts/",
    }[language]
    instruction = (
        "Improve one reusable executable Agent Skill package for future tasks of the same class. "
        "Read the complete visible request, SKILL.md, and scripts, find the most likely package defect, "
        "and propose one bounded repair. No AST ranking or verifier diagnosis is supplied. Preserve public "
        "interfaces, defaults, documentation, and unrelated behavior. Call submit_skill_patch exactly once "
        "with zero, one, or two replace_span edits. Copy a unique exact visible source span into "
        "observed_source, keep each observed span at most 30 lines and replacement at most 40 lines, and keep "
        f"all changes inside {extension}. Leave target_node_id and expected_node_sha256 empty. Do not request "
        "tests, task verdicts, expected outputs, mutation labels, gold code, oracle behavior, rewards, AST hints, "
        "or verifier feedback."
    )
    return "\n\n".join(
        (
            instruction,
            "USER_REQUEST.md\n" + task,
            "EVOLUTION_PROTOCOL.md\n" + protocol,
            "VISIBLE_EXECUTABLE_SKILL_PACKAGE.json\n"
            + json.dumps(
                _package_payload(package, language), indent=2, sort_keys=True, ensure_ascii=True
            ),
        )
    ) + "\n"


def _revision_prompt(
    case: Path,
    package: Path,
    language: str,
    previous_response: str,
    gate: dict[str, Any],
) -> str:
    task = (case / "task" / "task.md").read_text(encoding="utf-8")
    packet = {
        key: gate.get(key)
        for key in (
            "decision",
            "failed_checks",
            "application_error",
            "changed_nodes_after",
            "impact_closure",
            "call_edges_added",
            "call_edges_removed",
            "removed_declarations",
            "unresolved_name_findings",
            "parse_failures",
            "claim_boundary",
        )
    }
    packet.update({"task_verifier_consumed": False, "hidden_artifacts_consumed": False})
    instruction = (
        "Revise one prior package patch using only the complete visible package and the answer-free post-hoc "
        "structural safety report. The report maps the proposed diff to language AST nodes and local impact; "
        "it is not a task verdict and does not reveal correct code. Submit a complete replacement patch "
        "against the ORIGINAL visible package. Preserve public interfaces and unrelated behavior. Call "
        "submit_skill_patch exactly once with zero, one, or two bounded replace_span edits, using unique exact "
        "source spans and leaving AST id fields empty."
    )
    return "\n\n".join(
        (
            instruction,
            "USER_REQUEST.md\n" + task,
            "VISIBLE_EXECUTABLE_SKILL_PACKAGE.json\n"
            + json.dumps(
                _package_payload(package, language), indent=2, sort_keys=True, ensure_ascii=True
            ),
            "PREVIOUS_PATCH.json\n" + previous_response,
            "POSTHOC_STRUCTURAL_SAFETY_REPORT.json\n"
            + json.dumps(packet, indent=2, sort_keys=True, ensure_ascii=True),
        )
    ) + "\n"


def _source_rows() -> list[dict[str, Any]]:
    workspace = _workspace()
    registry_path = workspace / REGISTRY
    registry = read_json(registry_path)
    if registry.get("formal_case_count") != 105 or not _embedded_hash_valid(
        registry, "registry_hash"
    ):
        raise ValueError("valid_registry105_required")
    source_cache: dict[str, tuple[Path, dict[str, Any], dict[str, dict[str, Any]]]] = {}
    rows: list[dict[str, Any]] = []
    for task in registry["tasks"]:
        batch = str(task["batch_id"])
        if batch not in SOURCE_EXPERIMENTS:
            raise ValueError(f"source_experiment_missing:{batch}")
        if batch not in source_cache:
            experiment = (workspace / SOURCE_EXPERIMENTS[batch]).resolve()
            plan = read_json(experiment / "stage" / "FROZEN_PLAN.json")
            if not _embedded_hash_valid(plan, "plan_hash"):
                raise ValueError(f"source_plan_hash_invalid:{batch}")
            if canonical_json_hash(hash_tree(experiment / "stage" / "public")) != plan.get(
                "public_tree_hash"
            ):
                raise ValueError(f"source_public_tree_changed:{batch}")
            manifest = read_json(experiment / "stage" / "public" / "manifest.json")
            source_cache[batch] = (
                experiment,
                plan,
                {str(row["task_id"]): row for row in manifest["cases"]},
            )
        experiment, plan, manifest = source_cache[batch]
        task_id = str(task["task_id"])
        if task_id not in manifest:
            raise ValueError(f"task_missing_from_source_manifest:{batch}:{task_id}")
        source_case = experiment / "stage" / "public" / "cases" / task_id
        skill_name, _ = _discover_package(source_case, manifest[task_id].get("skill_name"))
        language = str(task.get("language") or "python").lower()
        rows.append(
            {
                "task_id": task_id,
                "batch": batch,
                "language": language,
                "skill_name": skill_name,
                "source_experiment": str(experiment),
                "source_plan_hash": plan["plan_hash"],
                "source_case": str(source_case),
                "imported_from_d34": batch in IMPORTED_BATCHES,
            }
        )
    rows.sort(key=lambda row: row["task_id"])
    if len(rows) != 105 or len({row["task_id"] for row in rows}) != 105:
        raise ValueError(f"expected_105_unique_cases:{len(rows)}")
    return rows


def prepare_stage(
    experiment_root: str | Path,
    *,
    model: str = MODEL,
    base_url: str = BASE_URL,
    timeout: int = 600,
) -> dict[str, Any]:
    if model != MODEL:
        raise ValueError("d35_requires_exact_gpt_5_5")
    experiment = Path(experiment_root).resolve()
    if experiment.exists():
        raise FileExistsError(experiment)
    public = experiment / "stage" / "public"
    prompts = experiment / "stage" / "proposal_prompts"
    audit = experiment / "_audit"
    prompts.mkdir(parents=True)
    audit.mkdir(parents=True)
    origins = _source_rows()
    calls: list[dict[str, Any]] = []
    manifest_rows: list[dict[str, Any]] = []
    for origin in origins:
        task_id = origin["task_id"]
        source_case = Path(origin["source_case"])
        target_case = public / "cases" / task_id
        copy_tree_clean(source_case / "task", target_case / "task")
        (target_case / "evolution").mkdir(parents=True)
        (target_case / "evolution" / "PROTOCOL.md").write_text(PROTOCOL_TEXT, encoding="utf-8")
        _, package = _discover_package(target_case, origin["skill_name"])
        if not origin["imported_from_d34"]:
            prompt = prompts / f"{task_id}--proposal--r1.txt"
            prompt.write_text(
                _proposal_prompt(target_case, package, origin["language"]), encoding="utf-8"
            )
            calls.append(
                {
                    "trial_id": f"{task_id}--proposal--r1",
                    "task_id": task_id,
                    "batch": origin["batch"],
                    "language": origin["language"],
                    "skill_name": origin["skill_name"],
                    "prompt_path": str(prompt.relative_to(experiment)),
                    "prompt_sha256": sha256_file(prompt),
                    "prompt_bytes": prompt.stat().st_size,
                    "source_package_hash": canonical_json_hash(hash_tree(package)),
                }
            )
        manifest_rows.append(
            {
                "task_id": task_id,
                "batch": origin["batch"],
                "language": origin["language"],
                "skill_name": origin["skill_name"],
                "imported_from_d34": origin["imported_from_d34"],
            }
        )
    write_json(public / "manifest.json", {"schema_version": SCHEMA_VERSION, "cases": manifest_rows})
    write_json(experiment / "stage" / "CASE_ORIGINS.json", {"schema_version": SCHEMA_VERSION, "rows": origins})
    new_ids = {row["task_id"] for row in calls}
    smoke = [task_id for task_id in SMOKE_TASK_IDS if task_id in new_ids]
    imported = sorted(row["task_id"] for row in origins if row["imported_from_d34"])
    splits: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "smoke": smoke,
        "formal": sorted(new_ids - set(smoke)),
        "imported": imported,
        "all": sorted(row["task_id"] for row in origins),
        "imported_candidate_source": str((_workspace() / D34_SOURCE).resolve()),
    }
    splits["selection_hash"] = canonical_json_hash(splits)
    write_json(experiment / "stage" / "SPLITS.json", splits)
    random.Random("d35-registry105-proposal-first-v288").shuffle(calls)
    source_d34 = (_workspace() / D34_SOURCE).resolve()
    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "frozen_before_new_proposal_calls",
        "created_at": _utc_now(),
        "model": model,
        "base_url": base_url,
        "temperature": 0,
        "timeout": timeout,
        "max_attempts": 1,
        "case_count": 105,
        "new_proposal_call_count": len(calls),
        "imported_d34_call_count": len(imported),
        "proposal_calls": calls,
        "conditions": list(CONDITIONS),
        "public_tree_hash": canonical_json_hash(hash_tree(public)),
        "prompts_tree_hash": canonical_json_hash(hash_tree(prompts)),
        "origins_sha256": sha256_file(experiment / "stage" / "CASE_ORIGINS.json"),
        "splits_sha256": sha256_file(experiment / "stage" / "SPLITS.json"),
        "registry_sha256": sha256_file(_workspace() / REGISTRY),
        "d34_source_replay_sha256": sha256_file(source_d34 / "CORRECTED_GATE_REPLAY.json"),
        "code_sha256": _code_hashes(),
        "tool_schema_hashes": {
            "python": canonical_json_hash(PYTHON_SPAN_PATCH_TOOL),
            "multilang": canonical_json_hash(UNIVERSAL_SPAN_PATCH_TOOL),
        },
        "proposal_sees_complete_skill_md_and_runtime_scripts": True,
        "proposal_excludes_tests_and_private_artifacts": True,
        "proposal_sees_ast_ranking": False,
        "proposal_sees_task_verifier": False,
        "posthoc_gate_uses_candidate_and_visible_package_only": True,
        "candidate_frozen_before_hidden_evaluation": True,
        "hidden_evaluator_loaded_during_generation": False,
        "runtime": {"python": platform.python_version(), "platform": platform.platform()},
        "credential_persisted": False,
    }
    if len(calls) != 78 or len(imported) != 27:
        raise ValueError(f"expected_78_new_27_imported:{len(calls)}:{len(imported)}")
    plan["plan_hash"] = canonical_json_hash(plan)
    write_json(experiment / "stage" / "FROZEN_PLAN.json", plan)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "ready_for_zero_call_preflight",
        "case_count": 105,
        "new_model_call_count": 78,
        "imported_model_call_count": 27,
        "smoke_count": len(smoke),
        "formal_new_count": 78 - len(smoke),
        "plan_hash": plan["plan_hash"],
        "model_calls": 0,
    }
    result["prepare_hash"] = canonical_json_hash(result)
    write_json(audit / "PREPARE_RECORD.json", result)
    return result


def _validate_stage(experiment: Path) -> tuple[dict[str, Any], dict[str, bool]]:
    plan = read_json(experiment / "stage" / "FROZEN_PLAN.json")
    splits = read_json(experiment / "stage" / "SPLITS.json")
    origins = read_json(experiment / "stage" / "CASE_ORIGINS.json")
    calls = plan.get("proposal_calls") or []
    checks = {
        "plan_hash": _embedded_hash_valid(plan, "plan_hash"),
        "status": plan.get("status") == "frozen_before_new_proposal_calls",
        "exact_model": plan.get("model") == MODEL,
        "single_transport_attempt": plan.get("max_attempts") == 1,
        "case_count": plan.get("case_count") == 105,
        "new_call_count": len(calls) == plan.get("new_proposal_call_count") == 78,
        "imported_count": len(splits.get("imported") or [])
        == plan.get("imported_d34_call_count")
        == 27,
        "unique_all_tasks": len(set(splits.get("all") or [])) == 105,
        "origin_count": len(origins.get("rows") or []) == 105,
        "public_tree": canonical_json_hash(hash_tree(experiment / "stage" / "public"))
        == plan.get("public_tree_hash"),
        "prompts_tree": canonical_json_hash(hash_tree(experiment / "stage" / "proposal_prompts"))
        == plan.get("prompts_tree_hash"),
        "origins": sha256_file(experiment / "stage" / "CASE_ORIGINS.json")
        == plan.get("origins_sha256"),
        "splits": sha256_file(experiment / "stage" / "SPLITS.json")
        == plan.get("splits_sha256"),
        "registry": sha256_file(_workspace() / REGISTRY) == plan.get("registry_sha256"),
        "d34_source": sha256_file(_workspace() / D34_SOURCE / "CORRECTED_GATE_REPLAY.json")
        == plan.get("d34_source_replay_sha256"),
        "code_hashes": all(
            (_workspace() / path).is_file()
            and sha256_file(_workspace() / path) == digest
            for path, digest in plan.get("code_sha256", {}).items()
        ),
        "tool_schemas": plan.get("tool_schema_hashes")
        == {
            "python": canonical_json_hash(PYTHON_SPAN_PATCH_TOOL),
            "multilang": canonical_json_hash(UNIVERSAL_SPAN_PATCH_TOOL),
        },
        "private_not_copied": not (experiment / "stage" / "_private").exists(),
        "ast_packets_not_copied": not any(
            path.name.endswith("AST_FACTS.json")
            for path in (experiment / "stage" / "public").rglob("*")
        ),
    }
    prompt_checks = []
    prompt_test_exposure_checks = []
    for call in calls:
        prompt = experiment / call["prompt_path"]
        text = prompt.read_text(encoding="utf-8")
        prompt_checks.append(
            sha256_file(prompt) == call["prompt_sha256"]
            and not any(marker in text for marker in FORBIDDEN_PROMPT_MARKERS)
            and call["task_id"] not in text
        )
        marker = "VISIBLE_EXECUTABLE_SKILL_PACKAGE.json\n"
        try:
            package_rows = json.loads(text.split(marker, 1)[1])
        except (IndexError, json.JSONDecodeError):
            prompt_test_exposure_checks.append(False)
        else:
            exposed_paths = [Path(str(row.get("path") or "")) for row in package_rows]
            prompt_test_exposure_checks.append(
                not any(
                    "tests" in path.parts
                    or "test" in path.parts
                    or path.name.startswith("test_")
                    or ".test." in path.name
                    or "_test." in path.name
                    for path in exposed_paths
                )
            )
    checks["prompt_isolation"] = all(prompt_checks)
    checks["tests_not_exposed_in_prompts"] = all(prompt_test_exposure_checks)
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
        "model_calls": 0,
        "credential_persisted": False,
    }
    result["preflight_hash"] = canonical_json_hash(result)
    write_json(experiment / "_audit" / "ZERO_CALL_PREFLIGHT.json", result)
    if result["status"] != "pass":
        raise ValueError(
            "zero_call_preflight_failed:"
            + ",".join(name for name, passed in checks.items() if not passed)
        )
    return result


def _call_completion(
    *, api_key: str, plan: dict[str, Any], prompt: str, language: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any]]:
    tool = PYTHON_SPAN_PATCH_TOOL if language == "python" else UNIVERSAL_SPAN_PATCH_TOOL
    request_payload = {
        "model": plan["model"],
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You repair one visible Agent Skill package. Call submit_skill_patch exactly once, "
                    "including an empty edits list when no bounded repair is justified."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "tools": [tool],
        "tool_choice": {"type": "function", "function": {"name": "submit_skill_patch"}},
    }
    encoded = json.dumps(request_payload).encode("utf-8")
    attempts: list[dict[str, Any]] = []
    response_payload = None
    started = time.monotonic()
    request = urllib.request.Request(
        f"{str(plan['base_url']).rstrip('/')}/chat/completions",
        data=encoded,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=int(plan["timeout"])) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
            status = response.status
        attempts.append(
            {
                "attempt": 1,
                "status": "success",
                "http_status": status,
                "elapsed_seconds": time.monotonic() - started,
            }
        )
    except urllib.error.HTTPError as exc:
        attempts.append(
            {
                "attempt": 1,
                "status": "http_error",
                "http_status": exc.code,
                "elapsed_seconds": time.monotonic() - started,
                "error": exc.read().decode("utf-8", errors="replace")[:2000],
            }
        )
    except (
        TimeoutError,
        urllib.error.URLError,
        http.client.HTTPException,
        ConnectionError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        attempts.append(
            {
                "attempt": 1,
                "status": "transport_or_response_error",
                "http_status": None,
                "elapsed_seconds": time.monotonic() - started,
                "error": f"{type(exc).__name__}:{exc}",
            }
        )
    return response_payload, attempts, request_payload


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
    case = experiment / "stage" / "public" / "cases" / call["task_id"]
    _, source_package = _discover_package(case, call["skill_name"])
    application = None
    application_error = parse_error
    if response is not None and parse_error is None:
        try:
            if call["language"] == "python":
                application = apply_python_span_patch(
                    content,
                    source_package,
                    temporary / "candidate" / "package",
                    visible_ast_facts=None,
                    require_ast_binding=False,
                )
            else:
                application = apply_universal_span_patch(
                    content, source_package, temporary / "candidate" / "package"
                )
            write_json(temporary / "RESPONSE_APPLICATION.json", application)
        except Exception as exc:
            application_error = f"{type(exc).__name__}:{exc}"
            shutil.rmtree(temporary / "candidate", ignore_errors=True)
    candidate = temporary / "candidate" / "package"
    if application is not None and candidate.is_dir():
        gate = build_posthoc_structural_gate(
            source_package,
            candidate,
            (case / "task" / "task.md").read_text(encoding="utf-8"),
        )
    else:
        gate = build_application_failure_gate(application_error or "provider_unavailable")
    write_json(temporary / "POSTHOC_STRUCTURAL_GATE.json", gate)
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
        "gate_sha256": sha256_file(temporary / "POSTHOC_STRUCTURAL_GATE.json"),
        "candidate_tree_hash": canonical_json_hash(hash_tree(candidate))
        if candidate.is_dir()
        else None,
        "gate_decision": gate["decision"],
        "application_error": application_error,
        "task_verifier_feedback_used": False,
        "visible_ast_facts_used": False,
        "hidden_evaluator_loaded_during_generation": False,
    }
    freeze["candidate_freeze_hash"] = canonical_json_hash(freeze)
    write_json(temporary / "CANDIDATE_FREEZE.json", freeze)
    usage = (response or {}).get("usage") or {}
    record = {
        "schema_version": SCHEMA_VERSION,
        "trial_id": call["trial_id"],
        "task_id": call["task_id"],
        "language": call["language"],
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


def _run_calls(
    experiment: Path,
    calls: list[dict[str, Any]],
    output: Path,
    *,
    api_key: str,
    plan: dict[str, Any],
    split: str,
    phase: str,
    workers: int,
) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    rows: list[dict[str, Any]] = []
    if calls:
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
        "status": "complete" if calls else "complete_no_calls",
        "phase": phase,
        "split": split,
        "trial_count": len(rows),
        "completed_model_response_count": sum(row["response_id"] is not None for row in rows),
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


def run_proposals(
    experiment_root: str | Path, *, split: str, api_key: str, workers: int = 6
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
    calls = [row for row in plan["proposal_calls"] if row["task_id"] in task_ids]
    return _run_calls(
        experiment,
        calls,
        experiment / "runs" / "proposal" / split,
        api_key=api_key,
        plan=plan,
        split=split,
        phase="proposal",
        workers=workers,
    )


def prepare_revisions(experiment_root: str | Path, *, split: str) -> dict[str, Any]:
    experiment = Path(experiment_root).resolve()
    plan, checks = _validate_stage(experiment)
    if not all(checks.values()):
        raise ValueError("stage_changed_before_revision_freeze")
    proposal_root = experiment / "runs" / "proposal" / split
    proposal_summary = read_json(proposal_root / "BATCH_RUN_SUMMARY.json")
    if not _embedded_hash_valid(proposal_summary, "run_hash"):
        raise ValueError("proposal_summary_hash_invalid")
    prompts = experiment / "stage" / "revision_prompts" / split
    if prompts.exists():
        raise FileExistsError(prompts)
    prompts.mkdir(parents=True)
    calls: list[dict[str, Any]] = []
    source_calls = {row["task_id"]: row for row in plan["proposal_calls"]}
    for row in proposal_summary["rows"]:
        if row["gate_decision"] == ACCEPT:
            continue
        source_call = source_calls[row["task_id"]]
        first = proposal_root / source_call["trial_id"]
        gate = read_json(first / "POSTHOC_STRUCTURAL_GATE.json")
        case = experiment / "stage" / "public" / "cases" / row["task_id"]
        _, package = _discover_package(case, source_call["skill_name"])
        prompt = prompts / f"{row['task_id']}--structural-revision--r1.txt"
        prompt.write_text(
            _revision_prompt(
                case,
                package,
                source_call["language"],
                (first / "raw_response.txt").read_text(encoding="utf-8"),
                gate,
            ),
            encoding="utf-8",
        )
        calls.append(
            {
                "trial_id": f"{row['task_id']}--structural-revision--r1",
                "task_id": row["task_id"],
                "batch": source_call["batch"],
                "language": source_call["language"],
                "skill_name": source_call["skill_name"],
                "prompt_path": str(prompt.relative_to(experiment)),
                "prompt_sha256": sha256_file(prompt),
                "prompt_bytes": prompt.stat().st_size,
                "source_proposal_freeze_hash": read_json(first / "CANDIDATE_FREEZE.json")[
                    "candidate_freeze_hash"
                ],
                "source_gate_hash": gate["gate_hash"],
            }
        )
    revision_plan = {
        "schema_version": SCHEMA_VERSION,
        "status": "frozen_before_revision_calls",
        "created_at": _utc_now(),
        "split": split,
        "source_plan_hash": plan["plan_hash"],
        "source_proposal_run_hash": proposal_summary["run_hash"],
        "model": plan["model"],
        "revision_call_count": len(calls),
        "revision_calls": calls,
        "prompts_tree_hash": canonical_json_hash(hash_tree(prompts)),
        "task_verifier_feedback_used": False,
        "hidden_evaluator_loaded": False,
    }
    revision_plan["revision_plan_hash"] = canonical_json_hash(revision_plan)
    write_json(experiment / "stage" / f"REVISION_PLAN_{split}.json", revision_plan)
    return revision_plan


def run_revisions(
    experiment_root: str | Path, *, split: str, api_key: str, workers: int = 6
) -> dict[str, Any]:
    experiment = Path(experiment_root).resolve()
    revision_plan = read_json(experiment / "stage" / f"REVISION_PLAN_{split}.json")
    if not _embedded_hash_valid(revision_plan, "revision_plan_hash"):
        raise ValueError("revision_plan_hash_invalid")
    plan = read_json(experiment / "stage" / "FROZEN_PLAN.json")
    return _run_calls(
        experiment,
        revision_plan["revision_calls"],
        experiment / "runs" / "revision" / split,
        api_key=api_key,
        plan=plan,
        split=split,
        phase="revision",
        workers=workers,
    )


def _write_selection(
    experiment: Path,
    condition: str,
    task_id: str,
    package: Path,
    *,
    reason: str,
    source_freeze_hash: str | None,
    model_calls: int,
    origin: str,
) -> dict[str, Any]:
    destination = experiment / "selected" / "all" / condition / task_id
    copy_tree_clean(package, destination / "package")
    freeze = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "condition": condition,
        "split": "all",
        "origin": origin,
        "reason": reason,
        "source_candidate_freeze_hash": source_freeze_hash,
        "model_calls": model_calls,
        "selected_tree_hash": canonical_json_hash(hash_tree(destination / "package")),
        "candidate_frozen_before_hidden_evaluation": True,
        "hidden_feedback_used_for_selection": False,
        "task_verifier_feedback_used": False,
    }
    freeze["selection_freeze_hash"] = canonical_json_hash(freeze)
    write_json(destination / "SELECTION_FREEZE.json", freeze)
    return freeze


def _d34_selection(task_id: str, condition: str) -> tuple[Path, dict[str, Any]]:
    root = _workspace() / D34_SOURCE / "selected"
    matches = list(root.glob(f"*/{condition}/{task_id}"))
    if len(matches) != 1:
        raise ValueError(f"d34_selection_not_unique:{condition}:{task_id}:{len(matches)}")
    return matches[0] / "package", read_json(matches[0] / "SELECTION_FREEZE.json")


def freeze_selections(experiment_root: str | Path) -> dict[str, Any]:
    experiment = Path(experiment_root).resolve()
    plan, checks = _validate_stage(experiment)
    if not all(checks.values()):
        raise ValueError("stage_changed_before_selection")
    splits = read_json(experiment / "stage" / "SPLITS.json")
    origins = {row["task_id"]: row for row in read_json(experiment / "stage" / "CASE_ORIGINS.json")["rows"]}
    call_by_task = {row["task_id"]: row for row in plan["proposal_calls"]}
    proposal_roots: dict[str, Path] = {}
    revision_calls: dict[str, dict[str, Any]] = {}
    for split in ("smoke", "formal"):
        proposal_summary = read_json(
            experiment / "runs" / "proposal" / split / "BATCH_RUN_SUMMARY.json"
        )
        revision_plan = read_json(experiment / "stage" / f"REVISION_PLAN_{split}.json")
        revision_summary = read_json(
            experiment / "runs" / "revision" / split / "BATCH_RUN_SUMMARY.json"
        )
        if not all(
            _embedded_hash_valid(payload, field)
            for payload, field in (
                (proposal_summary, "run_hash"),
                (revision_plan, "revision_plan_hash"),
                (revision_summary, "run_hash"),
            )
        ):
            raise ValueError(f"visible_run_hash_invalid:{split}")
        for task_id in splits[split]:
            proposal_roots[task_id] = experiment / "runs" / "proposal" / split
        revision_calls.update({row["task_id"]: row for row in revision_plan["revision_calls"]})

    rows: list[dict[str, Any]] = []
    for task_id in splits["all"]:
        origin = origins[task_id]
        case = experiment / "stage" / "public" / "cases" / task_id
        _, parent = _discover_package(case, origin["skill_name"])
        if origin["imported_from_d34"]:
            selected_sources = {
                condition: _d34_selection(task_id, condition) for condition in CONDITIONS
            }
            for condition in CONDITIONS:
                source_package, source_freeze = selected_sources[condition]
                freeze = _write_selection(
                    experiment,
                    condition,
                    task_id,
                    source_package,
                    reason="imported_frozen_d34_selection",
                    source_freeze_hash=source_freeze["selection_freeze_hash"],
                    model_calls=int(source_freeze["model_calls"]),
                    origin="imported_d34",
                )
                rows.append(
                    {
                        "task_id": task_id,
                        "condition": condition,
                        "origin": "imported_d34",
                        "reason": freeze["reason"],
                        "selection_freeze_hash": freeze["selection_freeze_hash"],
                    }
                )
            continue

        call = call_by_task[task_id]
        first_root = proposal_roots[task_id] / call["trial_id"]
        first_freeze = read_json(first_root / "CANDIDATE_FREEZE.json")
        first_candidate = first_root / "candidate" / "package"
        selected = {
            "no-evolution": (parent, "frozen_parent_package", None, 0),
            "raw-proposal": (
                first_candidate if first_candidate.is_dir() else parent,
                "first_proposal_candidate"
                if first_candidate.is_dir()
                else "first_proposal_invalid_fallback_parent",
                first_freeze["candidate_freeze_hash"],
                1,
            ),
        }
        ast_package = parent
        ast_reason = "posthoc_gate_rejected_fallback_parent"
        ast_source = first_freeze["candidate_freeze_hash"]
        ast_calls = 1
        if first_candidate.is_dir() and first_freeze["gate_decision"] == ACCEPT:
            ast_package = first_candidate
            ast_reason = "first_proposal_posthoc_structural_gate_accepted"
        elif task_id in revision_calls:
            revision_call = revision_calls[task_id]
            split = "smoke" if task_id in splits["smoke"] else "formal"
            revision_root = experiment / "runs" / "revision" / split / revision_call["trial_id"]
            revision_freeze = read_json(revision_root / "CANDIDATE_FREEZE.json")
            revision_candidate = revision_root / "candidate" / "package"
            ast_source = revision_freeze["candidate_freeze_hash"]
            ast_calls = 2
            if revision_candidate.is_dir() and revision_freeze["gate_decision"] == ACCEPT:
                ast_package = revision_candidate
                ast_reason = "bounded_revision_posthoc_structural_gate_accepted"
            else:
                ast_reason = "revision_posthoc_gate_rejected_fallback_parent"
        selected["proposal-first-ast"] = (ast_package, ast_reason, ast_source, ast_calls)
        for condition in CONDITIONS:
            package, reason, source_hash, model_calls = selected[condition]
            freeze = _write_selection(
                experiment,
                condition,
                task_id,
                package,
                reason=reason,
                source_freeze_hash=source_hash,
                model_calls=model_calls,
                origin="new_d35",
            )
            rows.append(
                {
                    "task_id": task_id,
                    "condition": condition,
                    "origin": "new_d35",
                    "reason": reason,
                    "selection_freeze_hash": freeze["selection_freeze_hash"],
                }
            )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "all_registry105_candidates_frozen_before_hidden_evaluation",
        "task_count": 105,
        "row_count": len(rows),
        "condition_counts": dict(Counter(row["condition"] for row in rows)),
        "origin_counts": dict(Counter(row["origin"] for row in rows)),
        "candidate_frozen_before_hidden_evaluation": True,
        "hidden_results_used_for_selection": False,
        "rows": rows,
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
    for name in ("run-proposals", "run-revisions"):
        command = commands.add_parser(name)
        command.add_argument("--experiment-root", type=Path, required=True)
        command.add_argument("--split", choices=("smoke", "formal"), required=True)
        command.add_argument("--workers", type=int, default=6)
    revision_prepare = commands.add_parser("prepare-revisions")
    revision_prepare.add_argument("--experiment-root", type=Path, required=True)
    revision_prepare.add_argument("--split", choices=("smoke", "formal"), required=True)
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
    elif args.command == "prepare-revisions":
        result = prepare_revisions(args.experiment_root, split=args.split)
    elif args.command == "freeze-selections":
        result = freeze_selections(args.experiment_root)
    else:
        if args.command == "run-proposals":
            call_count = len(
                set(read_json(args.experiment_root / "stage" / "SPLITS.json")[args.split])
            )
        else:
            call_count = read_json(
                args.experiment_root / "stage" / f"REVISION_PLAN_{args.split}.json"
            )["revision_call_count"]
        api_key = ""
        if call_count:
            api_key = getpass.getpass("OpenLux API key: ")
            if not api_key:
                raise ValueError("api_key_required")
        if args.command == "run-proposals":
            result = run_proposals(
                args.experiment_root,
                split=args.split,
                api_key=api_key,
                workers=args.workers,
            )
        else:
            result = run_revisions(
                args.experiment_root,
                split=args.split,
                api_key=api_key,
                workers=args.workers,
            )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
