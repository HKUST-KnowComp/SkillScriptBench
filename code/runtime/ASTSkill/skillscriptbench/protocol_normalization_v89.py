from __future__ import annotations

import json
import re
import ast
from pathlib import Path
from typing import Any

from skillscriptbench.io_utils import canonical_json_hash, sha256_file


SCHEMA_VERSION = "0.89-condition-blind-patch-protocol-normalization-v1"
PATCH_EDIT_KEYS = {
    "path",
    "expected_file_sha256",
    "operation",
    "target_node_id",
    "expected_node_sha256",
    "symbol",
    "start_line",
    "end_line",
    "replacement",
}
SEMANTIC_EDIT_FIELDS = {
    "path",
    "start_line",
    "end_line",
    "replacement",
}
PROTOCOL_DEFAULTS = {
    "operation": "replace_lines",
    "target_node_id": "",
    "expected_node_sha256": "",
    "symbol": "",
}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _semantic_edit_view(value: Any) -> Any:
    if not isinstance(value, list):
        return None
    result = []
    for edit in value:
        if not isinstance(edit, dict):
            return None
        result.append({key: edit.get(key) for key in sorted(SEMANTIC_EDIT_FIELDS)})
    return result


def _more_complete_protocol_value(left: Any, right: Any) -> Any:
    left_semantic = _semantic_edit_view(left)
    right_semantic = _semantic_edit_view(right)
    if (
        left_semantic is None
        or right_semantic is None
        or left_semantic != right_semantic
    ):
        return None
    left_fields = sum(len(item) for item in left) if isinstance(left, list) else 0
    right_fields = sum(len(item) for item in right) if isinstance(right, list) else 0
    return right if right_fields >= left_fields else left


def _strip_code_fence(content: str) -> tuple[str, bool]:
    stripped = content.strip()
    if not stripped.startswith("```"):
        return stripped, stripped != content
    lines = stripped.splitlines()
    if len(lines) < 3 or lines[-1].strip() != "```":
        return stripped, stripped != content
    return "\n".join(lines[1:-1]).strip(), True


def _decode_json_object(content: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    text, fence_removed = _strip_code_fence(content)
    corrections: list[dict[str, Any]] = []
    if fence_removed:
        corrections.append({"action": "removed_outer_code_fence"})

    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key not in result:
                result[key] = value
                continue
            if result[key] != value:
                preferred = (
                    _more_complete_protocol_value(result[key], value)
                    if key == "edits"
                    else None
                )
                if preferred is None:
                    raise ValueError(f"conflicting_duplicate_json_key:{key}")
                result[key] = preferred
                corrections.append(
                    {
                        "action": "collapsed_semantically_equivalent_duplicate_edits",
                        "field": key,
                        "value_hash": canonical_json_hash(_semantic_edit_view(value)),
                    }
                )
                continue
            corrections.append(
                {
                    "action": "collapsed_identical_duplicate_json_key",
                    "field": key,
                    "value_hash": canonical_json_hash(value),
                }
            )
        return result

    payload = json.loads(text, object_pairs_hook=hook)
    if not isinstance(payload, dict):
        raise ValueError("patch_arguments_must_be_object")
    return payload, corrections


def _visible_target(source_package: Path, relative: str, edit_index: int) -> Path:
    pure = Path(relative)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or not (relative == "SKILL.md" or relative.startswith("scripts/"))
    ):
        raise ValueError(f"edit_{edit_index}_path_outside_visible_package")
    target = source_package / pure
    if not target.is_file():
        raise FileNotFoundError(f"edit_{edit_index}_path_missing:{relative}")
    return target


