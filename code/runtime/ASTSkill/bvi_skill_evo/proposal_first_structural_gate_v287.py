from __future__ import annotations

import difflib
import re
import subprocess
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from bvi_skill_evo.proposal_first_ast_gate_v280 import (
    ACCEPT,
    REVISE,
    build_application_failure_gate,
    build_posthoc_ast_gate,
)
from bvi_skill_evo.proposal_first_ast_gate_v283 import correct_name_binding_gate
from skillscriptbench.io_utils import canonical_json_hash, hash_tree
from skillscriptbench.multilang_structural_v66 import (
    JS_SUFFIXES,
    SHELL_SUFFIXES,
    enumerate_package_nodes,
    extract_javascript_nodes,
    extract_shell_nodes,
)


SCHEMA_VERSION = "2.87-proposal-first-universal-structural-gate-v1"


def _line_ranges(before: str, after: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    old_ranges: list[tuple[int, int]] = []
    new_ranges: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        a=before.splitlines(), b=after.splitlines(), autojunk=False
    ).get_opcodes():
        if tag != "equal":
            old_ranges.append((i1 + 1, max(i1 + 1, i2)))
            new_ranges.append((j1 + 1, max(j1 + 1, j2)))
    return old_ranges, new_ranges


def _overlaps(node: dict[str, Any], ranges: list[tuple[int, int]]) -> bool:
    start = int(node["line"])
    end = int(node["end_line"])
    return any(start <= finish and begin <= end for begin, finish in ranges)


def _changed_nodes(
    nodes: list[dict[str, Any]], paths: dict[str, list[tuple[int, int]]]
) -> list[dict[str, Any]]:
    candidates = [node for node in nodes if _overlaps(node, paths.get(str(node["path"]), []))]
    minimal: list[dict[str, Any]] = []
    for node in candidates:
        start = int(node["byte_span"]["start"])
        end = int(node["byte_span"]["end"])
        if any(
            other is not node
            and other["path"] == node["path"]
            and int(other["byte_span"]["start"]) >= start
            and int(other["byte_span"]["end"]) <= end
            and (
                int(other["byte_span"]["start"]) > start
                or int(other["byte_span"]["end"]) < end
            )
            for other in candidates
        ):
            continue
        minimal.append(node)
    return [
        {
            "path": node["path"],
            "symbol": node["symbol"],
            "node_type": node["node_type"],
            "role": node["role"],
            "line": node["line"],
            "end_line": node["end_line"],
            "node_id": node["node_id"],
            "node_source_sha256": node["node_source_sha256"],
            "source_preview": node["observed_source"][:800],
        }
        for node in sorted(minimal, key=lambda row: (row["path"], row["line"], row["column"]))
    ]


