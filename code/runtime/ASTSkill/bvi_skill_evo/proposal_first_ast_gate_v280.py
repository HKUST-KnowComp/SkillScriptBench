from __future__ import annotations

import ast
import builtins
import difflib
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from bvi_skill_evo.python_span_patch_v237 import _api_signatures
from skillscriptbench.io_utils import canonical_json_hash, hash_tree
from skillscriptbench.python_structural_anomaly_facts_v254 import (
    build_compact_python_structural_anomaly_facts,
)


SCHEMA_VERSION = "2.80-proposal-first-posthoc-ast-gate-v1"
ACCEPT = "ACCEPT"
REVISE = "REVISE"


def _parse_package(package: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    parsed: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, str]] = []
    for file in sorted(package.rglob("*.py")):
        relative = file.relative_to(package)
        if "tests" in relative.parts or file.name.startswith("test_"):
            continue
        path = relative.as_posix()
        source = file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            failures.append({"path": path, "error": f"SyntaxError:{exc}"})
            continue
        parsed[path] = {"source": source, "tree": tree}
    return parsed, failures


def _qualified_symbols(tree: ast.AST) -> dict[ast.AST, str]:
    result: dict[ast.AST, str] = {}

    def visit(node: ast.AST, prefix: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = ".".join((*prefix, child.name))
                result[child] = qualified
                visit(child, (*prefix, child.name))
            else:
                visit(child, prefix)

    visit(tree, ())
    return result


def _declarations(parsed: dict[str, dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for path, payload in parsed.items():
        source = payload["source"]
        for node, qualified in _qualified_symbols(payload["tree"]).items():
            row: dict[str, Any] = {
                "path": path,
                "symbol": qualified,
                "node_type": type(node).__name__,
                "line": int(node.lineno),
                "end_line": int(getattr(node, "end_lineno", node.lineno)),
                "source_sha256": canonical_json_hash(ast.get_source_segment(source, node) or ""),
            }
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                row["signature"] = ast.dump(node.args, include_attributes=False)
            rows[(path, qualified)] = row
    return rows


def _line_ranges(before: str, after: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    old_lines = before.splitlines()
    new_lines = after.splitlines()
    old_ranges: list[tuple[int, int]] = []
    new_ranges: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        a=old_lines, b=new_lines, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        old_ranges.append((i1 + 1, max(i1 + 1, i2)))
        new_ranges.append((j1 + 1, max(j1 + 1, j2)))
    return old_ranges, new_ranges


def _overlaps(node: ast.AST, ranges: list[tuple[int, int]]) -> bool:
    start = int(getattr(node, "lineno", 0))
    end = int(getattr(node, "end_lineno", start))
    return any(start <= finish and begin <= end for begin, finish in ranges)


def _nearest_symbol(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
    qualified: dict[ast.AST, str],
) -> str:
    current: ast.AST | None = node
    while current is not None:
        if current in qualified:
            return qualified[current]
        current = parents.get(current)
    return "<module>"


def _changed_nodes(
    path: str,
    source: str,
    tree: ast.AST,
    ranges: list[tuple[int, int]],
) -> list[dict[str, Any]]:
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    qualified = _qualified_symbols(tree)
    candidates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt) and hasattr(node, "lineno") and _overlaps(node, ranges)
    ]
    minimal: list[ast.AST] = []
    for node in candidates:
        if any(
            other is not node
            and int(other.lineno) >= int(node.lineno)
            and int(getattr(other, "end_lineno", other.lineno))
            <= int(getattr(node, "end_lineno", node.lineno))
            for other in candidates
        ):
            continue
        minimal.append(node)
    rows: list[dict[str, Any]] = []
    for node in sorted(minimal, key=lambda item: (int(item.lineno), int(item.col_offset))):
        observed = ast.get_source_segment(source, node) or ""
        rows.append(
            {
                "path": path,
                "symbol": _nearest_symbol(node, parents, qualified),
                "node_type": type(node).__name__,
                "line": int(node.lineno),
                "column": int(node.col_offset),
                "end_line": int(getattr(node, "end_lineno", node.lineno)),
                "end_column": int(getattr(node, "end_col_offset", node.col_offset)),
                "node_sha256": canonical_json_hash(ast.dump(node, include_attributes=False)),
                "source_sha256": canonical_json_hash(observed),
                "source_preview": observed[:800],
            }
        )
    return rows


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _call_graph(parsed: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    definitions: dict[str, list[tuple[str, str]]] = defaultdict(list)
    symbol_maps: dict[str, dict[ast.AST, str]] = {}
    for path, payload in parsed.items():
        symbols = _qualified_symbols(payload["tree"])
        symbol_maps[path] = symbols
        for node, qualified in symbols.items():
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                definitions[node.name].append((path, qualified))
    edges: set[tuple[str, str, str, str]] = set()
    for path, payload in parsed.items():
        tree = payload["tree"]
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name not in definitions:
                continue
            caller = _nearest_symbol(node, parents, symbol_maps[path])
            for callee_path, callee in definitions[name]:
                edges.add((path, caller, callee_path, callee))
    return [
        {
            "caller_path": caller_path,
            "caller_symbol": caller,
            "callee_path": callee_path,
            "callee_symbol": callee,
        }
        for caller_path, caller, callee_path, callee in sorted(edges)
    ]


def _impact_closure(
    changed: set[tuple[str, str]], edges: list[dict[str, Any]]
) -> dict[str, Any]:
    adjacency: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for edge in edges:
        caller = (str(edge["caller_path"]), str(edge["caller_symbol"]))
        callee = (str(edge["callee_path"]), str(edge["callee_symbol"]))
        adjacency[caller].add(callee)
        adjacency[callee].add(caller)
    closure = set(changed)
    frontier = deque((node, 0) for node in changed)
    while frontier:
        node, depth = frontier.popleft()
        if depth >= 1:
            continue
        for neighbor in adjacency.get(node, set()):
            if neighbor not in closure:
                closure.add(neighbor)
                frontier.append((neighbor, depth + 1))
    components = 0
    remaining = set(changed)
    while remaining:
        components += 1
        queue = [remaining.pop()]
        while queue:
            current = queue.pop()
            for neighbor in adjacency.get(current, set()):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    queue.append(neighbor)
    return {
        "changed_symbols": [
            {"path": path, "symbol": symbol} for path, symbol in sorted(changed)
        ],
        "one_hop_symbols": [
            {"path": path, "symbol": symbol} for path, symbol in sorted(closure - changed)
        ],
        "changed_component_count": components,
        "relevant_edges": [
            edge
            for edge in edges
            if (str(edge["caller_path"]), str(edge["caller_symbol"])) in closure
            or (str(edge["callee_path"]), str(edge["callee_symbol"])) in closure
        ],
    }


def _undefined_names(parsed: dict[str, dict[str, Any]], changed: set[tuple[str, str]]) -> list[dict[str, Any]]:
    builtins_set = set(dir(builtins))
    findings: list[dict[str, Any]] = []
    for path, payload in parsed.items():
        tree = payload["tree"]
        module_names = {
            alias.asname or alias.name.split(".", 1)[0]
            for node in tree.body
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        module_names.update(
            alias.asname or alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        )
        module_names.update(
            target.id
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if isinstance(target, ast.Name)
        )
        module_names.update(
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
        for node, qualified in _qualified_symbols(tree).items():
            if (path, qualified) not in changed or not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            local = {arg.arg for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
            if node.args.vararg:
                local.add(node.args.vararg.arg)
            if node.args.kwarg:
                local.add(node.args.kwarg.arg)
            for child in ast.walk(node):
                if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Param)):
                    local.add(child.id)
            unresolved = sorted(
                {
                    child.id
                    for child in ast.walk(node)
                    if isinstance(child, ast.Name)
                    and isinstance(child.ctx, ast.Load)
                    and child.id not in local
                    and child.id not in module_names
                    and child.id not in builtins_set
                }
            )
            if unresolved:
                findings.append({"path": path, "symbol": qualified, "names": unresolved})
    return findings


def build_posthoc_ast_gate(
    parent_package: str | Path,
    candidate_package: str | Path,
    request_text: str,
) -> dict[str, Any]:
    parent = Path(parent_package).resolve()
    candidate = Path(candidate_package).resolve()
    parent_hashes = hash_tree(parent)
    candidate_hashes = hash_tree(candidate)
    changed_paths = sorted(
        path
        for path in set(parent_hashes) | set(candidate_hashes)
        if parent_hashes.get(path) != candidate_hashes.get(path)
    )
    parent_parsed, parent_failures = _parse_package(parent)
    candidate_parsed, candidate_failures = _parse_package(candidate)
    before_declarations = _declarations(parent_parsed)
    after_declarations = _declarations(candidate_parsed)
    changed_nodes_before: list[dict[str, Any]] = []
    changed_nodes_after: list[dict[str, Any]] = []
    for path in changed_paths:
        if path not in parent_parsed or path not in candidate_parsed:
            continue
        old_ranges, new_ranges = _line_ranges(
            parent_parsed[path]["source"], candidate_parsed[path]["source"]
        )
        changed_nodes_before.extend(
            _changed_nodes(
                path,
                parent_parsed[path]["source"],
                parent_parsed[path]["tree"],
                old_ranges,
            )
        )
        changed_nodes_after.extend(
            _changed_nodes(
                path,
                candidate_parsed[path]["source"],
                candidate_parsed[path]["tree"],
                new_ranges,
            )
        )
    changed_symbols = {
        (str(row["path"]), str(row["symbol"]))
        for row in (*changed_nodes_before, *changed_nodes_after)
        if row["symbol"] != "<module>"
    }
    call_edges_before = _call_graph(parent_parsed)
    call_edges_after = _call_graph(candidate_parsed)
    closure = _impact_closure(changed_symbols, call_edges_before + call_edges_after)
    removed_declarations = [
        before_declarations[key]
        for key in sorted(set(before_declarations) - set(after_declarations))
    ]
    parent_undefined = {
        (row["path"], row["symbol"]): set(row["names"])
        for row in _undefined_names(parent_parsed, changed_symbols)
    }
    undefined = []
    for row in _undefined_names(candidate_parsed, changed_symbols):
        new_names = sorted(
            set(row["names"]) - parent_undefined.get((row["path"], row["symbol"]), set())
        )
        if new_names:
            undefined.append({**row, "names": new_names})
    api_preserved = not parent_failures and not candidate_failures and _api_signatures(parent) == _api_signatures(candidate)
    checks = {
        "candidate_diff_nonempty": bool(changed_paths),
        "changed_paths_are_python_scripts": bool(changed_paths)
        and all(path.endswith(".py") and "scripts/" in path for path in changed_paths),
        "changed_path_count_bounded": len(changed_paths) <= 2,
        "parent_python_parse": not parent_failures,
        "candidate_python_parse": not candidate_failures,
        "public_api_signatures_preserved": api_preserved,
        "changed_nodes_resolved": bool(changed_nodes_after),
        "declarations_not_removed": not removed_declarations,
        "changed_symbol_count_bounded": len(changed_symbols) <= 4,
        "no_new_unresolved_names_in_changed_functions": not undefined,
    }
    hard_checks = tuple(checks)
    decision = ACCEPT if all(checks[name] for name in hard_checks) else REVISE
    residual = (
        build_compact_python_structural_anomaly_facts(
            candidate, request_text, maximum_findings=12, maximum_local_edges=48, maximum_def_use=24
        )
        if not candidate_failures
        else None
    )
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "decision": decision,
        "checks": checks,
        "failed_checks": [name for name in hard_checks if not checks[name]],
        "changed_paths": changed_paths,
        "changed_nodes_before": changed_nodes_before,
        "changed_nodes_after": changed_nodes_after,
        "impact_closure": closure,
        "call_edges_added": [edge for edge in call_edges_after if edge not in call_edges_before],
        "call_edges_removed": [edge for edge in call_edges_before if edge not in call_edges_after],
        "removed_declarations": removed_declarations,
        "unresolved_name_findings": undefined,
        "parse_failures": {"parent": parent_failures, "candidate": candidate_failures},
        "candidate_residual_structural_facts": residual,
        "hidden_artifacts_consumed": False,
        "task_verifier_consumed": False,
        "gold_or_oracle_consumed": False,
        "claim_boundary": (
            "ACCEPT means the proposed patch was mapped to concrete AST nodes, stayed inside a bounded "
            "package-local change, preserved public signatures and declarations, and introduced no "
            "statically unresolved names. It is not task correctness."
        ),
    }
    result["gate_hash"] = canonical_json_hash(result)
    return result


def build_application_failure_gate(error: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "decision": REVISE,
        "checks": {"candidate_materialized": False},
        "failed_checks": ["candidate_materialized"],
        "application_error": error,
        "hidden_artifacts_consumed": False,
        "task_verifier_consumed": False,
        "gold_or_oracle_consumed": False,
        "claim_boundary": "The patch protocol or structural materialization failed; no semantic verdict was used.",
    }
    result["gate_hash"] = canonical_json_hash(result)
    return result
