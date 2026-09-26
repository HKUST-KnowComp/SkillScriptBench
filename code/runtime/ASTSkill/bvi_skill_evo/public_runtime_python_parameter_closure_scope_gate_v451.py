from __future__ import annotations

import ast
import builtins
import copy
from pathlib import Path
from typing import Any, Iterable

from bvi_skill_evo import public_runtime_python_parameter_closure_v438 as prior


SCHEMA_VERSION = "4.51-public-runtime-python-parameter-closure-scope-gate-v1"
METHOD_ID = "public_runtime_python_parameter_flow_closure_scope_gate_v1"
ACCEPT_STRUCTURALLY = "ACCEPT_STRUCTURALLY"


def _contains(node: ast.AST, line: int, column: int) -> bool:
    start = (int(getattr(node, "lineno", 0)), int(getattr(node, "col_offset", 0)))
    end = (
        int(getattr(node, "end_lineno", getattr(node, "lineno", 0))),
        int(getattr(node, "end_col_offset", getattr(node, "col_offset", 0))),
    )
    return start <= (line, column) <= end


def _node_at(tree: ast.AST, line: int, column: int) -> ast.AST | None:
    exact = [
        node
        for node in ast.walk(tree)
        if int(getattr(node, "lineno", -1)) == line
        and int(getattr(node, "col_offset", -1)) == column
    ]
    exact.sort(
        key=lambda node: (
            int(getattr(node, "end_lineno", line)) - line,
            int(getattr(node, "end_col_offset", column)) - column,
        )
    )
    return exact[0] if exact else None


def _enclosing_function(tree: ast.AST, line: int, column: int) -> ast.AST | None:
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        and _contains(node, line, column)
    ]
    functions.sort(
        key=lambda node: (
            int(getattr(node, "end_lineno", line)) - int(getattr(node, "lineno", line)),
            int(getattr(node, "end_col_offset", column))
            - int(getattr(node, "col_offset", column)),
        )
    )
    return functions[0] if functions else None


def _argument_names(function: ast.AST | None) -> set[str]:
    if function is None:
        return set()
    arguments = function.args
    return {
        argument.arg
        for argument in (
            list(arguments.posonlyargs)
            + list(arguments.args)
            + list(arguments.kwonlyargs)
            + ([arguments.vararg] if arguments.vararg else [])
            + ([arguments.kwarg] if arguments.kwarg else [])
        )
    }


def _bound_names(nodes: Iterable[ast.AST], *, before_line: int | None = None) -> set[str]:
    names: set[str] = set()
    for root in nodes:
        for node in ast.walk(root):
            if before_line is not None and int(getattr(node, "lineno", 0)) >= before_line:
                continue
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Param)):
                names.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Import):
                names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.update(alias.asname or alias.name for alias in node.names)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                names.add(str(node.name))
    return names


def _module_bindings(tree: ast.Module) -> set[str]:
    return _bound_names(tree.body)


def _load_names(node: ast.AST) -> set[str]:
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
    }


def replacement_scope_findings(
    parent_package: str | Path,
    candidate_package: str | Path,
    frozen_facts: dict[str, Any],
) -> list[dict[str, Any]]:
    parent = Path(parent_package).resolve()
    candidate = Path(candidate_package).resolve()
    findings: list[dict[str, Any]] = []
    builtin_names = set(dir(builtins))
    for editable in frozen_facts.get("editable_nodes") or []:
        relative = str(editable["path"])
        line = int(editable["line"])
        column = int(editable["column"])
        parent_tree = ast.parse((parent / relative).read_text(encoding="utf-8"))
        candidate_tree = ast.parse((candidate / relative).read_text(encoding="utf-8"))
        parent_node = _node_at(parent_tree, line, column)
        candidate_node = _node_at(candidate_tree, line, column)
        candidate_function = _enclosing_function(candidate_tree, line, column)
        if parent_node is None or candidate_node is None:
            findings.append(
                {
                    "path": relative,
                    "node_id": editable.get("node_id"),
                    "kind": "replacement_scope_analysis_unavailable",
                    "unresolved_names": [],
                }
            )
            continue
        available = builtin_names | _module_bindings(candidate_tree)
        available |= _argument_names(candidate_function)
        if candidate_function is not None:
            body = getattr(candidate_function, "body", [])
            if isinstance(body, list):
                available |= _bound_names(body, before_line=line)
        new_loads = _load_names(candidate_node) - _load_names(parent_node)
        unresolved = sorted(new_loads - available)
        if unresolved:
            findings.append(
                {
                    "path": relative,
                    "node_id": editable.get("node_id"),
                    "kind": "replacement_introduces_unresolved_free_name",
                    "unresolved_names": unresolved,
                    "enclosing_symbol": getattr(candidate_function, "name", None),
                }
            )
    return findings


def analyze_candidate_closure(
    parent_package: str | Path,
    candidate_package: str | Path,
    request_text: str,
    *,
    frozen_facts: dict[str, Any],
) -> dict[str, Any]:
    result = prior.analyze_candidate_closure(
        parent_package,
        candidate_package,
        request_text,
        frozen_facts=frozen_facts,
    )
    scope_findings = replacement_scope_findings(
        parent_package, candidate_package, frozen_facts
    )
    result = copy.deepcopy(result)
    result["schema_version"] = SCHEMA_VERSION
    result["method"] = METHOD_ID
    result["checks"]["replacement_free_names_resolve_in_lexical_scope"] = not scope_findings
    result["replacement_scope_findings"] = scope_findings
    result["decision"] = (
        ACCEPT_STRUCTURALLY if all(result["checks"].values()) else "REVISE"
    )
    result["claim_boundary"] = (
        "Acceptance establishes bounded AST edits, API preservation, removal of visible "
        "parameter-flow disconnects, and lexical resolvability of introduced names only."
    )
    return result


def apply_public_python_closure_patch(
    content: str,
    source_package: str | Path,
    candidate_package: str | Path,
    *,
    frozen_facts: dict[str, Any],
) -> dict[str, Any]:
    result = prior.apply_public_python_closure_patch(
        content,
        source_package,
        candidate_package,
        frozen_facts=frozen_facts,
    )
    synthetic_request = "\n".join(
        [
            "Repair every listed public helper contract in this skill package:",
            "",
            *[
                f"- `{row['function_symbol']}`: its optional `{row['parameter']}` parameter "
                "does not reach the documented reusable behavior."
                for row in frozen_facts.get("obligations") or []
            ],
        ]
    )
    gate = analyze_candidate_closure(
        source_package,
        candidate_package,
        synthetic_request,
        frozen_facts=frozen_facts,
    )
    result["parameter_flow_closure_gate"] = gate
    result["structural_gate"]["decision"] = gate["decision"]
    result["structural_gate"]["checks"].update(gate["checks"])
    return result


build_public_python_parameter_closure = prior.build_public_python_parameter_closure
build_same_package_wrong_closure = prior.build_same_package_wrong_closure
validate_public_python_parameter_closure = prior.validate_public_python_parameter_closure
PYTHON_CLOSURE_PATCH_TOOL = prior.PYTHON_CLOSURE_PATCH_TOOL


__all__ = [
    "PYTHON_CLOSURE_PATCH_TOOL",
    "analyze_candidate_closure",
    "apply_public_python_closure_patch",
    "build_public_python_parameter_closure",
    "build_same_package_wrong_closure",
    "replacement_scope_findings",
    "validate_public_python_parameter_closure",
]
