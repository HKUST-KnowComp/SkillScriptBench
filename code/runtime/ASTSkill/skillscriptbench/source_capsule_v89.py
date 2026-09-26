from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from skillscriptbench.io_utils import (
    canonical_json_hash,
    hash_tree,
    read_json,
    sha256_file,
    write_json,
)


SCHEMA_VERSION = "0.89-executable-source-capsule-v1"
CAPSULE_DIRECTORY = "source_capsule"
SOURCE_DIRECTORY = "source"
MANIFEST_NAME = "MANIFEST.json"


def _relative_source_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"source_capsule_path_invalid:{value}")
    normalized = path.as_posix()
    if normalized.startswith("./") or normalized == ".":
        raise ValueError(f"source_capsule_path_invalid:{value}")
    return normalized


def create_source_capsule(
    workspace_root: str | Path,
    stage_root: str | Path,
    code_paths: Iterable[str],
    *,
    entrypoint_module: str,
) -> dict[str, Any]:
    """Freeze the exact experiment sources beside the stage before model calls."""

    workspace = Path(workspace_root).resolve()
    stage = Path(stage_root).resolve()
    capsule = stage / CAPSULE_DIRECTORY
    source_root = capsule / SOURCE_DIRECTORY
    if capsule.exists():
        raise FileExistsError(f"source_capsule_already_exists:{capsule}")

    declared = sorted({_relative_source_path(value) for value in code_paths})
    init_path = workspace / "skillscriptbench" / "__init__.py"
    if init_path.is_file() and "skillscriptbench/__init__.py" not in declared:
        declared.insert(0, "skillscriptbench/__init__.py")

    missing = [relative for relative in declared if not (workspace / relative).is_file()]
    if missing:
        raise FileNotFoundError(f"source_capsule_files_missing:{','.join(missing)}")

    source_root.mkdir(parents=True)
    files = []
    for relative in declared:
        source = workspace / relative
        destination = source_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        files.append(
            {
                "path": relative,
                "sha256": sha256_file(destination),
                "bytes": destination.stat().st_size,
                "executable": bool(source.stat().st_mode & 0o111),
            }
        )

    file_hashes = {row["path"]: row["sha256"] for row in files}
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "frozen_before_model_calls",
        "entrypoint_module": entrypoint_module,
        "source_root": SOURCE_DIRECTORY,
        "declared_code_paths": declared,
        "files": files,
        "file_hashes": file_hashes,
        "source_tree_hash": canonical_json_hash(hash_tree(source_root)),
        "runtime": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
        },
        "rerun_template": (
            f"PYTHONPATH={{capsule}}/{SOURCE_DIRECTORY} "
            f"{{python}} -m {entrypoint_module} <subcommand>"
        ),
        "contains_credentials": False,
    }
    manifest["manifest_hash"] = canonical_json_hash(manifest)
    write_json(capsule / MANIFEST_NAME, manifest)
    return manifest


def source_capsule_plan_receipt(stage_root: str | Path) -> dict[str, Any]:
    stage = Path(stage_root).resolve()
    manifest_path = stage / CAPSULE_DIRECTORY / MANIFEST_NAME
    manifest = read_json(manifest_path)
    return {
        "path": CAPSULE_DIRECTORY,
        "manifest_sha256": sha256_file(manifest_path),
        "manifest_hash": manifest["manifest_hash"],
        "source_tree_hash": manifest["source_tree_hash"],
        "entrypoint_module": manifest["entrypoint_module"],
    }


def validate_source_capsule(
    stage_root: str | Path,
    receipt: dict[str, Any],
    expected_code_hashes: dict[str, str],
) -> dict[str, Any]:
    stage = Path(stage_root).resolve()
    relative = _relative_source_path(str(receipt.get("path") or ""))
    capsule = stage / relative
    manifest_path = capsule / MANIFEST_NAME
    source_root = capsule / SOURCE_DIRECTORY
    checks: dict[str, bool] = {
        "manifest_exists": manifest_path.is_file(),
        "source_root_exists": source_root.is_dir(),
    }
    if not all(checks.values()):
        return {"status": "fail", "checks": checks, "manifest": None}

    manifest = read_json(manifest_path)
    embedded = dict(manifest)
    expected_manifest_hash = str(embedded.pop("manifest_hash", ""))
    checks.update(
        {
            "manifest_sha256": sha256_file(manifest_path)
            == receipt.get("manifest_sha256"),
            "manifest_hash": bool(expected_manifest_hash)
            and canonical_json_hash(embedded) == expected_manifest_hash
            == receipt.get("manifest_hash"),
            "status": manifest.get("status") == "frozen_before_model_calls",
            "entrypoint": manifest.get("entrypoint_module")
            == receipt.get("entrypoint_module"),
            "source_tree": canonical_json_hash(hash_tree(source_root))
            == manifest.get("source_tree_hash")
            == receipt.get("source_tree_hash"),
            "declared_hashes": all(
                (manifest.get("file_hashes") or {}).get(relative) == expected
                for relative, expected in expected_code_hashes.items()
            )
            and set(manifest.get("file_hashes") or {})
            <= set(expected_code_hashes) | {"skillscriptbench/__init__.py"},
            "file_receipts": all(
                (source_root / row["path"]).is_file()
                and sha256_file(source_root / row["path"]) == row["sha256"]
                and (source_root / row["path"]).stat().st_size == row["bytes"]
                for row in manifest.get("files") or []
            ),
            "no_compiled_cache": not any(
                path.suffix in {".pyc", ".pyo"}
                or "__pycache__" in path.relative_to(source_root).parts
                for path in source_root.rglob("*")
                if path.is_file()
            ),
        }
    )
    return {
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "manifest": manifest,
    }


def workspace_drift(
    workspace_root: str | Path, expected_code_hashes: dict[str, str]
) -> list[dict[str, Any]]:
    """Report current-workspace drift without making it a rerun blocker."""

    workspace = Path(workspace_root).resolve()
    rows = []
    for relative, expected in sorted(expected_code_hashes.items()):
        path = workspace / relative
        actual = sha256_file(path) if path.is_file() else None
        if actual != expected:
            rows.append(
                {
                    "path": relative,
                    "expected_sha256": expected,
                    "workspace_sha256": actual,
                }
            )
    return rows


def capsule_environment(stage_root: str | Path) -> dict[str, str]:
    """Return an environment that imports the frozen source tree first."""

    source_root = (
        Path(stage_root).resolve() / CAPSULE_DIRECTORY / SOURCE_DIRECTORY
    )
    if not source_root.is_dir():
        raise FileNotFoundError(f"source_capsule_source_missing:{source_root}")
    environment = dict(os.environ)
    current = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(source_root) if not current else f"{source_root}{os.pathsep}{current}"
    )
    environment.setdefault("PYTHON", sys.executable)
    return environment
