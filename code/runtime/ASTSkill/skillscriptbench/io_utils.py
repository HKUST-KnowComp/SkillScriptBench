from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable


IGNORED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, payload: Any) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return sha256_bytes(encoded)


def iter_benchmark_files(root: str | Path) -> Iterable[Path]:
    base = Path(root)
    if not base.exists():
        return
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(base)
        if any(part in IGNORED_DIRECTORIES for part in relative.parts):
            continue
        if path.suffix.lower() in IGNORED_SUFFIXES:
            continue
        yield path


def hash_tree(root: str | Path) -> dict[str, str]:
    base = Path(root)
    return {
        path.relative_to(base).as_posix(): sha256_file(path)
        for path in iter_benchmark_files(base)
    }


def copy_tree_clean(source: str | Path, destination: str | Path) -> None:
    source_path = Path(source)
    destination_path = Path(destination)
    if destination_path.exists():
        shutil.rmtree(destination_path)

    def ignore(directory: str, names: list[str]) -> set[str]:
        directory_path = Path(directory)
        unusable_symlinks: set[str] = set()
        for name in names:
            candidate = directory_path / name
            if not candidate.is_symlink():
                continue
            try:
                candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                unusable_symlinks.add(name)

        return {
            name
            for name in names
            if name in IGNORED_DIRECTORIES or Path(name).suffix.lower() in IGNORED_SUFFIXES
        } | unusable_symlinks

    shutil.copytree(source_path, destination_path, ignore=ignore)