def _function_start_line(source: str, replacement: str, end_line: int) -> int | None:
    try:
        replacement_tree = ast.parse(replacement)
        source_tree = ast.parse(source)
    except SyntaxError:
        return None
    if len(replacement_tree.body) != 1 or not isinstance(
        replacement_tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        return None
    name = replacement_tree.body[0].name
    matches = [
        node
        for node in ast.walk(source_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
        and int(getattr(node, "end_lineno", -1)) == end_line
    ]
    if len(matches) != 1:
        return None
    node = matches[0]
    decorators = [int(item.lineno) for item in node.decorator_list]
    return min([int(node.lineno), *decorators])


def _visible_anchor_start_line(source: str, replacement: str, end_line: int) -> int | None:
    source_lines = source.splitlines()
    replacement_lines = [line for line in replacement.splitlines() if line.strip()]
    for replacement_line in replacement_lines:
        matches = [
            index
            for index, source_line in enumerate(source_lines[:end_line], start=1)
            if source_line == replacement_line
            or source_line.strip() == replacement_line.strip()
        ]
        if len(matches) == 1 and matches[0] <= end_line:
            return matches[0]
    return None


def _infer_start_line(target: Path, replacement: str, end_line: int) -> int | None:
    source = target.read_text(encoding="utf-8")
    if target.suffix.lower() == ".py":
        function_line = _function_start_line(source, replacement, end_line)
        if function_line is not None:
            return function_line
    return _visible_anchor_start_line(source, replacement, end_line)


def normalize_patch_arguments(
    content: str,
    source_package: str | Path,
) -> tuple[str, dict[str, Any]]:
    """Normalize protocol representation without changing the proposed code edit."""

    package = Path(source_package).resolve()
    payload, corrections = _decode_json_object(content)
    if not set(payload) <= {"edits", "summary"} or "edits" not in payload:
        raise ValueError("patch_top_level_schema_not_normalizable")
    if "summary" not in payload:
        payload["summary"] = ""
        corrections.append(
            {"action": "filled_protocol_default", "field": "summary"}
        )
    if not isinstance(payload["summary"], str):
        raise TypeError("patch_summary_must_be_string")
    edits = payload["edits"]
    if not isinstance(edits, list):
        raise TypeError("patch_edits_must_be_array")

    normalized_edits: list[dict[str, Any]] = []
    for index, original in enumerate(edits):
        if not isinstance(original, dict):
            raise TypeError(f"edit_{index}_must_be_object")
        unknown = set(original) - PATCH_EDIT_KEYS
        if unknown:
            raise ValueError(
                f"edit_{index}_unknown_fields:{','.join(sorted(unknown))}"
            )
        edit = dict(original)
        if "path" not in edit or "replacement" not in edit:
            raise ValueError(f"edit_{index}_semantic_fields_missing")
        relative = edit["path"]
        replacement = edit["replacement"]
        if not isinstance(relative, str) or not isinstance(replacement, str):
            raise TypeError(f"edit_{index}_semantic_field_type_invalid")
        target = _visible_target(package, relative, index)

        for field, default in PROTOCOL_DEFAULTS.items():
            if field not in edit:
                edit[field] = default
                corrections.append(
                    {
                        "action": "filled_protocol_default",
                        "edit_index": index,
                        "field": field,
                        "value": default,
                    }
                )
        operation = str(edit.get("operation") or "replace_lines")
        if "start_line" not in edit and operation in {
            "replace_node",
            "insert_parameter",
            "append_markdown",
        }:
            edit["start_line"] = 0
            corrections.append(
                {
                    "action": "filled_structural_zero_line",
                    "edit_index": index,
                    "field": "start_line",
                    "value": 0,
                }
            )
        elif "start_line" not in edit and "end_line" in edit:
            end_value = edit["end_line"]
            if isinstance(end_value, str) and end_value.isdecimal():
                end_value = int(end_value)
            inferred = (
                0
                if operation in {"replace_node", "insert_parameter", "append_markdown"}
                else _infer_start_line(target, replacement, end_value)
                if isinstance(end_value, int) and not isinstance(end_value, bool)
                else None
            )
            if inferred is not None:
                edit["start_line"] = inferred
                corrections.append(
                    {
                        "action": "inferred_visible_start_line",
                        "edit_index": index,
                        "field": "start_line",
                        "value": inferred,
                    }
                )
        if "end_line" not in edit and operation in {
            "replace_node",
            "insert_parameter",
            "append_markdown",
        }:
            edit["end_line"] = 0
            corrections.append(
                {
                    "action": "filled_structural_zero_line",
                    "edit_index": index,
                    "field": "end_line",
                    "value": 0,
                }
            )
        for field in ("start_line", "end_line"):
            if field not in edit:
                raise ValueError(f"edit_{index}_{field}_missing")
            value = edit[field]
            if isinstance(value, str) and value.isdecimal():
                edit[field] = int(value)
                corrections.append(
                    {
                        "action": "coerced_decimal_string_to_integer",
                        "edit_index": index,
                        "field": field,
                    }
                )
            if isinstance(edit[field], bool) or not isinstance(edit[field], int):
                raise TypeError(f"edit_{index}_{field}_must_be_integer")

        supplied_hash = edit.get("expected_file_sha256")
        if supplied_hash is None or (
            isinstance(supplied_hash, str)
            and not SHA256_PATTERN.fullmatch(supplied_hash)
        ):
            edit["expected_file_sha256"] = sha256_file(target)
            corrections.append(
                {
                    "action": "filled_visible_file_sha256",
                    "edit_index": index,
                    "field": "expected_file_sha256",
                    "supplied_value_hash": canonical_json_hash(supplied_hash),
                    "path": relative,
                }
            )
        elif not isinstance(supplied_hash, str):
            raise TypeError(f"edit_{index}_expected_file_sha256_must_be_string")

        for field in ("target_node_id", "expected_node_sha256", "symbol"):
            if not isinstance(edit[field], str):
                raise TypeError(f"edit_{index}_{field}_must_be_string")
        if not isinstance(edit["operation"], str):
            raise TypeError(f"edit_{index}_operation_must_be_string")

        if (
            edit["start_line"] == 0
            and edit["end_line"] == 0
            and edit["replacement"] == ""
        ):
            corrections.append(
                {
                    "action": "canonicalized_empty_zero_range_to_abstention",
                    "edit_index": index,
                }
            )
            continue
        normalized_edits.append({key: edit[key] for key in sorted(PATCH_EDIT_KEYS)})

    payload = {"edits": normalized_edits, "summary": payload["summary"]}
    normalized = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "status": "normalized" if corrections else "valid_as_returned",
        "corrections": corrections,
        "correction_count": len(corrections),
        "semantic_fields_changed": False,
        "forbidden_automatic_repairs": [
            "path_change",
            "line_range_shift",
            "replacement_change",
            "hidden_artifact_lookup",
        ],
        "normalized_arguments_sha256": canonical_json_hash(payload),
    }
    receipt["normalization_hash"] = canonical_json_hash(receipt)
    return normalized, receipt


def classify_failure_text(message: str) -> str:
    if any(
        marker in message
        for marker in (
            "SyntaxError:",
            "IndentationError:",
            "TabError:",
            "js_ts_parse_failed",
            "babel_parse_failed",
            "shell_parse_failed",
        )
    ):
        return "candidate_syntax_invalid"
    if any(
        marker in message
        for marker in (
            "schema",
            "duplicate_json_key",
            "patch_",
            "response_must_contain",
            "edits_must_contain",
            "file_sha256_mismatch",
            "line_range_invalid",
            "path_outside",
            "path_missing",
            "operation_invalid",
            "target_not_selected",
        )
    ):
        return "protocol_invalid"
    return "candidate_structural_invalid"


def classify_candidate_failure(error: BaseException) -> str:
    if isinstance(error, json.JSONDecodeError):
        return "protocol_invalid"
    return classify_failure_text(f"{type(error).__name__}:{error}")
