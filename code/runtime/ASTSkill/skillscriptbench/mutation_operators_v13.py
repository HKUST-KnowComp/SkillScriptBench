from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from .io_utils import canonical_json_hash, sha256_bytes, sha256_file
from .source_test_mutations_v09 import _enumerate_package_operators


THRESHOLD_TOKENS = {
    "attempt",
    "attempts",
    "budget",
    "count",
    "limit",
    "max",
    "maximum",
    "min",
    "minimum",
    "retry",
    "retries",
    "size",
    "threshold",
    "timeout",
    "warn",
}
TIMEOUT_KEYWORDS = {
    "attempts",
    "limit",
    "max_attempts",
    "max_retries",
    "retries",
    "retry_count",
    "timeout",
}
PYTHON_SUFFIXES = {".py"}
SHELL_SUFFIXES = {".sh", ".bash"}


def _tokens(value: str) -> set[str]:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return {
        token
        for token in re.split(r"[^A-Za-z0-9]+", normalized.lower())
        if token
    }


def _numeric_literal(node: ast.AST) -> int | float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(
        node.value, bool
    ):
        return node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.UAdd, ast.USub))
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
        and not isinstance(node.operand.value, bool)
    ):
        return -node.operand.value if isinstance(node.op, ast.USub) else node.operand.value
    return None


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _scope_symbol(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    parts: list[str] = []
    current: ast.AST | None = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts.append(current.name)
    return ".".join(reversed(parts)) if parts else "<module>"


def _condition_role(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    child = node
    while child in parents:
        parent = parents[child]
        if isinstance(parent, ast.If) and parent.test is child:
            return "if_guard"
        if isinstance(parent, ast.While) and parent.test is child:
            return "while_guard"
        if isinstance(parent, ast.Assert) and parent.test is child:
            return "assertion_guard"
        if isinstance(parent, ast.IfExp) and parent.test is child:
            return "conditional_expression"
        if isinstance(parent, (ast.stmt, ast.comprehension)):
            return None
        child = parent
    return None


def _python_candidate(package_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    identity = {
        "package_id": package_id,
        **{
            key: payload.get(key)
            for key in (
                "operator",
                "path",
                "symbol",
                "line",
                "column",
                "op_index",
                "keyword",
                "original_value",
                "original_operator",
                "stream",
            )
        },
    }
    return {
        "operator_candidate_id": canonical_json_hash(identity)[:24],
        **payload,
    }


def enumerate_python_behavior_operators_v13(
    package_root: str | Path,
    script_files: list[str],
    *,
    package_id: str,
) -> list[dict[str, Any]]:
    root = Path(package_root).resolve()
    candidates: list[dict[str, Any]] = []
    for relative in sorted(script_files):
        path = root / relative
        if path.suffix.lower() not in PYTHON_SUFFIXES or path.name.startswith("test_"):
            continue
        try:
            source = path.read_bytes().decode("utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        source_hash = sha256_file(path)
        parents = _parent_map(tree)
        for node in ast.walk(tree):
            symbol = _scope_symbol(node, parents)
            base = {
                "path": relative,
                "symbol": symbol,
                "line": getattr(node, "lineno", 0),
                "column": getattr(node, "col_offset", 0),
                "source_hash": source_hash,
            }

            if isinstance(node, ast.Compare) and len(node.ops) == 1:
                condition_role = _condition_role(node, parents)
                operator = node.ops[0]
                operands = [node.left, node.comparators[0]]
                numeric_values = [_numeric_literal(item) for item in operands]
                expression_tokens = _tokens(" ".join(ast.unparse(item) for item in operands))
                boundary_map = {
                    ast.Lt: ("lt", "lte"),
                    ast.LtE: ("lte", "lt"),
                    ast.Gt: ("gt", "gte"),
                    ast.GtE: ("gte", "gt"),
                }
                if type(operator) in boundary_map and (
                    any(value is not None for value in numeric_values)
                    or expression_tokens & THRESHOLD_TOKENS
                ):
                    original, replacement = boundary_map[type(operator)]
                    family = "python_comparison_boundary"
                    candidates.append(
                        _python_candidate(
                            package_id,
                            {
                                **base,
                                "operator": "toggle_python_comparison_boundary_v13",
                                "operator_subfamily": f"{family}:{original}_to_{replacement}",
                                "dimension": "threshold_or_boundary",
                                "op_index": 0,
                                "original_operator": original,
                                "replacement_operator": replacement,
                                "numeric_values": numeric_values,
                                "condition_role": condition_role,
                                "structural_roles": [
                                    "numeric_or_named_threshold",
                                    condition_role or "value_comparison",
                                ],
                                "operator_template_fingerprint": canonical_json_hash(
                                    {
                                        "family": family,
                                        "original": original,
                                        "replacement": replacement,
                                        "condition_role": condition_role,
                                    }
                                ),
                                "construction_claim": (
                                    "One ordered comparison boundary changes between strict and inclusive."
                                ),
                            },
                        )
                    )
                polarity_map = {
                    ast.In: ("in", "not_in"),
                    ast.NotIn: ("not_in", "in"),
                }
                if type(operator) in polarity_map and condition_role is not None:
                    original, replacement = polarity_map[type(operator)]
                    family = "python_membership_polarity"
                    candidates.append(
                        _python_candidate(
                            package_id,
                            {
                                **base,
                                "operator": "toggle_python_membership_polarity_v13",
                                "operator_subfamily": f"{family}:{original}_to_{replacement}",
                                "dimension": "path_or_routing",
                                "op_index": 0,
                                "original_operator": original,
                                "replacement_operator": replacement,
                                "condition_role": condition_role,
                                "structural_roles": ["membership_route", condition_role],
                                "operator_template_fingerprint": canonical_json_hash(
                                    {
                                        "family": family,
                                        "original": original,
                                        "replacement": replacement,
                                        "condition_role": condition_role,
                                    }
                                ),
                                "construction_claim": (
                                    "One membership-based control-flow decision is inverted."
                                ),
                            },
                        )
                    )

            if isinstance(node, ast.BoolOp) and len(node.values) == 2:
                condition_role = _condition_role(node, parents)
                if condition_role is not None:
                    original = "and" if isinstance(node.op, ast.And) else "or"
                    replacement = "or" if original == "and" else "and"
                    family = "python_guard_connector"
                    candidates.append(
                        _python_candidate(
                            package_id,
                            {
                                **base,
                                "operator": "toggle_python_guard_connector_v13",
                                "operator_subfamily": f"{family}:{original}_to_{replacement}",
                                "dimension": "validation_or_recovery",
                                "original_operator": original,
                                "replacement_operator": replacement,
                                "condition_role": condition_role,
                                "structural_roles": ["compound_guard", condition_role],
                                "operator_template_fingerprint": canonical_json_hash(
                                    {
                                        "family": family,
                                        "original": original,
                                        "replacement": replacement,
                                        "condition_role": condition_role,
                                    }
                                ),
                                "construction_claim": (
                                    "One two-clause control-flow guard changes conjunction semantics."
                                ),
                            },
                        )
                    )

            if isinstance(node, ast.Return) and symbol.split(".")[-1] == "main":
                value = _numeric_literal(node.value) if node.value is not None else None
                if isinstance(value, int) and 0 <= value <= 3:
                    replacement = 1 if value == 0 else 0
                    family = "python_exit_status_contract"
                    candidates.append(
                        _python_candidate(
                            package_id,
                            {
                                **base,
                                "operator": "toggle_python_return_status_v13",
                                "operator_subfamily": f"{family}:{value}_to_{replacement}",
                                "dimension": "cli_contract",
                                "original_value": value,
                                "replacement_value": replacement,
                                "structural_roles": ["main_return_status"],
                                "operator_template_fingerprint": canonical_json_hash(
                                    {"family": family, "original": value, "replacement": replacement}
                                ),
                                "construction_claim": (
                                    "One explicit main return status changes between success and failure."
                                ),
                            },
                        )
                    )

            if isinstance(node, ast.Call):
                call_path = ast.unparse(node.func)
                if call_path in {"sys.exit", "exit"} and len(node.args) == 1:
                    value = _numeric_literal(node.args[0])
                    if isinstance(value, int) and 0 <= value <= 3:
                        replacement = 1 if value == 0 else 0
                        family = "python_exit_status_contract"
                        candidates.append(
                            _python_candidate(
                                package_id,
                                {
                                    **base,
                                    "operator": "toggle_python_exit_call_status_v13",
                                    "operator_subfamily": f"{family}:{value}_to_{replacement}",
                                    "dimension": "cli_contract",
                                    "original_value": value,
                                    "replacement_value": replacement,
                                    "call": call_path,
                                    "structural_roles": ["process_exit_status"],
                                    "operator_template_fingerprint": canonical_json_hash(
                                        {"family": family, "call": call_path, "original": value}
                                    ),
                                    "construction_claim": (
                                        "One explicit process exit status changes between success and failure."
                                    ),
                                },
                            )
                        )

                if call_path == "print":
                    for keyword in node.keywords:
                        if keyword.arg != "file" or not isinstance(keyword.value, ast.Attribute):
                            continue
                        stream_path = ast.unparse(keyword.value)
                        if stream_path not in {"sys.stderr", "sys.stdout"}:
                            continue
                        replacement = "sys.stdout" if stream_path == "sys.stderr" else "sys.stderr"
                        family = "python_output_stream_route"
                        candidates.append(
                            _python_candidate(
                                package_id,
                                {
                                    **base,
                                    "operator": "swap_python_output_stream_v13",
                                    "operator_subfamily": f"{family}:{stream_path}_to_{replacement}",
                                    "dimension": "cli_contract",
                                    "stream": stream_path,
                                    "replacement_stream": replacement,
                                    "call": call_path,
                                    "structural_roles": ["print_stream"],
                                    "operator_template_fingerprint": canonical_json_hash(
                                        {"family": family, "call": call_path, "stream": stream_path}
                                    ),
                                    "construction_claim": (
                                        "One explicit print destination changes between stdout and stderr."
                                    ),
                                },
                            )
                        )
                if isinstance(node.func, ast.Attribute) and node.func.attr == "write":
                    stream_path = ast.unparse(node.func.value)
                    if stream_path in {"sys.stderr", "sys.stdout"}:
                        replacement = "sys.stdout" if stream_path == "sys.stderr" else "sys.stderr"
                        family = "python_output_stream_route"
                        candidates.append(
                            _python_candidate(
                                package_id,
                                {
                                    **base,
                                    "operator": "swap_python_write_stream_v13",
                                    "operator_subfamily": f"{family}:{stream_path}_to_{replacement}",
                                    "dimension": "cli_contract",
                                    "stream": stream_path,
                                    "replacement_stream": replacement,
                                    "call": call_path,
                                    "structural_roles": ["write_stream"],
                                    "operator_template_fingerprint": canonical_json_hash(
                                        {"family": family, "call": "write", "stream": stream_path}
                                    ),
                                    "construction_claim": (
                                        "One explicit write destination changes between stdout and stderr."
                                    ),
                                },
                            )
                        )

                if isinstance(node.func, ast.Attribute) and node.func.attr in {
                    "absolute",
                    "expanduser",
                    "resolve",
                } and not node.args and not node.keywords:
                    family = "python_path_normalization"
                    candidates.append(
                        _python_candidate(
                            package_id,
                            {
                                **base,
                                "operator": "remove_python_path_normalization_v13",
                                "operator_subfamily": f"{family}:{node.func.attr}",
                                "dimension": "path_or_routing",
                                "call": call_path,
                                "normalizer": node.func.attr,
                                "structural_roles": ["path_normalization"],
                                "operator_template_fingerprint": canonical_json_hash(
                                    {"family": family, "normalizer": node.func.attr}
                                ),
                                "construction_claim": (
                                    "One explicit path-normalization call is removed while retaining its receiver."
                                ),
                            },
                        )
                    )

                if isinstance(node.func, ast.Attribute) and node.func.attr == "join" and len(node.args) == 1:
                    delimiter = node.func.value
                    if isinstance(delimiter, ast.Constant) and isinstance(delimiter.value, str) and delimiter.value:
                        replacement = "," if delimiter.value == "\n" else "\n"
                        family = "python_delimiter_format"
                        candidates.append(
                            _python_candidate(
                                package_id,
                                {
                                    **base,
                                    "operator": "swap_python_join_delimiter_v13",
                                    "operator_subfamily": f"{family}:{repr(delimiter.value)}",
                                    "dimension": "schema_or_format",
                                    "original_value": delimiter.value,
                                    "replacement_value": replacement,
                                    "call": call_path,
                                    "structural_roles": ["text_delimiter"],
                                    "operator_template_fingerprint": canonical_json_hash(
                                        {"family": family, "delimiter": delimiter.value}
                                    ),
                                    "construction_claim": "One literal text-join delimiter changes.",
                                },
                            )
                        )

                for keyword in node.keywords:
                    value = _numeric_literal(keyword.value)
                    if keyword.arg in TIMEOUT_KEYWORDS and isinstance(value, (int, float)) and value > 0:
                        replacement = value + 1
                        family = "python_timeout_retry_limit"
                        candidates.append(
                            _python_candidate(
                                package_id,
                                {
                                    **base,
                                    "operator": "increment_python_control_keyword_v13",
                                    "operator_subfamily": f"{family}:{keyword.arg}",
                                    "dimension": "timeout_retry_or_limit",
                                    "keyword": keyword.arg,
                                    "original_value": value,
                                    "replacement_value": replacement,
                                    "call": call_path,
                                    "structural_roles": ["bounded_external_operation", keyword.arg],
                                    "operator_template_fingerprint": canonical_json_hash(
                                        {"family": family, "keyword": keyword.arg}
                                    ),
                                    "construction_claim": (
                                        "One explicit timeout, retry, or limit keyword changes by one unit."
                                    ),
                                },
                            )
                        )

            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
                upper = _numeric_literal(node.slice.upper) if node.slice.upper is not None else None
                if isinstance(upper, int) and upper >= 2:
                    family = "python_fixed_cardinality_slice"
                    candidates.append(
                        _python_candidate(
                            package_id,
                            {
                                **base,
                                "operator": "decrement_python_slice_upper_v13",
                                "operator_subfamily": f"{family}:{upper}_to_{upper - 1}",
                                "dimension": "cardinality_or_order",
                                "original_value": upper,
                                "replacement_value": upper - 1,
                                "structural_roles": ["fixed_slice_cardinality"],
                                "operator_template_fingerprint": canonical_json_hash(
                                    {"family": family, "upper_kind": "positive_integer"}
                                ),
                                "construction_claim": "One fixed slice upper bound decreases by one.",
                            },
                        )
                    )

    deduped = {row["operator_candidate_id"]: row for row in candidates}
    return sorted(
        deduped.values(),
        key=lambda row: (
            row["dimension"],
            row["operator_subfamily"],
            row["path"],
            row["line"],
            row["column"],
            row["operator_candidate_id"],
        ),
    )


_COMPARE_CLASS = {
    "lt": ast.Lt,
    "lte": ast.LtE,
    "gt": ast.Gt,
    "gte": ast.GtE,
    "in": ast.In,
    "not_in": ast.NotIn,
}


class _PythonV13Transformer(ast.NodeTransformer):
    def __init__(self, candidate: dict[str, Any]) -> None:
        self.candidate = candidate
        self.replaced = 0
        self.original_span: tuple[int, int, int, int] | None = None
        self.replacement_node: ast.AST | None = None

    def _record_replacement(
        self, original: ast.AST, replacement: ast.AST | None = None
    ) -> None:
        coordinates = (
            getattr(original, "lineno", None),
            getattr(original, "col_offset", None),
            getattr(original, "end_lineno", None),
            getattr(original, "end_col_offset", None),
        )
        if not all(isinstance(value, int) for value in coordinates):
            raise ValueError("python_operator_target_missing_source_span")
        self.original_span = coordinates  # type: ignore[assignment]
        self.replacement_node = replacement or original
        self.replaced += 1

    def _matches(self, node: ast.AST) -> bool:
        return (
            getattr(node, "lineno", None) == self.candidate["line"]
            and getattr(node, "col_offset", None) == self.candidate["column"]
        )

    def visit_Compare(self, node: ast.Compare):  # noqa: N802
        node = self.generic_visit(node)
        if not self._matches(node):
            return node
        if self.candidate["operator"] in {
            "toggle_python_comparison_boundary_v13",
            "toggle_python_membership_polarity_v13",
        }:
            index = int(self.candidate.get("op_index", 0))
            if index < len(node.ops):
                expected = _COMPARE_CLASS[self.candidate["original_operator"]]
                if isinstance(node.ops[index], expected):
                    node.ops[index] = _COMPARE_CLASS[self.candidate["replacement_operator"]]()
                    self._record_replacement(node)
        return node

    def visit_BoolOp(self, node: ast.BoolOp):  # noqa: N802
        node = self.generic_visit(node)
        if self._matches(node) and self.candidate["operator"] == "toggle_python_guard_connector_v13":
            expected = ast.And if self.candidate["original_operator"] == "and" else ast.Or
            if isinstance(node.op, expected):
                node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
                self._record_replacement(node)
        return node

    def visit_Return(self, node: ast.Return):  # noqa: N802
        node = self.generic_visit(node)
        if self._matches(node) and self.candidate["operator"] == "toggle_python_return_status_v13":
            if _numeric_literal(node.value) == self.candidate["original_value"]:
                node.value = ast.copy_location(ast.Constant(self.candidate["replacement_value"]), node.value)
                self._record_replacement(node)
        return node

    def visit_Call(self, node: ast.Call):  # noqa: N802
        node = self.generic_visit(node)
        if not self._matches(node):
            return node
        operator = self.candidate["operator"]
        if operator == "toggle_python_exit_call_status_v13" and len(node.args) == 1:
            if _numeric_literal(node.args[0]) == self.candidate["original_value"]:
                node.args[0] = ast.copy_location(
                    ast.Constant(self.candidate["replacement_value"]), node.args[0]
                )
                self._record_replacement(node)
        elif operator == "swap_python_output_stream_v13":
            for keyword in node.keywords:
                if keyword.arg == "file" and ast.unparse(keyword.value) == self.candidate["stream"]:
                    keyword.value = ast.copy_location(
                        ast.parse(self.candidate["replacement_stream"], mode="eval").body,
                        keyword.value,
                    )
                    self._record_replacement(node)
                    break
        elif operator == "swap_python_write_stream_v13" and isinstance(node.func, ast.Attribute):
            if ast.unparse(node.func.value) == self.candidate["stream"]:
                node.func.value = ast.copy_location(
                    ast.parse(self.candidate["replacement_stream"], mode="eval").body,
                    node.func.value,
                )
                self._record_replacement(node)
        elif operator == "remove_python_path_normalization_v13" and isinstance(
            node.func, ast.Attribute
        ):
            if node.func.attr == self.candidate["normalizer"] and not node.args and not node.keywords:
                replacement = ast.copy_location(node.func.value, node)
                self._record_replacement(node, replacement)
                return replacement
        elif (
            operator == "swap_python_join_delimiter_v13"
            and len(node.args) == 1
            and isinstance(node.func, ast.Attribute)
        ):
            delimiter = node.func.value
            if isinstance(delimiter, ast.Constant) and delimiter.value == self.candidate["original_value"]:
                node.func.value = ast.copy_location(
                    ast.Constant(self.candidate["replacement_value"]), delimiter
                )
                self._record_replacement(node)
        elif operator == "increment_python_control_keyword_v13":
            for keyword in node.keywords:
                if keyword.arg == self.candidate["keyword"] and _numeric_literal(
                    keyword.value
                ) == self.candidate["original_value"]:
                    keyword.value = ast.copy_location(
                        ast.Constant(self.candidate["replacement_value"]), keyword.value
                    )
                    self._record_replacement(node)
                    break
        return node

    def visit_Subscript(self, node: ast.Subscript):  # noqa: N802
        node = self.generic_visit(node)
        if (
            self._matches(node)
            and self.candidate["operator"] == "decrement_python_slice_upper_v13"
            and isinstance(node.slice, ast.Slice)
            and _numeric_literal(node.slice.upper) == self.candidate["original_value"]
        ):
            node.slice.upper = ast.copy_location(
                ast.Constant(self.candidate["replacement_value"]), node.slice.upper
            )
            self._record_replacement(node)
        return node


def _replace_python_source_span(
    source: str,
    span: tuple[int, int, int, int],
    replacement: str,
) -> str:
    start_line, start_column, end_line, end_column = span
    lines = source.splitlines(keepends=True)
    if start_line < 1 or end_line < start_line or end_line > len(lines):
        raise ValueError("python_operator_target_span_out_of_range")
    encoded_lines = [line.encode("utf-8") for line in lines]
    start = sum(len(line) for line in encoded_lines[: start_line - 1]) + start_column
    end = sum(len(line) for line in encoded_lines[: end_line - 1]) + end_column
    encoded = source.encode("utf-8")
    if not (0 <= start < end <= len(encoded)):
        raise ValueError("python_operator_target_byte_span_invalid")
    newline = "\r\n" if "\r\n" in source else "\n"
    localized = replacement.replace("\n", newline).encode("utf-8")
    return (encoded[:start] + localized + encoded[end:]).decode("utf-8")


def apply_python_behavior_operator_v13(source: str, candidate: dict[str, Any]) -> str:
    if sha256_bytes(source.encode("utf-8")) != candidate["source_hash"]:
        raise ValueError("source_hash_mismatch")
    tree = ast.parse(source, filename="<skillscriptbench-v13-visible-source>")
    transformer = _PythonV13Transformer(candidate)
    transformed = transformer.visit(tree)
    ast.fix_missing_locations(transformed)
    if transformer.replaced != 1:
        raise ValueError(f"operator_replacement_count:{transformer.replaced}:expected:1")
    if transformer.original_span is None or transformer.replacement_node is None:
        raise ValueError("operator_replacement_missing_locality_receipt")
    replacement = ast.unparse(transformer.replacement_node)
    rendered = _replace_python_source_span(
        source, transformer.original_span, replacement
    )
    compile(rendered, "<skillscriptbench-v13-transformed-source>", "exec")
    expected_ast = ast.dump(transformed, include_attributes=False)
    localized_ast = ast.dump(ast.parse(rendered), include_attributes=False)
    if localized_ast != expected_ast:
        raise ValueError("localized_python_operator_ast_mismatch")
    return rendered


def _shell_candidate(
    package_id: str,
    *,
    source: str,
    source_hash: str,
    path: str,
    start: int,
    end: int,
    replacement: str,
    operator: str,
    family: str,
    dimension: str,
    role: str,
) -> dict[str, Any]:
    fragment = source[start:end]
    identity = {
        "package_id": package_id,
        "path": path,
        "start": start,
        "end": end,
        "operator": operator,
        "replacement": replacement,
    }
    return {
        "operator_candidate_id": canonical_json_hash(identity)[:24],
        "operator": operator,
        "operator_subfamily": f"{family}:{fragment}_to_{replacement or 'removed'}",
        "dimension": dimension,
        "path": path,
        "source_hash": source_hash,
        "line": source.count("\n", 0, start) + 1,
        "start": start,
        "end": end,
        "original_fragment": fragment,
        "replacement_fragment": replacement,
        "original_fragment_hash": sha256_bytes(fragment.encode("utf-8")),
        "structural_roles": [role],
        "operator_template_fingerprint": canonical_json_hash(
            {"family": family, "original": fragment, "replacement": replacement, "role": role}
        ),
        "construction_claim": (
            "One typed shell control or interface token changes while all other source bytes remain fixed."
        ),
    }


def _shell_bracket_spans(line: str) -> list[tuple[int, int]]:
    spans = [(match.start(), match.end()) for match in re.finditer(r"\[\[.*?\]\]", line)]
    masked = list(line)
    for start, end in spans:
        masked[start:end] = " " * (end - start)
    spans.extend((match.start(), match.end()) for match in re.finditer(r"\[[^\[\]]*?\]", "".join(masked)))
    return sorted(spans)


def enumerate_shell_behavior_operators_v13(
    source: str,
    *,
    source_hash: str,
    path: str,
    package_id: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    heredoc_end: str | None = None
    for line in source.splitlines(keepends=True):
        stripped = line.strip()
        if heredoc_end is not None:
            if stripped.lstrip("\t") == heredoc_end:
                heredoc_end = None
            offset += len(line)
            continue
        if not stripped or stripped.startswith("#"):
            offset += len(line)
            continue
        heredoc_match = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", line)
        if heredoc_match:
            heredoc_end = heredoc_match.group(1)

        for span_start, span_end in _shell_bracket_spans(line):
            expression = line[span_start:span_end]
            unary_map = {"-z": "-n", "-n": "-z", "-f": "-d", "-d": "-f"}
            for match in re.finditer(r"(?<!\S)(-z|-n|-f|-d)(?=\s)", expression):
                original = match.group(1)
                replacement = unary_map[original]
                role = "empty_value_guard" if original in {"-z", "-n"} else "path_kind_guard"
                rows.append(
                    _shell_candidate(
                        package_id,
                        source=source,
                        source_hash=source_hash,
                        path=path,
                        start=offset + span_start + match.start(1),
                        end=offset + span_start + match.end(1),
                        replacement=replacement,
                        operator="toggle_shell_unary_predicate_v13",
                        family="shell_unary_predicate",
                        dimension=("validation_or_recovery" if original in {"-z", "-n"} else "path_or_routing"),
                        role=role,
                    )
                )
            numeric_map = {
                "-lt": "-le",
                "-le": "-lt",
                "-gt": "-ge",
                "-ge": "-gt",
                "-eq": "-ne",
                "-ne": "-eq",
            }
            for match in re.finditer(r"(?<!\S)(-lt|-le|-gt|-ge|-eq|-ne)(?=\s)", expression):
                original = match.group(1)
                rows.append(
                    _shell_candidate(
                        package_id,
                        source=source,
                        source_hash=source_hash,
                        path=path,
                        start=offset + span_start + match.start(1),
                        end=offset + span_start + match.end(1),
                        replacement=numeric_map[original],
                        operator="toggle_shell_numeric_predicate_v13",
                        family="shell_numeric_predicate",
                        dimension="threshold_or_boundary",
                        role="numeric_guard",
                    )
                )
            for match in re.finditer(r"(?<=\s)(==|=|!=)(?=\s)", expression):
                original = match.group(1)
                replacement = "!=" if original in {"=", "=="} else "="
                rows.append(
                    _shell_candidate(
                        package_id,
                        source=source,
                        source_hash=source_hash,
                        path=path,
                        start=offset + span_start + match.start(1),
                        end=offset + span_start + match.end(1),
                        replacement=replacement,
                        operator="toggle_shell_string_predicate_v13",
                        family="shell_string_predicate",
                        dimension="path_or_routing",
                        role="string_route_guard",
                    )
                )

        exit_match = re.match(r"^(\s*)(exit|return)\s+([0-3])(?=\s*(?:#.*)?$)", line.rstrip("\r\n"))
        if exit_match:
            original = int(exit_match.group(3))
            replacement = "1" if original == 0 else "0"
            rows.append(
                _shell_candidate(
                    package_id,
                    source=source,
                    source_hash=source_hash,
                    path=path,
                    start=offset + exit_match.start(3),
                    end=offset + exit_match.end(3),
                    replacement=replacement,
                    operator="toggle_shell_exit_status_v13",
                    family="shell_exit_status_contract",
                    dimension="cli_contract",
                    role=f"{exit_match.group(2)}_status",
                )
            )

        for match in re.finditer(r"(?<!\S)(?:1)?(?:>&2|>&1)(?!\S)", line):
            original = match.group(0)
            replacement = "1>&1" if original.endswith(">&2") else "1>&2"
            rows.append(
                _shell_candidate(
                    package_id,
                    source=source,
                    source_hash=source_hash,
                    path=path,
                    start=offset + match.start(),
                    end=offset + match.end(),
                    replacement=replacement,
                    operator="swap_shell_output_stream_v13",
                    family="shell_output_stream_route",
                    dimension="cli_contract",
                    role="output_stream_redirection",
                )
            )

        for match in re.finditer(r"\s+\|\|\s+true(?=\s*(?:#.*)?$)", line.rstrip("\r\n")):
            rows.append(
                _shell_candidate(
                    package_id,
                    source=source,
                    source_hash=source_hash,
                    path=path,
                    start=offset + match.start(),
                    end=offset + match.end(),
                    replacement="",
                    operator="remove_shell_failure_mask_v13",
                    family="shell_failure_recovery",
                    dimension="validation_or_recovery",
                    role="failure_mask",
                )
            )
        offset += len(line)
    deduped = {row["operator_candidate_id"]: row for row in rows}
    return sorted(
        deduped.values(),
        key=lambda row: (
            row["dimension"],
            row["operator_subfamily"],
            row["path"],
            row["line"],
            row["start"],
        ),
    )


def apply_shell_behavior_operator_v13(source: str, candidate: dict[str, Any]) -> str:
    if sha256_bytes(source.encode("utf-8")) != candidate["source_hash"]:
        raise ValueError("source_hash_mismatch")
    start = int(candidate["start"])
    end = int(candidate["end"])
    fragment = source[start:end]
    if fragment != candidate["original_fragment"]:
        raise ValueError("original_fragment_mismatch")
    if sha256_bytes(fragment.encode("utf-8")) != candidate["original_fragment_hash"]:
        raise ValueError("original_fragment_hash_mismatch")
    transformed = source[:start] + candidate["replacement_fragment"] + source[end:]
    if transformed == source:
        raise ValueError("operator_did_not_change_source")
    return transformed


PYTHON_V13_OPERATORS = {
    "decrement_python_slice_upper_v13",
    "increment_python_control_keyword_v13",
    "remove_python_path_normalization_v13",
    "swap_python_join_delimiter_v13",
    "swap_python_output_stream_v13",
    "swap_python_write_stream_v13",
    "toggle_python_comparison_boundary_v13",
    "toggle_python_exit_call_status_v13",
    "toggle_python_guard_connector_v13",
    "toggle_python_membership_polarity_v13",
    "toggle_python_return_status_v13",
}
SHELL_V13_OPERATORS = {
    "remove_shell_failure_mask_v13",
    "swap_shell_output_stream_v13",
    "toggle_shell_exit_status_v13",
    "toggle_shell_numeric_predicate_v13",
    "toggle_shell_string_predicate_v13",
    "toggle_shell_unary_predicate_v13",
}


def apply_behavior_operator_v13(language: str, source: str, operator: dict[str, Any]) -> str:
    name = operator.get("operator")
    if language == "python" and name in PYTHON_V13_OPERATORS:
        return apply_python_behavior_operator_v13(source, operator)
    if language == "shell" and name in SHELL_V13_OPERATORS:
        return apply_shell_behavior_operator_v13(source, operator)
    if language in {"javascript", "typescript"} and name == "typed_js_ts_mutation_v08":
        from .js_mutations_v08 import apply_js_mutation_v08

        return apply_js_mutation_v08(source, operator)
    from .source_test_mutations_v09 import _apply

    return _apply(language, source, operator)


def mutation_locality_receipt_v13(
    source: str,
    transformed: str,
    *,
    maximum_edit_span_bytes: int = 12_000,
    maximum_edit_fraction: float = 0.20,
) -> dict[str, Any]:
    original = source.encode("utf-8")
    candidate = transformed.encode("utf-8")
    prefix = 0
    shared_limit = min(len(original), len(candidate))
    while prefix < shared_limit and original[prefix] == candidate[prefix]:
        prefix += 1
    suffix = 0
    suffix_limit = shared_limit - prefix
    while (
        suffix < suffix_limit
        and original[len(original) - suffix - 1]
        == candidate[len(candidate) - suffix - 1]
    ):
        suffix += 1
    original_changed = len(original) - prefix - suffix
    candidate_changed = len(candidate) - prefix - suffix
    maximum_changed = max(original_changed, candidate_changed)
    fraction = maximum_changed / max(len(original), 1)
    changed = source != transformed
    bounded = bool(
        changed
        and maximum_changed <= maximum_edit_span_bytes
        and fraction <= maximum_edit_fraction
    )
    return {
        "policy": "single_span_source_locality_v1",
        "status": "pass" if bounded else "fail",
        "reason": (
            "bounded_single_source_span"
            if bounded
            else "operator_did_not_change_source"
            if not changed
            else "source_change_span_exceeds_locality_bound"
        ),
        "source_byte_count": len(original),
        "transformed_byte_count": len(candidate),
        "shared_prefix_byte_count": prefix,
        "shared_suffix_byte_count": suffix,
        "original_changed_span_byte_count": original_changed,
        "transformed_changed_span_byte_count": candidate_changed,
        "maximum_changed_span_byte_count": maximum_changed,
        "maximum_changed_fraction": fraction,
        "maximum_edit_span_bytes": maximum_edit_span_bytes,
        "maximum_edit_fraction_limit": maximum_edit_fraction,
    }


def enumerate_repository_operators_v13(
    package: dict[str, Any],
    *,
    node_executable: str | None = None,
    preserve_typescript_language: bool = False,
) -> list[dict[str, Any]]:
    rows = _enumerate_package_operators(
        package,
        node_executable=node_executable,
        preserve_typescript_language=preserve_typescript_language,
    )
    root = Path(package["source_local_root"]).resolve() / package["relative_root"]
    target_files = package.get("target_files", [])
    for operator in enumerate_python_behavior_operators_v13(
        root,
        target_files,
        package_id=package["package_id"],
    ):
        rows.append(
            {
                "operator_id": operator["operator_candidate_id"],
                "language": "python",
                "family": operator["operator_subfamily"],
                "dimension": operator["dimension"],
                "operator": operator,
            }
        )
    for relative in target_files:
        path = root / relative
        if path.suffix.lower() not in SHELL_SUFFIXES or not path.is_file():
            continue
        source = path.read_bytes().decode("utf-8")
        for operator in enumerate_shell_behavior_operators_v13(
            source,
            source_hash=sha256_file(path),
            path=relative,
            package_id=package["package_id"],
        ):
            rows.append(
                {
                    "operator_id": operator["operator_candidate_id"],
                    "language": "shell",
                    "family": operator["operator_subfamily"],
                    "dimension": operator["dimension"],
                    "operator": operator,
                }
            )
    from .js_mutations_v08 import enumerate_js_mutations_v08

    for relative in target_files:
        path = root / relative
        if path.suffix.lower() not in {".js", ".mjs", ".cjs", ".ts"} or not path.is_file():
            continue
        source = path.read_bytes().decode("utf-8")
        for mutation in enumerate_js_mutations_v08(
            source,
            source_hash=sha256_file(path),
            path=relative,
            package_id=package["package_id"],
            node_executable=node_executable,
        ):
            operator = {
                **mutation,
                "operator_candidate_id": mutation["mutation_id"],
                "operator": "typed_js_ts_mutation_v08",
                "operator_subfamily": mutation["family"],
                "operator_template_fingerprint": mutation[
                    "mutation_template_fingerprint"
                ],
            }
            rows.append(
                {
                    "operator_id": operator["operator_candidate_id"],
                    "language": mutation["language"],
                    "family": mutation["family"],
                    "dimension": mutation["dimension"],
                    "operator": operator,
                }
            )
    deduped = {row["operator_id"]: row for row in rows}
    return sorted(
        deduped.values(),
        key=lambda row: (row["dimension"], row["family"], row["operator_id"]),
    )
