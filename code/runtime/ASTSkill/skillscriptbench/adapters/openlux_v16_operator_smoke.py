from __future__ import annotations

import argparse
import ast
import copy
import datetime as dt
import getpass
import json
import math
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable

from skillscriptbench.adapters.openlux_v04_smoke import (
    _call_chat_completion,
    _call_responses,
    _model_content,
    _normalized_usage,
    _parse_model_json,
)
from skillscriptbench.adapters.bounded_exact_edits_v16 import (
    exact_replacement_instruction,
    parse_and_apply_exact_replacements,
)
from skillscriptbench.io_utils import (
    canonical_json_hash,
    hash_tree,
    read_json,
    sha256_bytes,
    sha256_file,
    write_json,
)
from skillscriptbench.mutation_operators_v13 import apply_python_behavior_operator_v13


CONDITIONS = ("raw-script", "skill-script")
DEFAULT_OPERATOR_PRIORITY = (
    "toggle_python_comparison_boundary_v13",
    "decrement_python_slice_upper_v13",
    "remove_python_path_normalization_v13",
    "swap_python_join_delimiter_v13",
    "toggle_python_guard_connector_v13",
    "toggle_python_membership_polarity_v13",
    "toggle_python_return_status_v13",
    "swap_python_output_stream_v13",
    "swap_python_write_stream_v13",
    "toggle_python_exit_call_status_v13",
    "increment_python_control_keyword_v13",
)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _read_utf8_exact(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _ast_dump(source: str) -> str:
    return ast.dump(ast.parse(source), include_attributes=False)


def _ast_hash(source: str) -> str:
    return sha256_bytes(_ast_dump(source).encode("utf-8"))


def _ast_data(value: Any) -> Any:
    if isinstance(value, ast.AST):
        return {
            "_type": type(value).__name__,
            **{field: _ast_data(getattr(value, field)) for field in value._fields},
        }
    if isinstance(value, list):
        return [_ast_data(item) for item in value]
    return value


def _leaf_diffs(left: Any, right: Any, path: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    if type(left) is not type(right):
        return [{"path": list(path), "left": left, "right": right}]
    if isinstance(left, dict):
        rows: list[dict[str, Any]] = []
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                rows.append(
                    {
                        "path": list((*path, key)),
                        "left": left.get(key, "<missing>"),
                        "right": right.get(key, "<missing>"),
                    }
                )
            else:
                rows.extend(_leaf_diffs(left[key], right[key], (*path, key)))
        return rows
    if isinstance(left, list):
        rows = []
        common = min(len(left), len(right))
        for index in range(common):
            rows.extend(_leaf_diffs(left[index], right[index], (*path, index)))
        for index in range(common, max(len(left), len(right))):
            rows.append(
                {
                    "path": list((*path, index)),
                    "left": left[index] if index < len(left) else "<missing>",
                    "right": right[index] if index < len(right) else "<missing>",
                }
            )
        return rows
    return [] if left == right else [{"path": list(path), "left": left, "right": right}]


def _leaf_count(value: Any) -> int:
    if isinstance(value, dict):
        return sum(_leaf_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_leaf_count(item) for item in value)
    return 1


def _bounded_ast_similarity(
    oracle_data: Any,
    candidate_data: Any,
    oracle_dump: str,
    candidate_dump: str,
    candidate_oracle_diffs: list[dict[str, Any]],
    *,
    exact_sequence_limit: int = 100_000,
) -> tuple[float, str]:
    if oracle_dump == candidate_dump:
        return 1.0, "exact_ast_dump_match"
    if max(len(oracle_dump), len(candidate_dump)) <= exact_sequence_limit:
        return (
            SequenceMatcher(None, oracle_dump, candidate_dump, autojunk=False).ratio(),
            "exact_sequence_matcher_v1",
        )
    denominator = max(_leaf_count(oracle_data), _leaf_count(candidate_data), 1)
    agreement = max(0.0, 1.0 - (len(candidate_oracle_diffs) / denominator))
    return agreement, "normalized_ast_leaf_agreement_v1"


def _path_value(value: Any, path: list[Any]) -> tuple[bool, Any]:
    current = value
    try:
        for key in path:
            current = current[key]
    except (IndexError, KeyError, TypeError):
        return False, None
    return True, current


def _brief_value(value: Any, limit: int = 240) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        rendered = value
    else:
        rendered = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=True,
            default=lambda item: (
                {
                    "type": "bytes",
                    "length": len(item),
                    "sha256": sha256_bytes(item),
                    "preview_hex": item[:16].hex(),
                }
                if isinstance(item, bytes)
                else repr(item)
            ),
        )
    if isinstance(rendered, str) and len(rendered) > limit:
        return rendered[:limit] + "..."
    return rendered


def _typed_ancestor(
    root: Any, path: list[Any], node_type: str
) -> tuple[list[Any], dict[str, Any]] | None:
    for length in range(len(path), -1, -1):
        prefix = path[:length]
        found, value = _path_value(root, prefix)
        if found and isinstance(value, dict) and value.get("_type") == node_type:
            return prefix, value
    return None


def _numeric_constant(node: Any) -> int | float | None:
    if not isinstance(node, dict) or node.get("_type") != "Constant":
        return None
    value = node.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _is_integer_expression(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    if node.get("_type") == "Call":
        function = node.get("func")
        if isinstance(function, dict) and function.get("_type") == "Name":
            return function.get("id") in {"len", "int", "round"}
    return False


def _ordered_compare(operator: str, left: int | float, right: int | float) -> bool:
    operations: dict[str, Callable[[int | float, int | float], bool]] = {
        "Lt": lambda a, b: a < b,
        "LtE": lambda a, b: a <= b,
        "Gt": lambda a, b: a > b,
        "GtE": lambda a, b: a >= b,
    }
    if operator not in operations:
        raise ValueError(operator)
    return operations[operator](left, right)


def _comparison_signature(
    comparison: dict[str, Any], probe_values: list[int]
) -> tuple[Any, ...] | None:
    operators = comparison.get("ops")
    comparators = comparison.get("comparators")
    if not isinstance(operators, list) or len(operators) != 1:
        return None
    if not isinstance(comparators, list) or len(comparators) != 1:
        return None
    operator = operators[0].get("_type") if isinstance(operators[0], dict) else None
    if operator not in {"Lt", "LtE", "Gt", "GtE"}:
        return None
    left = comparison.get("left")
    right = comparators[0]
    left_constant = _numeric_constant(left)
    right_constant = _numeric_constant(right)
    if (left_constant is None) == (right_constant is None):
        return None
    if right_constant is not None:
        dynamic = left
        constant = right_constant
        orientation = "dynamic_left"
        truth = tuple(_ordered_compare(operator, value, constant) for value in probe_values)
    else:
        dynamic = right
        constant = left_constant
        orientation = "dynamic_right"
        truth = tuple(_ordered_compare(operator, constant, value) for value in probe_values)
    if not _is_integer_expression(dynamic):
        return None
    return orientation, dynamic, truth


def _comparison_equivalence_analysis(
    oracle_data: Any,
    candidate_data: Any,
    target_diffs: list[dict[str, Any]],
    candidate_oracle_diffs: list[dict[str, Any]],
) -> dict[str, Any]:
    if not target_diffs:
        return {"status": "abstain", "reason": "no_mutation_target_diff"}
    oracle_ancestor = _typed_ancestor(oracle_data, target_diffs[0]["path"], "Compare")
    if oracle_ancestor is None:
        return {"status": "abstain", "reason": "target_not_within_compare"}
    compare_path, oracle_compare = oracle_ancestor
    found, candidate_compare = _path_value(candidate_data, compare_path)
    if not found or not isinstance(candidate_compare, dict) or candidate_compare.get("_type") != "Compare":
        return {"status": "fail", "reason": "candidate_compare_missing", "compare_path": compare_path}
    constants = [
        value
        for comparison in (oracle_compare, candidate_compare)
        for value in (
            _numeric_constant(comparison.get("left")),
            _numeric_constant((comparison.get("comparators") or [None])[0]),
        )
        if value is not None
    ]
    if not constants or not all(float(value).is_integer() for value in constants):
        return {"status": "abstain", "reason": "non_integer_or_missing_threshold"}
    lower = int(min(constants)) - 3
    upper = int(max(constants)) + 3
    probes = list(range(lower, upper + 1))
    oracle_signature = _comparison_signature(oracle_compare, probes)
    candidate_signature = _comparison_signature(candidate_compare, probes)
    if oracle_signature is None or candidate_signature is None:
        return {"status": "abstain", "reason": "unsupported_comparison_shape"}
    equivalent = oracle_signature == candidate_signature
    outside_diffs = [
        row
        for row in candidate_oracle_diffs
        if row["path"][: len(compare_path)] != compare_path
    ]
    return {
        "status": "pass" if equivalent else "fail",
        "reason": "integer_truth_table_equivalent" if equivalent else "integer_truth_table_differs",
        "compare_path": compare_path,
        "probe_values": probes,
        "outside_compare_diff_count": len(outside_diffs),
        "outside_compare_diff_paths": [row["path"] for row in outside_diffs[:10]],
    }


def _symbol_data_path(root: Any, symbol: str) -> list[Any] | None:
    target_parts = symbol.split(".")
    matches: list[list[Any]] = []

    def visit(value: Any, path: list[Any], scope: list[str]) -> None:
        if isinstance(value, dict):
            node_type = value.get("_type")
            next_scope = scope
            if node_type in {"ClassDef", "FunctionDef", "AsyncFunctionDef"}:
                name = value.get("name")
                if isinstance(name, str):
                    next_scope = [*scope, name]
                    if next_scope == target_parts:
                        matches.append(path)
            for key, child in value.items():
                if key != "_type":
                    visit(child, [*path, key], next_scope)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, [*path, index], scope)

    visit(root, [], [])
    return matches[0] if len(matches) == 1 else None


def _find_python_function(tree: ast.AST, symbol: str) -> ast.FunctionDef | None:
    target_parts = symbol.split(".")
    matches: list[ast.FunctionDef] = []

    def visit(node: ast.AST, scope: list[str]) -> None:
        next_scope = scope
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            next_scope = [*scope, node.name]
            if next_scope == target_parts and isinstance(node, ast.FunctionDef):
                matches.append(node)
        for child in ast.iter_child_nodes(node):
            visit(child, next_scope)

    visit(tree, [])
    return matches[0] if len(matches) == 1 else None


_SAFE_FORMAT_STRING_NODES = {
    ast.Module,
    ast.FunctionDef,
    ast.arguments,
    ast.arg,
    ast.Expr,
    ast.Constant,
    ast.Assign,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Call,
    ast.Attribute,
    ast.If,
    ast.Compare,
    ast.In,
    ast.NotIn,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.Return,
    ast.JoinedStr,
    ast.FormattedValue,
}


def _isolated_format_string_function(source: str, symbol: str) -> Callable[[str], Any] | None:
    tree = ast.parse(source)
    function = _find_python_function(tree, symbol)
    if function is None:
        return None
    cloned = copy.deepcopy(function)
    cloned.name = "_skillscriptbench_format_string_probe"
    cloned.decorator_list = []
    cloned.returns = None
    for argument in [
        *cloned.args.posonlyargs,
        *cloned.args.args,
        *cloned.args.kwonlyargs,
    ]:
        argument.annotation = None
    if cloned.args.vararg:
        cloned.args.vararg.annotation = None
    if cloned.args.kwarg:
        cloned.args.kwarg.annotation = None
    module = ast.Module(body=[cloned], type_ignores=[])
    ast.fix_missing_locations(module)
    for node in ast.walk(module):
        if type(node) not in _SAFE_FORMAT_STRING_NODES:
            return None
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "replace":
                return None
    namespace: dict[str, Any] = {"__builtins__": {}}
    exec(compile(module, "<skillscriptbench-format-string-probe>", "exec"), namespace)
    function_value = namespace[cloned.name]
    return function_value if callable(function_value) else None


def _groovy_literal_valid(value: str, output: Any) -> bool:
    if not isinstance(output, str):
        return False
    if output.startswith("'''") and output.endswith("'''") and len(output) >= 6:
        delimiter = "'''"
        inner = output[3:-3]
    elif len(output) >= 2 and output[0] == output[-1] and output[0] in {"'", '"'}:
        delimiter = output[0]
        inner = output[1:-1]
    else:
        return False
    if inner != value.replace("\\", "\\\\"):
        return False
    if delimiter in {"'", '"'} and delimiter in value:
        return False
    return True


def _format_string_property(source: str, symbol: str) -> dict[str, Any]:
    function = _isolated_format_string_function(source, symbol)
    if function is None:
        return {"status": "abstain", "reason": "unsafe_or_unsupported_function_shape"}
    probes = ["", "plain", "a'b", 'a"b', "a\\b", "a'\"b"]
    rows: list[dict[str, Any]] = []
    for value in probes:
        try:
            output = function(value)
            valid = _groovy_literal_valid(value, output)
            rows.append({"input": value, "output": output, "valid": valid})
        except Exception as exc:  # pragma: no cover - defensive result capture
            rows.append({"input": value, "error": type(exc).__name__, "valid": False})
    return {
        "status": "pass" if all(row["valid"] for row in rows) else "fail",
        "reason": "all_literals_valid" if all(row["valid"] for row in rows) else "invalid_literal",
        "rows": rows,
    }


def _guard_source_property_analysis(
    oracle: str,
    mutant: str,
    candidate: str,
    symbol: str,
    oracle_data: Any,
    candidate_oracle_diffs: list[dict[str, Any]],
) -> dict[str, Any]:
    oracle_property = _format_string_property(oracle, symbol)
    mutant_property = _format_string_property(mutant, symbol)
    candidate_property = _format_string_property(candidate, symbol)
    symbol_path = _symbol_data_path(oracle_data, symbol)
    if symbol_path is None:
        return {
            "status": "abstain",
            "reason": "target_symbol_path_not_unique",
            "oracle_property": oracle_property,
            "mutant_property": mutant_property,
            "candidate_property": candidate_property,
        }
    outside_diffs = [
        row
        for row in candidate_oracle_diffs
        if row["path"][: len(symbol_path)] != symbol_path
    ]
    discriminates = oracle_property["status"] == "pass" and mutant_property["status"] == "fail"
    candidate_passes = candidate_property["status"] == "pass"
    bounded = not outside_diffs
    return {
        "status": "pass" if discriminates and candidate_passes and bounded else "fail",
        "reason": (
            "property_restored_within_target_symbol"
            if discriminates and candidate_passes and bounded
            else "property_or_scope_gate_failed"
        ),
        "oracle_mutant_discrimination": discriminates,
        "candidate_property_pass": candidate_passes,
        "target_symbol_path": symbol_path,
        "outside_target_symbol_diff_count": len(outside_diffs),
        "outside_target_symbol_diff_paths": [row["path"] for row in outside_diffs[:10]],
        "oracle_property": oracle_property,
        "mutant_property": mutant_property,
        "candidate_property": candidate_property,
    }


def _skill_markdown(package_root: Path) -> Path | None:
    direct = package_root / "SKILL.md"
    if direct.is_file():
        return direct
    casefold_matches = sorted(
        path for path in package_root.iterdir() if path.is_file() and path.name.lower() == "skill.md"
    )
    return casefold_matches[0] if len(casefold_matches) == 1 else None


def _sanitize_skill_provenance(
    markdown: str,
    *,
    source_repo_url: str | None,
    source_commit: str | None,
) -> str:
    """Hide source coordinates that can point directly to the held-out oracle."""
    replacements: set[str] = set()
    if source_repo_url:
        normalized = source_repo_url.rstrip("/")
        replacements.update({source_repo_url, normalized, f"{normalized}/"})
        if normalized.endswith(".git"):
            replacements.add(normalized[:-4])
        else:
            replacements.add(f"{normalized}.git")
    if source_commit and len(source_commit) >= 7:
        replacements.add(source_commit)
    sanitized = markdown
    for value in sorted((value for value in replacements if value), key=len, reverse=True):
        sanitized = sanitized.replace(value, "[withheld for benchmark isolation]")
    return sanitized


def _load_attempts(pool_paths: Iterable[Path]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    pool_receipts: list[dict[str, Any]] = []
    for pool_path in pool_paths:
        payload = read_json(pool_path)
        rows = payload.get("attempts")
        if not isinstance(rows, list):
            raise ValueError(f"attempt pool has no attempts list: {pool_path}")
        attempts.extend(rows)
        pool_receipts.append(
            {
                "path": str(pool_path.resolve()),
                "sha256": sha256_file(pool_path),
                "attempt_pool_hash": payload.get("attempt_pool_hash"),
                "attempt_count": len(rows),
            }
        )
    return attempts, pool_receipts


def select_python_v13_cases(
    attempts: list[dict[str, Any]],
    *,
    case_count: int,
    max_script_bytes: int,
    max_skill_bytes: int,
    operator_priority: tuple[str, ...] = DEFAULT_OPERATOR_PRIORITY,
    excluded_transformation_ids: set[str] | None = None,
    transformation_ids: tuple[str, ...] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if case_count < 1:
        raise ValueError("case_count must be positive")
    priority = {name: index for index, name in enumerate(operator_priority)}
    excluded = excluded_transformation_ids or set()
    if transformation_ids:
        if excluded:
            raise ValueError("explicit and excluded transformation ids cannot be combined")
        if len(transformation_ids) != case_count:
            raise ValueError("explicit transformation count must equal case_count")
        by_id = {str(row.get("transformation_id")): row for row in attempts}
        missing = [identifier for identifier in transformation_ids if identifier not in by_id]
        if missing:
            raise ValueError(f"unknown transformation ids: {missing}")
        candidates = [by_id[identifier] for identifier in transformation_ids]
    else:
        candidates = sorted(
            (
                attempt
                for attempt in attempts
                if attempt.get("backend") == "python_ast_v13"
                and attempt.get("operator", {}).get("operator") in priority
                and attempt.get("transformation_id") not in excluded
            ),
            key=lambda row: (
                priority[row["operator"]["operator"]],
                row.get("source_id", ""),
                row.get("relative_root", ""),
                row["operator"].get("path", ""),
                row["operator"].get("operator_candidate_id", ""),
            ),
        )
    selected: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    used_sources: set[str] = set()
    used_components: set[str] = set()
    used_operators: set[str] = set()
    enforce_diversity = not bool(transformation_ids)

    def eligible(attempt: dict[str, Any], *, require_new_operator: bool) -> tuple[bool, str]:
        operator = attempt["operator"]
        source_id = str(attempt.get("source_id", ""))
        component_id = str(attempt.get("content_component_id", ""))
        operator_name = str(operator["operator"])
        if attempt.get("backend") != "python_ast_v13" or operator_name not in priority:
            return False, "not_python_v13"
        if enforce_diversity and source_id in used_sources:
            return False, "source_already_selected"
        if enforce_diversity and component_id and component_id in used_components:
            return False, "component_already_selected"
        if require_new_operator and operator_name in used_operators:
            return False, "operator_already_selected"
        source_root = Path(attempt["source_local_root"])
        package_root = source_root / attempt["relative_root"]
        script_path = package_root / operator["path"]
        skill_path = _skill_markdown(package_root) if package_root.is_dir() else None
        if not package_root.is_dir():
            return False, "package_missing"
        if not script_path.is_file():
            return False, "script_missing"
        if skill_path is None:
            return False, "skill_markdown_missing_or_ambiguous"
        if script_path.stat().st_size > max_script_bytes:
            return False, "script_too_large"
        if skill_path.stat().st_size > max_skill_bytes:
            return False, "skill_markdown_too_large"
        try:
            source = _read_utf8_exact(script_path)
            _read_utf8_exact(skill_path)
            ast.parse(source)
        except (OSError, UnicodeError, SyntaxError):
            return False, "source_not_utf8_python"
        if sha256_bytes(source.encode("utf-8")) != operator.get("source_hash"):
            return False, "source_hash_mismatch"
        try:
            mutant = apply_python_behavior_operator_v13(source, operator)
        except (SyntaxError, ValueError):
            return False, "operator_replay_failed"
        if _ast_hash(mutant) == _ast_hash(source):
            return False, "operator_ast_noop"
        return True, "eligible"

    passes = (False,) if transformation_ids else (True, False)
    for require_new_operator in passes:
        for attempt in candidates:
            if len(selected) >= case_count:
                break
            if attempt in selected:
                continue
            ok, reason = eligible(attempt, require_new_operator=require_new_operator)
            if not ok:
                exclusions.append(
                    {
                        "transformation_id": attempt.get("transformation_id"),
                        "operator_candidate_id": attempt["operator"].get("operator_candidate_id"),
                        "reason": reason,
                    }
                )
                continue
            selected.append(attempt)
            used_sources.add(str(attempt.get("source_id", "")))
            component_id = str(attempt.get("content_component_id", ""))
            if component_id:
                used_components.add(component_id)
            used_operators.add(str(attempt["operator"]["operator"]))
        if len(selected) >= case_count:
            break
    if len(selected) != case_count:
        raise RuntimeError(f"selected {len(selected)} of {case_count} requested cases")
    return selected, exclusions


def _case_id(attempt: dict[str, Any]) -> str:
    identity = {
        "transformation_id": attempt.get("transformation_id"),
        "operator_candidate_id": attempt["operator"].get("operator_candidate_id"),
        "source_commit": attempt.get("source_commit"),
    }
    return f"v16-api-{canonical_json_hash(identity)[:16]}"


def prepare_stage(
    attempt_pools: list[Path],
    output_root: Path,
    *,
    case_count: int = 4,
    max_script_bytes: int = 24_000,
    max_skill_bytes: int = 48_000,
    model: str = "gpt-5.5",
    base_url: str = "https://api.openlux.ai/v1",
    timeout: int = 600,
    max_attempts: int = 2,
    api_protocol: str = "chat-completions",
    max_output_tokens: int = 8_192,
    repeats: int = 1,
    operator_priority: tuple[str, ...] = DEFAULT_OPERATOR_PRIORITY,
    excluded_transformation_ids: set[str] | None = None,
    transformation_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    if api_protocol not in {"chat-completions", "responses"}:
        raise ValueError(f"unsupported api protocol: {api_protocol}")
    root = output_root.resolve()
    if root.exists():
        raise FileExistsError(root)
    if repeats != 1:
        raise ValueError("stage smoke is intentionally fixed to one repeat")
    root.mkdir(parents=True)
    public_root = root / "public"
    private_root = root / "_private"
    public_root.mkdir()
    private_root.mkdir()
    attempts, pool_receipts = _load_attempts(attempt_pools)
    selected, exclusions = select_python_v13_cases(
        attempts,
        case_count=case_count,
        max_script_bytes=max_script_bytes,
        max_skill_bytes=max_skill_bytes,
        operator_priority=operator_priority,
        excluded_transformation_ids=excluded_transformation_ids,
        transformation_ids=transformation_ids,
    )
    public_cases: list[dict[str, Any]] = []
    private_cases: list[dict[str, Any]] = []
    for attempt in selected:
        operator = attempt["operator"]
        case_id = _case_id(attempt)
        source_root = Path(attempt["source_local_root"])
        package_root = source_root / attempt["relative_root"]
        script_path = package_root / operator["path"]
        skill_path = _skill_markdown(package_root)
        if skill_path is None:
            raise RuntimeError(f"selected package lost SKILL.md: {package_root}")
        oracle = _read_utf8_exact(script_path)
        mutant = apply_python_behavior_operator_v13(oracle, operator)
        skill_markdown = _sanitize_skill_provenance(
            _read_utf8_exact(skill_path),
            source_repo_url=attempt.get("source_repo_url"),
            source_commit=attempt.get("source_commit"),
        )

        public_case_root = public_root / "cases" / case_id
        public_case_root.mkdir(parents=True)
        (public_case_root / "mutant.py").write_text(mutant, encoding="utf-8")
        (public_case_root / "SKILL.md").write_text(skill_markdown, encoding="utf-8")
        task = {
            "task_id": case_id,
            "objective": (
                "Repair one localized behavioral regression in the visible Python script using "
                "only the supplied evidence."
            ),
            "constraints": [
                "Preserve public interfaces and all unrelated behavior.",
                "Do not refactor, add features, or rewrite documentation.",
                "Return only minimal exact source replacements in the required JSON schema.",
            ],
        }
        write_json(public_case_root / "TASK.json", task)

        private_case_root = private_root / "cases" / case_id
        private_case_root.mkdir(parents=True)
        (private_case_root / "oracle.py").write_text(oracle, encoding="utf-8")
        private_label = {
            "case_id": case_id,
            "source_id": attempt.get("source_id"),
            "source_commit": attempt.get("source_commit"),
            "source_repo_url": attempt.get("source_repo_url"),
            "relative_root": attempt.get("relative_root"),
            "target_path": operator["path"],
            "transformation_id": attempt.get("transformation_id"),
            "operator": operator,
            "oracle_sha256": sha256_bytes(oracle.encode("utf-8")),
            "oracle_ast_hash": _ast_hash(oracle),
            "mutant_sha256": sha256_bytes(mutant.encode("utf-8")),
            "mutant_ast_hash": _ast_hash(mutant),
        }
        write_json(private_case_root / "label.json", private_label)
        public_cases.append(
            {
                "case_id": case_id,
                "public_case_path": f"cases/{case_id}",
                "target_path": operator["path"],
                "mutant_sha256": private_label["mutant_sha256"],
                "skill_markdown_sha256": sha256_bytes(skill_markdown.encode("utf-8")),
            }
        )
        private_cases.append(private_label)

    public_manifest = {
        "schema_version": "0.16-api-operator-smoke-public-v1",
        "created_at": _utc_now(),
        "cases": public_cases,
        "claim_boundary": (
            "This stage tests bounded single-regression repairability and the marginal value of "
            "SKILL.md visibility. It does not establish package behavior, AST causality, task "
            "correctness, or self-evolution efficacy."
        ),
    }
    write_json(public_root / "manifest.json", public_manifest)
    private_manifest = {
        "schema_version": "0.16-api-operator-smoke-private-v1",
        "created_at": _utc_now(),
        "cases": private_cases,
    }
    write_json(private_root / "manifest.json", private_manifest)

    calls = [
        {
            "trial_id": f"{case['case_id']}--{condition}--r1",
            "case_id": case["case_id"],
            "condition": condition,
            "repeat": 1,
        }
        for case in public_cases
        for condition in CONDITIONS
    ]
    plan = {
        "schema_version": "0.16-api-operator-smoke-plan-v1",
        "status": "frozen_before_model_calls",
        "created_at": _utc_now(),
        "model": model,
        "base_url": base_url,
        "api_protocol": api_protocol,
        "temperature": 0 if api_protocol == "chat-completions" else None,
        "max_output_tokens": max_output_tokens if api_protocol == "responses" else None,
        "response_mode": "exact_replacement_edits_v1",
        "response_limits": {
            "minimum_edit_count": 1,
            "maximum_edit_count": 4,
            "maximum_single_edit_bytes": 8_000,
            "maximum_total_edit_span_bytes": 12_000,
        },
        "input_limits": {
            "maximum_script_bytes": max_script_bytes,
            "maximum_skill_bytes": max_skill_bytes,
        },
        "timeout": timeout,
        "max_attempts": max_attempts,
        "repeats": repeats,
        "conditions": list(CONDITIONS),
        "calls": calls,
        "public_tree_hashes": hash_tree(public_root),
        "public_manifest_hash": canonical_json_hash(public_manifest),
        "hidden_evaluation_loaded": False,
    }
    plan["plan_hash"] = canonical_json_hash(plan)
    write_json(root / "FROZEN_PLAN.json", plan)
    prepare_record = {
        "schema_version": "0.16-api-operator-smoke-prepare-v1",
        "status": "ready_for_model_smoke",
        "created_at": _utc_now(),
        "attempt_pools": pool_receipts,
        "selected_case_count": len(public_cases),
        "model_call_count": len(calls),
        "operator_counts": {
            name: sum(1 for row in private_cases if row["operator"]["operator"] == name)
            for name in sorted({row["operator"]["operator"] for row in private_cases})
        },
        "operator_priority": list(operator_priority),
        "excluded_transformation_ids": sorted(excluded_transformation_ids or set()),
        "selected_transformation_ids": [row["transformation_id"] for row in selected],
        "selection_mode": (
            "explicit_model_independent_preflight"
            if transformation_ids
            else "automatic_diversity_selection"
        ),
        "source_count": len({row["source_id"] for row in private_cases}),
        "component_count": len(
            {
                row.get("content_component_id")
                for row in selected
                if row.get("content_component_id")
            }
        ),
        "selection_exclusion_count": len(exclusions),
        "selection_exclusions": exclusions,
        "public_tree_hashes": plan["public_tree_hashes"],
        "private_tree_hashes": hash_tree(private_root),
        "plan_hash": plan["plan_hash"],
        "response_mode": plan["response_mode"],
        "input_limits": plan["input_limits"],
        "model_calls": 0,
        "credential_persisted": False,
    }
    write_json(root / "PREPARE_RECORD.json", prepare_record)
    return prepare_record


def _visible_prompt(public_case_root: Path, *, condition: str) -> str:
    if condition not in CONDITIONS:
        raise ValueError(condition)
    task = read_json(public_case_root / "TASK.json")
    source = _read_utf8_exact(public_case_root / "mutant.py")
    visible: list[dict[str, str]] = [
        {"path": "TASK.json", "content": json.dumps(task, indent=2, sort_keys=True)},
        {"path": "script.py", "content": source},
    ]
    if condition == "skill-script":
        visible.append(
            {"path": "SKILL.md", "content": _read_utf8_exact(public_case_root / "SKILL.md")}
        )
    return (
        "Perform exactly one bounded repair. One localized behavioral regression was introduced "
        "into script.py. Use only the visible evidence, preserve interfaces and unrelated behavior, "
        f"and do not add features or refactor. {exact_replacement_instruction('script.py')}\n\n"
        f"Visible files:\n{json.dumps(visible, indent=2, ensure_ascii=True)}\n"
    )


def _parse_complete_script_response(
    content: str,
    public_case_root: Path,
) -> tuple[str, str, dict[str, Any]]:
    del public_case_root
    parsed = _parse_model_json(content)
    if not isinstance(parsed, dict) or set(parsed) != {"script", "summary"}:
        raise ValueError("response must contain exactly script and summary")
    if not isinstance(parsed["script"], str) or not isinstance(parsed["summary"], str):
        raise TypeError("script and summary must be strings")
    ast.parse(parsed["script"])
    return parsed["script"], parsed["summary"], {"response_mode": "complete_script"}


def _parse_exact_edit_response(
    content: str,
    public_case_root: Path,
) -> tuple[str, str, dict[str, Any]]:
    return parse_and_apply_exact_replacements(
        content,
        _read_utf8_exact(public_case_root / "mutant.py"),
        parse_json=_parse_model_json,
        validate_candidate=ast.parse,
    )


def _credential_absent(root: Path, api_key: str) -> bool:
    if not api_key:
        return True
    needle = api_key.encode("utf-8")
    return all(needle not in path.read_bytes() for path in root.rglob("*") if path.is_file())


CallFunction = Callable[..., tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any]]]
PromptBuilder = Callable[[Path, str], str]
ResponseParser = Callable[[str, Path], tuple[str, str, dict[str, Any]]]
CandidateValidator = Callable[[str], Any]
CandidateStructuralHash = Callable[[str], str]


def run_one_trial(
    public_root: Path,
    trial: dict[str, Any],
    output_root: Path,
    *,
    api_key: str,
    model: str,
    base_url: str,
    timeout: int,
    max_attempts: int,
    api_protocol: str = "chat-completions",
    call_function: CallFunction = _call_chat_completion,
    prompt_builder: PromptBuilder | None = None,
    response_parser: ResponseParser | None = None,
    system_prompt: str | None = None,
    candidate_validator: CandidateValidator | None = None,
    candidate_filename: str = "script.py",
    candidate_structural_hash: CandidateStructuralHash | None = None,
) -> dict[str, Any]:
    trial_root = output_root / trial["trial_id"]
    if trial_root.exists():
        raise FileExistsError(trial_root)
    trial_root.mkdir(parents=True)
    public_case_root = public_root / "cases" / trial["case_id"]
    builder = prompt_builder or (
        lambda case_root, condition: _visible_prompt(case_root, condition=condition)
    )
    prompt = builder(public_case_root, trial["condition"])
    (trial_root / "prompt.txt").write_text(prompt, encoding="utf-8")
    system_instruction = system_prompt or (
        "You repair one visible Agent Skill script. Never request hidden tests, mutation labels, "
        "gold code, verifier output, or oracle behavior. Return strict JSON only."
    )
    response, attempts, request_payload = call_function(
        api_key=api_key,
        base_url=base_url,
        model=model,
        system_prompt=system_instruction,
        user_prompt=prompt,
        timeout=timeout,
        max_attempts=max_attempts,
    )
    if response is not None:
        write_json(trial_root / "provider_response.json", response)
    write_json(trial_root / "request_payload.json", request_payload)
    content_error: str | None = None
    try:
        content = _model_content(response) if response is not None else ""
    except (KeyError, TypeError, ValueError) as exc:
        content = ""
        content_error = f"{type(exc).__name__}:{exc}"
    (trial_root / "raw_response.txt").write_text(content, encoding="utf-8")
    parse_error: str | None = content_error
    candidate_source: str | None = None
    summary: str | None = None
    response_application: dict[str, Any] | None = None
    if content_error is None:
        try:
            parser = response_parser or _parse_exact_edit_response
            candidate_source, summary, response_application = parser(content, public_case_root)
            (candidate_validator or ast.parse)(candidate_source)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, SyntaxError) as exc:
            parse_error = f"{type(exc).__name__}:{exc}"
    if candidate_source is not None:
        candidate_root = trial_root / "candidate"
        candidate_root.mkdir()
        (candidate_root / candidate_filename).write_text(candidate_source, encoding="utf-8")
        write_json(
            trial_root / "RESPONSE_APPLICATION.json",
            response_application or {"response_mode": "unspecified"},
        )
    if response is None:
        freeze_status = "provider_unavailable_no_candidate"
    elif candidate_source is None:
        freeze_status = "invalid_response_frozen"
    else:
        freeze_status = "candidate_frozen"
    freeze = {
        "schema_version": "0.16-api-operator-smoke-candidate-freeze-v1",
        "trial_id": trial["trial_id"],
        "status": freeze_status,
        "created_at": _utc_now(),
        "prompt_sha256": sha256_file(trial_root / "prompt.txt"),
        "raw_response_sha256": sha256_file(trial_root / "raw_response.txt"),
        "candidate_sha256": (
            sha256_bytes(candidate_source.encode("utf-8")) if candidate_source is not None else None
        ),
        "candidate_ast_hash": (
            (candidate_structural_hash or _ast_hash)(candidate_source)
            if candidate_source is not None
            else None
        ),
        "response_application_sha256": (
            sha256_file(trial_root / "RESPONSE_APPLICATION.json")
            if candidate_source is not None
            else None
        ),
        "hidden_evaluation_loaded": False,
    }
    write_json(trial_root / "CANDIDATE_FREEZE.json", freeze)
    credential_persisted = not _credential_absent(trial_root, api_key)
    record = {
        "schema_version": "0.16-api-operator-smoke-run-v1",
        "trial_id": trial["trial_id"],
        "case_id": trial["case_id"],
        "condition": trial["condition"],
        "repeat": trial["repeat"],
        "status": freeze["status"] if not credential_persisted else "credential_persistence_failure",
        "created_at": _utc_now(),
        "provider": base_url,
        "model": model,
        "api_protocol": api_protocol,
        "temperature": 0 if api_protocol == "chat-completions" else None,
        "transport_attempt_count": len(attempts),
        "completed_model_response_count": int(response is not None),
        "model_calls": int(response is not None),
        "attempts": attempts,
        "response_id": response.get("id") if response else None,
        "usage": _normalized_usage(response.get("usage")) if response else None,
        "request_hash": canonical_json_hash(request_payload),
        "prompt_hash": canonical_json_hash(prompt),
        "parse_error": parse_error,
        "model_summary": summary,
        "response_mode": (
            response_application.get("response_mode") if response_application else None
        ),
        "candidate_freeze_hash": canonical_json_hash(freeze),
        "hidden_evaluation_loaded": False,
        "credential_persisted": credential_persisted,
    }
    write_json(trial_root / "RUN_RECORD.json", record)
    if credential_persisted:
        raise RuntimeError("API credential appeared in persisted trial artifacts")
    return record


def run_stage(
    public_root: Path,
    plan_path: Path,
    output_root: Path,
    *,
    api_key: str,
    workers: int = 4,
    trial_ids: set[str] | None = None,
    call_function: CallFunction | None = None,
    prompt_builder: PromptBuilder | None = None,
    response_parser: ResponseParser | None = None,
    system_prompt: str | None = None,
    candidate_validator: CandidateValidator | None = None,
    candidate_filename: str = "script.py",
    candidate_structural_hash: CandidateStructuralHash | None = None,
) -> dict[str, Any]:
    public = public_root.resolve()
    output = output_root.resolve()
    if output.exists():
        raise FileExistsError(output)
    plan = read_json(plan_path)
    api_protocol = str(plan.get("api_protocol", "chat-completions"))
    if api_protocol not in {"chat-completions", "responses"}:
        raise ValueError(f"unsupported api protocol: {api_protocol}")
    if call_function is None:
        if api_protocol == "responses":
            max_output_tokens = int(plan.get("max_output_tokens") or 8_192)

            def selected_call_function(**kwargs: Any):
                return _call_responses(
                    **kwargs,
                    max_output_tokens=max_output_tokens,
                )

            call_function = selected_call_function
        else:
            call_function = _call_chat_completion
    if response_parser is None:
        response_mode = plan.get("response_mode")
        if response_mode == "exact_replacement_edits_v1":
            response_parser = _parse_exact_edit_response
        elif response_mode == "complete_script_json_v1":
            response_parser = _parse_complete_script_response
        else:
            raise ValueError("operator_smoke_stage_response_mode_mismatch")
    if plan.get("status") != "frozen_before_model_calls":
        raise ValueError("plan is not frozen")
    if canonical_json_hash({key: value for key, value in plan.items() if key != "plan_hash"}) != plan.get(
        "plan_hash"
    ):
        raise ValueError("plan hash mismatch")
    if hash_tree(public) != plan.get("public_tree_hashes"):
        raise ValueError("public tree changed after plan freeze")
    plan_calls = list(plan["calls"])
    if trial_ids is not None:
        known_ids = {trial["trial_id"] for trial in plan_calls}
        unknown = sorted(trial_ids - known_ids)
        if unknown:
            raise ValueError(f"trial ids are not in frozen plan: {unknown}")
        plan_calls = [trial for trial in plan_calls if trial["trial_id"] in trial_ids]
        if len(plan_calls) != len(trial_ids):
            raise ValueError("duplicate or missing trial ids")
    if not plan_calls:
        raise ValueError("no trials selected")
    output.mkdir(parents=True)
    records: list[dict[str, Any]] = []
    worker_count = max(1, min(workers, len(plan_calls)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                run_one_trial,
                public,
                trial,
                output,
                api_key=api_key,
                model=plan["model"],
                base_url=plan["base_url"],
                timeout=int(plan["timeout"]),
                max_attempts=int(plan["max_attempts"]),
                api_protocol=api_protocol,
                call_function=call_function,
                prompt_builder=prompt_builder,
                response_parser=response_parser,
                system_prompt=system_prompt,
                candidate_validator=candidate_validator,
                candidate_filename=candidate_filename,
                candidate_structural_hash=candidate_structural_hash,
            ): trial
            for trial in plan_calls
        }
        for future in as_completed(futures):
            records.append(future.result())
    records.sort(key=lambda row: row["trial_id"])
    summary = {
        "schema_version": "0.16-api-operator-smoke-batch-run-v1",
        "status": (
            "all_candidates_frozen"
            if all(row["status"] == "candidate_frozen" for row in records)
            else "completed_with_invalid_trials"
        ),
        "created_at": _utc_now(),
        "plan_hash": plan["plan_hash"],
        "frozen_plan_trial_count": len(plan["calls"]),
        "trial_count": len(records),
        "is_partial_transport_recovery_run": len(records) != len(plan["calls"]),
        "candidate_frozen_count": sum(row["status"] == "candidate_frozen" for row in records),
        "invalid_trial_count": sum(row["status"] != "candidate_frozen" for row in records),
        "transport_attempt_count": sum(int(row["transport_attempt_count"]) for row in records),
        "completed_model_response_count": sum(
            int(row["completed_model_response_count"]) for row in records
        ),
        "model_calls": sum(int(row["completed_model_response_count"]) for row in records),
        "usage": {
            key: sum(int((row.get("usage") or {}).get(key, 0)) for row in records)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "hidden_evaluation_loaded": False,
        "credential_persisted": False,
        "trials": records,
    }
    write_json(output / "BATCH_RUN_SUMMARY.json", summary)
    return summary


def consolidate_transport_recovery_runs(
    plan_path: Path,
    primary_runs_root: Path,
    recovery_runs_roots: list[Path],
    output_root: Path,
) -> dict[str, Any]:
    plan = read_json(plan_path)
    primary = primary_runs_root.resolve()
    recoveries = [path.resolve() for path in recovery_runs_roots]
    output = output_root.resolve()
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    selections: list[dict[str, Any]] = []
    selected_records: list[dict[str, Any]] = []
    for trial in plan["calls"]:
        trial_id = trial["trial_id"]
        primary_trial_root = primary / trial_id
        primary_record = read_json(primary_trial_root / "RUN_RECORD.json")
        selected_root = primary_trial_root
        selection_reason = "primary_frozen_result"
        considered: list[dict[str, Any]] = [
            {
                "root": str(primary),
                "status": primary_record.get("status"),
                "run_record_sha256": sha256_file(primary_trial_root / "RUN_RECORD.json"),
            }
        ]
        if primary_record.get("status") == "provider_unavailable_no_candidate":
            selection_reason = "no_completed_transport_recovery"
            for recovery in recoveries:
                recovery_trial_root = recovery / trial_id
                if not (recovery_trial_root / "RUN_RECORD.json").is_file():
                    continue
                recovery_record = read_json(recovery_trial_root / "RUN_RECORD.json")
                considered.append(
                    {
                        "root": str(recovery),
                        "status": recovery_record.get("status"),
                        "run_record_sha256": sha256_file(
                            recovery_trial_root / "RUN_RECORD.json"
                        ),
                    }
                )
                if recovery_record.get("status") != "provider_unavailable_no_candidate":
                    selected_root = recovery_trial_root
                    selection_reason = "first_completed_transport_recovery"
                    break
        elif primary_record.get("status") != "candidate_frozen":
            selection_reason = "primary_completed_invalid_result_not_retried"
        selected_record = read_json(selected_root / "RUN_RECORD.json")
        if selected_record.get("trial_id") != trial_id:
            raise ValueError(f"trial identity mismatch: {trial_id}")
        shutil.copytree(selected_root, output / trial_id)
        selected_records.append(selected_record)
        selections.append(
            {
                "trial_id": trial_id,
                "primary_status": primary_record.get("status"),
                "selected_status": selected_record.get("status"),
                "selected_root": str(selected_root.parent),
                "selection_reason": selection_reason,
                "considered_runs": considered,
            }
        )
    source_batches = [primary, *recoveries]
    report = {
        "schema_version": "0.16-api-operator-smoke-transport-consolidation-v1",
        "status": (
            "all_candidates_frozen"
            if all(row.get("status") == "candidate_frozen" for row in selected_records)
            else "consolidated_with_invalid_trials"
        ),
        "created_at": _utc_now(),
        "plan_hash": plan["plan_hash"],
        "selection_policy": (
            "Keep every primary completed response, including invalid responses. Only a primary "
            "provider_unavailable_no_candidate may be replaced, using the first recovery root "
            "with a completed provider response. No hidden evaluation is loaded."
        ),
        "source_batch_summaries": [
            {
                "root": str(root),
                "sha256": sha256_file(root / "BATCH_RUN_SUMMARY.json"),
            }
            for root in source_batches
            if (root / "BATCH_RUN_SUMMARY.json").is_file()
        ],
        "trial_count": len(selected_records),
        "candidate_frozen_count": sum(
            row.get("status") == "candidate_frozen" for row in selected_records
        ),
        "selected_completed_model_response_count": sum(
            int(row.get("completed_model_response_count", bool(row.get("response_id"))))
            for row in selected_records
        ),
        "selected_transport_attempt_count": sum(
            int(row.get("transport_attempt_count", len(row.get("attempts", []))))
            for row in selected_records
        ),
        "all_source_transport_attempt_count": sum(
            len(read_json(path).get("attempts", []))
            for root in source_batches
            for path in root.glob("*/RUN_RECORD.json")
        ),
        "hidden_evaluation_loaded": False,
        "selections": selections,
    }
    write_json(output / "CONSOLIDATION_RECORD.json", report)
    return report


def audit_legacy_transport_run(runs_root: Path, output_path: Path) -> dict[str, Any]:
    runs = runs_root.resolve()
    batch_path = runs / "BATCH_RUN_SUMMARY.json"
    batch = read_json(batch_path)
    records = [
        read_json(path)
        for path in sorted(runs.glob("*/RUN_RECORD.json"))
        if path.is_file()
    ]
    completed_responses = sum(bool(row.get("response_id")) for row in records)
    transport_attempts = sum(len(row.get("attempts", [])) for row in records)
    provider_unavailable = sum(
        row.get("response_id") is None
        and bool(row.get("attempts"))
        and all(attempt.get("status") != "success" for attempt in row.get("attempts", []))
        for row in records
    )
    report = {
        "schema_version": "0.16-api-operator-smoke-superseding-transport-audit-v1",
        "status": "supersedes_model_call_accounting_only",
        "created_at": _utc_now(),
        "source_batch_summary_sha256": sha256_file(batch_path),
        "source_schema_version": batch.get("schema_version"),
        "trial_count": len(records),
        "transport_attempt_count": transport_attempts,
        "completed_model_response_count": completed_responses,
        "provider_unavailable_trial_count": provider_unavailable,
        "candidate_frozen_count": sum(
            (runs / row["trial_id"] / "candidate" / "script.py").is_file() for row in records
        ),
        "corrected_model_calls": completed_responses,
        "historical_reported_model_calls": batch.get("model_calls"),
        "interpretation": (
            "HTTP/provider attempts are not model completions. Trials without a provider response "
            "are infrastructure failures and are excluded from behavioral pass/fail analysis."
        ),
    }
    write_json(output_path, report)
    return report


def _prompt_leakage_report(prompt: str, label: dict[str, Any]) -> dict[str, Any]:
    operator = label["operator"]
    forbidden = {
        "operator_name": str(operator.get("operator", "")),
        "operator_candidate_id": str(operator.get("operator_candidate_id", "")),
        "transformation_id": str(label.get("transformation_id", "")),
        "oracle_sha256": str(label.get("oracle_sha256", "")),
        "oracle_ast_hash": str(label.get("oracle_ast_hash", "")),
        "source_repo_url": str(label.get("source_repo_url", "")),
    }
    matches = [name for name, value in forbidden.items() if value and value in prompt]
    return {"status": "pass" if not matches else "fail", "matched_private_fields": matches}


def evaluate_stage(
    public_root: Path,
    private_root: Path,
    plan_path: Path,
    runs_root: Path,
    output_path: Path,
) -> dict[str, Any]:
    public = public_root.resolve()
    private = private_root.resolve()
    runs = runs_root.resolve()
    plan = read_json(plan_path)
    if hash_tree(public) != plan.get("public_tree_hashes"):
        raise ValueError("public tree changed after plan freeze")
    private_manifest = read_json(private / "manifest.json")
    labels = {row["case_id"]: row for row in private_manifest["cases"]}
    rows: list[dict[str, Any]] = []
    for trial in plan["calls"]:
        trial_root = runs / trial["trial_id"]
        freeze = read_json(trial_root / "CANDIDATE_FREEZE.json")
        run_record = read_json(trial_root / "RUN_RECORD.json")
        label = labels[trial["case_id"]]
        oracle = _read_utf8_exact(private / "cases" / trial["case_id"] / "oracle.py")
        mutant = _read_utf8_exact(public / "cases" / trial["case_id"] / "mutant.py")
        prompt = _read_utf8_exact(trial_root / "prompt.txt")
        leakage = _prompt_leakage_report(prompt, label)
        candidate_path = trial_root / "candidate" / "script.py"
        freeze_integrity = False
        candidate_valid = False
        candidate_source: str | None = None
        candidate_ast_hash: str | None = None
        if candidate_path.is_file() and freeze.get("candidate_sha256"):
            candidate_source = _read_utf8_exact(candidate_path)
            application_path = trial_root / "RESPONSE_APPLICATION.json"
            application_integral = (
                not freeze.get("response_application_sha256")
                or (
                    application_path.is_file()
                    and sha256_file(application_path)
                    == freeze["response_application_sha256"]
                )
            )
            freeze_integrity = (
                sha256_bytes(candidate_source.encode("utf-8")) == freeze["candidate_sha256"]
                and sha256_file(trial_root / "prompt.txt") == freeze["prompt_sha256"]
                and sha256_file(trial_root / "raw_response.txt") == freeze["raw_response_sha256"]
                and application_integral
            )
            try:
                candidate_ast_hash = _ast_hash(candidate_source)
                candidate_valid = True
            except SyntaxError:
                candidate_valid = False
        oracle_dump = _ast_dump(oracle)
        mutant_dump = _ast_dump(mutant)
        candidate_dump = _ast_dump(candidate_source) if candidate_valid and candidate_source else ""
        oracle_data = _ast_data(ast.parse(oracle))
        mutant_data = _ast_data(ast.parse(mutant))
        target_diffs = _leaf_diffs(oracle_data, mutant_data)
        candidate_data = _ast_data(ast.parse(candidate_source)) if candidate_valid and candidate_source else None
        candidate_oracle_diffs = (
            _leaf_diffs(oracle_data, candidate_data) if candidate_data is not None else []
        )
        target_paths = {tuple(row["path"]) for row in target_diffs}
        target_restored = bool(target_diffs) and candidate_data is not None
        exact_candidate_data = target_restored and candidate_data == oracle_data
        if target_restored and not exact_candidate_data:
            for target_diff in target_diffs:
                found, candidate_value = _path_value(candidate_data, target_diff["path"])
                oracle_found, oracle_value = _path_value(oracle_data, target_diff["path"])
                if not found or not oracle_found or candidate_value != oracle_value:
                    target_restored = False
                    break
        extra_diffs = [
            row for row in candidate_oracle_diffs if tuple(row["path"]) not in target_paths
        ]
        semantic_equivalence: dict[str, Any] = {
            "status": "not_needed" if target_restored else "abstain",
            "reason": "exact_target_restored" if target_restored else "unsupported_operator",
        }
        behavior_extra_diffs = extra_diffs
        if (
            not target_restored
            and candidate_data is not None
            and label["operator"]["operator"] == "toggle_python_comparison_boundary_v13"
        ):
            semantic_equivalence = _comparison_equivalence_analysis(
                oracle_data,
                candidate_data,
                target_diffs,
                candidate_oracle_diffs,
            )
            if semantic_equivalence["status"] == "pass":
                compare_path = semantic_equivalence["compare_path"]
                behavior_extra_diffs = [
                    row
                    for row in candidate_oracle_diffs
                    if row["path"][: len(compare_path)] != compare_path
                ]
        operator_target_contract_pass = bool(
            target_restored or semantic_equivalence["status"] == "pass"
        )
        operator_local_semantic_pass = bool(
            operator_target_contract_pass and not behavior_extra_diffs
        )
        source_property: dict[str, Any] = {
            "status": "not_applicable",
            "reason": "no_operator_property_backend",
        }
        if (
            candidate_source is not None
            and label["operator"]["operator"] == "toggle_python_guard_connector_v13"
        ):
            source_property = _guard_source_property_analysis(
                oracle,
                mutant,
                candidate_source,
                str(label["operator"].get("symbol", "")),
                oracle_data,
                candidate_oracle_diffs,
            )
        source_property_pass = source_property["status"] == "pass"
        behavioral_local_pass = bool(operator_local_semantic_pass or source_property_pass)
        exact_oracle_ast = candidate_valid and candidate_ast_hash == label["oracle_ast_hash"]
        no_op = candidate_valid and candidate_ast_hash == label["mutant_ast_hash"]
        if candidate_valid and candidate_data is not None:
            similarity, similarity_method = _bounded_ast_similarity(
                oracle_data,
                candidate_data,
                oracle_dump,
                candidate_dump,
                candidate_oracle_diffs,
            )
        else:
            similarity, similarity_method = 0.0, "invalid_candidate"
        strong_pass = bool(
            run_record.get("status") == "candidate_frozen"
            and freeze_integrity
            and leakage["status"] == "pass"
            and exact_oracle_ast
        )
        repair_class = (
            "invalid_candidate"
            if not candidate_valid
            else "exact_target_only_repair"
            if exact_oracle_ast
            else "operator_equivalent_target_only_repair"
            if semantic_equivalence["status"] == "pass" and not behavior_extra_diffs
            else "target_repaired_with_extra_semantic_edits"
            if operator_target_contract_pass
            else "target_missed_with_other_semantic_edits"
            if not no_op
            else "ast_noop"
        )
        rows.append(
            {
                "trial_id": trial["trial_id"],
                "case_id": trial["case_id"],
                "condition": trial["condition"],
                "repeat": trial["repeat"],
                "operator": label["operator"]["operator"],
                "dimension": label["operator"].get("dimension"),
                "source_id": label["source_id"],
                "candidate_valid_python": candidate_valid,
                "candidate_freeze_integrity": freeze_integrity,
                "prompt_private_label_leakage": leakage,
                "candidate_changed_from_mutant": candidate_valid and not no_op,
                "candidate_is_ast_noop": no_op,
                "mutation_target_leaf_diff_count": len(target_diffs),
                "mutation_target_paths": [row["path"] for row in target_diffs],
                "mutation_target_restored": target_restored,
                "operator_semantic_equivalence": semantic_equivalence,
                "operator_target_contract_pass": operator_target_contract_pass,
                "operator_local_semantic_pass": operator_local_semantic_pass,
                "source_property": source_property,
                "source_property_pass": source_property_pass,
                "behavioral_local_pass": behavioral_local_pass,
                "candidate_oracle_leaf_diff_count": len(candidate_oracle_diffs),
                "extra_semantic_leaf_diff_count": len(extra_diffs),
                "behavior_extra_semantic_leaf_diff_count": len(behavior_extra_diffs),
                "candidate_oracle_diff_preview": [
                    {
                        "path": row["path"],
                        "oracle": _brief_value(row["left"]),
                        "candidate": _brief_value(row["right"]),
                    }
                    for row in candidate_oracle_diffs[:10]
                ],
                "repair_class": repair_class,
                "exact_oracle_ast": exact_oracle_ast,
                "oracle_ast_similarity": similarity,
                "oracle_ast_similarity_method": similarity_method,
                "strong_mutation_inversion_pass": strong_pass,
                "model_calls": run_record.get("model_calls", 0),
                "usage": run_record.get("usage"),
            }
        )
    condition_summary: dict[str, Any] = {}
    plan_conditions = tuple(plan.get("conditions") or CONDITIONS)
    for condition in plan_conditions:
        condition_rows = [row for row in rows if row["condition"] == condition]
        condition_summary[condition] = {
            "trial_count": len(condition_rows),
            "strong_pass_count": sum(row["strong_mutation_inversion_pass"] for row in condition_rows),
            "changed_count": sum(row["candidate_changed_from_mutant"] for row in condition_rows),
            "target_restored_count": sum(row["mutation_target_restored"] for row in condition_rows),
            "operator_target_contract_pass_count": sum(
                row["operator_target_contract_pass"] for row in condition_rows
            ),
            "operator_local_semantic_pass_count": sum(
                row["operator_local_semantic_pass"] for row in condition_rows
            ),
            "source_property_pass_count": sum(
                row["source_property_pass"] for row in condition_rows
            ),
            "behavioral_local_pass_count": sum(
                row["behavioral_local_pass"] for row in condition_rows
            ),
            "operator_equivalent_target_only_repair_count": sum(
                row["repair_class"] == "operator_equivalent_target_only_repair"
                for row in condition_rows
            ),
            "target_repaired_with_extra_semantic_edits_count": sum(
                row["repair_class"] == "target_repaired_with_extra_semantic_edits"
                for row in condition_rows
            ),
            "target_missed_with_other_semantic_edits_count": sum(
                row["repair_class"] == "target_missed_with_other_semantic_edits"
                for row in condition_rows
            ),
            "ast_noop_count": sum(row["candidate_is_ast_noop"] for row in condition_rows),
            "invalid_count": sum(not row["candidate_valid_python"] for row in condition_rows),
            "mean_oracle_ast_similarity": (
                sum(float(row["oracle_ast_similarity"]) for row in condition_rows)
                / len(condition_rows)
                if condition_rows
                else math.nan
            ),
        }
    pairwise: list[dict[str, Any]] = []
    for case_id in sorted(labels):
        case_rows = {row["condition"]: row for row in rows if row["case_id"] == case_id}
        raw_pass = bool(case_rows["raw-script"]["strong_mutation_inversion_pass"])
        skill_pass = bool(case_rows["skill-script"]["strong_mutation_inversion_pass"])
        raw_operator_pass = bool(case_rows["raw-script"]["operator_local_semantic_pass"])
        skill_operator_pass = bool(case_rows["skill-script"]["operator_local_semantic_pass"])
        raw_behavioral_pass = bool(case_rows["raw-script"]["behavioral_local_pass"])
        skill_behavioral_pass = bool(case_rows["skill-script"]["behavioral_local_pass"])
        outcome = (
            "skill_only"
            if skill_pass and not raw_pass
            else "raw_only"
            if raw_pass and not skill_pass
            else "both"
            if raw_pass and skill_pass
            else "neither"
        )
        operator_outcome = (
            "skill_only"
            if skill_operator_pass and not raw_operator_pass
            else "raw_only"
            if raw_operator_pass and not skill_operator_pass
            else "both"
            if raw_operator_pass and skill_operator_pass
            else "neither"
        )
        behavioral_outcome = (
            "skill_only"
            if skill_behavioral_pass and not raw_behavioral_pass
            else "raw_only"
            if raw_behavioral_pass and not skill_behavioral_pass
            else "both"
            if raw_behavioral_pass and skill_behavioral_pass
            else "neither"
        )
        pairwise.append(
            {
                "case_id": case_id,
                "raw_script_pass": raw_pass,
                "skill_script_pass": skill_pass,
                "paired_outcome": outcome,
                "raw_script_operator_local_semantic_pass": raw_operator_pass,
                "skill_script_operator_local_semantic_pass": skill_operator_pass,
                "operator_semantic_paired_outcome": operator_outcome,
                "raw_script_behavioral_local_pass": raw_behavioral_pass,
                "skill_script_behavioral_local_pass": skill_behavioral_pass,
                "behavioral_local_paired_outcome": behavioral_outcome,
            }
        )
    report = {
        "schema_version": "0.16-api-operator-smoke-hidden-evaluation-v5",
        "status": "complete",
        "created_at": _utc_now(),
        "plan_hash": plan["plan_hash"],
        "claim_boundary": (
            "A strong pass means AST-equivalent restoration of the held-out source after one "
            "controlled mutation. An operator-local semantic pass additionally accepts a proven "
            "integer comparison equivalence with no AST changes outside that comparison. A "
            "source-property pass additionally requires oracle/mutant discrimination, candidate "
            "property satisfaction, and AST-bounded changes within the target symbol. These local "
            "metrics do not establish whole-package runtime correctness, AST-guided causality, or "
            "transferable self-evolution. Candidate freeze and private-label isolation are required."
        ),
        "condition_summary": condition_summary,
        "paired_summary": {
            outcome: sum(row["paired_outcome"] == outcome for row in pairwise)
            for outcome in ("skill_only", "raw_only", "both", "neither")
        },
        "operator_semantic_paired_summary": {
            outcome: sum(row["operator_semantic_paired_outcome"] == outcome for row in pairwise)
            for outcome in ("skill_only", "raw_only", "both", "neither")
        },
        "behavioral_local_paired_summary": {
            outcome: sum(row["behavioral_local_paired_outcome"] == outcome for row in pairwise)
            for outcome in ("skill_only", "raw_only", "both", "neither")
        },
        "all_freezes_integral": all(row["candidate_freeze_integrity"] for row in rows),
        "all_prompts_private_label_free": all(
            row["prompt_private_label_leakage"]["status"] == "pass" for row in rows
        ),
        "model_calls": sum(int(row["model_calls"]) for row in rows),
        "rows": rows,
        "pairwise": pairwise,
    }
    write_json(output_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare, run, or evaluate a frozen v0.16 operator API smoke."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--attempt-pool", action="append", type=Path, required=True)
    prepare.add_argument("--output-root", type=Path, required=True)
    prepare.add_argument("--case-count", type=int, default=4)
    prepare.add_argument("--max-script-bytes", type=int, default=24_000)
    prepare.add_argument("--max-skill-bytes", type=int, default=48_000)
    prepare.add_argument("--model", default="gpt-5.5")
    prepare.add_argument("--base-url", default="https://api.openlux.ai/v1")
    prepare.add_argument("--timeout", type=int, default=600)
    prepare.add_argument("--max-attempts", type=int, default=2)
    prepare.add_argument(
        "--api-protocol",
        choices=("chat-completions", "responses"),
        default="chat-completions",
    )
    prepare.add_argument("--max-output-tokens", type=int, default=8_192)
    prepare.add_argument("--operator", action="append")
    prepare.add_argument("--transformation-id", action="append")
    prepare.add_argument("--exclude-transformation-id", action="append")

    run = subparsers.add_parser("run")
    run.add_argument("--public-root", type=Path, required=True)
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument("--workers", type=int, default=4)
    run.add_argument("--trial-id", action="append")

    consolidate = subparsers.add_parser("consolidate")
    consolidate.add_argument("--plan", type=Path, required=True)
    consolidate.add_argument("--primary-runs-root", type=Path, required=True)
    consolidate.add_argument("--recovery-runs-root", action="append", type=Path, required=True)
    consolidate.add_argument("--output-root", type=Path, required=True)

    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--public-root", type=Path, required=True)
    evaluate.add_argument("--private-root", type=Path, required=True)
    evaluate.add_argument("--plan", type=Path, required=True)
    evaluate.add_argument("--runs-root", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)

    audit_transport = subparsers.add_parser("audit-transport")
    audit_transport.add_argument("--runs-root", type=Path, required=True)
    audit_transport.add_argument("--output", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "prepare":
        record = prepare_stage(
            args.attempt_pool,
            args.output_root,
            case_count=args.case_count,
            max_script_bytes=args.max_script_bytes,
            max_skill_bytes=args.max_skill_bytes,
            model=args.model,
            base_url=args.base_url,
            timeout=args.timeout,
            max_attempts=args.max_attempts,
            api_protocol=args.api_protocol,
            max_output_tokens=args.max_output_tokens,
            operator_priority=(
                tuple(args.operator) if args.operator else DEFAULT_OPERATOR_PRIORITY
            ),
            excluded_transformation_ids=set(args.exclude_transformation_id or []),
            transformation_ids=tuple(args.transformation_id or ()),
        )
    elif args.command == "run":
        api_key = getpass.getpass("OpenLux API key: ")
        if not api_key:
            raise SystemExit("API key is required")
        record = run_stage(
            args.public_root,
            args.plan,
            args.output_root,
            api_key=api_key,
            workers=args.workers,
            trial_ids=set(args.trial_id) if args.trial_id else None,
        )
    elif args.command == "consolidate":
        record = consolidate_transport_recovery_runs(
            args.plan,
            args.primary_runs_root,
            args.recovery_runs_root,
            args.output_root,
        )
    elif args.command == "evaluate":
        record = evaluate_stage(
            args.public_root,
            args.private_root,
            args.plan,
            args.runs_root,
            args.output,
        )
    else:
        record = audit_legacy_transport_run(args.runs_root, args.output)
    print(json.dumps(record, indent=2, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
