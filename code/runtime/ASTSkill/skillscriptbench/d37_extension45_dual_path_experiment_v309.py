from __future__ import annotations

import argparse
import copy
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bvi_skill_evo import python_span_patch_v237 as patch_base
from bvi_skill_evo import python_span_patch_v246 as python_patch
from bvi_skill_evo.proposal_first_ast_gate_v280 import build_application_failure_gate
from bvi_skill_evo.proposal_first_closure_gate_v292 import (
    ACCEPT,
    build_proposal_first_closure_report,
)
from bvi_skill_evo.universal_span_patch_v286 import (
    UNIVERSAL_SPAN_PATCH_TOOL,
    apply_universal_span_patch,
)

from .d36_dual_ast_arbitration_audit_v300 import (
    _candidate_features,
    _coverage_key,
    choose_candidate,
)
from .io_utils import (
    canonical_json_hash,
    copy_tree_clean,
    hash_tree,
    read_json,
    sha256_bytes,
    sha256_file,
    write_json,
)


SCHEMA_VERSION = "3.09-d37-extension45-dual-path-experiment-v1"
MODEL = "gpt-5.5"
BASE_URL = "https://api.openlux.ai/v1"
MAX_EDITS = 3
PRIMARY_CONDITIONS = ("raw-one-shot", "prescriptive-ast")
REVISION_CONDITIONS = ("generic-revision", "ast-closure-revision")
CONDITIONS = (
    "no-evolution",
    *PRIMARY_CONDITIONS,
    *REVISION_CONDITIONS,
    "dual-path-ast",
    "generic-plus-closure-control",
)
FACT_NAMES = ("PYTHON_AST_FACTS.json", "MULTILANG_AST_FACTS.json")
PRIVATE_MARKERS = (
    "_private/",
    "EVALUATION_SPEC",
    "MUTATION_LABEL",
    "source_tests/",
    "oracle_package",
    "mutation_killed_by_test",
)
PATCH_TOOL = copy.deepcopy(UNIVERSAL_SPAN_PATCH_TOOL)
PATCH_TOOL["function"]["description"] = (
    "Submit zero to three bounded source-span replacements inside visible Agent Skill runtime "
    "scripts. Supported files are Python, JavaScript, TypeScript, and Shell."
)
PATCH_TOOL["function"]["parameters"]["properties"]["edits"]["maxItems"] = MAX_EDITS


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _embedded_hash_valid(payload: dict[str, Any], field: str) -> bool:
    expected = str(payload.get(field) or "")
    body = dict(payload)
    body.pop(field, None)
    return bool(expected) and canonical_json_hash(body) == expected


def _configure_edit_limit() -> None:
    patch_base.MAX_EDITS = MAX_EDITS
    python_patch.MAX_EDITS = MAX_EDITS


def _discover_package(case: Path, expected: str) -> Path:
    package = case / "task" / "environment" / "skills" / expected
    if not (package / "SKILL.md").is_file():
        raise FileNotFoundError(f"skill_package_missing:{case.name}:{expected}")
    return package


def _package_payload(package: Path, language: str) -> list[dict[str, Any]]:
    editable_suffixes = {
        "python": {".py"},
        "javascript": {".js", ".mjs", ".cjs"},
        "typescript": {".ts"},
        "shell": {".sh", ".bash"},
    }[language]
    rows = []
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


def _visible_facts(case: Path) -> tuple[dict[str, Any], Path]:
    for name in FACT_NAMES:
        path = case / "evolution" / name
        if path.is_file():
            return read_json(path), path
    raise FileNotFoundError(f"visible_structural_facts_missing:{case.name}")


def _common_sections(case: Path, package: Path, language: str) -> list[str]:
    return [
        "USER_REQUEST.md\n" + (case / "task" / "task.md").read_text(encoding="utf-8"),
        "EVOLUTION_PROTOCOL.md\n"
        + (case / "evolution" / "PROTOCOL.md").read_text(encoding="utf-8"),
        "VISIBLE_EXECUTABLE_SKILL_PACKAGE.json\n"
        + json.dumps(
            _package_payload(package, language),
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
        ),
    ]


