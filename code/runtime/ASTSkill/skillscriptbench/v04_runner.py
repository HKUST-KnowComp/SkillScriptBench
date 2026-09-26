from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .io_utils import canonical_json_hash, copy_tree_clean, hash_tree, read_json, write_json
from .v04_eval import freeze_candidate


V04_EVOLUTION_CONDITIONS = ("no-evolution", "raw-source")


def _family(manifest: dict[str, Any], family_id: str) -> dict[str, Any]:
    try:
        return next(row for row in manifest["families"] if row["family_id"] == family_id)
    except StopIteration as exc:
        raise KeyError(family_id) from exc


def build_v04_evolution_prompt(public_root: str | Path, family_id: str) -> str:
    public = Path(public_root).resolve()
    manifest = read_json(public / "benchmark_manifest.json")
    family = _family(manifest, family_id)
    if family.get("evolution_kind") is None:
        raise ValueError("utility_only_family")
    task = read_json(public / family["public_family_path"] / "evolution" / "TASK.json")
    return (
        "Improve the skill package in the current directory according to the task below.\n\n"
        "Rules:\n"
        "- Inspect SKILL.md and scripts/ before editing.\n"
        "- Modify only SKILL.md and files under scripts/.\n"
        "- Preserve backward compatibility for existing calls.\n"
        "- Do not search outside the current directory.\n"
        "- Do not look for tests, verifiers, gold answers, or benchmark artifacts.\n"
        "- Run only local checks that use the visible package.\n\n"
        "Visible task:\n"
        f"{json.dumps(task, indent=2, sort_keys=True, ensure_ascii=True)}\n"
    )


def _usage_from_trace(trace: str) -> dict[str, int]:
    usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    for line in trace.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "turn.completed" or not isinstance(event.get("usage"), dict):
            continue
        for key in usage:
            usage[key] += int(event["usage"].get(key, 0) or 0)
    return usage


def audit_v04_trace(
    trace: str,
    candidate_root: str | Path,
    *,
    allow_temporary_outputs: bool = False,
) -> dict[str, Any]:
    candidate = str(Path(candidate_root).resolve())
    findings: list[str] = []
    warnings: list[str] = []
    command_count = 0
    file_change_count = 0
    turn_completed = False
    for line in trace.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed":
            turn_completed = True
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if item.get("type") == "command_execution" and event.get("type") == "item.completed":
            command_count += 1
            command = str(item.get("command") or "")
            output = str(item.get("aggregated_output") or "")
            combined = f"{command}\n{output}".lower()
            for forbidden in ("_private", "evolution_evaluator", "utility_expected", "private_construction_label"):
                if forbidden in combined:
                    findings.append(f"forbidden_artifact_term:{forbidden}")
            if re.search(r"(^|[\s'\"])(?:\.\./|\.\.$)", command):
                findings.append("parent_traversal")
            absolute_paths = re.findall(r"/(?:Users|home|workspace|private|tmp)/[^\s'\"]+", command)
            for path in absolute_paths:
                normalized = path.rstrip(";,:)")
                if not normalized.startswith(candidate):
                    temporary_output = normalized.startswith("/tmp/") and any(
                        marker in command
                        for marker in (
                            f">{normalized}",
                            f"> {normalized}",
                            f">>{normalized}",
                            f">> {normalized}",
                        )
                    )
                    if allow_temporary_outputs and temporary_output:
                        warnings.append(f"temporary_output_outside_candidate:{normalized}")
                    else:
                        findings.append(f"absolute_path_outside_candidate:{normalized}")
        if item.get("type") == "file_change" and event.get("type") == "item.completed":
            file_change_count += 1
    return {
        "status": "pass" if not findings and turn_completed else "fail",
        "turn_completed": turn_completed,
        "command_count": command_count,
        "file_change_count": file_change_count,
        "findings": sorted(set(findings)),
        "warnings": sorted(set(warnings)),
    }


def _scope_report(candidate: Path, baseline_hashes: dict[str, str]) -> dict[str, Any]:
    current = hash_tree(candidate)
    changed = sorted(
        path
        for path in set(baseline_hashes) | set(current)
        if baseline_hashes.get(path) != current.get(path)
    )
    violations = [
        path
        for path in changed
        if path != "SKILL.md" and not path.startswith("scripts/")
    ]
    return {
        "status": "pass" if not violations else "fail",
        "changed_paths": changed,
        "violations": violations,
    }


def run_v04_evolution_proposal(
    public_root: str | Path,
    *,
    family_id: str,
    condition: str,
    output_root: str | Path,
    model: str = "gpt-5.5",
    codex_bin: str = "codex",
    codex_home: str | Path | None = None,
    timeout: int = 1200,
    execute: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    if condition not in V04_EVOLUTION_CONDITIONS:
        raise ValueError(condition)
    public = Path(public_root).resolve()
    manifest = read_json(public / "benchmark_manifest.json")
    family = _family(manifest, family_id)
    if family.get("evolution_kind") is None:
        raise ValueError("utility_only_family")
    run = Path(output_root).resolve()
    if run.exists():
        if not overwrite:
            raise FileExistsError(run)
        shutil.rmtree(run)
    candidate = run / "candidate"
    visible = public / family["public_family_path"] / "evolution" / "visible"
    copy_tree_clean(visible, candidate)
    baseline_hashes = hash_tree(candidate)
    prompt = build_v04_evolution_prompt(public, family_id)
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
        str(candidate),
        "-",
    ]
    record: dict[str, Any] = {
        "schema_version": "0.4-proposal-run-1",
        "family_id": family_id,
        "condition": condition,
        "model": model,
        "executed": execute,
        "codex_invocations": 0,
        "planned_argv": planned_command,
        "public_root": str(public),
        "candidate_root": str(candidate),
        "hidden_evaluation_loaded": False,
    }
    trace = ""
    stderr = ""
    returncode: int | None = None
    transport_status = "not_executed"
    if execute and condition == "raw-source":
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
    elif execute:
        transport_status = "complete"
    (run / "trace.jsonl").write_text(trace, encoding="utf-8")
    (run / "stderr.txt").write_text(stderr, encoding="utf-8")
    trace_audit = audit_v04_trace(trace, candidate) if condition == "raw-source" and execute else {
        "status": "pass",
        "turn_completed": condition == "no-evolution",
        "command_count": 0,
        "file_change_count": 0,
        "findings": [],
    }
    scope = _scope_report(candidate, baseline_hashes)
    freeze = freeze_candidate(candidate, run, family_id=family_id)
    record.update(
        {
            "status": (
                "candidate_frozen"
                if transport_status == "complete" and trace_audit["status"] == "pass" and scope["status"] == "pass"
                else "invalid_proposal"
            ),
            "transport_status": transport_status,
            "returncode": returncode,
            "usage": _usage_from_trace(trace),
            "trace_audit": trace_audit,
            "scope_report": scope,
            "candidate_freeze_hash": canonical_json_hash(freeze),
        }
    )
    write_json(run / "run_record.json", record)
    return record
