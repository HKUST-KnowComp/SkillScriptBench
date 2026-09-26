from __future__ import annotations

import ast
import hashlib
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .io_utils import canonical_json_hash, sha256_file


SCHEMA_VERSION = "2.35-python-structural-anomaly-facts-v1"
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
STOPWORDS = {
    "and",
    "for",
    "from",
    "future",
    "into",
    "must",
    "package",
    "preserve",
    "request",
    "same",
    "skill",
    "task",
    "that",
    "the",
    "this",
    "using",
    "with",
}


def _tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in TOKEN_RE.findall(text)
        if token.lower() not in STOPWORDS
    }


def _node_source(source: str, node: ast.AST) -> str:
    return ast.get_source_segment(source, node) or ""


def _node_id(path: str, source: str, node: ast.AST) -> str:
    payload = "\0".join(
        [
            path,
            type(node).__name__,
            str(getattr(node, "lineno", 0)),
            str(getattr(node, "col_offset", 0)),
            str(getattr(node, "end_lineno", 0)),
            str(getattr(node, "end_col_offset", 0)),
            _node_source(source, node),
        ]
    )
    return "py:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parents.get(current)
    return None


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        pieces = [node.func.attr]
        value = node.func.value
        while isinstance(value, ast.Attribute):
            pieces.append(value.attr)
            value = value.value
        if isinstance(value, ast.Name):
            pieces.append(value.id)
        return ".".join(reversed(pieces))
    return ""