def _syntax(package: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    successes: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    for path in sorted((package / "scripts").rglob("*")) if (package / "scripts").is_dir() else []:
        if not path.is_file() or path.suffix.lower() not in JS_SUFFIXES | SHELL_SUFFIXES:
            continue
        relative = path.relative_to(package).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            if path.suffix.lower() in JS_SUFFIXES:
                extract_javascript_nodes(relative, source)
                backend = "babel_ast_v66"
            else:
                completed = subprocess.run(
                    ["bash", "-n", str(path)], capture_output=True, text=True, timeout=30, check=False
                )
                if completed.returncode != 0:
                    raise SyntaxError(completed.stderr[-600:])
                extract_shell_nodes(relative, source)
                backend = "bash_n_plus_tree_sitter_bash_v66"
            successes.append({"path": relative, "backend": backend})
        except Exception as exc:
            failures.append({"path": relative, "error": f"{type(exc).__name__}:{exc}"})
    return successes, failures


def _declaration_signatures(package: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    scripts = package / "scripts"
    for path in sorted(scripts.rglob("*")) if scripts.is_dir() else []:
        if not path.is_file() or path.suffix.lower() not in JS_SUFFIXES | SHELL_SUFFIXES:
            continue
        source = path.read_text(encoding="utf-8")
        rows: set[str] = set()
        if path.suffix.lower() in JS_SUFFIXES:
            for match in re.finditer(
                r"(?m)^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+"
                r"(?P<name>[A-Za-z_$][\w$]*)\s*\((?P<args>[^)]*)\)",
                source,
            ):
                rows.add(f"function:{match.group('name')}:{re.sub(r'\\s+', '', match.group('args'))}")
            for match in re.finditer(
                r"(?m)^\s*(?:export\s+)?(?:default\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)",
                source,
            ):
                rows.add(f"class:{match.group('name')}")
            for match in re.finditer(
                r"(?m)^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*"
                r"(?:async\s*)?\((?P<args>[^)]*)\)\s*=>",
                source,
            ):
                rows.add(f"arrow:{match.group('name')}:{re.sub(r'\\s+', '', match.group('args'))}")
        else:
            for match in re.finditer(
                r"(?m)^\s*(?:function\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\)\s*\{",
                source,
            ):
                rows.add(f"function:{match.group('name')}")
        result[path.relative_to(package).as_posix()] = sorted(rows)
    return result


def _call_graph(nodes: list[dict[str, Any]]) -> list[dict[str, str]]:
    definitions = {str(node["symbol"]) for node in nodes if node["symbol"] != "<module>"}
    edges: set[tuple[str, str, str]] = set()
    for node in nodes:
        if node.get("node_type") != "CallExpression":
            continue
        facts = node.get("facts") or {}
        callee = str(facts.get("calleeName") or "")
        caller = str(node.get("symbol") or "<module>")
        if callee in definitions and callee != caller:
            edges.add((str(node["path"]), caller, callee))
    return [
        {"path": path, "caller_symbol": caller, "callee_symbol": callee}
        for path, caller, callee in sorted(edges)
    ]


def _impact_closure(changed: set[tuple[str, str]], edges: list[dict[str, str]]) -> dict[str, Any]:
    adjacency: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for edge in edges:
        caller = (edge["path"], edge["caller_symbol"])
        callee = (edge["path"], edge["callee_symbol"])
        adjacency[caller].add(callee)
        adjacency[callee].add(caller)
    closure = set(changed)
    queue = deque((item, 0) for item in changed)
    while queue:
        item, depth = queue.popleft()
        if depth >= 1:
            continue
        for neighbor in adjacency.get(item, set()):
            if neighbor not in closure:
                closure.add(neighbor)
                queue.append((neighbor, depth + 1))
    return {
        "changed_symbols": [
            {"path": path, "symbol": symbol} for path, symbol in sorted(changed)
        ],
        "one_hop_symbols": [
            {"path": path, "symbol": symbol} for path, symbol in sorted(closure - changed)
        ],
        "relevant_edges": [
            edge
            for edge in edges
            if (edge["path"], edge["caller_symbol"]) in closure
            or (edge["path"], edge["callee_symbol"]) in closure
        ],
    }


def _build_multilang_gate(parent: Path, candidate: Path) -> dict[str, Any]:
    parent_hashes = hash_tree(parent)
    candidate_hashes = hash_tree(candidate)
    changed_paths = sorted(
        path
        for path in set(parent_hashes) | set(candidate_hashes)
        if parent_hashes.get(path) != candidate_hashes.get(path)
    )
    old_ranges: dict[str, list[tuple[int, int]]] = {}
    new_ranges: dict[str, list[tuple[int, int]]] = {}
    for path in changed_paths:
        if not (parent / path).is_file() or not (candidate / path).is_file():
            continue
        before, after = _line_ranges(
            (parent / path).read_text(encoding="utf-8"),
            (candidate / path).read_text(encoding="utf-8"),
        )
        old_ranges[path] = before
        new_ranges[path] = after
    parent_nodes = enumerate_package_nodes(parent, include_markdown=False)
    candidate_nodes = enumerate_package_nodes(candidate, include_markdown=False)
    before_nodes = _changed_nodes(parent_nodes, old_ranges)
    after_nodes = _changed_nodes(candidate_nodes, new_ranges)
    changed_symbols = {
        (str(row["path"]), str(row["symbol"]))
        for row in (*before_nodes, *after_nodes)
        if row["symbol"] != "<module>"
    }
    parent_syntax, parent_failures = _syntax(parent)
    candidate_syntax, candidate_failures = _syntax(candidate)
    before_signatures = _declaration_signatures(parent)
    after_signatures = _declaration_signatures(candidate)
    edges_before = _call_graph(parent_nodes)
    edges_after = _call_graph(candidate_nodes)
    checks = {
        "candidate_diff_nonempty": bool(changed_paths),
        "changed_paths_are_supported_scripts": bool(changed_paths)
        and all(
            path.startswith("scripts/")
            and Path(path).suffix.lower() in JS_SUFFIXES | SHELL_SUFFIXES
            for path in changed_paths
        ),
        "changed_path_count_bounded": len(changed_paths) <= 2,
        "parent_syntax_valid": not parent_failures,
        "candidate_syntax_valid": not candidate_failures,
        "public_declaration_signatures_preserved": before_signatures == after_signatures,
        "changed_nodes_resolved": bool(after_nodes),
        "changed_symbol_count_bounded": len(changed_symbols) <= 4,
    }
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "decision": ACCEPT if all(checks.values()) else REVISE,
        "checks": checks,
        "failed_checks": [name for name, passed in checks.items() if not passed],
        "changed_paths": changed_paths,
        "changed_nodes_before": before_nodes,
        "changed_nodes_after": after_nodes,
        "impact_closure": _impact_closure(changed_symbols, edges_before + edges_after),
        "call_edges_added": [edge for edge in edges_after if edge not in edges_before],
        "call_edges_removed": [edge for edge in edges_before if edge not in edges_after],
        "parse_failures": {"parent": parent_failures, "candidate": candidate_failures},
        "syntax_backends": {"parent": parent_syntax, "candidate": candidate_syntax},
        "hidden_artifacts_consumed": False,
        "task_verifier_consumed": False,
        "gold_or_oracle_consumed": False,
        "claim_boundary": (
            "ACCEPT means the diff was resolved to package-local language AST nodes, parsed, stayed "
            "bounded, and preserved extracted declaration signatures. It is not task correctness."
        ),
    }
    result["gate_hash"] = canonical_json_hash(result)
    return result


def build_posthoc_structural_gate(
    parent_package: str | Path,
    candidate_package: str | Path,
    request_text: str,
) -> dict[str, Any]:
    parent = Path(parent_package).resolve()
    candidate = Path(candidate_package).resolve()
    changed = [
        path
        for path in set(hash_tree(parent)) | set(hash_tree(candidate))
        if hash_tree(parent).get(path) != hash_tree(candidate).get(path)
    ]
    suffixes = {Path(path).suffix.lower() for path in changed}
    if suffixes and suffixes <= {".py"}:
        return correct_name_binding_gate(candidate, build_posthoc_ast_gate(parent, candidate, request_text))
    if suffixes and suffixes <= JS_SUFFIXES | SHELL_SUFFIXES:
        return _build_multilang_gate(parent, candidate)
    if not suffixes:
        return _build_multilang_gate(parent, candidate)
    return build_application_failure_gate(f"mixed_or_unsupported_changed_suffixes:{sorted(suffixes)}")

