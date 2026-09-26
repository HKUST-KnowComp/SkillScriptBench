from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Iterable

from skillscriptbench.io_utils import canonical_json_hash, sha256_bytes
from skillscriptbench.structural_evolution_v65 import (
    extract_python_role_sites,
    parse_request_contract,
    rank_role_sites,
)


SCHEMA_VERSION = "0.90-query-aware-ast-def-use-facts-v1"


def _node_id(payload: dict[str, Any]) -> str:
    return f"fact-{canonical_json_hash(payload)[:16]}"


def _source_segment(source: str, node: ast.AST) -> str:
    return (ast.get_source_segment(source, node) or ast.unparse(node))[:800]


def _iter_python_sources(package: Path) -> Iterable[tuple[str, str]]:
    paths: list[Path] = []
    for directory in (package / "scripts", package / "script"):
        if directory.is_dir():
            paths.extend(directory.rglob("*.py"))
    for path in sorted(set(paths)):
        try:
            source = path.read_text(encoding="utf-8")
            ast.parse(source, filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        yield path.relative_to(package).as_posix(), source


def _all_arguments(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    return [
        argument.arg
        for argument in (
            list(function.args.posonlyargs)
            + list(function.args.args)
            + list(function.args.kwonlyargs)
        )
    ]


def _parents(root: ast.AST) -> dict[ast.AST, ast.AST]:
    result: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(root):
        for child in ast.iter_child_nodes(parent):
            result[child] = parent
    return result


def _assignment_target(node: ast.AST) -> str | None:
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if len(targets) == 1 and isinstance(targets[0], ast.Name):
            return targets[0].id
    return None


def _nearest_semantic_node(
    name: ast.Name,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    parents: dict[ast.AST, ast.AST],
) -> ast.AST | None:
    current: ast.AST = name
    candidate: ast.AST | None = None
    while current is not function and current in parents:
        current = parents[current]
        if isinstance(current, (ast.Call, ast.Compare, ast.Subscript, ast.Return)):
            candidate = current
        if isinstance(current, (ast.Assign, ast.AnnAssign)):
            return candidate
        if isinstance(current, ast.stmt):
            return candidate
    return candidate


def _def_use_graph(
    path: str,
    source: str,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    parameter: str,
) -> dict[str, Any]:
    parents = _parents(function)
    nodes: dict[str, dict[str, Any]] = {}
    edges: set[tuple[str, str, str]] = set()

    def ensure_variable(name: str, role: str, line: int) -> str:
        identity = {"path": path, "symbol": function.name, "name": name, "role": role}
        identifier = _node_id(identity)
        if identifier not in nodes:
            nodes[identifier] = {
                "node_id": identifier,
                "kind": "variable",
                "role": role,
                "name": name,
                "path": path,
                "symbol": function.name,
                "line": line,
            }
        return identifier

    arguments = _all_arguments(function)
    parameter_ids = {
        name: ensure_variable(name, "requested_parameter" if name == parameter else "parameter", function.lineno)
        for name in arguments
    }
    variable_ids = dict(parameter_ids)

    for statement in function.body:
        target = _assignment_target(statement)
        if target is None:
            continue
        target_id = ensure_variable(target, "local_value", int(statement.lineno))
        variable_ids[target] = target_id
        value = statement.value if isinstance(statement, (ast.Assign, ast.AnnAssign)) else None
        if value is None:
            continue
        loads = sorted(
            {
                child.id
                for child in ast.walk(value)
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
            }
        )
        if not loads and isinstance(value, ast.Constant):
            literal_identity = {
                "path": path,
                "symbol": function.name,
                "line": int(statement.lineno),
                "literal": repr(value.value),
            }
            literal_id = _node_id(literal_identity)
            nodes[literal_id] = {
                "node_id": literal_id,
                "kind": "literal",
                "role": "literal_source",
                "value": value.value,
                "path": path,
                "symbol": function.name,
                "line": int(statement.lineno),
            }
            edges.add((literal_id, "assigns", target_id))
        for name in loads:
            source_id = variable_ids.get(name) or ensure_variable(
                name, "external_or_prior_value", int(statement.lineno)
            )
            variable_ids.setdefault(name, source_id)
            edges.add((source_id, "flows_to", target_id))

    sink_by_key: dict[tuple[int, str], str] = {}
    executable_nodes = (
        node
        for statement in function.body
        for node in ast.walk(statement)
    )
    for node in executable_nodes:
        if not isinstance(node, ast.Name) or not isinstance(node.ctx, ast.Load):
            continue
        semantic = _nearest_semantic_node(node, function, parents)
        if semantic is None:
            continue
        key = (int(getattr(semantic, "lineno", node.lineno)), ast.dump(semantic, include_attributes=False))
        sink_id = sink_by_key.get(key)
        if sink_id is None:
            identity = {
                "path": path,
                "symbol": function.name,
                "line": key[0],
                "semantic_ast": key[1],
            }
            sink_id = _node_id(identity)
            sink_by_key[key] = sink_id
            nodes[sink_id] = {
                "node_id": sink_id,
                "kind": "behavior_sink",
                "role": "semantic_expression",
                "path": path,
                "symbol": function.name,
                "line": key[0],
                "node_type": type(semantic).__name__,
                "source": _source_segment(source, semantic),
            }
        source_id = variable_ids.get(node.id) or ensure_variable(
            node.id, "external_or_prior_value", int(node.lineno)
        )
        variable_ids.setdefault(node.id, source_id)
        edges.add((source_id, "consumed_by", sink_id))

    adjacency: dict[str, set[str]] = {}
    for left, _relation, right in edges:
        adjacency.setdefault(left, set()).add(right)
    start = variable_ids.get(parameter)
    reachable: set[str] = set()
    frontier = [start] if start else []
    while frontier:
        current = frontier.pop()
        if current is None or current in reachable:
            continue
        reachable.add(current)
        frontier.extend(sorted(adjacency.get(current, set()) - reachable))

    sinks = [node for node in nodes.values() if node["kind"] == "behavior_sink"]
    variable_names = {
        node_id: str(node.get("name") or "")
        for node_id, node in nodes.items()
        if node.get("kind") == "variable"
    }
    parameter_related = {
        node_id
        for node_id, name in variable_names.items()
        if name == parameter or parameter.casefold() in name.casefold()
    }
    disconnected = []
    for sink in sinks:
        inbound = sorted(
            left
            for left, relation, right in edges
            if right == sink["node_id"] and relation == "consumed_by"
        )
        if (
            inbound
            and set(inbound) & parameter_related
            and sink["node_id"] not in reachable
        ):
            disconnected.append(
                {
                    "sink_node_id": sink["node_id"],
                    "line": sink["line"],
                    "source": sink["source"],
                    "consumed_variable_nodes": inbound,
                }
            )

    edge_rows = [
        {"source_node_id": left, "relation": relation, "target_node_id": right}
        for left, relation, right in sorted(edges)
    ]
    return {
        "path": path,
        "symbol": function.name,
        "requested_parameter": parameter,
        "function_source_sha256": sha256_bytes(
            _source_segment(source, function).encode("utf-8")
        ),
        "nodes": sorted(nodes.values(), key=lambda row: (row["line"], row["node_id"])),
        "edges": edge_rows,
        "requested_parameter_reachable_node_ids": sorted(reachable),
        "disconnected_behavior_sinks": disconnected,
        "requested_parameter_has_behavior_path": any(
            sink["node_id"] in reachable for sink in sinks
        ),
    }


def build_d23_fact_packet(package_root: str | Path, request_text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    package = Path(package_root).resolve()
    contract = parse_request_contract(request_text)
    role_sites: list[dict[str, Any]] = []
    target_functions: list[tuple[str, str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    parse_failures: list[dict[str, str]] = []
    for path, source in _iter_python_sources(package):
        try:
            tree = ast.parse(source, filename=path)
            role_sites.extend(extract_python_role_sites(path, source))
        except SyntaxError as exc:
            parse_failures.append({"path": path, "error": str(exc)})
            continue
        if contract.function:
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == contract.function:
                    target_functions.append((path, source, node))

    ranked, ranked_contract = rank_role_sites(role_sites, request_text)
    top_sites = [
        {
            key: site.get(key)
            for key in (
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
            )
        }
        for site in ranked[:12]
    ]
    selected_targets = list(target_functions)
    parameter_matches = [
        row for row in target_functions if contract.parameter in _all_arguments(row[2])
    ] if contract.parameter else []
    selection_reason = "unique_function_name"
    if len(parameter_matches) == 1:
        selected_targets = parameter_matches
        selection_reason = "unique_function_and_parameter_signature"
    elif ranked and contract.function:
        top_path = str(ranked[0]["path"])
        top_matches = [
            row
            for row in target_functions
            if row[0] == top_path and row[2].name == contract.function
        ]
        if len(top_matches) == 1:
            selected_targets = top_matches
            selection_reason = "top_ranked_structural_path"

    def_use = None
    if contract.parameter and len(selected_targets) == 1:
        path, source, function = selected_targets[0]
        def_use = _def_use_graph(path, source, function, contract.parameter)

    packet = {
        "schema_version": SCHEMA_VERSION,
        "claim": "visible_package_query_aware_structural_observations_only",
        "disclaimer": (
            "These facts are observations, not a prescribed patch or correctness claim. "
            "Independently decide the minimal edit."
        ),
        "request_contract": {
            "function": ranked_contract.function,
            "parameter": ranked_contract.parameter,
            "default": ranked_contract.default,
            "has_default": ranked_contract.has_default,
            "family": ranked_contract.family,
        },
        "ranked_structural_sites": top_sites,
        "def_use_graph": def_use,
        "parse_failures": parse_failures,
    }
    audit = {
        "packet_hash": canonical_json_hash(packet),
        "python_role_site_count": len(role_sites),
        "ranked_site_count": len(ranked),
        "function_name_match_count": len(target_functions),
        "parameter_signature_match_count": len(parameter_matches),
        "target_function_match_count": len(selected_targets),
        "target_function_selection_reason": selection_reason,
        "def_use_available": def_use is not None,
        "requested_parameter_has_behavior_path": (
            def_use.get("requested_parameter_has_behavior_path")
            if def_use is not None
            else None
        ),
        "disconnected_behavior_sink_count": (
            len(def_use.get("disconnected_behavior_sinks") or [])
            if def_use is not None
            else None
        ),
    }
    return packet, audit