def _primary_prompt(
    case: Path, package: Path, language: str, condition: str
) -> str:
    instruction = (
        "Improve one reusable executable Agent Skill package for future tasks of the same class. "
        "Read the complete visible request, SKILL.md, and runtime scripts and repair the smallest coherent "
        "set of defects. Preserve public interfaces, defaults, documentation, and unrelated behavior. "
        f"Call submit_skill_patch exactly once with zero to {MAX_EDITS} bounded replace_span edits. "
        "Each observed_source must be a unique exact visible source span of at most 30 lines and each "
        "replacement at most 40 lines. Keep changes inside visible runtime scripts. Do not request tests, "
        "task verdicts, expected outputs, mutation labels, gold code, oracle behavior, rewards, or verifier feedback."
    )
    sections = [instruction, *_common_sections(case, package, language)]
    if condition == "raw-one-shot":
        sections.append(
            "No structural ranking is supplied. Leave target_node_id and expected_node_sha256 empty."
        )
    elif condition == "prescriptive-ast":
        facts, _ = _visible_facts(case)
        sections.append(
            "VISIBLE_PACKAGE_AST_FINDINGS.json\n"
            + json.dumps(facts, indent=2, sort_keys=True, ensure_ascii=True)
        )
        sections.append(
            "The findings are answer-free review candidates and may contain false positives or omit the best "
            "repair site. Copy node_id and node_sha256/source_sha256 when an edit matches a listed node. When "
            "the full visible package justifies a different site, submit its unique exact observed_source and "
            "leave the AST id fields empty; the post-hoc AST gate will resolve the actual changed nodes."
        )
    else:
        raise ValueError(f"unknown_primary_condition:{condition}")
    return "\n\n".join(sections) + "\n"


def _closure_packet(report: dict[str, Any]) -> dict[str, Any]:
    return {
        key: report.get(key)
        for key in (
            "changed_nodes_before",
            "changed_nodes_after",
            "impact_closure",
            "local_def_use_findings",
            "call_edges_added",
            "call_edges_removed",
            "removed_public_declarations",
            "removed_private_implementation_declarations",
            "unresolved_name_findings",
            "parse_failures",
            "candidate_aware_visible_structural_facts",
            "claim_boundary",
        )
    }


def _revision_prompt(
    case: Path,
    current: Path,
    language: str,
    condition: str,
    report: dict[str, Any],
) -> str:
    instruction = (
        "Review the current candidate for one reusable executable Agent Skill. Read the complete request, "
        "SKILL.md, and current scripts. Submit only an incremental patch against the CURRENT package; zero "
        "edits keeps it. Preserve public interfaces, defaults, documentation, and unrelated behavior. "
        f"Call submit_skill_patch exactly once with zero to {MAX_EDITS} bounded replace_span edits inside "
        "runtime scripts. Leave AST id fields empty. Do not request tests, task verdicts, expected outputs, "
        "mutation labels, gold code, oracle behavior, rewards, or verifier feedback."
    )
    sections = [instruction, *_common_sections(case, current, language)]
    if condition == "ast-closure-revision":
        sections.append(
            "CANDIDATE_AWARE_STRUCTURAL_COMPLETENESS_REPORT.json\n"
            + json.dumps(
                _closure_packet(report), indent=2, sort_keys=True, ensure_ascii=True
            )
        )
        sections.append(
            "This report is a visible-package structural review aid, not a correctness verdict. Resolve "
            "remaining structural risks only when the package and request justify the change."
        )
    elif condition != "generic-revision":
        raise ValueError(f"unknown_revision_condition:{condition}")
    return "\n\n".join(sections) + "\n"


def _code_hashes() -> dict[str, str]:
    workspace = Path(__file__).resolve().parents[1]
    paths = (
        "skillscriptbench/d37_extension45_dual_path_experiment_v309.py",
        "skillscriptbench/d37_hidden_source_test_evaluator_v308.py",
        "bvi_skill_evo/proposal_first_closure_gate_v292.py",
        "bvi_skill_evo/proposal_first_structural_gate_v287.py",
        "bvi_skill_evo/python_span_patch_v246.py",
        "bvi_skill_evo/python_span_patch_v237.py",
        "bvi_skill_evo/universal_span_patch_v286.py",
        "skillscriptbench/d36_dual_ast_arbitration_audit_v300.py",
        "skillscriptbench/io_utils.py",
    )
    return {path: sha256_file(workspace / path) for path in paths}


