from __future__ import annotations

import copy
import subprocess
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from bvi_skill_evo import python_span_patch_v237 as base
from bvi_skill_evo.python_span_patch_v246 import _parse_response
from skillscriptbench.io_utils import canonical_json_hash, copy_tree_clean, hash_tree
from skillscriptbench.multilang_structural_v66 import (
    JS_SUFFIXES,
    SHELL_SUFFIXES,
    extract_javascript_nodes,
    extract_shell_nodes,
)


SCHEMA_VERSION = "2.86-universal-span-patch-v1"
SCRIPT_SUFFIXES = {".py", *JS_SUFFIXES, *SHELL_SUFFIXES}
UNIVERSAL_SPAN_PATCH_TOOL = copy.deepcopy(base.PYTHON_SPAN_PATCH_TOOL)
UNIVERSAL_SPAN_PATCH_TOOL["function"]["description"] = (
    "Submit zero, one, or two bounded source-span replacements inside visible Agent Skill scripts. "
    "Supported files are Python, JavaScript, TypeScript, and Shell. The post-hoc structural gate "
    "resolves submitted spans to language AST nodes after the proposal is materialized."
)


def _safe_script_path(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or pure.suffix.lower() not in SCRIPT_SUFFIXES:
        raise ValueError(f"unsafe_script_path:{relative}")
    if not pure.parts or pure.parts[0] != "scripts":
        raise ValueError(f"edit_outside_scripts:{relative}")
    target = root.joinpath(*pure.parts)
    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"path_escape:{relative}")
    return target


def _validate_syntax(path: Path, relative: str) -> dict[str, Any]:
    source = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".py":
        import ast

        ast.parse(source, filename=relative)
        backend = "python_ast"
    elif suffix in JS_SUFFIXES:
        extract_javascript_nodes(relative, source)
        backend = "babel_ast_v66"
    elif suffix in SHELL_SUFFIXES:
        completed = subprocess.run(
            ["bash", "-n", str(path)], capture_output=True, text=True, timeout=30, check=False
        )
        if completed.returncode != 0:
            raise SyntaxError(f"bash_n_failed:{relative}:{completed.stderr[-600:]}")
        extract_shell_nodes(relative, source)
        backend = "bash_n_plus_tree_sitter_bash_v66"
    else:
        raise ValueError(f"unsupported_script_suffix:{relative}")
    return {"path": relative, "status": "pass", "backend": backend}


def apply_universal_span_patch(
    content: str,
    source_package: str | Path,
    candidate_package: str | Path,
) -> dict[str, Any]:
    source_root = Path(source_package).resolve()
    candidate_root = Path(candidate_package).resolve()
    parsed, ignored_fields = _parse_response(content)
    copy_tree_clean(source_root, candidate_root)
    grouped: dict[str, list[tuple[int, int, dict[str, str], dict[str, str]]]] = defaultdict(list)
    for edit in parsed["edits"]:
        target = _safe_script_path(candidate_root, edit["path"])
        if not target.is_file():
            raise FileNotFoundError(edit["path"])
        source = target.read_text(encoding="utf-8")
        if edit["target_node_id"] or edit["expected_node_sha256"]:
            ignored_fields.extend(["unbound_target_node_id", "unbound_expected_node_sha256"])
        start, end, locator = base._unbound_span(source, edit["observed_source"])
        grouped[edit["path"]].append((start, end, edit, locator))

    receipts: list[dict[str, Any]] = []
    syntax: list[dict[str, Any]] = []
    for relative, edits in grouped.items():
        target = _safe_script_path(candidate_root, relative)
        source = target.read_text(encoding="utf-8")
        ordered = sorted(edits, key=lambda row: (row[0], row[1]), reverse=True)
        for (start, end, edit, locator), next_row in zip(ordered, ordered[1:] + [None]):
            if next_row is not None and next_row[1] > start:
                raise ValueError("overlapping_edits_forbidden")
            source = source[:start] + edit["replacement_source"] + source[end:]
            receipts.append(
                {
                    "path": relative,
                    "symbol": edit["symbol"],
                    "locator": locator,
                    "observed_sha256": canonical_json_hash(edit["observed_source"]),
                    "replacement_sha256": canonical_json_hash(edit["replacement_source"]),
                }
            )
        target.write_text(source, encoding="utf-8")
        syntax.append(_validate_syntax(target, relative))

    source_hashes = hash_tree(source_root)
    candidate_hashes = hash_tree(candidate_root)
    changed_paths = sorted(
        path
        for path in set(source_hashes) | set(candidate_hashes)
        if source_hashes.get(path) != candidate_hashes.get(path)
    )
    if len(changed_paths) > base.MAX_EDITS or any(
        not path.startswith("scripts/") or Path(path).suffix.lower() not in SCRIPT_SUFFIXES
        for path in changed_paths
    ):
        raise ValueError(f"changed_path_scope_invalid:{changed_paths}")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "candidate_materialized",
        "summary": parsed["summary"],
        "edit_count": len(parsed["edits"]),
        "changed_paths": changed_paths,
        "ignored_response_fields": sorted(set(ignored_fields)),
        "edit_receipts": receipts,
        "syntax": syntax,
        "candidate_tree_hash": canonical_json_hash(candidate_hashes),
        "structural_gate": {
            "decision": base.ACCEPT_STRUCTURALLY,
            "hidden_semantic_correctness_checked": False,
            "claim_boundary": "Materialization and syntax validity are not task correctness.",
        },
    }

