from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .io_utils import canonical_json_hash, copy_tree_clean, hash_tree, read_json, sha256_file, write_json
from .v04_runner import _usage_from_trace, audit_v04_trace


V04_UTILITY_CONDITIONS = ("no-skill", "md-only-literal", "full-package")
V04_EXPERIMENTAL_UTILITY_CONDITIONS = ("scripts-only-executable",)


def _family(manifest: dict[str, Any], family_id: str) -> dict[str, Any]:
    try:
        return next(row for row in manifest["families"] if row["family_id"] == family_id)
    except StopIteration as exc:
        raise KeyError(family_id) from exc


def _utility_instance(family: dict[str, Any], task_id: str) -> dict[str, Any]:
    try:
        return next(row for row in family["utility_instances"] if row["task_id"] == task_id)
    except StopIteration as exc:
        raise KeyError(task_id) from exc


def build_v04_utility_prompt(public_root: str | Path, family_id: str, task_id: str) -> str:
    public = Path(public_root).resolve()
    manifest = read_json(public / "benchmark_manifest.json")
    family = _family(manifest, family_id)
    instance = _utility_instance(family, task_id)
    task = read_json(public / instance["public_path"])
    return (
        "Solve the task in TASK.json. The current directory may include an Agent Skill package under "
        "skill/. Use any local resources that are available.\n\n"
        "Rules:\n"
        "- Do not search outside the current directory.\n"
        "- Do not look for tests, verifiers, gold answers, or benchmark artifacts.\n"
        "- Do not modify TASK.json or files under skill/.\n"
        "- Write exactly one valid JSON value to answer.json.\n"
        "- Run only local checks that use files in the current directory.\n\n"
        "Visible task:\n"
        f"{json.dumps(task, indent=2, sort_keys=True, ensure_ascii=True)}\n"
    )


def materialize_v04_utility_workspace(
    public_root: str | Path,
    *,
    family_id: str,
    task_id: str,
    condition: str,
    workspace: str | Path,
) -> dict[str, Any]:
    if condition not in (*V04_UTILITY_CONDITIONS, *V04_EXPERIMENTAL_UTILITY_CONDITIONS):
        raise ValueError(condition)
    public = Path(public_root).resolve()
    destination = Path(workspace).resolve()
    manifest = read_json(public / "benchmark_manifest.json")
    family = _family(manifest, family_id)
    instance = _utility_instance(family, task_id)
    task_path = public / instance["public_path"]
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    shutil.copy2(task_path, destination / "TASK.json")

    skill_source = public / family["public_family_path"] / "utility" / "skill"
    if condition != "no-skill":
        copy_tree_clean(skill_source, destination / "skill")
    if condition == "md-only-literal":
        for path in sorted((destination / "skill").rglob("*"), reverse=True):
            if path.is_dir() and path.name.lower() in {"script", "scripts"}:
                shutil.rmtree(path)
    if condition == "scripts-only-executable":
        skill_markdown = destination / "skill" / "SKILL.md"
        if skill_markdown.exists():
            skill_markdown.unlink()

    return {
        "schema_version": "0.4-utility-workspace-1",
        "family_id": family_id,
        "task_id": task_id,
        "condition": condition,
        "workspace_root": str(destination),
        "workspace_hashes": hash_tree(destination),
        "task_public_hash": sha256_file(task_path),
    }


def freeze_v04_utility_answer(
    workspace: str | Path,
    run_root: str | Path,
    *,
    family_id: str,
    task_id: str,
    condition: str,
) -> dict[str, Any]:
    root = Path(workspace).resolve()
    answer = root / "answer.json"
    failures: list[str] = []
    answer_hash: str | None = None
    answer_value_hash: str | None = None
    if not answer.is_file():
        failures.append("answer_missing")
    else:
        answer_hash = sha256_file(answer)
        try:
            value = json.loads(answer.read_text(encoding="utf-8"))
            answer_value_hash = canonical_json_hash(value)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            failures.append(f"answer_invalid_json:{type(exc).__name__}")
    record = {
        "schema_version": "0.4-utility-answer-freeze-1",
        "status": "candidate_frozen" if not failures else "invalid_candidate",
        "family_id": family_id,
        "task_id": task_id,
        "condition": condition,
        "workspace_root": str(root),
        "workspace_hashes": hash_tree(root),
        "workspace_tree_hash": canonical_json_hash(hash_tree(root)),
        "answer_sha256": answer_hash,
        "answer_value_hash": answer_value_hash,
        "hidden_evaluation_loaded": False,
        "failures": failures,
    }
    write_json(Path(run_root).resolve() / "answer_frozen.json", record)
    return record


def _utility_scope_report(workspace: Path, baseline_hashes: dict[str, str]) -> dict[str, Any]:
    current = hash_tree(workspace)
    changed = sorted(
        path
        for path in set(baseline_hashes) | set(current)
        if baseline_hashes.get(path) != current.get(path)
    )
    violations = [path for path in changed if path != "answer.json"]
    return {
        "status": "pass" if not violations else "fail",
        "changed_paths": changed,
        "violations": violations,
    }


