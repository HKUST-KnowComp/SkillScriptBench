from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from skillscriptbench.d23_structural_facts_v90 import (
    _all_arguments,
    _iter_python_sources,
    _source_segment,
    build_d23_fact_packet,
)
from skillscriptbench.io_utils import canonical_json_hash, sha256_bytes
from skillscriptbench.multilang_structural_v66 import (
    enumerate_package_nodes,
    node_public_view,
)
from skillscriptbench.structural_evolution_v65 import (
    extract_python_role_sites,
    rank_role_sites,
)


SCHEMA_VERSION = "0.96-query-aware-multiscope-interprocedural-node-facts-v1"


_PUBLIC_SITE_KEYS = (
    "site_id",
    "path",
    "line",
    "end_line",
    "column",
    "end_column",
    "symbol",
    "node_type",
    "semantic_node_type",
    "role",
    "family",
    "parameter_candidate",
    "default",
    "observed_source",
    "window",
    "localization_score",
    "score_reasons",
    "query_symbol_overlap",
)


def _lexical_tokens(value: str) -> set[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return {
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9]*", expanded.replace("_", " "))
        if len(token) >= 2
    }


def _behavior_query_tokens(request_text: str) -> set[str]:
    match = re.search(
        r"package(?:'s|\N{RIGHT SINGLE QUOTATION MARK}s)\s+(.+?)\s+behavior",
        request_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return _lexical_tokens(match.group(1) if match else request_text)


def _ranked_structural_sites_v96(
    package: Path, request_text: str
) -> list[dict[str, Any]]:
    role_sites: list[dict[str, Any]] = []
    for path, source in _iter_python_sources(package):
        role_sites.extend(extract_python_role_sites(path, source))
    ranked, _contract = rank_role_sites(role_sites, request_text)
    request_tokens = _behavior_query_tokens(request_text)
    for site in ranked:
        overlap = sorted(request_tokens & _lexical_tokens(str(site.get("symbol") or "")))
        if overlap:
            site["localization_score"] = round(
                float(site.get("localization_score") or 0.0) + 8.0 * len(overlap),
                6,
            )
            site["score_reasons"] = [
                *list(site.get("score_reasons") or []),
                "query_symbol_overlap",
            ]
        site["query_symbol_overlap"] = overlap
    ranked.sort(
        key=lambda row: (
            -float(row["localization_score"]),
            row["path"],
            int(row["line"]),
            int(row["column"]),
            row["site_id"],
        )
    )
    return [
        {key: site.get(key) for key in _PUBLIC_SITE_KEYS}
        for site in ranked[:12]
    ]


def _ast_registry_node(
    *,
    path: str,
    source: str,
    node: ast.AST,
    symbol: str,
    role: str,
) -> dict[str, Any]:
    if not all(
        hasattr(node, field)
        for field in ("lineno", "col_offset", "end_lineno", "end_col_offset")
    ):
        raise ValueError("ast_node_missing_source_span")
    lines = source.splitlines(keepends=True)
    start_line = int(node.lineno)
    end_line = int(node.end_lineno)
    start_column = int(node.col_offset)
    end_column = int(node.end_col_offset)
    start = sum(len(line.encode("utf-8")) for line in lines[: start_line - 1]) + start_column
    end = sum(len(line.encode("utf-8")) for line in lines[: end_line - 1]) + end_column
    encoded = source.encode("utf-8")
    observed = encoded[start:end].decode("utf-8")
    identity = {
        "backend": "python_ast_v96",
        "path": path,
        "symbol": symbol,
        "role": role,
        "start": start,
        "end": end,
        "source_sha256": sha256_bytes(encoded[start:end]),
    }
    identifier = f"node-{canonical_json_hash(identity)[:16]}"
    low = max(1, start_line - 3)
    high = min(len(lines), end_line + 3)
    window = "\n".join(
        f"{index}: {lines[index - 1].rstrip()}"
        for index in range(low, high + 1)
    )
    return {
        "site_id": identifier,
        "node_id": identifier,
        "path": path,
        "language": "python",
        "backend": "python_ast_v96",
        "node_type": type(node).__name__,
        "role": role,
        "symbol": symbol,
        "span": {
            "start_line": start_line,
            "start_column": start_column,
            "end_line": end_line,
            "end_column": end_column,
        },
        "byte_span": {"start": start, "end": end},
        "line": start_line,
        "column": start_column,
        "end_line": end_line,
        "end_column": end_column,
        "start": start,
        "end": end,
        "observed_source": observed,
        "target_source": observed,
        "window": window,
        "facts": {"argument_flow_role": role},
        "node_source_sha256": sha256_bytes(encoded[start:end]),
    }


def _matching_registry_node(
    registry: list[dict[str, Any]],
    *,
    path: str,
    line: int,
    column: int,
    node_type: str | None = None,
) -> dict[str, Any] | None:
    matches = [
        node
        for node in registry
        if node.get("path") == path
        and int(node.get("line") or -1) == line
        and int(node.get("column") or -1) == column
        and (node_type is None or node.get("node_type") == node_type)
    ]
    return matches[0] if len(matches) == 1 else None


def _function_scope_node(
    registry: list[dict[str, Any]], path: str, symbol: str
) -> dict[str, Any] | None:
    matches = [
        node
        for node in registry
        if node.get("path") == path
        and node.get("symbol") == symbol
        and node.get("role") == "function_scope"
    ]
    return matches[0] if len(matches) == 1 else None


def _formal_load_count(function: ast.FunctionDef, formal: str) -> int:
    count = 0
    for statement in function.body:
        for node in ast.walk(statement):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                if node is not function:
                    continue
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                count += int(node.id == formal)
    return count


def _interprocedural_argument_findings(
    package: Path,
    *,
    function_name: str | None,
    parameter: str | None,
    registry: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not function_name or not parameter:
        return []
    findings: list[dict[str, Any]] = []
    for path, source in _iter_python_sources(package):
        tree = ast.parse(source, filename=path)
        targets = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
            and parameter in _all_arguments(node)
        ]
        if len(targets) != 1:
            continue
        target = targets[0]
        helpers: dict[str, tuple[ast.FunctionDef, str]] = {
            node.name: (node, "nested")
            for node in target.body
            if isinstance(node, ast.FunctionDef)
        }
        helpers.update(
            {
                node.name: (node, "module")
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node is not target
            }
        )
        for statement in target.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for call in (
                node for node in ast.walk(statement) if isinstance(node, ast.Call)
            ):
                if not isinstance(call.func, ast.Name) or call.func.id not in helpers:
                    continue
                helper, helper_scope = helpers[call.func.id]
                formals = _all_arguments(helper)
                for index, argument in enumerate(call.args):
                    if (
                        not isinstance(argument, ast.Name)
                        or argument.id != parameter
                        or index >= len(formals)
                    ):
                        continue
                    formal = formals[index]
                    returns = [
                        node
                        for helper_statement in helper.body
                        for node in ast.walk(helper_statement)
                        if isinstance(node, ast.Return)
                    ]
                    constant_returns = [
                        node
                        for node in returns
                        if isinstance(node.value, ast.Constant)
                    ]
                    loads = _formal_load_count(helper, formal)
                    if loads != 0 or len(constant_returns) != 1:
                        continue
                    returned = constant_returns[0]
                    assert isinstance(returned.value, ast.Constant)
                    editable = _matching_registry_node(
                        registry,
                        path=path,
                        line=int(returned.value.lineno),
                        column=int(returned.value.col_offset),
                        node_type="Constant",
                    )
                    if editable is None:
                        editable = _ast_registry_node(
                            path=path,
                            source=source,
                            node=returned.value,
                            symbol=helper.name,
                            role="resolver_return_value",
                        )
                        registry.append(editable)
                    findings.append(
                        {
                            "caller": function_name,
                            "callee": helper.name,
                            "callee_scope": helper_scope,
                            "path": path,
                            "requested_parameter": parameter,
                            "callee_formal": formal,
                            "call_line": int(call.lineno),
                            "call_source": _source_segment(source, call),
                            "formal_load_count": loads,
                            "return_line": int(returned.lineno),
                            "return_source": _source_segment(source, returned),
                            "returned_constant": returned.value.value,
                            "argument_reaches_return": False,
                            "editable_return_node": (
                                node_public_view(editable) if editable else None
                            ),
                        }
                    )
    return findings


def _local_alias_findings(
    package: Path,
    *,
    function_name: str | None,
    parameter: str | None,
    registry: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not function_name or not parameter:
        return []
    findings: list[dict[str, Any]] = []
    for path, source in _iter_python_sources(package):
        tree = ast.parse(source, filename=path)
        targets = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
            and parameter in _all_arguments(node)
        ]
        if len(targets) != 1:
            continue
        target = targets[0]
        parameter_load_count = sum(
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id == parameter
            for statement in target.body
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            for node in ast.walk(statement)
        )
        if parameter_load_count != 0:
            continue
        for statement in target.body:
            if (
                not isinstance(statement, ast.Assign)
                or len(statement.targets) != 1
                or not isinstance(statement.targets[0], ast.Name)
                or not isinstance(statement.value, ast.Constant)
            ):
                continue
            alias = statement.targets[0].id
            if parameter.casefold() not in alias.casefold():
                continue
            alias_load_count = sum(
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id == alias
                for other in target.body
                if other is not statement
                and not isinstance(other, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                for node in ast.walk(other)
            )
            if alias_load_count < 1:
                continue
            editable = _matching_registry_node(
                registry,
                path=path,
                line=int(statement.value.lineno),
                column=int(statement.value.col_offset),
                node_type="Constant",
            )
            if editable is None:
                editable = _ast_registry_node(
                    path=path,
                    source=source,
                    node=statement.value,
                    symbol=function_name,
                    role="alias_source_value",
                )
                registry.append(editable)
            findings.append(
                {
                    "function": function_name,
                    "path": path,
                    "requested_parameter": parameter,
                    "alias": alias,
                    "parameter_load_count": parameter_load_count,
                    "alias_load_count": alias_load_count,
                    "assignment_line": int(statement.lineno),
                    "assignment_source": _source_segment(source, statement),
                    "argument_reaches_alias": False,
                    "editable_alias_node": node_public_view(editable),
                }
            )
    return findings


def _deduplicate_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for node in nodes:
        result[str(node["node_id"])] = node
    return [result[key] for key in sorted(result)]


def build_d23_fact_packet_v96(
    package_root: str | Path, request_text: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    package = Path(package_root).resolve()
    base, base_audit = build_d23_fact_packet(package, request_text)
    base["ranked_structural_sites"] = _ranked_structural_sites_v96(
        package, request_text
    )
    registry_rows = enumerate_package_nodes(package, include_markdown=False)
    contract = base.get("request_contract") or {}
    findings = _interprocedural_argument_findings(
        package,
        function_name=contract.get("function"),
        parameter=contract.get("parameter"),
        registry=registry_rows,
    )
    alias_findings = _local_alias_findings(
        package,
        function_name=contract.get("function"),
        parameter=contract.get("parameter"),
        registry=registry_rows,
    )
    registry = {str(node["node_id"]): node for node in registry_rows}

    editable: list[dict[str, Any]] = []
    for finding in findings:
        if finding.get("editable_return_node"):
            editable.append(finding["editable_return_node"])
    for finding in alias_findings:
        if finding.get("editable_alias_node"):
            editable.append(finding["editable_alias_node"])

    ranked = base.get("ranked_structural_sites") or []
    ranked_nodes: list[dict[str, Any]] = []
    function_nodes: list[dict[str, Any]] = []
    for site in ranked[:3]:
        ranked_node = _matching_registry_node(
            registry_rows,
            path=str(site.get("path") or ""),
            line=int(site.get("line") or -1),
            column=int(site.get("column") or -1),
            node_type=str(site.get("node_type") or "") or None,
        )
        function_scope = _function_scope_node(
            registry_rows,
            str(site.get("path") or ""),
            str(site.get("symbol") or ""),
        )
        if ranked_node is not None:
            ranked_nodes.append(ranked_node)
        if function_scope is not None:
            function_nodes.append(function_scope)
    if not findings and not alias_findings:
        editable.extend(node_public_view(node) for node in ranked_nodes)
        editable.extend(node_public_view(node) for node in function_nodes)

    editable = _deduplicate_nodes(editable)
    packet = {
        **base,
        "schema_version": SCHEMA_VERSION,
        "claim": "visible_package_query_aware_ast_and_interprocedural_observations_only",
        "interprocedural_argument_flow": {
            "findings": findings,
            "dropped_argument_count": len(findings),
        },
        "local_alias_argument_flow": {
            "findings": alias_findings,
            "dropped_argument_count": len(alias_findings),
        },
        "structured_edit_spec": {
            "decision": "PROPOSE" if editable else "ABSTAIN",
            "editable_nodes": editable,
            "script_operations": ["replace_node", "insert_parameter"],
            "markdown_operation": "append_markdown",
            "constraints": [
                "bind script edits to one listed node id",
                "preserve bytes outside selected script nodes",
                "preserve the compatibility behavior",
                "make the smallest sufficient package change",
            ],
        },
    }
    if packet.get("def_use_graph") is not None:
        packet["def_use_graph"]["requested_parameter_has_effective_behavior_path"] = (
            False if findings or alias_findings else packet["def_use_graph"].get(
                "requested_parameter_has_behavior_path"
            )
        )
    audit = {
        **base_audit,
        "packet_hash": canonical_json_hash(packet),
        "node_registry_count": len(registry),
        "interprocedural_dropped_argument_count": len(findings),
        "local_alias_dropped_argument_count": len(alias_findings),
        "structural_disconnect_count": len(findings) + len(alias_findings),
        "editable_node_count": len(editable),
        "ranked_site_limit": 3,
        "query_symbol_reranking": True,
        "ranked_site_editable_count": len(ranked_nodes),
        "ranked_function_scope_editable_count": len(
            {node["node_id"] for node in function_nodes}
        ),
        "top_ranked_node_editable": bool(ranked_nodes),
        "top_ranked_function_scope_editable": bool(function_nodes),
        "structured_decision": packet["structured_edit_spec"]["decision"],
    }
    return packet, audit, registry
