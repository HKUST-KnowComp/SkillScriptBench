from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import Any

from skillscriptbench.io_utils import sha256_file
from skillscriptbench.js_ts_discrimination import _node_check
from skillscriptbench.multilang_structural_v66 import (
    JS_SUFFIXES,
    SHELL_SUFFIXES,
    enumerate_package_nodes,
)
from skillscriptbench.structural_evolution_v65 import apply_structured_edits


SCHEMA_VERSION = "bvi.multilang-node-patch.v1"
ACCEPT_STRUCTURALLY = "ACCEPT_STRUCTURALLY"
REJECT_STRUCTURALLY = "REJECT_STRUCTURALLY"
ABSTAIN = "ABSTAIN"

MULTILANG_NODE_PATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_skill_patch",
        "description": "Submit one bounded AST-node edit for a visible executable Agent Skill package.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["edits", "summary"],
            "properties": {
                "edits": {
                    "type": "array",
                    "maxItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "path",
                            "expected_file_sha256",
                            "operation",
                            "target_node_id",
                            "expected_node_sha256",
                            "symbol",
                            "start_line",
                            "start_column",
                            "end_line",
                            "end_column",
                            "observed_source",
                            "replacement",
                        ],
                        "properties": {
                            "path": {
                                "type": "string",
                                "pattern": "^scripts/.+\\.(js|mjs|cjs|ts|sh|bash)$",
                            },
                            "expected_file_sha256": {
                                "type": "string",
                                "pattern": "^[0-9a-f]{64}$",
                            },
                            "operation": {
                                "type": "string",
                                "enum": ["replace_node"],
                            },
                            "target_node_id": {"type": "string"},
                            "expected_node_sha256": {
                                "type": "string",
                                "pattern": "^$|^[0-9a-f]{64}$",
                            },
                            "symbol": {"type": "string"},
                            "start_line": {"type": "integer", "minimum": 1},
                            "start_column": {"type": "integer", "minimum": 0},
                            "end_line": {"type": "integer", "minimum": 1},
                            "end_column": {"type": "integer", "minimum": 0},
                            "observed_source": {"type": "string"},
                            "replacement": {"type": "string"},
                        },
                    },
                },
                "summary": {"type": "string"},
            },
        },
    },
}


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


def _parse_payload(content: str) -> dict[str, Any]:
    value = json.loads(content, object_pairs_hook=_strict_object)
    if not isinstance(value, dict) or set(value) != {"edits", "summary"}:
        raise ValueError("response_schema_invalid")
    if not isinstance(value["summary"], str):
        raise TypeError("summary_must_be_string")
    if not isinstance(value["edits"], list) or len(value["edits"]) > 1:
        raise ValueError("edits_must_contain_zero_or_one_item")
    return value


def _node_span(node: dict[str, Any]) -> tuple[int, int, int, int]:
    span = node["span"]
    return (
        int(span["start_line"]),
        int(span["start_column"]),
        int(span["end_line"]),
        int(span["end_column"]),
    )


def _resolve_node(
    edit: dict[str, Any],
    registry: dict[str, dict[str, Any]],
    *,
    allowed_node_ids: set[str] | None,
    require_ast_binding: bool,
) -> dict[str, Any]:
    node_id = str(edit["target_node_id"])
    node_hash = str(edit["expected_node_sha256"])
    if require_ast_binding:
        if not node_id or not node_hash:
            raise ValueError("ast_condition_requires_node_binding")
        if allowed_node_ids is None or node_id not in allowed_node_ids:
            raise ValueError("ast_target_not_in_visible_facts")
    else:
        if node_id or node_hash:
            raise ValueError("non_ast_condition_must_leave_node_binding_empty")

    if node_id:
        node = registry.get(node_id)
        if node is None:
            raise ValueError("unknown_target_node_id")
        if node_hash != node["node_source_sha256"]:
            raise ValueError("target_node_sha256_mismatch")
        candidates = [node]
    else:
        start_line = int(edit["start_line"])
        end_line = int(edit["end_line"])
        candidates = [
            node
            for node in registry.values()
            if node["path"] == edit["path"]
            and node["observed_source"] == edit["observed_source"]
            and (not edit["symbol"] or node["symbol"] == edit["symbol"])
        ]
        line_candidates = [
            node
            for node in candidates
            if int(node["span"]["start_line"]) <= start_line
            and end_line <= int(node["span"]["end_line"])
        ]
        if line_candidates:
            candidates = line_candidates
    if len(candidates) != 1:
        raise ValueError(f"node_locator_not_unique:{len(candidates)}")
    node = candidates[0]
    if node["path"] != edit["path"]:
        raise ValueError("node_path_mismatch")
    if node["observed_source"] != edit["observed_source"]:
        raise ValueError("observed_source_mismatch")
    if require_ast_binding and _node_span(node) != (
        int(edit["start_line"]),
        int(edit["start_column"]),
        int(edit["end_line"]),
        int(edit["end_column"]),
    ):
        raise ValueError("node_span_mismatch")
    if node["role"] == "function_scope":
        raise ValueError("function_scope_not_editable")
    return node


