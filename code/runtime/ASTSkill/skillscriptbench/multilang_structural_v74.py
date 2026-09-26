from __future__ import annotations

import json
import shutil
import subprocess
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
    extract_javascript_nodes as extract_javascript_nodes_v66,
    extract_python_nodes,
    extract_shell_nodes,
    rank_script_nodes as rank_script_nodes_v66,
    select_editable_nodes as select_editable_nodes_v66,
)


SCHEMA_VERSION = "0.74-multilang-byte-node-v1"
JS_PROPERTY_HELPER = (
    Path(__file__).resolve().parent
    / "js_parser"
    / "extract_object_property_nodes_v74.mjs"
)


def _extract_object_property_nodes(path: str, source: str) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [shutil.which("node") or "node", str(JS_PROPERTY_HELPER)],
        input=json.dumps({"source": source, "filename": path}),
        text=True,
        capture_output=True,
        cwd=JS_PROPERTY_HELPER.parent,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise SyntaxError(
            f"babel_v74_property_parse_failed:{path}:{completed.stderr[-600:]}"
        )
    payload = json.loads(completed.stdout)
    language = "typescript" if Path(path).suffix.lower() == ".ts" else "javascript"
    rows = []
    for value in payload.get("nodes", []):
        function = value.get("enclosingFunction") or {}
        rows.append(
            _node(
                path=path,
                language=language,
                backend="babel_ast_v74",
                node_type=str(value.get("nodeType") or "Node"),
                role=str(value.get("role") or "object_property"),
                symbol=str(function.get("name") or "<module>"),
                source=source,
                start_byte=int(value["startByte"]),
                end_byte=int(value["endByte"]),
                facts=value.get("facts") or {},
            )
        )
    return rows


def extract_javascript_nodes(path: str, source: str) -> list[dict[str, Any]]:
    return _dedupe_nodes(
        [
            *extract_javascript_nodes_v66(path, source),
            *_extract_object_property_nodes(path, source),
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
        except (UnicodeDecodeError, SyntaxError, ValueError, subprocess.SubprocessError):
            continue
    if include_markdown and (root / "SKILL.md").is_file():
        rows.extend(
            extract_markdown_nodes(
                "SKILL.md", (root / "SKILL.md").read_text(encoding="utf-8")
            )
        )
    return _dedupe_nodes(rows)


def rank_script_nodes(
    nodes: Iterable[dict[str, Any]], request: str, package: str | Path
) -> list[dict[str, Any]]:
    request_tokens = _tokens(request)
    rows = rank_script_nodes_v66(nodes, request, package)
    reranked = []
    for row in rows:
        item = dict(row)
        facts = item.get("facts") or {}
        property_name = str(facts.get("propertyName") or "").lower()
        direct_property_match = bool(property_name) and property_name in request_tokens
        property_role = item.get("role") in {"object_property", "object_property_entry"}
        bonus = 0.0
        if property_role:
            bonus += 70.0 if item["role"] == "object_property_entry" else 25.0
            if direct_property_match:
                bonus += 320.0
            if item.get("v66_document_linked_path"):
                bonus += 90.0
            if item.get("v66_explicit_path"):
                bonus += 180.0
        item["localization_score"] = round(
            float(item.get("localization_score") or 0.0) + bonus, 6
        )
        item["v74_property_name_in_request"] = direct_property_match
        item["v74_property_role_bonus"] = bonus
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
        if row.get("role") == "object_property_entry"
        and (
            row.get("v74_property_name_in_request")
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
        add(row, "documented_object_property")
    for row in baseline:
        add(row, str(row.get("selection_channel") or "v66_baseline"))
    for row in ordered:
        add(row, "rank_fill")
    return selected
