from __future__ import annotations

import ast
import copy
import io
import json
import tokenize
from collections import Counter
from pathlib import Path
from typing import Any

from .io_utils import canonical_json_hash, read_json, sha256_bytes, sha256_file, write_json


PARAMETER_DIMENSIONS = {
    "parameter_or_threshold": {
        "attempt",
        "count",
        "cutoff",
        "limit",
        "max",
        "min",
        "rank",
        "retries",
        "retry",
        "threshold",
        "timeout",
        "tolerance",
        "top",
    },
    "schema_or_format": {
        "column",
        "delimiter",
        "encoding",
        "field",
        "format",
        "key",
        "schema",
        "separator",
        "suffix",
    },
    "state_or_artifact": {
        "destination",
        "dir",
        "directory",
        "file",
        "filename",
        "output",
        "path",
        "root",
    },
}
SENSITIVE_PARAMETER_TOKENS = {
    "api",
    "auth",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
}
ARTIFACT_CALLS = {
    "dump",
    "mkdir",
    "makedirs",
    "rename",
    "touch",
    "unlink",
    "write",
    "write_bytes",
    "write_text",
}


class _OperatorTemplateNormalizer(ast.NodeTransformer):
    def visit_Name(self, node: ast.Name):  # noqa: N802
        return ast.copy_location(ast.Name(id="VAR", ctx=node.ctx), node)

    def visit_arg(self, node: ast.arg):  # noqa: N802
        return ast.copy_location(ast.arg(arg="ARG", annotation=None), node)

    def visit_Attribute(self, node: ast.Attribute):  # noqa: N802
        node = self.generic_visit(node)
        node.attr = "ATTR"
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef):  # noqa: N802
        node = self.generic_visit(node)
        node.name = "FUNCTION"
        node.decorator_list = []
        node.returns = None
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):  # noqa: N802
        node = self.generic_visit(node)
        node.name = "FUNCTION"
        node.decorator_list = []
        node.returns = None
        return node

    def visit_Constant(self, node: ast.Constant):  # noqa: N802
        if node.value is None or isinstance(node.value, bool):
            value: Any = node.value
        elif isinstance(node.value, str):
            value = "STR"
        elif isinstance(node.value, int):
            value = 1
        elif isinstance(node.value, float):
            value = 1.0
        else:
            value = type(node.value).__name__
        return ast.copy_location(ast.Constant(value=value), node)


def _identifier_tokens(value: str) -> set[str]:
    return {token for token in value.lower().replace("-", "_").split("_") if token}