def _validate_changed_script(path: Path, relative: str) -> dict[str, Any]:
    source = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".py":
        ast.parse(source, filename=relative)
        detail = "python_ast_parse"
    elif suffix in JS_SUFFIXES:
        ok, detail = _node_check(source, suffix)
        if not ok:
            raise SyntaxError(f"js_ts_parse_failed:{relative}:{detail}")
    elif suffix in SHELL_SUFFIXES:
        completed = subprocess.run(
            ["bash", "--noprofile", "--norc", "-n"],
            input=source,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if completed.returncode != 0:
            raise SyntaxError(f"shell_parse_failed:{relative}:{completed.stderr[-800:]}")
        detail = "bash_n"
    else:
        raise ValueError(f"unsupported_changed_script:{relative}")
    return {"path": relative, "status": "pass", "detail": detail}


def apply_multilang_node_patch(
    content: str,
    source_package: str | Path,
    candidate_package: str | Path,
    *,
    visible_ast_facts: dict[str, Any] | None,
    require_ast_binding: bool,
) -> dict[str, Any]:
    source = Path(source_package)
    candidate = Path(candidate_package)
    payload = _parse_payload(content)
    nodes = enumerate_package_nodes(source, include_markdown=False)
    registry = {str(node["site_id"]): node for node in nodes}
    allowed_ids = (
        {
            str(row["node_id"])
            for row in (visible_ast_facts or {}).get("editable_nodes", [])
        }
        if visible_ast_facts is not None
        else None
    )
    normalized: list[dict[str, Any]] = []
    for index, edit in enumerate(payload["edits"]):
        required = {
            "path",
            "expected_file_sha256",
            "operation",
            "target_node_id",
            "expected_node_sha256",
            "symbol",
            "start_line",
            "start_column",
            "end_line",
            "end_column",
            "observed_source",
            "replacement",
        }
        if not isinstance(edit, dict) or set(edit) != required:
            raise ValueError(f"edit_{index}_schema_invalid")
        if edit["operation"] != "replace_node":
            raise ValueError(f"edit_{index}_operation_invalid")
        relative = str(edit["path"])
        if not relative.startswith("scripts/"):
            raise ValueError(f"edit_{index}_outside_scripts")
        file = source / relative
        if not file.is_file() or sha256_file(file) != edit["expected_file_sha256"]:
            raise ValueError(f"edit_{index}_file_hash_mismatch")
        replacement = edit["replacement"]
        if not isinstance(replacement, str) or len(replacement.encode("utf-8")) > 4096:
            raise ValueError(f"edit_{index}_replacement_invalid")
        node = _resolve_node(
            edit,
            registry,
            allowed_node_ids=allowed_ids,
            require_ast_binding=require_ast_binding,
        )
        normalized.append(
            {
                "path": relative,
                "expected_file_sha256": edit["expected_file_sha256"],
                "operation": "replace_node",
                "target_node_id": node["site_id"],
                "expected_node_sha256": node["node_source_sha256"],
                "symbol": node["symbol"],
                "start_line": 0,
                "end_line": 0,
                "replacement": replacement,
            }
        )
    application = apply_structured_edits(
        normalized,
        source,
        candidate,
        node_registry=registry,
        allowed_edit_paths={str(edit["path"]) for edit in payload["edits"]},
    )
    syntax = [
        _validate_changed_script(candidate / relative, relative)
        for relative in application["changed_paths"]
    ]
    decision = (
        ABSTAIN
        if not application["changed_paths"]
        else ACCEPT_STRUCTURALLY
        if application["edit_count"] == 1 and len(application["changed_paths"]) == 1
        else REJECT_STRUCTURALLY
    )
    return {
        "schema_version": SCHEMA_VERSION,
        **application,
        "summary": payload["summary"],
        "syntax": syntax,
        "structural_gate": {
            "decision": decision,
            "semantic_correctness": "unknown",
            "outside_selected_node_preserved_by_construction": True,
            "ast_binding_required": require_ast_binding,
        },
    }