def prepare_stage(
    benchmark_root: str | Path,
    experiment_root: str | Path,
    *,
    expected_freeze_hash: str,
    model: str = MODEL,
    base_url: str = BASE_URL,
    timeout: int = 600,
) -> dict[str, Any]:
    if model != MODEL:
        raise ValueError("d37_requires_exact_gpt_5_5")
    benchmark = Path(benchmark_root).resolve()
    experiment = Path(experiment_root).resolve()
    if experiment.exists():
        raise FileExistsError(experiment)
    freeze = read_json(benchmark / "FREEZE_MANIFEST.json")
    if not _embedded_hash_valid(freeze, "freeze_hash") or freeze["freeze_hash"] != expected_freeze_hash:
        raise ValueError("benchmark_freeze_invalid")
    if canonical_json_hash(hash_tree(benchmark / "public")) != freeze["public_tree_hash"]:
        raise ValueError("benchmark_public_tree_changed")
    if canonical_json_hash(hash_tree(benchmark / "_private")) != freeze["private_tree_hash"]:
        raise ValueError("benchmark_private_tree_changed")
    public = experiment / "stage" / "public"
    prompts = experiment / "stage" / "primary_prompts"
    prompts.mkdir(parents=True)
    (experiment / "_audit").mkdir()
    copy_tree_clean(benchmark / "public", public)
    manifest = read_json(public / "manifest.json")
    calls = []
    origins = []
    for row in sorted(manifest["cases"], key=lambda item: item["task_id"]):
        task_id = str(row["task_id"])
        case = public / "cases" / task_id
        package = _discover_package(case, str(row["skill_name"]))
        facts, facts_path = _visible_facts(case)
        origins.append(
            {
                "task_id": task_id,
                "language": row["language"],
                "skill_name": row["skill_name"],
                "difficulty": row["difficulty"],
                "package_id": row["package_id"],
                "benchmark_root": str(benchmark),
                "benchmark_freeze_hash": freeze["freeze_hash"],
                "visible_facts_sha256": sha256_file(facts_path),
                "source_package_tree_hash": canonical_json_hash(hash_tree(package)),
            }
        )
        for condition in PRIMARY_CONDITIONS:
            prompt = prompts / f"{task_id}--{condition}--r1.txt"
            prompt.write_text(
                _primary_prompt(case, package, str(row["language"]), condition),
                encoding="utf-8",
            )
            calls.append(
                {
                    "trial_id": f"{task_id}--{condition}--r1",
                    "task_id": task_id,
                    "condition": condition,
                    "phase": "primary",
                    "language": row["language"],
                    "skill_name": row["skill_name"],
                    "prompt_path": prompt.relative_to(experiment).as_posix(),
                    "prompt_sha256": sha256_file(prompt),
                    "prompt_bytes": prompt.stat().st_size,
                    "source_package_tree_hash": canonical_json_hash(hash_tree(package)),
                    "visible_structural_facts_used": condition == "prescriptive-ast",
                }
            )
    smoke = sorted(str(row["task_id"]) for row in manifest["cases"] if row["smoke"])
    all_ids = sorted(str(row["task_id"]) for row in manifest["cases"])
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
        {"schema_version": SCHEMA_VERSION, "rows": origins},
    )
    random.Random("d37-extension45-dual-path-primary-v309").shuffle(calls)
    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "frozen_before_primary_model_calls",
        "created_at": _utc_now(),
        "model": model,
        "base_url": base_url,
        "temperature": 0,
        "timeout": timeout,
        "max_transport_attempts": 1,
        "maximum_patch_edits": MAX_EDITS,
        "case_count": len(all_ids),
        "primary_call_count": len(calls),
        "primary_conditions": list(PRIMARY_CONDITIONS),
        "revision_conditions": list(REVISION_CONDITIONS),
        "selection_conditions": list(CONDITIONS),
        "primary_calls": calls,
        "benchmark_root": str(benchmark),
        "benchmark_freeze_hash": freeze["freeze_hash"],
        "public_tree_hash": canonical_json_hash(hash_tree(public)),
        "prompts_tree_hash": canonical_json_hash(hash_tree(prompts)),
        "origins_sha256": sha256_file(experiment / "stage" / "CASE_ORIGINS.json"),
        "splits_sha256": sha256_file(experiment / "stage" / "SPLITS.json"),
        "code_sha256": _code_hashes(),
        "tool_schema_hash": canonical_json_hash(PATCH_TOOL),
        "candidate_frozen_before_hidden_evaluation": True,
        "hidden_evaluator_loaded_during_generation": False,
        "task_verifier_feedback_used": False,
        "runtime": {"python": platform.python_version(), "platform": platform.platform()},
        "credential_persisted": False,
    }
    if len(all_ids) != 45 or len(smoke) != 2 or len(calls) != 90:
        raise ValueError("d37_primary_cardinality_mismatch")
    plan["plan_hash"] = canonical_json_hash(plan)
    write_json(experiment / "stage" / "FROZEN_PLAN.json", plan)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "ready_for_zero_call_preflight",
        "case_count": 45,
        "smoke_case_count": 2,
        "primary_call_count": 90,
        "plan_hash": plan["plan_hash"],
        "model_calls": 0,
    }
    result["prepare_hash"] = canonical_json_hash(result)
    write_json(experiment / "_audit" / "PREPARE_RECORD.json", result)
    return result


