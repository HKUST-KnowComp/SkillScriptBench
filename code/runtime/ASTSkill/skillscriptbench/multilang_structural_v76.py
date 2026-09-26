from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Iterable

from skillscriptbench.multilang_structural_v66 import (
    JS_SUFFIXES,
    SCRIPT_SUFFIXES,
    SHELL_SUFFIXES,
    _dedupe_nodes,
    _node,
    _tokens,
    extract_markdown_nodes,
    extract_python_nodes as extract_python_nodes_v66,
    extract_shell_nodes,
    rank_script_nodes as rank_script_nodes_v66,
    select_editable_nodes as select_editable_nodes_v66,
)
from skillscriptbench.multilang_structural_v74 import extract_javascript_nodes


SCHEMA_VERSION = "0.76-multilang-python-property-node-v1"


def _ast_byte_offset(source: str, line: int, byte_column: int) -> int:
    lines = source.splitlines(keepends=True)
    if not 1 <= line <= len(lines):
        raise ValueError("ast_line_outside_source")
    return sum(len(value.encode("utf-8")) for value in lines[: line - 1]) + byte_column


def extract_python_property_nodes(path: str, source: str) -> list[dict[str, Any]]:
    tree = ast.parse(source, filename=path)
    rows: list[dict[str, Any]] = []
    stack: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def _literal(self, node: ast.Constant, access_style: str) -> None:
            if not isinstance(node.value, str) or not hasattr(node, "end_lineno"):
                return
            start = _ast_byte_offset(source, node.lineno, node.col_offset)
            end = _ast_byte_offset(source, int(node.end_lineno), int(node.end_col_offset))
            rows.append(
                _node(
                    path=path,
                    language="python",
                    backend="python_ast_v76",
                    node_type="Constant",
                    role="python_input_property",
                    symbol=stack[-1] if stack else "<module>",
                    source=source,
                    start_byte=start,
                    end_byte=end,
                    facts={
                        "propertyName": node.value,
                        "accessStyle": access_style,
                        "literalStyle": "string",
                    },
                )
            )

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if hasattr(node, "end_lineno"):
                end = _ast_byte_offset(
                    source, int(node.end_lineno), int(node.end_col_offset)
                )
                encoded = node.attr.encode("utf-8")
                start = end - len(encoded)
                if source.encode("utf-8")[start:end] == encoded:
                    rows.append(
                        _node(
                            path=path,
                            language="python",
                            backend="python_ast_v76",
                            node_type="Attribute",
                            role="python_input_property",
                            symbol=stack[-1] if stack else "<module>",
                            source=source,
                            start_byte=start,
                            end_byte=end,
                            facts={
                                "propertyName": node.attr,
                                "accessStyle": "attribute",
                                "literalStyle": "identifier",
                            },
                        )
                    )
            self.generic_visit(node)

        def visit_Subscript(self, node: ast.Subscript) -> None:
            if isinstance(node.slice, ast.Constant):
                self._literal(node.slice, "subscript")
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                self._literal(node.args[0], "get")
            self.generic_visit(node)

    Visitor().visit(tree)
    return _dedupe_nodes(rows)


def extract_python_nodes(path: str, source: str) -> list[dict[str, Any]]:
    try:
        legacy = extract_python_nodes_v66(path, source)
    except (AttributeError, SyntaxError, ValueError):
        legacy = []
    return _dedupe_nodes(
        [
            *legacy,
            *extract_python_property_nodes(path, source),
        ]
    )


def enumerate_package_nodes(
    package: str | Path, *, include_markdown: bool = True
) -> list[dict[str, Any]]:
    root = Path(package)
    rows: list[dict[str, Any]] = []
    scripts = root / "scripts"
    for file in sorted(scripts.rglob("*")) if scripts.is_dir() else []:
        if not file.is_file() or file.suffix.lower() not in SCRIPT_SUFFIXES:
            continue
        relative = file.relative_to(root).as_posix()
        try:
            source = file.read_text(encoding="utf-8")
            suffix = file.suffix.lower()
            if suffix == ".py":
                rows.extend(extract_python_nodes(relative, source))
            elif suffix in JS_SUFFIXES:
                rows.extend(extract_javascript_nodes(relative, source))
            elif suffix in SHELL_SUFFIXES:
                rows.extend(extract_shell_nodes(relative, source))
        except (UnicodeDecodeError, SyntaxError, ValueError):
            continue
    if include_markdown and (root / "SKILL.md").is_file():
        rows.extend(
            extract_markdown_nodes(
                "SKILL.md", (root / "SKILL.md").read_text(encoding="utf-8")
            )
        )
    return _dedupe_nodes(rows)


def _property_matches_request(node: dict[str, Any], request: str) -> bool:
    property_name = str((node.get("facts") or {}).get("propertyName") or "")
    if not property_name:
        return False
    lower = request.lower()
    variants = {
        property_name.lower(),
        property_name.lower().replace("_", "-"),
        "--" + property_name.lower().replace("_", "-"),
    }
    return any(value in lower for value in variants)


def rank_script_nodes(
    nodes: Iterable[dict[str, Any]], request: str, package: str | Path
) -> list[dict[str, Any]]:
    rows = rank_script_nodes_v66(nodes, request, package)
    reranked = []
    request_tokens = _tokens(request)
    for row in rows:
        item = dict(row)
        facts = item.get("facts") or {}
        property_name = str(facts.get("propertyName") or "").lower()
        role = item.get("role")
        direct = _property_matches_request(item, request)
        token_overlap = bool(property_name) and bool(
            _tokens(property_name) & request_tokens
        )
        bonus = 0.0
        if role == "python_input_property":
            bonus += 90.0
            if direct:
                bonus += 360.0
            elif token_overlap:
                bonus += 120.0
            if item.get("v66_document_linked_path"):
                bonus += 90.0
            if item.get("v66_explicit_path"):
                bonus += 180.0
        item["localization_score"] = round(
            float(item.get("localization_score") or 0.0) + bonus, 6
        )
        item["v76_property_name_in_request"] = direct
        item["v76_property_role_bonus"] = bonus
        reranked.append(item)
    return sorted(
        reranked,
        key=lambda row: (
            -float(row["localization_score"]),
            row["path"],
            row["line"],
            row["site_id"],
        ),
    )


def select_editable_nodes(
    ranked: Iterable[dict[str, Any]], *, max_nodes: int = 16
) -> list[dict[str, Any]]:
    ordered = list(ranked)
    if max_nodes <= 0:
        return []
    baseline = select_editable_nodes_v66(ordered, max_nodes=max_nodes)
    property_channel = [
        row
        for row in ordered
        if row.get("role") == "python_input_property"
        and (
            row.get("v76_property_name_in_request")
            or row.get("v66_document_linked_path")
            or row.get("v66_explicit_path")
        )
    ]
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    selected_spans: set[tuple[str, int, int]] = set()

    def add(row: dict[str, Any], channel: str) -> None:
        if len(selected) >= max_nodes or row["site_id"] in selected_ids:
            return
        span = (
            row["path"],
            int(row["byte_span"]["start"]),
            int(row["byte_span"]["end"]),
        )
        if span in selected_spans:
            return
        item = dict(row)
        item["selection_channel"] = channel
        selected.append(item)
        selected_ids.add(row["site_id"])
        selected_spans.add(span)

    for row in property_channel[: min(8, max_nodes)]:
        add(row, "documented_python_property")
    for row in baseline:
        add(row, str(row.get("selection_channel") or "v66_baseline"))
    for row in ordered:
        add(row, "rank_fill")
    return selected
