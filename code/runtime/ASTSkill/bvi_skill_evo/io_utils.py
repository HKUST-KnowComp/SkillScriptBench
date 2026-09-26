from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Iterable


IGNORED_PARTS = {".git", ".pytest_cache", "__pycache__"}
IGNORED_NAMES = {".DS_Store", ".env"}
IGNORED_SUFFIXES = {".pyc", ".pyo", ".orig", ".rej"}


def stable_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def stable_hash(payload: Any) -> str:
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tracked_files(root: str | Path) -> list[Path]:
    base = Path(root).resolve()
    files: list[Path] = []
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(base)
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        if path.name in IGNORED_NAMES or path.suffix in IGNORED_SUFFIXES or path.name.endswith("~"):
            continue
        files.append(path)
    return sorted(files)


def hash_tree(root: str | Path) -> dict[str, str]:
    base = Path(root).resolve()
    return {str(path.relative_to(base)): sha256_file(path) for path in tracked_files(base)}


def tree_hash(root: str | Path) -> str:
    return stable_hash(hash_tree(root))


def write_json(path: str | Path, payload: Any) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, destination)


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def copy_tree(source: str | Path, destination: str | Path) -> Path:
    src = Path(source).resolve()
    dst = Path(destination).resolve()
    if dst.exists():
        shutil.rmtree(dst)

    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in IGNORED_PARTS
            or name in IGNORED_NAMES
            or Path(name).suffix in IGNORED_SUFFIXES
            or name.endswith("~")
        }

    shutil.copytree(src, dst, ignore=ignore)
    return dst


def assert_within(path: str | Path, root: str | Path) -> Path:
    candidate = Path(path).resolve()
    base = Path(root).resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError(f"Path escapes root: {candidate} not within {base}")
    return candidate


def paths_hash(paths: Iterable[str | Path]) -> str:
    payload = {str(Path(path)): sha256_file(Path(path)) for path in sorted(map(Path, paths))}
    return stable_hash(payload)
