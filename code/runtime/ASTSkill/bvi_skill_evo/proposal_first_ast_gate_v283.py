from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from skillscriptbench.io_utils import canonical_json_hash


SCHEMA_VERSION = "2.83-proposal-first-posthoc-ast-gate-binding-correction-v1"
ACCEPT = "ACCEPT"


def _qualified_symbols(tree: ast.AST) -> dict[str, ast.AST]:
    result: dict[str, ast.AST] = {}

    def visit(node: ast.AST, prefix: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = ".".join((*prefix, child.name))
                result[qualified] = child
                visit(child, (*prefix, child.name))
            else:
                visit(child, prefix)

    visit(tree, ())
    return result


def _syntactic_bindings(node: ast.AST) -> set[str]:
    names = {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Param))
    }
    names.update(child.arg for child in ast.walk(node) if isinstance(child, ast.arg))
    names.update(
        child.name
        for child in ast.walk(node)
        if isinstance(child, ast.ExceptHandler) and isinstance(child.name, str)
    )
    for child in ast.walk(node):
        if isinstance(child, (ast.Import, ast.ImportFrom)):
            names.update(
                alias.asname or alias.name.split(".", 1)[0] for alias in child.names
            )
    return names


def correct_name_binding_gate(
    candidate_package: str | Path,
    gate: dict[str, Any],
) -> dict[str, Any]:
    candidate = Path(candidate_package).resolve()
    remaining: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    for finding in gate.get("unresolved_name_findings", []):
        path = str(finding["path"])
        source = (candidate / path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=path)
        symbol = str(finding["symbol"])
        node = _qualified_symbols(tree).get(symbol)
        bindings = _syntactic_bindings(node) if node is not None else set()
        corrected = sorted(set(finding["names"]) & bindings)
        unresolved = sorted(set(finding["names"]) - bindings)
        if corrected:
            corrections.append(
                {
                    "path": path,
                    "symbol": symbol,
                    "names": corrected,
                    "binding_kinds_checked": [
                        "ast.Store",
                        "ast.arg_including_lambda",
                        "ExceptHandler.name",
                        "local_import_alias",
                    ],
                }
            )
        if unresolved:
            remaining.append({**finding, "names": unresolved})
    result = dict(gate)
    result.pop("gate_hash", None)
    checks = dict(result.get("checks", {}))
    checks["no_new_unresolved_names_in_changed_functions"] = not remaining
    result.update(
        {
            "schema_version": SCHEMA_VERSION,
            "checks": checks,
            "unresolved_name_findings": remaining,
            "binding_corrections": corrections,
            "binding_correction_model_calls": 0,
            "binding_correction_hidden_artifacts_consumed": False,
            "supersedes_gate_hash": gate.get("gate_hash"),
        }
    )
    result["failed_checks"] = [name for name, passed in checks.items() if not passed]
    result["decision"] = ACCEPT if all(checks.values()) else "REVISE"
    result["gate_hash"] = canonical_json_hash(result)
    return result