def _call_terminal(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _call_path(node: ast.Call) -> str:
    current: ast.AST = node.func
    pieces: list[str] = []
    while isinstance(current, ast.Attribute):
        pieces.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        pieces.append(current.id)
    return ".".join(reversed(pieces))


def _expression_path(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _expression_path(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _is_artifact_call(node: ast.Call) -> bool:
    terminal = _call_terminal(node)
    if terminal not in ARTIFACT_CALLS:
        return False
    path = _call_path(node).lower()
    if path.startswith(("sys.stderr.", "sys.stdout.", "logging.")):
        return False
    if terminal == "dump":
        target = node.args[1] if len(node.args) >= 2 else next(
            (
                keyword.value
                for keyword in node.keywords
                if keyword.arg in {"file", "fp", "stream"}
            ),
            None,
        )
        if target is None:
            return False
        if _expression_path(target) in {"sys.stderr", "sys.stdout"}:
            return False
        return True
    if terminal == "write":
        receiver_tokens = _identifier_tokens(path.rsplit(".", 1)[0])
        return bool(
            receiver_tokens
            & {"archive", "file", "handle", "output", "stream", "writer", "zip", "zipf"}
        )
    if terminal == "rename":
        receiver_tokens = _identifier_tokens(path.rsplit(".", 1)[0])
        return path.startswith("os.") or bool(receiver_tokens & {"file", "path"})
    return True


def _literal_value(node: ast.AST) -> Any:
    value = ast.literal_eval(node)
    json.dumps(value, sort_keys=True)
    return value


def _function_defaults(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[tuple[str, ast.AST, Any]]:
    positional = [*function.args.posonlyargs, *function.args.args]
    aligned = [None] * (len(positional) - len(function.args.defaults)) + list(
        function.args.defaults
    )
    rows: list[tuple[str, ast.AST, Any]] = []
    for argument, default in zip(positional, aligned):
        if default is None or argument.arg in {"self", "cls"}:
            continue
        try:
            rows.append((argument.arg, default, _literal_value(default)))
        except (TypeError, ValueError, SyntaxError):
            continue
    for argument, default in zip(function.args.kwonlyargs, function.args.kw_defaults):
        if default is None:
            continue
        try:
            rows.append((argument.arg, default, _literal_value(default)))
        except (TypeError, ValueError, SyntaxError):
            continue
    return rows


def _parameter_dimension(name: str) -> str | None:
    tokens = _identifier_tokens(name)
    if tokens & SENSITIVE_PARAMETER_TOKENS:
        return None
    for dimension, vocabulary in PARAMETER_DIMENSIONS.items():
        if tokens & vocabulary:
            return dimension
    return None


def _top_level_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    ]


class _ScopeNodeCollector(ast.NodeVisitor):
    def __init__(self, root: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.root = root
        self.nodes: list[ast.AST] = []

    def generic_visit(self, node: ast.AST) -> None:
        self.nodes.append(node)
        super().generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        if node is self.root:
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        if node is self.root:
            self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        if node is self.root:
            self.generic_visit(node)


def _scope_nodes(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    collector = _ScopeNodeCollector(function)
    collector.visit(function)
    return collector.nodes


def _function_argument_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    return {
        argument.arg
        for argument in [
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        ]
    }


class _ClosureLoadCounter(ast.NodeVisitor):
    def __init__(self, root: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> None:
        self.root = root
        self.name = name
        self.count = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        if node is not self.root and self.name in _function_argument_names(node):
            return
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        if node is not self.root and self.name in _function_argument_names(node):
            return
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        arguments = {
            argument.arg
            for argument in [
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ]
        }
        if self.name not in arguments:
            self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if node.id == self.name and isinstance(node.ctx, ast.Load):
            self.count += 1


def _closure_load_count(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    name: str,
) -> int:
    visitor = _ClosureLoadCounter(function, name)
    visitor.visit(function)
    return visitor.count


def _operator_template_fingerprint(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    operator_subfamily: str,
) -> str:
    normalized = _OperatorTemplateNormalizer().visit(copy.deepcopy(function))
    ast.fix_missing_locations(normalized)
    return canonical_json_hash(
        {
            "operator_subfamily": operator_subfamily,
            "normalized_function": ast.dump(normalized, include_attributes=False),
        }
    )


def _scope_parent_map(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[ast.AST, ast.AST]:
    nodes = _scope_nodes(function)
    node_ids = {id(node) for node in nodes}
    return {
        child: parent
        for parent in nodes
        for child in ast.iter_child_nodes(parent)
        if id(child) in node_ids
    }


def _parameter_structural_roles(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    parameter: str,
) -> list[str]:
    parents = _scope_parent_map(function)
    roles: set[str] = set()
    for node in _scope_nodes(function):
        if not (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id == parameter
        ):
            continue
        current: ast.AST = node
        for _depth in range(6):
            parent = parents.get(current)
            if parent is None:
                break
            if isinstance(parent, ast.Slice):
                roles.add("slice_bound")
                break
            if isinstance(parent, ast.Compare):
                roles.add("comparison_operand")
                break
            if isinstance(parent, (ast.If, ast.IfExp, ast.While)) and parent.test is current:
                roles.add("branch_condition")
                break
            if isinstance(parent, ast.keyword):
                roles.add("call_keyword")
                break
            if isinstance(parent, ast.Call):
                roles.add("call_argument")
                break
            if isinstance(parent, ast.Subscript) and parent.slice is current:
                roles.add("index_or_key")
                break
            if isinstance(parent, (ast.BinOp, ast.UnaryOp)):
                roles.add("arithmetic_operand")
                break
            current = parent
        else:
            roles.add("general_value")
    return sorted(roles or {"general_value"})


def _call_context_role(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    call: ast.Call,
) -> str:
    parents = _scope_parent_map(function)
    current: ast.AST = call
    for _depth in range(5):
        parent = parents.get(current)
        if parent is None:
            break
        if isinstance(parent, ast.Return):
            return "return_pipeline"
        if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            return "assigned_intermediate"
        if isinstance(parent, ast.keyword):
            return "nested_call_argument"
        if isinstance(parent, ast.Call):
            return "nested_call_argument"
        if isinstance(parent, ast.Expr):
            return "side_effect_call"
        if isinstance(parent, (ast.BinOp, ast.BoolOp, ast.Compare, ast.Subscript)):
            return "expression_pipeline"
        current = parent
    return "general_call_edge"


def _artifact_subfamily(call: ast.Call) -> str:
    terminal = _call_terminal(call)
    if terminal in {"write", "write_bytes", "write_text"}:
        return "file_content_write"
    if terminal == "dump":
        return "serializer_write"
    if terminal in {"mkdir", "makedirs", "touch"}:
        return "path_creation"
    if terminal == "rename":
        return "move_or_atomic_commit"
    if terminal == "unlink":
        return "cleanup"
    return "other_artifact_effect"


def _recovery_roles(handler: ast.ExceptHandler) -> list[str]:
    roles: set[str] = set()
    for statement in handler.body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Return):
                roles.add("fallback_return")
            elif isinstance(node, ast.Continue):
                roles.add("retry_continue")
            elif isinstance(node, ast.Break):
                roles.add("loop_abort")
            elif isinstance(node, ast.Raise):
                roles.add("error_translation")
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                roles.add("state_update")
            elif isinstance(node, ast.Call):
                terminal = _call_terminal(node)
                if terminal in {"print", "debug", "error", "exception", "info", "warning"}:
                    roles.add("error_reporting")
                else:
                    roles.add("fallback_call")
    return sorted(roles or {"empty_or_pass"})


def _candidate(payload: dict[str, Any]) -> dict[str, Any]:
    identity = {
        key: payload.get(key)
        for key in (
            "operator",
            "dimension",
            "path",
            "symbol",
            "line",
            "column",
            "parameter",
            "callee_symbol",
            "callee_path",
        )
    }
    return {"operator_candidate_id": canonical_json_hash(identity)[:24], **payload}


def enumerate_python_operators(
    package_root: str | Path,
    script_files: list[str] | None = None,
) -> list[dict[str, Any]]:
    root = Path(package_root).resolve()
    relative_paths = (
        sorted(script_files)
        if script_files is not None
        else sorted(
            path.relative_to(root).as_posix()
            for directory in (root / "scripts", root / "script")
            if directory.is_dir()
            for path in directory.rglob("*.py")
        )
    )
    python_paths = [relative for relative in relative_paths if Path(relative).suffix == ".py"]
    trees: dict[str, ast.Module] = {}
    for relative in python_paths:
        path = root / relative
        if path.name.startswith("test_"):
            continue
        try:
            trees[relative] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
    module_paths: dict[str, str] = {}
    duplicate_modules: set[str] = set()
    for relative in trees:
        stem = Path(relative).stem
        if stem in module_paths:
            duplicate_modules.add(stem)
        module_paths[stem] = relative
    for stem in duplicate_modules:
        module_paths.pop(stem, None)

    candidates: list[dict[str, Any]] = []
    for relative, tree in trees.items():
        source_hash = sha256_file(root / relative)
        imported_symbols: dict[str, tuple[str, str]] = {}
        for statement in tree.body:
            if not isinstance(statement, ast.ImportFrom) or not statement.module:
                continue
            module = statement.module.rsplit(".", 1)[-1]
            callee_path = module_paths.get(module)
            if not callee_path or callee_path == relative:
                continue
            for alias in statement.names:
                imported_symbols[alias.asname or alias.name] = (callee_path, alias.name)

        for function in _top_level_functions(tree):
            scope_nodes = _scope_nodes(function)
            assigned_names = {
                node.id
                for node in scope_nodes
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
            }
            for parameter, default_node, default_value in _function_defaults(function):
                dimension = _parameter_dimension(parameter)
                load_count = _closure_load_count(function, parameter)
                if dimension is None or parameter in assigned_names or load_count == 0:
                    continue
                structural_roles = _parameter_structural_roles(function, parameter)
                operator_subfamily = f"lock_optional_parameter:{structural_roles[0]}"
                candidates.append(
                    _candidate(
                        {
                            "operator": "lock_optional_parameter",
                            "dimension": dimension,
                            "path": relative,
                            "symbol": function.name,
                            "line": function.lineno,
                            "column": function.col_offset,
                            "parameter": parameter,
                            "default": default_value,
                            "parameter_load_count": load_count,
                            "structural_roles": structural_roles,
                            "operator_subfamily": operator_subfamily,
                            "operator_template_fingerprint": _operator_template_fingerprint(
                                function,
                                operator_subfamily,
                            ),
                            "source_hash": source_hash,
                            "construction_claim": (
                                "The transformed implementation keeps the signature but replaces all "
                                "reads of one optional parameter with its compatibility default."
                            ),
                        }
                    )
                )

            for node in scope_nodes:
                if isinstance(node, ast.ExceptHandler):
                    already_reraises = len(node.body) == 1 and isinstance(node.body[0], ast.Raise)
                    if not already_reraises:
                        recovery_roles = _recovery_roles(node)
                        operator_subfamily = (
                            f"remove_recovery_handler:{recovery_roles[0]}"
                        )
                        candidates.append(
                            _candidate(
                                {
                                    "operator": "remove_recovery_handler",
                                    "dimension": "error_dependency_or_doc_code_contract",
                                    "path": relative,
                                    "symbol": function.name,
                                    "line": node.lineno,
                                    "column": node.col_offset,
                                    "structural_roles": recovery_roles,
                                    "operator_subfamily": operator_subfamily,
                                    "operator_template_fingerprint": _operator_template_fingerprint(
                                        function,
                                        operator_subfamily,
                                    ),
                                    "source_hash": source_hash,
                                    "construction_claim": (
                                        "The transformed implementation re-raises one previously handled error."
                                    ),
                                }
                            )
                        )
                if (
                    isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Call)
                    and _is_artifact_call(node.value)
                ):
                    artifact_subfamily = _artifact_subfamily(node.value)
                    operator_subfamily = (
                        f"remove_artifact_effect:{artifact_subfamily}"
                    )
                    candidates.append(
                        _candidate(
                            {
                                "operator": "remove_artifact_effect",
                                "dimension": "state_or_artifact",
                                "path": relative,
                                "symbol": function.name,
                                "line": node.lineno,
                                "column": node.col_offset,
                                "call": _call_path(node.value),
                                "structural_roles": [artifact_subfamily],
                                "operator_subfamily": operator_subfamily,
                                "operator_template_fingerprint": _operator_template_fingerprint(
                                    function,
                                    operator_subfamily,
                                ),
                                "source_hash": source_hash,
                                "construction_claim": (
                                    "The transformed implementation omits one filesystem or artifact effect."
                                ),
                            }
                        )
                    )
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in imported_symbols
                    and node.args
                ):
                    callee_path, callee_symbol = imported_symbols[node.func.id]
                    call_role = _call_context_role(function, node)
                    operator_subfamily = f"bypass_local_call_edge:{call_role}"
                    candidates.append(
                        _candidate(
                            {
                                "operator": "bypass_local_call_edge",
                                "dimension": "cross_script_composition_or_dataflow",
                                "path": relative,
                                "symbol": function.name,
                                "line": node.lineno,
                                "column": node.col_offset,
                                "callee_path": callee_path,
                                "callee_symbol": callee_symbol,
                                "structural_roles": [call_role],
                                "operator_subfamily": operator_subfamily,
                                "operator_template_fingerprint": _operator_template_fingerprint(
                                    function,
                                    operator_subfamily,
                                ),
                                "source_hash": source_hash,
                                "construction_claim": (
                                    "The transformed implementation bypasses one imported package-local helper call."
                                ),
                            }
                        )
                    )
    deduped = {row["operator_candidate_id"]: row for row in candidates}
    return sorted(
        deduped.values(),
        key=lambda row: (
            row["dimension"],
            row["operator"],
            row["path"],
            row["line"],
            row["column"],
            row["operator_candidate_id"],
        ),
    )


class _OperatorTransformer(ast.NodeTransformer):
    def __init__(self, candidate: dict[str, Any]) -> None:
        self.candidate = candidate
        self.target_function = False
        self.replaced = 0
        self.source_edits: list[
            tuple[tuple[int, int, int, int], ast.AST]
        ] = []
        self.source_text_edits: list[
            tuple[tuple[int, int, int, int], str]
        ] = []
        self.function_name_edits: list[tuple[int, int, str, str]] = []

    @staticmethod
    def _span(node: ast.AST) -> tuple[int, int, int, int]:
        coordinates = (
            getattr(node, "lineno", None),
            getattr(node, "col_offset", None),
            getattr(node, "end_lineno", None),
            getattr(node, "end_col_offset", None),
        )
        if not all(isinstance(value, int) for value in coordinates):
            raise ValueError("operator_target_missing_source_span")
        return coordinates  # type: ignore[return-value]

    def _record_replacement(
        self, original: ast.AST, replacement: ast.AST | None = None
    ) -> None:
        coordinates = self._span(original)
        self.source_edits.append(
            (coordinates, replacement or original)
        )
        self.replaced += 1

    def _record_text_replacement(
        self, span: tuple[int, int, int, int], replacement: str
    ) -> None:
        self.source_text_edits.append((span, replacement))
        self.replaced += 1

    def visit_FunctionDef(self, node: ast.FunctionDef):  # noqa: N802
        if self.target_function:
            if self.candidate["operator"] != "lock_optional_parameter":
                return node
            if self.candidate["parameter"] in _function_argument_names(node):
                return node
            return self.generic_visit(node)
        if node.name != self.candidate["symbol"]:
            return node
        if self.candidate["operator"] == "rename_documented_function":
            original_name = node.name
            node.name = self.candidate["replacement_symbol"]
            self.function_name_edits.append(
                (
                    node.lineno,
                    node.col_offset,
                    original_name,
                    node.name,
                )
            )
            self.replaced += 1
            return node
        previous = self.target_function
        self.target_function = True
        node = self.generic_visit(node)
        self.target_function = previous
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):  # noqa: N802
        return self.visit_FunctionDef(node)  # type: ignore[arg-type]

    def visit_ClassDef(self, node: ast.ClassDef):  # noqa: N802
        if self.target_function:
            return node
        return self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda):  # noqa: N802
        if not self.target_function or self.candidate["operator"] != "lock_optional_parameter":
            return self.generic_visit(node)
        arguments = {
            argument.arg
            for argument in [
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ]
        }
        if self.candidate["parameter"] in arguments:
            return node
        return self.generic_visit(node)

    def visit_Name(self, node: ast.Name):  # noqa: N802
        if (
            self.target_function
            and self.candidate["operator"] == "lock_optional_parameter"
            and node.id == self.candidate["parameter"]
            and isinstance(node.ctx, ast.Load)
        ):
            replacement = ast.parse(repr(self.candidate["default"]), mode="eval").body
            self._record_replacement(node, replacement)
            return ast.copy_location(replacement, node)
        return node

    def visit_Constant(self, node: ast.Constant):  # noqa: N802
        if not (
            self.target_function
            and self.candidate["operator"] in {
                "replace_cross_script_artifact_name",
                "replace_documented_input_artifact_name",
            }
            and node.lineno == self.candidate["line"]
            and node.col_offset == self.candidate["column"]
            and isinstance(node.value, str)
            and node.value == self.candidate["artifact_name"]
        ):
            return node
        replacement = ast.Constant(value=self.candidate["mutated_artifact_name"])
        self._record_replacement(node, replacement)
        return ast.copy_location(replacement, node)

    def visit_Subscript(self, node: ast.Subscript):  # noqa: N802
        node = self.generic_visit(node)
        if (
            self.target_function
            and self.candidate["operator"] == "replace_cross_script_call_result_field"
            and isinstance(node.value, ast.Name)
            and node.value.id == self.candidate["result_variable"]
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
            and node.slice.value == self.candidate["field_name"]
            and (node.lineno, node.col_offset)
            in {
                (int(location["line"]), int(location["column"]))
                for location in self.candidate["locations"]
            }
        ):
            original = node.slice
            replacement = ast.Constant(value=self.candidate["mutated_field"])
            self._record_replacement(original, replacement)
            node.slice = ast.copy_location(replacement, original)
            return node
        if not (
            self.target_function
            and self.candidate["operator"] in {
                "replace_cross_script_handoff_field",
                "replace_documented_input_field",
            }
            and node.lineno == self.candidate["line"]
            and node.col_offset == self.candidate["column"]
        ):
            return node
        original = node.slice
        if not (
            isinstance(original, ast.Constant)
            and isinstance(original.value, str)
            and original.value == self.candidate["field_name"]
        ):
            return node
        replacement = ast.Constant(value=self.candidate["mutated_field"])
        self._record_replacement(original, replacement)
        node.slice = ast.copy_location(replacement, original)
        return node

    def visit_ExceptHandler(self, node: ast.ExceptHandler):  # noqa: N802
        node = self.generic_visit(node)
        if (
            self.target_function
            and self.candidate["operator"] == "remove_recovery_handler"
            and node.lineno == self.candidate["line"]
            and node.col_offset == self.candidate["column"]
        ):
            node.body = [ast.copy_location(ast.Raise(), node)]
            self._record_replacement(node)
        return node

    def visit_Expr(self, node: ast.Expr):  # noqa: N802
        node = self.generic_visit(node)
        if (
            self.target_function
            and self.candidate["operator"] == "remove_artifact_effect"
            and node.lineno == self.candidate["line"]
            and node.col_offset == self.candidate["column"]
        ):
            replacement = ast.copy_location(ast.Pass(), node)
            self._record_replacement(node, replacement)
            return replacement
        return node

    def visit_If(self, node: ast.If):  # noqa: N802
        node = self.generic_visit(node)
        if (
            self.target_function
            and self.candidate["operator"] == "remove_validation_guard"
            and node.lineno == self.candidate["line"]
            and node.col_offset == self.candidate["column"]
        ):
            replacement = ast.copy_location(ast.Pass(), node)
            self._record_replacement(node, replacement)
            return replacement
        return node

    def visit_Dict(self, node: ast.Dict):  # noqa: N802
        node = self.generic_visit(node)
        if (
            self.target_function
            and self.candidate["operator"] == "replace_documented_return_field"
            and node.lineno == self.candidate["line"]
            and node.col_offset == self.candidate["column"]
        ):
            index = int(self.candidate["field_index"])
            if index < 0 or index >= len(node.keys):
                return node
            original = node.keys[index]
            if not (
                isinstance(original, ast.Constant)
                and isinstance(original.value, str)
                and original.value == self.candidate["field_name"]
            ):
                return node
            replacement = ast.Constant(value=self.candidate["mutated_field"])
            self._record_replacement(original, replacement)
            node.keys[index] = ast.copy_location(replacement, original)
            return node
        if (
            self.target_function
            and self.candidate["operator"] == "drop_output_schema_field"
            and node.lineno == self.candidate["line"]
            and node.col_offset == self.candidate["column"]
        ):
            index = int(self.candidate["field_index"])
            if index < 0 or index >= len(node.keys):
                return node
            if index < len(node.keys) - 1 and node.keys[index + 1] is not None:
                current = self._span(node.keys[index])
                following = self._span(node.keys[index + 1])
                removal_span = (
                    current[0],
                    current[1],
                    following[0],
                    following[1],
                )
                self._record_text_replacement(removal_span, "")
            elif index > 0:
                previous = self._span(node.values[index - 1])
                current = self._span(node.values[index])
                removal_span = (
                    previous[2],
                    previous[3],
                    current[2],
                    current[3],
                )
                self._record_text_replacement(removal_span, "")
            else:
                self._record_replacement(node)
            del node.keys[index]
            del node.values[index]
        return node

    def visit_Call(self, node: ast.Call):  # noqa: N802
        node = self.generic_visit(node)
        if not (
            self.target_function
            and node.lineno == self.candidate["line"]
            and node.col_offset == self.candidate["column"]
        ):
            return node
        if self.candidate["operator"] == "remove_documented_cli_flag":
            if (
                _call_terminal(node) == "add_argument"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == self.candidate["flag"]
            ):
                replacement = ast.copy_location(ast.Constant(value=None), node)
                self._record_replacement(node, replacement)
                return replacement
            return node
        if self.candidate["operator"] == "drop_documented_cli_choice":
            if not (
                _call_terminal(node) == "add_argument"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == self.candidate["flag"]
            ):
                return node
            choices_keyword = next(
                (keyword for keyword in node.keywords if keyword.arg == "choices"),
                None,
            )
            if choices_keyword is None or not isinstance(
                choices_keyword.value, (ast.List, ast.Tuple, ast.Set)
            ):
                return node
            index = int(self.candidate["choice_index"])
            elements = choices_keyword.value.elts
            if index < 0 or index >= len(elements):
                return node
            try:
                current_value = ast.literal_eval(elements[index])
            except (TypeError, ValueError, SyntaxError):
                return node
            if current_value != self.candidate["choice"]:
                return node
            original = choices_keyword.value
            replacement = copy.deepcopy(original)
            del replacement.elts[index]
            self._record_replacement(original, replacement)
            choices_keyword.value = ast.copy_location(replacement, original)
            return node
        if self.candidate["operator"] == "replace_documented_cli_default":
            if not (
                _call_terminal(node) == "add_argument"
                and self.candidate["flag"]
                in {
                    argument.value
                    for argument in node.args
                    if isinstance(argument, ast.Constant)
                    and isinstance(argument.value, str)
                }
            ):
                return node
            default_keyword = next(
                (keyword for keyword in node.keywords if keyword.arg == "default"),
                None,
            )
            if default_keyword is None:
                return node
            try:
                current_default = ast.literal_eval(default_keyword.value)
            except (TypeError, ValueError, SyntaxError):
                return node
            if current_default != self.candidate["original_default"]:
                return node
            original = default_keyword.value
            replacement = ast.parse(
                repr(self.candidate["mutated_default"]), mode="eval"
            ).body
            self._record_replacement(original, replacement)
            default_keyword.value = ast.copy_location(replacement, original)
            return node
        if self.candidate["operator"] == "replace_documented_environment_key":
            call_path = _call_path(node)
            if call_path not in {"os.getenv", "os.environ.get"} or not node.args:
                return node
            original = node.args[0]
            if not (
                isinstance(original, ast.Constant)
                and isinstance(original.value, str)
                and original.value == self.candidate["original_env_key"]
            ):
                return node
            replacement = ast.Constant(value=self.candidate["mutated_env_key"])
            self._record_replacement(original, replacement)
            node.args[0] = ast.copy_location(replacement, original)
            return node
        call_scoped_operators = {
            "remove_environment_fallback",
            "remove_order_key",
            "remove_serializer_option",
            "toggle_sort_direction",
        }
        if (
            self.candidate["operator"] in call_scoped_operators
            and self.candidate.get("call")
            and _call_path(node) != self.candidate["call"]
        ):
            return node
        if self.candidate["operator"] in {
            "remove_order_key",
            "remove_serializer_option",
        }:
            keyword = self.candidate["keyword"]
            previous = len(node.keywords)
            node.keywords = [item for item in node.keywords if item.arg != keyword]
            if len(node.keywords) == previous - 1:
                self._record_replacement(node)
            return node
        if self.candidate["operator"] == "toggle_sort_direction":
            for keyword in node.keywords:
                if (
                    keyword.arg == "reverse"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, bool)
                ):
                    keyword.value = ast.copy_location(
                        ast.Constant(value=not keyword.value.value),
                        keyword.value,
                    )
                    self._record_replacement(node)
                    break
            return node
        if self.candidate["operator"] == "remove_environment_fallback":
            if self.candidate["fallback_kind"] == "positional" and len(node.args) == 2:
                del node.args[1]
                self._record_replacement(node)
            elif self.candidate["fallback_kind"] == "keyword":
                previous = len(node.keywords)
                node.keywords = [
                    item for item in node.keywords if item.arg != "default"
                ]
                if len(node.keywords) == previous - 1:
                    self._record_replacement(node)
            return node
        if (
            self.candidate["operator"] == "bypass_local_call_edge"
            and node.args
        ):
            replacement = ast.copy_location(node.args[0], node)
            self._record_replacement(node, replacement)
            return replacement
        return node


def _python_span_to_offsets(
    source: str, span: tuple[int, int, int, int]
) -> tuple[int, int]:
    start_line, start_column, end_line, end_column = span
    lines = source.splitlines(keepends=True)
    if start_line < 1 or end_line < start_line or end_line > len(lines):
        raise ValueError("operator_target_span_out_of_range")
    encoded_lines = [line.encode("utf-8") for line in lines]
    start = sum(len(line) for line in encoded_lines[: start_line - 1]) + start_column
    end = sum(len(line) for line in encoded_lines[: end_line - 1]) + end_column
    if not (0 <= start < end <= len(source.encode("utf-8"))):
        raise ValueError("operator_target_byte_span_invalid")
    return start, end


def _function_name_span(
    source: str,
    *,
    line: int,
    minimum_column: int,
    original_name: str,
) -> tuple[int, int, int, int]:
    lines = source.splitlines(keepends=True)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (IndentationError, tokenize.TokenError) as exc:
        raise ValueError(f"operator_tokenize_failed:{exc}") from exc
    for index, token in enumerate(tokens):
        if token.type != tokenize.NAME or token.string != "def" or token.start[0] != line:
            continue
        for candidate in tokens[index + 1 :]:
            if candidate.start[0] != line:
                break
            if candidate.type != tokenize.NAME:
                continue
            if candidate.string != original_name:
                break
            start_column = len(lines[line - 1][: candidate.start[1]].encode("utf-8"))
            end_column = len(lines[line - 1][: candidate.end[1]].encode("utf-8"))
            if start_column < minimum_column:
                break
            return line, start_column, line, end_column
    raise ValueError("operator_function_name_span_not_found")


def _localized_replacement(
    source: str,
    span: tuple[int, int, int, int],
    replacement: str,
) -> str:
    start_line, start_column, _, _ = span
    newline = "\r\n" if "\r\n" in source else "\n"
    rendered = replacement.replace("\n", newline)
    if newline in rendered:
        source_line = source.splitlines(keepends=True)[start_line - 1].encode("utf-8")
        prefix_bytes = source_line[:start_column]
        if not prefix_bytes.strip():
            continuation_prefix = prefix_bytes.decode("utf-8")
        else:
            continuation_prefix = " " * start_column
        rendered = rendered.replace(newline, newline + continuation_prefix)
    return rendered


def _apply_python_source_edits(
    source: str,
    edits: list[tuple[tuple[int, int, int, int], str]],
) -> str:
    positioned = [(*_python_span_to_offsets(source, span), span, replacement) for span, replacement in edits]
    positioned.sort(key=lambda row: (row[0], row[1]))
    for previous, current in zip(positioned, positioned[1:]):
        if previous[1] > current[0]:
            raise ValueError("operator_source_edits_overlap")
    encoded = source.encode("utf-8")
    for start, end, span, replacement in reversed(positioned):
        localized = _localized_replacement(source, span, replacement).encode("utf-8")
        encoded = encoded[:start] + localized + encoded[end:]
    return encoded.decode("utf-8")


def _utf16_offset_to_python_index(source: str, offset: int) -> int:
    if offset < 0:
        raise ValueError("js_operator_offset_negative")
    units = 0
    for index, character in enumerate(source):
        if units == offset:
            return index
        units += 2 if ord(character) > 0xFFFF else 1
        if units > offset:
            raise ValueError("js_operator_offset_splits_surrogate_pair")
    if units == offset:
        return len(source)
    raise ValueError("js_operator_offset_out_of_range")


def _apply_js_cross_script_source_edits(source: str, candidate: dict[str, Any]) -> str:
    locations = list(candidate.get("locations") or [])
    expected = int(candidate.get("parameter_load_count", len(locations)))
    if len(locations) != expected or expected < 1:
        raise ValueError(
            f"js_operator_replacement_count:{len(locations)}:expected:{expected}"
        )
    replacement = str(candidate["mutated_field"])
    positioned: list[tuple[int, int, str]] = []
    for location in locations:
        start = _utf16_offset_to_python_index(source, int(location["start"]))
        end = _utf16_offset_to_python_index(source, int(location["end"]))
        if not (0 <= start < end <= len(source)):
            raise ValueError("js_operator_target_span_invalid")
        original = source[start:end]
        if original != location["original_fragment"]:
            raise ValueError("js_operator_original_fragment_mismatch")
        rendered = replacement if location["access_style"] == "dot" else json.dumps(replacement)
        positioned.append((start, end, rendered))
    positioned.sort()
    for previous, current in zip(positioned, positioned[1:]):
        if previous[1] > current[0]:
            raise ValueError("js_operator_source_edits_overlap")
    transformed = source
    for start, end, rendered in reversed(positioned):
        transformed = transformed[:start] + rendered + transformed[end:]
    if transformed == source:
        raise ValueError("js_operator_mutation_did_not_change_source")
    return transformed


def apply_operator(source: str, candidate: dict[str, Any]) -> str:
    if candidate.get("source_hash") and sha256_bytes(source.encode("utf-8")) != candidate[
        "source_hash"
    ]:
        raise ValueError("source_hash_mismatch")
    if candidate.get("operator") == "replace_js_cross_script_call_result_field":
        return _apply_js_cross_script_source_edits(source, candidate)
    tree = ast.parse(source, filename="<skillscriptbench-visible-source>")
    transformer = _OperatorTransformer(candidate)
    transformed = transformer.visit(tree)
    ast.fix_missing_locations(transformed)
    expected = int(candidate.get("parameter_load_count", 1))
    if transformer.replaced != expected:
        raise ValueError(
            f"operator_replacement_count:{transformer.replaced}:expected:{expected}"
        )
    source_edits = [
        (span, ast.unparse(replacement))
        for span, replacement in transformer.source_edits
    ]
    source_edits.extend(transformer.source_text_edits)
    source_edits.extend(
        (
            _function_name_span(
                source,
                line=line,
                minimum_column=column,
                original_name=original_name,
            ),
            replacement_name,
        )
        for line, column, original_name, replacement_name in transformer.function_name_edits
    )
    if len(source_edits) != transformer.replaced:
        raise ValueError("operator_replacement_missing_source_edit")
    rendered = _apply_python_source_edits(source, source_edits)
    compile(rendered, "<skillscriptbench-transformed-source>", "exec")
    expected_ast = ast.dump(transformed, include_attributes=False)
    localized_ast = ast.dump(ast.parse(rendered), include_attributes=False)
    if localized_ast != expected_ast:
        raise ValueError("localized_operator_ast_mismatch")
    return rendered


def build_operator_audit(
    expansion_audit: str | Path | dict[str, Any],
    output: str | Path | None = None,
    *,
    formal_only: bool = True,
) -> dict[str, Any]:
    payload = (
        read_json(expansion_audit)
        if isinstance(expansion_audit, (str, Path))
        else expansion_audit
    )
    packages: list[dict[str, Any]] = []
    for row in payload.get("packages", []):
        if formal_only and row.get("license_tier") != "formal_redistributable":
            continue
        package_root = Path(row["source_local_root"]) / row["relative_root"]
        operators = enumerate_python_operators(package_root, row.get("script_files"))
        if not operators:
            continue
        packages.append(
            {
                "package_id": row["package_id"],
                "source_id": row["source_id"],
                "source_commit": row["source_commit"],
                "source_repo_url": row["source_repo_url"],
                "relative_root": row["relative_root"],
                "split_group": row.get("split_group"),
                "license_tier": row.get("license_tier"),
                "package_hashes": row.get("package_hashes", {}),
                "operators": operators,
            }
        )
    all_operators = [operator for package in packages for operator in package["operators"]]
    result = {
        "schema_version": "0.5-package-operator-audit-1",
        "benchmark": "SkillScriptBench",
        "status": "zero_model_static_operator_audit_not_executable_cases",
        "claim_boundary": (
            "An operator candidate is a reversible construction site, not evidence that the mutation "
            "is behaviorally discriminating, valuable, safe, or suitable for a benchmark case."
        ),
        "expansion_audit_hash": payload.get("audit_hash") or canonical_json_hash(payload),
        "formal_only": formal_only,
        "summary": {
            "package_count": len(packages),
            "operator_candidate_count": len(all_operators),
            "source_count": len({package["source_id"] for package in packages}),
            "content_component_count": len(
                {package["split_group"] for package in packages if package.get("split_group")}
            ),
            "operator_counts": dict(
                sorted(Counter(row["operator"] for row in all_operators).items())
            ),
            "dimension_counts": dict(
                sorted(Counter(row["dimension"] for row in all_operators).items())
            ),
            "operator_subfamily_counts": dict(
                sorted(Counter(row["operator_subfamily"] for row in all_operators).items())
            ),
            "operator_template_count": len(
                {row["operator_template_fingerprint"] for row in all_operators}
            ),
            "source_package_counts": dict(
                sorted(Counter(package["source_id"] for package in packages).items())
            ),
        },
        "packages": packages,
    }
    result["operator_audit_hash"] = canonical_json_hash(result)
    if output is not None:
        write_json(output, result)
    return result


def validate_operator_audit(
    operator_audit: str | Path | dict[str, Any],
    expansion_audit: str | Path | dict[str, Any],
    output: str | Path | None = None,
) -> dict[str, Any]:
    operators = (
        read_json(operator_audit)
        if isinstance(operator_audit, (str, Path))
        else operator_audit
    )
    expansion = (
        read_json(expansion_audit)
        if isinstance(expansion_audit, (str, Path))
        else expansion_audit
    )
    roots = {
        row["package_id"]: Path(row["source_local_root"]) / row["relative_root"]
        for row in expansion.get("packages", [])
    }
    records: list[dict[str, Any]] = []
    for package in operators.get("packages", []):
        package_id = package["package_id"]
        package_root = roots.get(package_id)
        for candidate in package.get("operators", []):
            reasons: list[str] = []
            transformed_hash: str | None = None
            source_hash: str | None = None
            if package_root is None:
                reasons.append("package_missing_from_expansion_audit")
            else:
                source_path = package_root / candidate["path"]
                try:
                    source = source_path.read_text(encoding="utf-8")
                    source_hash = sha256_file(source_path)
                    if source_hash != candidate.get("source_hash"):
                        reasons.append("source_hash_mismatch")
                    transformed = apply_operator(source, candidate)
                    if transformed == source:
                        reasons.append("operator_did_not_change_source")
                    transformed_hash = sha256_bytes(transformed.encode("utf-8"))
                except Exception as exc:
                    reasons.append(f"generation_failed:{type(exc).__name__}:{exc}")
            records.append(
                {
                    "package_id": package_id,
                    "operator_candidate_id": candidate["operator_candidate_id"],
                    "operator": candidate["operator"],
                    "operator_subfamily": candidate.get("operator_subfamily"),
                    "operator_template_fingerprint": candidate.get(
                        "operator_template_fingerprint"
                    ),
                    "dimension": candidate["dimension"],
                    "path": candidate["path"],
                    "status": "pass" if not reasons else "fail",
                    "failure_reasons": reasons,
                    "source_hash": source_hash,
                    "transformed_hash": transformed_hash,
                }
            )
    failures = [record for record in records if record["status"] != "pass"]
    result = {
        "schema_version": "0.5-package-operator-generation-check-1",
        "benchmark": "SkillScriptBench",
        "status": "pass" if not failures else "fail",
        "claim_boundary": (
            "Generation success proves only that the declared source was found, changed exactly as "
            "specified, and remained parseable. Behavioral discrimination is a later gate."
        ),
        "operator_audit_hash": operators.get("operator_audit_hash")
        or canonical_json_hash(operators),
        "expansion_audit_hash": expansion.get("audit_hash")
        or canonical_json_hash(expansion),
        "summary": {
            "candidate_count": len(records),
            "pass_count": len(records) - len(failures),
            "fail_count": len(failures),
            "operator_pass_counts": dict(
                sorted(
                    Counter(
                        record["operator"]
                        for record in records
                        if record["status"] == "pass"
                    ).items()
                )
            ),
        },
        "failures": failures,
        "records": records,
    }
    result["validation_hash"] = canonical_json_hash(result)
    if output is not None:
        write_json(output, result)
    return result
