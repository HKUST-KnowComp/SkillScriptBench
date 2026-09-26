from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from skillscriptbench.io_utils import (
    canonical_json_hash,
    copy_tree_clean,
    hash_tree,
    sha256_file,
)


ACCEPT_STRUCTURALLY = "ACCEPT_STRUCTURALLY"
MAX_EDITS = 2
MAX_OBSERVED_LINES = 30
MAX_REPLACEMENT_LINES = 40
MAX_REPLACEMENT_BYTES = 8000


PYTHON_SPAN_PATCH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_skill_patch",
        "description": (
            "Submit zero, one, or two bounded Python source-span replacements. All conditions use "
            "the same parser, normalizer, syntax check, public-API check, and structural gate."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "edits"],
            "properties": {
                "summary": {"type": "string"},
                "edits": {
                    "type": "array",
                    "maxItems": MAX_EDITS,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "operation",
                            "path",
                            "symbol",
                            "observed_source",
                            "replacement_source",
                            "target_node_id",
                            "expected_node_sha256",
                        ],
                        "properties": {
                            "operation": {"type": "string", "enum": ["replace_span"]},
                            "path": {"type": "string"},
                            "symbol": {"type": "string"},
                            "observed_source": {"type": "string"},
                            "replacement_source": {"type": "string"},
                            "target_node_id": {"type": "string"},
                            "expected_node_sha256": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}


def _safe_path(root: Path, relative: str) -> Path:
    posix = PurePosixPath(relative)
    if posix.is_absolute() or ".." in posix.parts or posix.suffix != ".py":
        raise ValueError(f"unsafe_python_path:{relative}")
    if "scripts" not in posix.parts:
        raise ValueError(f"edit_outside_scripts:{relative}")
    target = root.joinpath(*posix.parts)
    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"path_escape:{relative}")
    return target


def _strip_fence(value: str) -> str:
    text = value.replace("\r\n", "\n")
    match = re.fullmatch(r"\s*```(?:python)?\s*\n(.*?)\n```\s*", text, re.S | re.I)
    return match.group(1) if match else text


def _parse_response(content: str) -> tuple[dict[str, Any], list[str]]:
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise TypeError("patch_payload_must_be_object")
    ignored = sorted(set(payload) - {"summary", "edits"})
    edits = payload.get("edits")
    if not isinstance(edits, list) or len(edits) > MAX_EDITS:
        raise ValueError("bounded_edits_required")
    normalized: list[dict[str, str]] = []
    for index, raw in enumerate(edits):
        if not isinstance(raw, dict):
            raise TypeError(f"edit_must_be_object:{index}")
        ignored.extend(f"edits[{index}].{key}" for key in sorted(set(raw) - {
            "operation", "path", "symbol", "observed_source", "replacement_source",
            "replacement", "target_node_id", "expected_node_sha256"
        }))
        operation = str(raw.get("operation") or "replace_span")
        if operation not in {"replace_span", "replace_node", "replace_statement"}:
            raise ValueError(f"unsupported_operation:{operation}")
        replacement = raw.get("replacement_source", raw.get("replacement"))
        row = {
            "operation": "replace_span",
            "path": str(raw.get("path") or ""),
            "symbol": str(raw.get("symbol") or ""),
            "observed_source": _strip_fence(str(raw.get("observed_source") or "")),
            "replacement_source": _strip_fence(str(replacement or "")),
            "target_node_id": str(raw.get("target_node_id") or ""),
            "expected_node_sha256": str(raw.get("expected_node_sha256") or ""),
        }
        if not row["path"] or not row["observed_source"]:
            raise ValueError(f"edit_path_and_observed_source_required:{index}")
        if row["observed_source"] == row["replacement_source"]:
            continue
        if len(row["observed_source"].splitlines()) > MAX_OBSERVED_LINES:
            raise ValueError(f"observed_span_too_large:{index}")
        if len(row["replacement_source"].splitlines()) > MAX_REPLACEMENT_LINES:
            raise ValueError(f"replacement_span_too_large:{index}")
        if len(row["replacement_source"].encode("utf-8")) > MAX_REPLACEMENT_BYTES:
            raise ValueError(f"replacement_bytes_too_large:{index}")
        if re.search(r"(?m)^\s*(?:async\s+def|def|class)\s+", row["replacement_source"]):
            raise ValueError(f"declaration_replacement_forbidden:{index}")
        normalized.append(row)
    return {"summary": str(payload.get("summary") or ""), "edits": normalized}, ignored


def _line_col_offset(source: str, line: int, byte_column: int) -> int:
    lines = source.splitlines(keepends=True)
    if line < 1 or line > len(lines):
        raise ValueError("node_line_out_of_range")
    current = lines[line - 1]
    prefix = current.encode("utf-8")[:byte_column].decode("utf-8")
    return sum(len(value) for value in lines[: line - 1]) + len(prefix)