def run_v04_utility_trial(
    public_root: str | Path,
    *,
    family_id: str,
    task_id: str,
    condition: str,
    output_root: str | Path,
    model: str = "gpt-5.5",
    codex_bin: str = "codex",
    codex_home: str | Path | None = None,
    timeout: int = 1200,
    execute: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    if condition not in V04_UTILITY_CONDITIONS:
        raise ValueError(condition)
    public = Path(public_root).resolve()
    run = Path(output_root).resolve()
    if run.exists():
        if not overwrite:
            raise FileExistsError(run)
        shutil.rmtree(run)
    run.mkdir(parents=True)
    workspace = run / "workspace"
    workspace_record = materialize_v04_utility_workspace(
        public,
        family_id=family_id,
        task_id=task_id,
        condition=condition,
        workspace=workspace,
    )
    baseline_hashes = hash_tree(workspace)
    prompt = build_v04_utility_prompt(public, family_id, task_id)
    (run / "prompt.txt").write_text(prompt, encoding="utf-8")
    planned_command = [
        codex_bin,
        "exec",
        "-m",
        model,
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "workspace-write",
        "-C",
        str(workspace),
        "-",
    ]
    record: dict[str, Any] = {
        "schema_version": "0.4-utility-run-1",
        "family_id": family_id,
        "task_id": task_id,
        "condition": condition,
        "model": model,
        "executed": execute,
        "codex_invocations": 0,
        "planned_argv": planned_command,
        "public_root": str(public),
        "workspace_record_hash": canonical_json_hash(workspace_record),
        "hidden_evaluation_loaded": False,
    }
    trace = ""
    stderr = ""
    returncode: int | None = None
    transport_status = "not_executed"
    if execute:
        environment = os.environ.copy()
        if codex_home is not None:
            environment["CODEX_HOME"] = str(Path(codex_home).resolve())
        record["codex_invocations"] = 1
        try:
            completed = subprocess.run(
                planned_command,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=environment,
            )
            trace = completed.stdout
            stderr = completed.stderr
            returncode = completed.returncode
            transport_status = "complete" if returncode == 0 else "process_failed"
        except subprocess.TimeoutExpired as exc:
            trace = exc.stdout or ""
            stderr = exc.stderr or ""
            transport_status = "timeout_outcome_unknown"
    (run / "trace.jsonl").write_text(trace, encoding="utf-8")
    (run / "stderr.txt").write_text(stderr, encoding="utf-8")
    trace_audit = audit_v04_trace(trace, workspace, allow_temporary_outputs=True) if execute else {
        "status": "pass",
        "turn_completed": False,
        "command_count": 0,
        "file_change_count": 0,
        "findings": [],
        "warnings": [],
    }
    scope = _utility_scope_report(workspace, baseline_hashes)
    freeze = freeze_v04_utility_answer(
        workspace,
        run,
        family_id=family_id,
        task_id=task_id,
        condition=condition,
    )
    valid = (
        transport_status == "complete"
        and trace_audit["status"] == "pass"
        and scope["status"] == "pass"
        and freeze["status"] == "candidate_frozen"
    )
    record.update(
        {
            "status": "candidate_frozen" if valid else "invalid_trial",
            "transport_status": transport_status,
            "returncode": returncode,
            "usage": _usage_from_trace(trace),
            "trace_audit": trace_audit,
            "scope_report": scope,
            "answer_freeze_hash": canonical_json_hash(freeze),
        }
    )
    write_json(run / "run_record.json", record)
    return record


def evaluate_v04_utility_trial(
    benchmark_root: str | Path,
    *,
    run_root: str | Path,
    output: str | Path | None = None,
) -> dict[str, Any]:
    benchmark = Path(benchmark_root).resolve()
    run = Path(run_root).resolve()
    freeze = read_json(run / "answer_frozen.json")
    run_record = read_json(run / "run_record.json")
    workspace = run / "workspace"
    failures: list[str] = []
    current_hashes = hash_tree(workspace)
    if run_record.get("status") != "candidate_frozen":
        failures.append("run_not_validly_frozen")
    if run_record.get("hidden_evaluation_loaded") is not False:
        failures.append("run_loaded_hidden_before_freeze")
    if run_record.get("answer_freeze_hash") != canonical_json_hash(freeze):
        failures.append("answer_freeze_hash_mismatch")
    if freeze.get("hidden_evaluation_loaded") is not False:
        failures.append("hidden_loaded_before_freeze")
    if freeze.get("status") != "candidate_frozen":
        failures.append("candidate_not_frozen")
    if freeze.get("workspace_hashes") != current_hashes:
        failures.append("workspace_changed_after_freeze")
    answer_path = workspace / "answer.json"
    try:
        observed = json.loads(answer_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        observed = None
        failures.append(f"answer_unreadable:{type(exc).__name__}")

    manifest = read_json(benchmark / "public" / "benchmark_manifest.json")
    family = _family(manifest, freeze["family_id"])
    _utility_instance(family, freeze["task_id"])
    expected_rows = read_json(
        benchmark / "_private" / "families" / family["family_id"] / "utility_expected.json"
    )["instances"]
    try:
        expected = next(row["expected_output"] for row in expected_rows if row["task_id"] == freeze["task_id"])
    except StopIteration as exc:
        raise KeyError(freeze["task_id"]) from exc
    correct = not failures and canonical_json_hash(observed) == canonical_json_hash(expected)
    report = {
        "schema_version": "0.4-utility-evaluation-1",
        "status": "pass" if correct else "fail",
        "family_id": family["family_id"],
        "task_id": freeze["task_id"],
        "condition": freeze["condition"],
        "correct": correct,
        "freeze_integrity": {"status": "pass" if not failures else "fail", "failures": failures},
        "observed_value_hash": canonical_json_hash(observed) if not failures else None,
        "expected_value_hash": canonical_json_hash(expected),
        "candidate_tree_hash": canonical_json_hash(current_hashes),
    }
    if output is not None:
        write_json(output, report)
    return report
