from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .io_utils import canonical_json_hash, read_json, sha256_file, write_json


def _verify_embedded_hash(payload: dict[str, Any], field: str, label: str) -> None:
    expected = payload.get(field)
    if not expected:
        raise ValueError(f"{label}_hash_missing")
    unhashed = dict(payload)
    del unhashed[field]
    if canonical_json_hash(unhashed) != expected:
        raise ValueError(f"{label}_hash_mismatch")


def freeze_node_runtime_v11(
    base_runtime: str | Path | dict[str, Any],
    node_executable: str | Path,
    parser_root: str | Path,
    output: str | Path | None = None,
) -> dict[str, Any]:
    base = read_json(base_runtime) if isinstance(base_runtime, (str, Path)) else base_runtime
    _verify_embedded_hash(base, "runtime_hash", "base_runtime")
    if base.get("status") != "frozen":
        raise ValueError("base_runtime_not_frozen")

    executable = Path(node_executable).absolute()
    parser = Path(parser_root).resolve()
    package_path = parser / "package.json"
    lock_path = parser / "package-lock.json"
    if not executable.is_file():
        raise FileNotFoundError(executable)
    if not package_path.is_file() or not lock_path.is_file():
        raise FileNotFoundError("node_parser_package_or_lock_missing")

    package = json.loads(package_path.read_text(encoding="utf-8"))
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    direct_dependencies = package.get("dependencies", {})
    lock_packages = lock.get("packages", {})
    mismatches = []
    for name, expected in sorted(direct_dependencies.items()):
        observed = lock_packages.get(f"node_modules/{name}", {}).get("version")
        if observed != expected:
            mismatches.append(f"{name}:expected={expected}:observed={observed}")

    version = subprocess.run(
        [str(executable), "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.strip()
    process_versions_raw = subprocess.run(
        [str(executable), "-p", "JSON.stringify(process.versions)"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.strip()
    process_versions = json.loads(process_versions_raw)
    helper_hashes = {
        path.name: sha256_file(path)
        for path in sorted(parser.glob("*.mjs"))
        if path.is_file()
    }
    if not helper_hashes:
        raise ValueError("node_parser_helpers_missing")

    result = {
        "schema_version": "0.11-node-runtime-contract-v1",
        "benchmark": "SkillScriptBench",
        "status": "frozen" if not mismatches else "fail",
        "claim_boundary": (
            "This contract freezes the Node runtime and construction parser after case selection. "
            "It does not retroactively make development cases untouched evaluation."
        ),
        "base_python_runtime_hash": base["runtime_hash"],
        "node_executable": str(executable),
        "node_executable_realpath": str(executable.resolve()),
        "node_executable_sha256": sha256_file(executable),
        "node_version": version,
        "node_process_versions": process_versions,
        "parser_root": str(parser),
        "parser_package_sha256": sha256_file(package_path),
        "parser_lock_sha256": sha256_file(lock_path),
        "parser_lockfile_version": lock.get("lockfileVersion"),
        "parser_direct_dependencies": direct_dependencies,
        "parser_dependency_mismatches": mismatches,
        "parser_helper_hashes": helper_hashes,
        "model_calls": 0,
        "behavior_executions": 0,
    }
    result["node_runtime_hash"] = canonical_json_hash(result)
    if output is not None:
        write_json(output, result)
    return result


def verified_node_runtime(
    value: str | Path | dict[str, Any] | None,
    *,
    base_runtime_hash: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    payload = read_json(value) if isinstance(value, (str, Path)) else value
    _verify_embedded_hash(payload, "node_runtime_hash", "node_runtime")
    if payload.get("status") != "frozen":
        raise ValueError("node_runtime_not_frozen")
    if payload.get("base_python_runtime_hash") != base_runtime_hash:
        raise ValueError("node_runtime_base_runtime_hash_mismatch")
    executable = Path(payload["node_executable"])
    if not executable.is_file() or sha256_file(executable) != payload["node_executable_sha256"]:
        raise ValueError("node_runtime_executable_hash_drift")
    return payload