def _name_text(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _context(source: str, node: ast.AST, radius: int = 3) -> str:
    lines = source.splitlines()
    start = max(1, int(getattr(node, "lineno", 1)) - radius)
    end = min(len(lines), int(getattr(node, "end_lineno", start)) + radius)
    return "\n".join(f"{index}: {lines[index - 1]}" for index in range(start, end + 1))


def _finding(
    *,
    path: str,
    source: str,
    node: ast.AST,
    symbol: str,
    finding_type: str,
    base_confidence: float,
    explanation: str,
    evidence: dict[str, Any],
    request_tokens: set[str],
) -> dict[str, Any]:
    source_text = _node_source(source, node)
    lexical = _tokens(" ".join([path, symbol, source_text, explanation]))
    overlap = sorted(request_tokens & lexical)
    confidence = min(0.99, base_confidence + min(0.09, 0.015 * len(overlap)))
    return {
        "finding_type": finding_type,
        "confidence": round(confidence, 3),
        "path": path,
        "symbol": symbol,
        "node_type": type(node).__name__,
        "node_id": _node_id(path, source, node),
        "node_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "line": int(getattr(node, "lineno", 0)),
        "column": int(getattr(node, "col_offset", 0)),
        "end_line": int(getattr(node, "end_lineno", 0)),
        "end_column": int(getattr(node, "end_col_offset", 0)),
        "observed_source": source_text,
        "local_context": _context(source, node),
        "request_token_overlap": overlap,
        "explanation": explanation,
        "evidence": evidence,
    }


def _function_name(node: ast.AST | None) -> str:
    return str(getattr(node, "name", "<module>"))


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    result: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            result[child] = parent
    return result


def _imported_names(tree: ast.AST) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                result[alias.asname or alias.name] = f"{module}.{alias.name}".strip(".")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                result[alias.asname or alias.name.split(".")[0]] = alias.name
    return result


def _name_uses(function: ast.AST) -> Counter[str]:
    return Counter(
        node.id
        for node in ast.walk(function)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    )


def _aggregate_names(function: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"append", "extend", "add", "update"}:
            continue
        if isinstance(node.func.value, ast.Name):
            names.add(node.func.value.id)
    return names


def _returned_names(node: ast.Return) -> set[str]:
    if not isinstance(node.value, ast.Dict):
        return set()
    names: set[str] = set()
    for value in node.value.values:
        for child in ast.walk(value):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                names.add(child.id)
    return names


def _helper_candidates(package_functions: dict[str, list[dict[str, str]]]) -> list[dict[str, str]]:
    rows = []
    for name, definitions in package_functions.items():
        lowered = name.lower()
        if any(token in lowered for token in ("iter", "coerce", "normal", "sort", "valid")):
            rows.extend(definitions)
    return rows


def build_python_structural_anomaly_facts(
    package_root: str | Path,
    request: str,
    *,
    maximum_findings: int = 20,
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    request_tokens = _tokens(request)
    parsed: list[tuple[str, Path, str, ast.Module, dict[ast.AST, ast.AST]]] = []
    package_functions: dict[str, list[dict[str, str]]] = defaultdict(list)
    parse_failures: list[dict[str, str]] = []
    for file in sorted(package.rglob("*.py")):
        relative = file.relative_to(package)
        if "tests" in relative.parts or file.name.startswith("test_"):
            continue
        path = relative.as_posix()
        source = file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            parse_failures.append({"path": path, "error": f"SyntaxError:{exc}"})
            continue
        parents = _parents(tree)
        parsed.append((path, file, source, tree, parents))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                package_functions[node.name].append({"path": path, "symbol": node.name})

    findings: list[dict[str, Any]] = []
    call_edges: list[dict[str, Any]] = []
    def_use: list[dict[str, Any]] = []
    helpers = _helper_candidates(package_functions)
    helper_names = {row["symbol"] for row in helpers}
    for path, file, source, tree, parents in parsed:
        imported = _imported_names(tree)
        for node in ast.walk(tree):
            function = _enclosing_function(node, parents)
            symbol = _function_name(function)
            if isinstance(node, ast.Call):
                callee = _call_name(node)
                short = callee.rsplit(".", 1)[-1]
                if short in package_functions:
                    for definition in package_functions[short]:
                        call_edges.append(
                            {
                                "caller_path": path,
                                "caller_symbol": symbol,
                                "callee_path": definition["path"],
                                "callee_symbol": short,
                                "line": int(node.lineno),
                                "call_source": _node_source(source, node),
                            }
                        )

            if isinstance(node, ast.Pass):
                parent = parents.get(node)
                findings.append(
                    _finding(
                        path=path,
                        source=source,
                        node=node,
                        symbol=symbol,
                        finding_type="effectless_placeholder",
                        base_confidence=0.9,
                        explanation=(
                            "An activated code block contains only an effectless pass; inspect whether the "
                            "surrounding contract requires validation, persistence, or setup here."
                        ),
                        evidence={"parent_node_type": type(parent).__name__ if parent else None},
                        request_tokens=request_tokens,
                    )
                )

            if isinstance(node, ast.Raise) and node.exc is None:
                handler = parents.get(node)
                if isinstance(handler, ast.ExceptHandler) and handler.body == [node]:
                    findings.append(
                        _finding(
                            path=path,
                            source=source,
                            node=node,
                            symbol=symbol,
                            finding_type="bare_reraise_only_recovery",
                            base_confidence=0.68,
                            explanation=(
                                "The exception handler only re-raises, so it provides no package-level "
                                "reporting, fallback, or termination policy."
                            ),
                            evidence={"handler_body_statement_count": 1},
                            request_tokens=request_tokens,
                        )
                    )

            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                if value is not None and len(targets) == 1:
                    target_text = _name_text(targets[0])
                    value_text = _name_text(value)
                    if target_text and target_text == value_text:
                        imported_matches = sorted(
                            name
                            for name in imported
                            if name in helper_names or name in source
                        )
                        findings.append(
                            _finding(
                                path=path,
                                source=source,
                                node=node,
                                symbol=symbol,
                                finding_type="identity_assignment_bypasses_transform",
                                base_confidence=0.94,
                                explanation=(
                                    "A value is assigned to itself at a transformation point; package-local "
                                    "normalization helpers may be bypassed."
                                ),
                                evidence={
                                    "target": target_text,
                                    "value": value_text,
                                    "imported_helper_candidates": imported_matches[:8],
                                },
                                request_tokens=request_tokens,
                            )
                        )
                        def_use.append(
                            {
                                "path": path,
                                "symbol": symbol,
                                "line": int(node.lineno),
                                "sink": target_text,
                                "observed_origin": value_text,
                                "relation": "identity_flow",
                            }
                        )

            if isinstance(node, ast.Call):
                callee = _call_name(node)
                if callee == "sorted" or callee.endswith(".sort"):
                    has_key = any(keyword.arg == "key" for keyword in node.keywords)
                    if not has_key:
                        findings.append(
                            _finding(
                                path=path,
                                source=source,
                                node=node,
                                symbol=symbol,
                                finding_type="ordering_without_explicit_key",
                                base_confidence=0.78,
                                explanation=(
                                    "A collection is ordered without an explicit key even though its elements "
                                    "may be records or domain objects with a documented ordering field."
                                ),
                                evidence={"callee": callee, "keyword_names": [k.arg for k in node.keywords]},
                                request_tokens=request_tokens,
                            )
                        )

            if isinstance(node, ast.For) and isinstance(node.target, (ast.Tuple, ast.List)):
                target_count = len(node.target.elts)
                if target_count >= 2 and isinstance(node.iter, ast.Name):
                    related = sorted(
                        row["symbol"]
                        for row in helpers
                        if "iter" in row["symbol"].lower() or "node" in row["symbol"].lower()
                    )
                    if related:
                        findings.append(
                            _finding(
                                path=path,
                                source=source,
                                node=node.iter,
                                symbol=symbol,
                                finding_type="direct_container_iteration_with_helper_available",
                                base_confidence=0.82,
                                explanation=(
                                    "A multi-value loop iterates a container directly while the package exposes "
                                    "iteration helpers that may define the required item protocol."
                                ),
                                evidence={
                                    "iterated_name": node.iter.id,
                                    "unpacked_target_count": target_count,
                                    "package_helper_candidates": related[:8],
                                },
                                request_tokens=request_tokens,
                            )
                        )

            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict) and function:
                aggregates = _aggregate_names(function)
                returned = _returned_names(node)
                omitted = sorted(aggregates - returned)
                for name in omitted:
                    findings.append(
                        _finding(
                            path=path,
                            source=source,
                            node=node,
                            symbol=symbol,
                            finding_type="accumulated_value_absent_from_return_mapping",
                            base_confidence=0.73,
                            explanation=(
                                "A function accumulates a named collection but its final returned mapping does "
                                "not expose that collection; inspect the documented output schema."
                            ),
                            evidence={
                                "accumulated_name": name,
                                "returned_names": sorted(returned),
                                "name_load_count": _name_uses(function).get(name, 0),
                            },
                            request_tokens=request_tokens,
                        )
                    )
                    def_use.append(
                        {
                            "path": path,
                            "symbol": symbol,
                            "line": int(node.lineno),
                            "source": name,
                            "sink": "returned_mapping",
                            "relation": "accumulated_but_not_returned",
                        }
                    )

    findings.sort(
        key=lambda row: (
            -float(row["confidence"]),
            -len(row["request_token_overlap"]),
            row["path"],
            int(row["line"]),
            row["finding_type"],
        )
    )
    selected = findings[:maximum_findings]
    for rank, row in enumerate(selected, start=1):
        row["rank"] = rank
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete" if parsed else "abstain",
        "method": (
            "request-conditioned generic Python AST anomaly scan over the visible package; no tests, "
            "verdict, expected output, mutation label, gold source, or oracle is consumed"
        ),
        "request_sha256": hashlib.sha256(request.encode("utf-8")).hexdigest(),
        "package_file_count": len(parsed),
        "parse_failures": parse_failures,
        "finding_count_before_limit": len(findings),
        "finding_count": len(selected),
        "findings": selected,
        "editable_nodes": [
            {
                key: row[key]
                for key in (
                    "rank",
                    "path",
                    "symbol",
                    "node_type",
                    "node_id",
                    "node_sha256",
                    "line",
                    "column",
                    "end_line",
                    "end_column",
                    "observed_source",
                )
            }
            for row in selected
        ],
        "package_call_edges": sorted(
            call_edges,
            key=lambda row: (
                row["caller_path"], row["caller_symbol"], row["line"], row["callee_symbol"]
            ),
        ),
        "def_use_findings": sorted(
            def_use, key=lambda row: (row["path"], row["symbol"], row["line"], row["relation"])
        ),
        "confidence_policy": (
            "Confidence ranks structural suspicion and request relevance only. It is not a correctness "
            "probability; consumers may abstain when no finding justifies a bounded edit."
        ),
        "hidden_artifacts_consumed": False,
    }
    payload["facts_hash"] = canonical_json_hash(payload)
    return payload