def _bound_span(
    source: str,
    edit: dict[str, str],
    facts: dict[str, Any],
) -> tuple[int, int, dict[str, Any]]:
    matches = [
        row
        for row in facts.get("editable_nodes", [])
        if row.get("node_id") == edit["target_node_id"]
    ]
    if len(matches) != 1:
        raise ValueError(f"ast_node_binding_not_unique:{len(matches)}")
    node = matches[0]
    if node["path"] != edit["path"]:
        raise ValueError("ast_node_path_mismatch")
    if node["node_sha256"] != edit["expected_node_sha256"]:
        raise ValueError("ast_node_sha256_mismatch")
    start = _line_col_offset(source, int(node["line"]), int(node["column"]))
    end = _line_col_offset(source, int(node["end_line"]), int(node["end_column"]))
    observed = source[start:end]
    if observed != node["observed_source"] or observed != edit["observed_source"]:
        raise ValueError("ast_observed_source_mismatch")
    return start, end, {"mode": "exact_visible_ast_node", "node_id": node["node_id"]}


def _unbound_span(source: str, observed: str) -> tuple[int, int, dict[str, Any]]:
    starts: list[int] = []
    offset = 0
    while True:
        found = source.find(observed, offset)
        if found < 0:
            break
        starts.append(found)
        offset = found + max(1, len(observed))
    if len(starts) != 1:
        raise ValueError(f"unbound_observed_source_not_unique:{len(starts)}")
    return starts[0], starts[0] + len(observed), {"mode": "unique_exact_visible_span"}


def _api_signatures(package: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for file in sorted(package.rglob("*.py")):
        relative = file.relative_to(package).as_posix()
        if "tests" in Path(relative).parts or file.name.startswith("test_"):
            continue
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=relative)
        signatures: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name.startswith("_"):
                    continue
                if isinstance(node, ast.ClassDef):
                    signatures.append(f"class {node.name}")
                else:
                    signatures.append(
                        f"{type(node).__name__}:{node.name}:{ast.dump(node.args, include_attributes=False)}"
                    )
        result[relative] = signatures
    return result


def apply_python_span_patch(
    content: str,
    source_package: str | Path,
    candidate_package: str | Path,
    *,
    visible_ast_facts: dict[str, Any] | None,
    require_ast_binding: bool,
) -> dict[str, Any]:
    source_root = Path(source_package).resolve()
    candidate_root = Path(candidate_package).resolve()
    if require_ast_binding and not isinstance(visible_ast_facts, dict):
        raise ValueError("visible_ast_facts_required")
    parsed, ignored_fields = _parse_response(content)
    before_api = _api_signatures(source_root)
    copy_tree_clean(source_root, candidate_root)
    grouped: dict[str, list[tuple[int, int, dict[str, str], dict[str, Any]]]] = defaultdict(list)
    receipts: list[dict[str, Any]] = []
    for edit in parsed["edits"]:
        target = _safe_path(candidate_root, edit["path"])
        if not target.is_file():
            raise FileNotFoundError(edit["path"])
        source = target.read_text(encoding="utf-8")
        if require_ast_binding:
            start, end, locator = _bound_span(source, edit, visible_ast_facts or {})
        else:
            if edit["target_node_id"] or edit["expected_node_sha256"]:
                ignored_fields.extend(["unbound_target_node_id", "unbound_expected_node_sha256"])
            start, end, locator = _unbound_span(source, edit["observed_source"])
        grouped[edit["path"]].append((start, end, edit, locator))

    for relative, edits in grouped.items():
        target = _safe_path(candidate_root, relative)
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
        ast.parse(source, filename=relative)
        target.write_text(source, encoding="utf-8")

    after_api = _api_signatures(candidate_root)
    if before_api != after_api:
        raise ValueError("public_api_signature_changed")
    source_hashes = hash_tree(source_root)
    candidate_hashes = hash_tree(candidate_root)
    changed_paths = sorted(
        path
        for path in set(source_hashes) | set(candidate_hashes)
        if source_hashes.get(path) != candidate_hashes.get(path)
    )
    if len(changed_paths) > MAX_EDITS or any(
        not path.endswith(".py") or "scripts/" not in path for path in changed_paths
    ):
        raise ValueError(f"changed_path_scope_invalid:{changed_paths}")
    gate = {
        "decision": ACCEPT_STRUCTURALLY,
        "checks": {
            "python_parse": True,
            "public_api_signatures_preserved": True,
            "changed_paths_within_scripts": True,
            "bounded_edit_count": len(parsed["edits"]) <= MAX_EDITS,
            "hidden_semantic_correctness_checked": False,
        },
        "claim_boundary": "Structural acceptance is not task correctness.",
    }
    return {
        "status": "candidate_materialized",
        "summary": parsed["summary"],
        "edit_count": len(parsed["edits"]),
        "changed_paths": changed_paths,
        "ignored_response_fields": sorted(set(ignored_fields)),
        "edit_receipts": receipts,
        "candidate_tree_hash": canonical_json_hash(candidate_hashes),
        "structural_gate": gate,
    }