def _validate_stage(experiment: Path) -> tuple[dict[str, Any], dict[str, bool]]:
    plan = read_json(experiment / "stage" / "FROZEN_PLAN.json")
    splits = read_json(experiment / "stage" / "SPLITS.json")
    origins = read_json(experiment / "stage" / "CASE_ORIGINS.json")
    prompts = experiment / "stage" / "primary_prompts"
    checks = {
        "plan_hash": _embedded_hash_valid(plan, "plan_hash"),
        "plan_status": plan.get("status") == "frozen_before_primary_model_calls",
        "exact_model": plan.get("model") == MODEL,
        "one_transport_attempt": plan.get("max_transport_attempts") == 1,
        "case_count": plan.get("case_count") == len(splits.get("all", [])) == 45,
        "primary_call_count": len(plan.get("primary_calls", [])) == 90,
        "smoke_count": len(splits.get("smoke", [])) == 2,
        "origin_count": len(origins.get("rows", [])) == 45,
        "public_tree": canonical_json_hash(hash_tree(experiment / "stage" / "public"))
        == plan.get("public_tree_hash"),
        "prompt_tree": canonical_json_hash(hash_tree(prompts)) == plan.get("prompts_tree_hash"),
        "origins": sha256_file(experiment / "stage" / "CASE_ORIGINS.json")
        == plan.get("origins_sha256"),
        "splits": sha256_file(experiment / "stage" / "SPLITS.json")
        == plan.get("splits_sha256"),
        "code_hashes": all(
            sha256_file(Path(__file__).resolve().parents[1] / path) == digest
            for path, digest in plan.get("code_sha256", {}).items()
        ),
        "tool_schema": plan.get("tool_schema_hash") == canonical_json_hash(PATCH_TOOL),
        "private_not_copied": not (experiment / "stage" / "_private").exists(),
    }
    prompt_ok = []
    for call in plan.get("primary_calls", []):
        prompt = experiment / call["prompt_path"]
        text = prompt.read_text(encoding="utf-8")
        prompt_ok.append(
            sha256_file(prompt) == call["prompt_sha256"]
            and not any(marker in text for marker in PRIVATE_MARKERS)
            and call["task_id"] not in text
        )
    checks["prompt_isolation"] = all(prompt_ok)
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
    api_key: str, plan: dict[str, Any], prompt: str
) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
    payload = {
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
        "tools": [PATCH_TOOL],
        "tool_choice": {"type": "function", "function": {"name": "submit_skill_patch"}},
    }
    request = urllib.request.Request(
        f"{str(plan['base_url']).rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=int(plan["timeout"])) as response:
            observed = json.loads(response.read().decode("utf-8"))
            attempt = {
                "status": "success",
                "http_status": response.status,
                "elapsed_seconds": time.monotonic() - started,
            }
    except urllib.error.HTTPError as exc:
        observed = None
        attempt = {
            "status": "http_error",
            "http_status": exc.code,
            "elapsed_seconds": time.monotonic() - started,
            "error": exc.read().decode("utf-8", errors="replace")[:2000],
        }
    except (
        TimeoutError,
        urllib.error.URLError,
        http.client.HTTPException,
        ConnectionError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        observed = None
        attempt = {
            "status": "transport_or_response_error",
            "http_status": None,
            "elapsed_seconds": time.monotonic() - started,
            "error": f"{type(exc).__name__}:{exc}",
        }
    return observed, attempt, payload


def _patch_arguments(response: dict[str, Any]) -> str:
    message = response["choices"][0]["message"]
    calls = message.get("tool_calls") or []
    if len(calls) != 1 or calls[0].get("function", {}).get("name") != "submit_skill_patch":
        raise ValueError("exactly_one_submit_skill_patch_call_required")
    arguments = calls[0]["function"]["arguments"]
    if not isinstance(arguments, str):
        raise TypeError("tool_arguments_must_be_string")
    json.loads(arguments)
    return arguments


def _advisory_binding_audit(content: str, facts: dict[str, Any]) -> list[dict[str, Any]]:
    payload = json.loads(content)
    nodes = {row["node_id"]: row for row in facts.get("editable_nodes", [])}
    rows = []
    for index, edit in enumerate(payload.get("edits", [])):
        node_id = str(edit.get("target_node_id") or "")
        node = nodes.get(node_id)
        expected = str(edit.get("expected_node_sha256") or "")
        matched = bool(
            node
            and expected
            == str(node.get("node_sha256") or node.get("source_sha256") or "")
            and str(edit.get("path") or "") == str(node["path"])
            and str(node["observed_source"])
            in str(edit.get("observed_source") or "")
        )
        rows.append(
            {
                "edit_index": index,
                "supplied_node_id": node_id,
                "listed_node_binding_matched": matched,
                "fallback": None if matched else "exact_span_then_posthoc_ast_resolution",
            }
        )
    return rows


def _apply_patch(
    content: str,
    source: Path,
    destination: Path,
    *,
    language: str,
    facts: dict[str, Any] | None,
    require_binding: bool,
) -> dict[str, Any]:
    _configure_edit_limit()
    advisory = _advisory_binding_audit(content, facts or {}) if require_binding else []
    if language == "python":
        result = python_patch.apply_python_span_patch(
            content,
            source,
            destination,
            visible_ast_facts=None,
            require_ast_binding=False,
        )
    else:
        result = apply_universal_span_patch(content, source, destination)
    result["advisory_binding_audit"] = advisory
    result["binding_policy"] = (
        "listed_node_advisory_then_exact_span_posthoc_ast_resolution"
        if require_binding
        else "unique_exact_span_posthoc_ast_resolution"
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
    response, attempt, request_payload = _call_completion(
        api_key, plan, prompt_path.read_text(encoding="utf-8")
    )
    if response is not None:
        write_json(temporary / "provider_response.json", response)
    parse_error = None
    try:
        content = _patch_arguments(response) if response is not None else ""
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        content = ""
        parse_error = f"{type(exc).__name__}:{exc}"
    (temporary / "raw_response.txt").write_text(content, encoding="utf-8")
    case = experiment / "stage" / "public" / "cases" / call["task_id"]
    parent = _discover_package(case, call["skill_name"])
    source = (
        parent
        if call["phase"] == "primary"
        else experiment / "stage" / "current_candidates" / call["task_id"] / "package"
    )
    facts, _ = _visible_facts(case)
    application = None
    application_error = parse_error
    if response is not None and parse_error is None:
        try:
            application = _apply_patch(
                content,
                source,
                temporary / "candidate" / "package",
                language=call["language"],
                facts=facts if call["condition"] == "prescriptive-ast" else None,
                require_binding=call["condition"] == "prescriptive-ast",
            )
            write_json(temporary / "RESPONSE_APPLICATION.json", application)
        except Exception as exc:
            application_error = f"{type(exc).__name__}:{exc}"
            shutil.rmtree(temporary / "candidate", ignore_errors=True)
    candidate = temporary / "candidate" / "package"
    if candidate.is_dir():
        gate = build_proposal_first_closure_report(
            parent,
            candidate,
            (case / "task" / "task.md").read_text(encoding="utf-8"),
            visible_structural_facts=facts,
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
        "phase": call["phase"],
        "language": call["language"],
        "status": status,
        "created_at": _utc_now(),
        "plan_hash": plan["plan_hash"],
        "prompt_sha256": sha256_file(prompt_path),
        "provider_response_sha256": sha256_file(temporary / "provider_response.json")
        if response is not None
        else None,
        "raw_response_sha256": sha256_file(temporary / "raw_response.txt"),
        "application_sha256": sha256_file(temporary / "RESPONSE_APPLICATION.json")
        if application is not None
        else None,
        "gate_sha256": sha256_file(temporary / "POSTHOC_CLOSURE_GATE.json"),
        "candidate_tree_hash": canonical_json_hash(hash_tree(candidate))
        if candidate.is_dir()
        else None,
        "gate_decision": gate["decision"],
        "application_error": application_error,
        "task_verifier_feedback_used": False,
        "hidden_evaluator_loaded_during_generation": False,
        "visible_structural_facts_used": bool(call["visible_structural_facts_used"]),
    }
    freeze["candidate_freeze_hash"] = canonical_json_hash(freeze)
    write_json(temporary / "CANDIDATE_FREEZE.json", freeze)
    usage = (response or {}).get("usage") or {}
    record = {
        "schema_version": SCHEMA_VERSION,
        "trial_id": call["trial_id"],
        "task_id": call["task_id"],
        "condition": call["condition"],
        "phase": call["phase"],
        "status": status,
        "response_id": (response or {}).get("id"),
        "transport_attempt": attempt,
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
    rows = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(calls)))) as executor:
        futures = {
            executor.submit(_run_one, experiment, call, output, api_key=api_key, plan=plan): call[
                "trial_id"
            ]
            for call in calls
        }
        for future in as_completed(futures):
            rows.append(future.result())
    rows.sort(key=lambda row: row["trial_id"])
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete",
        "phase": phase,
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


def run_primary(
    experiment_root: str | Path, *, split: str, api_key: str, workers: int = 6
) -> dict[str, Any]:
    experiment = Path(experiment_root).resolve()
    preflight = read_json(experiment / "_audit" / "ZERO_CALL_PREFLIGHT.json")
    if preflight.get("status") != "pass" or not _embedded_hash_valid(preflight, "preflight_hash"):
        raise ValueError("valid_zero_call_preflight_required")
    plan, checks = _validate_stage(experiment)
    if not all(checks.values()):
        raise ValueError("stage_changed_after_preflight")
    task_ids = set(read_json(experiment / "stage" / "SPLITS.json")[split])
    calls = [row for row in plan["primary_calls"] if row["task_id"] in task_ids]
    return _run_calls(
        experiment,
        calls,
        experiment / "runs" / "primary" / split,
        api_key=api_key,
        plan=plan,
        split=split,
        phase="primary",
        workers=workers,
    )


def prepare_revisions(experiment_root: str | Path, *, split: str) -> dict[str, Any]:
    experiment = Path(experiment_root).resolve()
    plan, checks = _validate_stage(experiment)
    if not all(checks.values()):
        raise ValueError("stage_changed_before_revision_freeze")
    primary_root = experiment / "runs" / "primary" / split
    summary = read_json(primary_root / "BATCH_RUN_SUMMARY.json")
    if not _embedded_hash_valid(summary, "run_hash"):
        raise ValueError("primary_summary_hash_invalid")
    task_ids = read_json(experiment / "stage" / "SPLITS.json")[split]
    origins = {
        row["task_id"]: row
        for row in read_json(experiment / "stage" / "CASE_ORIGINS.json")["rows"]
    }
    calls_by_key = {
        (row["task_id"], row["condition"]): row for row in plan["primary_calls"]
    }
    current_root = experiment / "stage" / "current_candidates"
    report_root = experiment / "stage" / "closure_reports"
    prompts = experiment / "stage" / "revision_prompts" / split
    prompts.mkdir(parents=True, exist_ok=False)
    calls = []
    rows = []
    for task_id in sorted(task_ids):
        origin = origins[task_id]
        case = experiment / "stage" / "public" / "cases" / task_id
        parent = _discover_package(case, origin["skill_name"])
        raw_call = calls_by_key[(task_id, "raw-one-shot")]
        raw_run = primary_root / raw_call["trial_id"]
        raw_candidate = raw_run / "candidate" / "package"
        current = current_root / task_id / "package"
        copy_tree_clean(raw_candidate if raw_candidate.is_dir() else parent, current)
        facts, facts_path = _visible_facts(case)
        report = build_proposal_first_closure_report(
            parent,
            current,
            (case / "task" / "task.md").read_text(encoding="utf-8"),
            visible_structural_facts=facts,
        )
        report_path = report_root / f"{task_id}.json"
        write_json(report_path, report)
        for condition in REVISION_CONDITIONS:
            prompt = prompts / f"{task_id}--{condition}--r2.txt"
            prompt.write_text(
                _revision_prompt(case, current, origin["language"], condition, report),
                encoding="utf-8",
            )
            calls.append(
                {
                    "trial_id": f"{task_id}--{condition}--r2",
                    "task_id": task_id,
                    "condition": condition,
                    "phase": "revision",
                    "language": origin["language"],
                    "skill_name": origin["skill_name"],
                    "prompt_path": prompt.relative_to(experiment).as_posix(),
                    "prompt_sha256": sha256_file(prompt),
                    "prompt_bytes": prompt.stat().st_size,
                    "source_package_tree_hash": canonical_json_hash(hash_tree(current)),
                    "source_report_hash": report["gate_hash"],
                    "visible_structural_facts_used": condition == "ast-closure-revision",
                }
            )
        rows.append(
            {
                "task_id": task_id,
                "current_candidate_tree_hash": canonical_json_hash(hash_tree(current)),
                "closure_report_hash": report["gate_hash"],
                "visible_facts_sha256": sha256_file(facts_path),
            }
        )
    random.Random(f"d37-extension45-{split}-revision-v309").shuffle(calls)
    revision_plan = {
        "schema_version": SCHEMA_VERSION,
        "status": "frozen_before_revision_model_calls",
        "created_at": _utc_now(),
        "split": split,
        "source_plan_hash": plan["plan_hash"],
        "source_primary_run_hash": summary["run_hash"],
        "model": plan["model"],
        "revision_call_count": len(calls),
        "revision_calls": calls,
        "rows": rows,
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


def _run_root_by_key(experiment: Path) -> dict[tuple[str, str], Path]:
    rows = {}
    for phase in ("primary", "revision"):
        for split in ("smoke", "formal"):
            root = experiment / "runs" / phase / split
            summary = read_json(root / "BATCH_RUN_SUMMARY.json")
            if not _embedded_hash_valid(summary, "run_hash"):
                raise ValueError(f"run_summary_invalid:{phase}:{split}")
            for row in summary["rows"]:
                rows[(row["task_id"], row["condition"])] = root / row["trial_id"]
    return rows


def _candidate_or_fallback(
    run_root: Path, fallback: Path, *, require_gate: bool
) -> tuple[Path, str, str, bool]:
    freeze = read_json(run_root / "CANDIDATE_FREEZE.json")
    if not _embedded_hash_valid(freeze, "candidate_freeze_hash"):
        raise ValueError(f"candidate_freeze_invalid:{run_root}")
    candidate = run_root / "candidate" / "package"
    accepted = candidate.is_dir() and (not require_gate or freeze["gate_decision"] == ACCEPT)
    return (
        candidate if accepted else fallback,
        "candidate_accepted" if accepted else "candidate_rejected_fallback",
        freeze["candidate_freeze_hash"],
        accepted,
    )


def _select_generic_control(
    features: dict[str, dict[str, Any]]
) -> tuple[str, str]:
    eligible = [name for name, row in features.items() if row["structurally_safe"]]
    if not eligible:
        return "ast-closure-revision", "no_structurally_safe_candidate"
    best_key = max(_coverage_key(features[name]) for name in eligible)
    best = [name for name in eligible if _coverage_key(features[name]) == best_key]
    selected = "ast-closure-revision" if "ast-closure-revision" in best else best[0]
    return selected, "lexicographic_visible_structure"


def _write_selection(
    experiment: Path,
    condition: str,
    task_id: str,
    package: Path,
    *,
    reason: str,
    source_hashes: list[str],
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
        "source_candidate_freeze_hashes": source_hashes,
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
    runs = _run_root_by_key(experiment)
    origins = {
        row["task_id"]: row
        for row in read_json(experiment / "stage" / "CASE_ORIGINS.json")["rows"]
    }
    selector_root = experiment / "visible_selector_reports"
    selector_root.mkdir(parents=True, exist_ok=False)
    selection_rows = []
    for task_id in sorted(origins):
        origin = origins[task_id]
        case = experiment / "stage" / "public" / "cases" / task_id
        parent = _discover_package(case, origin["skill_name"])
        request = (case / "task" / "task.md").read_text(encoding="utf-8")
        facts, _ = _visible_facts(case)

        raw_run = runs[(task_id, "raw-one-shot")]
        raw, raw_reason, raw_hash, raw_ok = _candidate_or_fallback(
            raw_run, parent, require_gate=False
        )
        raw_gate = read_json(raw_run / "POSTHOC_CLOSURE_GATE.json")
        safe_raw = raw if raw_ok and raw_gate["decision"] == ACCEPT else parent
        prescriptive, prescriptive_reason, prescriptive_hash, _ = _candidate_or_fallback(
            runs[(task_id, "prescriptive-ast")], parent, require_gate=True
        )
        generic, generic_reason, generic_hash, _ = _candidate_or_fallback(
            runs[(task_id, "generic-revision")], safe_raw, require_gate=True
        )
        closure, closure_reason, closure_hash, _ = _candidate_or_fallback(
            runs[(task_id, "ast-closure-revision")], safe_raw, require_gate=True
        )

        pair_packages = {
            "historical-prescriptive-ast": prescriptive,
            "ast-closure-revision": closure,
        }
        pair_features = {}
        pair_reports = {}
        for name, package in pair_packages.items():
            feature, report = _candidate_features(parent, package, request, facts)
            pair_features[name] = feature
            pair_reports[name] = report
        dual_name, dual_reason = choose_candidate(
            "coverage-lexicographic", pair_features
        )
        dual = pair_packages[dual_name]

        control_packages = {
            "generic-revision": generic,
            "ast-closure-revision": closure,
        }
        control_features = {}
        control_reports = {}
        for name, package in control_packages.items():
            feature, report = _candidate_features(parent, package, request, facts)
            control_features[name] = feature
            control_reports[name] = report
        control_name, control_reason = _select_generic_control(control_features)
        control = control_packages[control_name]
        selector = {
            "schema_version": SCHEMA_VERSION,
            "task_id": task_id,
            "policy": "visible_structure_lexicographic",
            "dual": {
                "selected": dual_name,
                "reason": dual_reason,
                "features": pair_features,
                "report_hashes": {name: report["gate_hash"] for name, report in pair_reports.items()},
            },
            "matched_control": {
                "selected": control_name,
                "reason": control_reason,
                "features": control_features,
                "report_hashes": {name: report["gate_hash"] for name, report in control_reports.items()},
            },
            "hidden_results_used": False,
            "task_verifier_feedback_used": False,
        }
        selector["selector_hash"] = canonical_json_hash(selector)
        write_json(selector_root / f"{task_id}.json", selector)

        selected = {
            "no-evolution": (parent, "frozen_parent", [], 0),
            "raw-one-shot": (raw, raw_reason, [raw_hash], 1),
            "prescriptive-ast": (
                prescriptive,
                prescriptive_reason,
                [prescriptive_hash],
                1,
            ),
            "generic-revision": (
                generic,
                generic_reason,
                [raw_hash, generic_hash],
                2,
            ),
            "ast-closure-revision": (
                closure,
                closure_reason,
                [raw_hash, closure_hash],
                2,
            ),
            "dual-path-ast": (
                dual,
                f"dual_selected:{dual_name}:{dual_reason}",
                [raw_hash, closure_hash, prescriptive_hash],
                3,
            ),
            "generic-plus-closure-control": (
                control,
                f"control_selected:{control_name}:{control_reason}",
                [raw_hash, generic_hash, closure_hash],
                3,
            ),
        }
        for condition in CONDITIONS:
            package, reason, source_hashes, model_calls = selected[condition]
            frozen = _write_selection(
                experiment,
                condition,
                task_id,
                package,
                reason=reason,
                source_hashes=source_hashes,
                model_calls=model_calls,
            )
            selection_rows.append(
                {
                    "task_id": task_id,
                    "condition": condition,
                    "difficulty": origin["difficulty"],
                    "package_id": origin["package_id"],
                    "reason": reason,
                    "selection_freeze_hash": frozen["selection_freeze_hash"],
                }
            )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "all_extension45_candidates_frozen_before_hidden_evaluation",
        "task_count": len(origins),
        "row_count": len(selection_rows),
        "conditions": list(CONDITIONS),
        "condition_counts": dict(Counter(row["condition"] for row in selection_rows)),
        "candidate_frozen_before_hidden_evaluation": True,
        "hidden_results_used_for_selection": False,
        "task_verifier_feedback_used": False,
        "rows": selection_rows,
    }
    summary["selection_hash"] = canonical_json_hash(summary)
    write_json(experiment / "selected" / "all" / "SELECTION_SUMMARY.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="D37 extension45 Dual-Path experiment")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--benchmark-root", type=Path, required=True)
    prepare.add_argument("--experiment-root", type=Path, required=True)
    prepare.add_argument("--expected-freeze-hash", required=True)
    prepare.add_argument("--model", default=MODEL)
    prepare.add_argument("--base-url", default=BASE_URL)
    prepare.add_argument("--timeout", type=int, default=600)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--experiment-root", type=Path, required=True)
    primary = commands.add_parser("run-primary")
    primary.add_argument("--experiment-root", type=Path, required=True)
    primary.add_argument("--split", choices=("smoke", "formal"), required=True)
    primary.add_argument("--workers", type=int, default=6)
    revision_prepare = commands.add_parser("prepare-revisions")
    revision_prepare.add_argument("--experiment-root", type=Path, required=True)
    revision_prepare.add_argument("--split", choices=("smoke", "formal"), required=True)
    revision_run = commands.add_parser("run-revisions")
    revision_run.add_argument("--experiment-root", type=Path, required=True)
    revision_run.add_argument("--split", choices=("smoke", "formal"), required=True)
    revision_run.add_argument("--workers", type=int, default=6)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--experiment-root", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare_stage(
            args.benchmark_root,
            args.experiment_root,
            expected_freeze_hash=args.expected_freeze_hash,
            model=args.model,
            base_url=args.base_url,
            timeout=args.timeout,
        )
    elif args.command == "preflight":
        result = zero_call_preflight(args.experiment_root)
    elif args.command == "run-primary":
        result = run_primary(
            args.experiment_root,
            split=args.split,
            api_key=getpass.getpass("OpenLux API key: "),
            workers=args.workers,
        )
    elif args.command == "prepare-revisions":
        result = prepare_revisions(args.experiment_root, split=args.split)
    elif args.command == "run-revisions":
        result = run_revisions(
            args.experiment_root,
            split=args.split,
            api_key=getpass.getpass("OpenLux API key: "),
            workers=args.workers,
        )
    else:
        result = freeze_selections(args.experiment_root)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
