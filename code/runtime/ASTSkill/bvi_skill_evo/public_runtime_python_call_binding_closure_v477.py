from __future__ import annotations

import ast
import copy
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from bvi_skill_evo import public_runtime_python_call_binding_closure_v464 as legacy
from bvi_skill_evo.balanced_hybrid_ast_v316 import _assert_public_package, _tree_hash
from skillscriptbench.io_utils import canonical_json_hash, sha256_file


SCHEMA_VERSION = "4.77-public-runtime-python-call-binding-closure-v4"
METHOD_ID = "public_runtime_python_call_binding_closure_v4"
FAMILY = "call_binding_closure"
MAX_CLOSURE_EDITS = 12
MAX_CALLER_SYMBOLS = 6
MAX_CLOSURE_ROOTS = 2
MAX_CHANGED_PATHS = 3
MIN_CONFIDENCE = 0.96
EVIDENCE_CONFIDENCE = {
    "reciprocal_same_call": 0.995,
    "duplicate_displacement": 0.99,
    "reciprocal_across_calls": 0.99,
    "repeated_pair": 0.98,
    "repeated_target_formal_across_callers": 0.975,
    "anchored_displacement_chain": 0.975,
    "anchored_single_outlier": 0.96,
    "caller_family_closure_completion": 0.96,
}


def _iter_python_files(package: Path) -> Iterable[Path]:
    return legacy._iter_python_files(package)


def _nearest_parent(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
    kinds: tuple[type[ast.AST], ...],
) -> ast.AST | None:
    return legacy._nearest_parent(node, parents, kinds)


def _annotation_key(node: ast.expr | None) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return "".join(node.value.split())
    try:
        return "".join(ast.unparse(node).split())
    except (AttributeError, ValueError):
        return ast.dump(node, annotate_fields=False, include_attributes=False)


def _argument_records(arguments: ast.arguments) -> tuple[list[str], list[str], dict[str, str]]:
    positional_nodes = [*arguments.posonlyargs, *arguments.args]
    all_nodes = [*positional_nodes, *arguments.kwonlyargs]
    if arguments.vararg is not None:
        all_nodes.append(arguments.vararg)
    if arguments.kwarg is not None:
        all_nodes.append(arguments.kwarg)
    positional = [node.arg for node in positional_nodes]
    names = [node.arg for node in all_nodes]
    annotations = {node.arg: _annotation_key(node.annotation) for node in all_nodes}
    return positional, names, annotations


def _owned_nodes(function: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[ast.AST]:
    return [
        node
        for node in ast.walk(function)
        if node is function
        or _nearest_parent(
            node,
            parents,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
        )
        is function
    ]


def _local_bindings(function: ast.AST, parents: dict[ast.AST, ast.AST]) -> set[str]:
    result: set[str] = set()
    for node in _owned_nodes(function, parents):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            result.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                result.add(alias.asname or alias.name.split(".")[0])
    return result


def _scope_declarations(
    function: ast.AST, parents: dict[ast.AST, ast.AST]
) -> tuple[set[str], set[str]]:
    globals_: set[str] = set()
    nonlocals: set[str] = set()
    for node in _owned_nodes(function, parents):
        if isinstance(node, ast.Global):
            globals_.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            nonlocals.update(node.names)
    return globals_, nonlocals


def _module_keys(path: str) -> set[str]:
    pure = PurePosixPath(path)
    without_suffix = pure.with_suffix("")
    parts = list(without_suffix.parts)
    result = {".".join(parts)}
    if parts and parts[0] == "scripts":
        result.add(".".join(parts[1:]))
    if parts and parts[-1] == "__init__":
        result.add(".".join(parts[:-1]))
        if parts[0] == "scripts":
            result.add(".".join(parts[1:-1]))
    return {value for value in result if value}


def _build_public_graph(package: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []
    by_node: dict[ast.AST, dict[str, Any]] = {}
    for path in _iter_python_files(package):
        relative = path.relative_to(package).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        imported_names: dict[str, dict[str, Any]] = {}
        imported_modules: dict[str, dict[str, Any]] = {}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported_names[alias.asname or alias.name] = {
                        "module": node.module or "",
                        "name": alias.name,
                        "level": int(node.level),
                    }
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules[alias.asname or alias.name.split(".")[0]] = {
                        "module": alias.name,
                        "level": 0,
                    }
        file_record = {
            "path": relative,
            "file": path,
            "source": source,
            "tree": tree,
            "parents": parents,
            "imported_names": imported_names,
            "imported_modules": imported_modules,
            "module_keys": _module_keys(relative),
        }
        files.append(file_record)
        function_nodes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        function_nodes.sort(
            key=lambda node: (
                int(node.lineno),
                int(node.col_offset),
            )
        )
        for node in function_nodes:
            parent_function = _nearest_parent(
                node, parents, (ast.FunctionDef, ast.AsyncFunctionDef)
            )
            class_node = _nearest_parent(node, parents, (ast.ClassDef,))
            class_name = class_node.name if isinstance(class_node, ast.ClassDef) else None
            positional, own_parameters, annotations = _argument_records(node.args)
            receiver = (
                positional[0]
                if class_name and parent_function is None and positional
                and positional[0] in {"self", "cls"}
                else None
            )
            positional = positional[1:] if receiver else positional
            own_parameters = [value for value in own_parameters if value != receiver]
            record = {
                "path": relative,
                "name": node.name,
                "class_name": class_name,
                "node": node,
                "parent_function_node": parent_function,
                "positional": positional,
                "own_parameters": own_parameters,
                "own_annotations": annotations,
                "receiver": receiver,
                "file_record": file_record,
            }
            functions.append(record)
            by_node[node] = record

    for function in functions:
        ancestors: list[dict[str, Any]] = []
        current = function["parent_function_node"]
        while current is not None:
            parent = by_node[current]
            ancestors.append(parent)
            current = parent["parent_function_node"]
        ancestors.reverse()
        symbol_parts = [record["name"] for record in ancestors] + [function["name"]]
        if function["class_name"]:
            symbol_parts.insert(0, function["class_name"])
        function["symbol"] = ".".join(symbol_parts)
        root = ancestors[0] if ancestors else function
        root_parts = [root["name"]]
        if root["class_name"]:
            root_parts.insert(0, root["class_name"])
        function["closure_root"] = ".".join(root_parts)
        function["ancestor_functions"] = ancestors
        local_bindings = _local_bindings(
            function["node"], function["file_record"]["parents"]
        )
        globals_, nonlocals = _scope_declarations(
            function["node"], function["file_record"]["parents"]
        )
        visible = list(function["own_parameters"])
        parameter_scope = {name: "own" for name in visible}
        annotations = dict(function["own_annotations"])
        for ancestor in reversed(ancestors):
            for name in ancestor["own_parameters"]:
                if name in visible or name in globals_:
                    continue
                if name in local_bindings and name not in nonlocals:
                    continue
                visible.append(name)
                parameter_scope[name] = "lexical"
                annotations[name] = ancestor["own_annotations"].get(name, "")
        function["parameters"] = visible
        function["parameter_scope"] = parameter_scope
        function["parameter_annotations"] = annotations

    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_path_name: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    by_method: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    by_module_name: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for function in functions:
        by_name[function["name"]].append(function)
        by_path_name[(function["path"], function["name"])].append(function)
        if function["class_name"] and function["parent_function_node"] is None:
            by_method[
                (function["path"], function["class_name"], function["name"])
            ].append(function)
        if function["parent_function_node"] is None:
            for key in function["file_record"]["module_keys"]:
                by_module_name[(key, function["name"])].append(function)
    return {
        "files": files,
        "functions": functions,
        "by_name": by_name,
        "by_path_name": by_path_name,
        "by_method": by_method,
        "by_module_name": by_module_name,
    }


def _resolve_imported_function(
    graph: dict[str, Any], caller: dict[str, Any], record: dict[str, Any]
) -> list[dict[str, Any]]:
    module = str(record.get("module") or "")
    name = str(record["name"])
    level = int(record.get("level") or 0)
    caller_path = PurePosixPath(str(caller["path"]))
    candidates: list[str] = []
    if module:
        candidates.append(module)
    module_path = module.replace(".", "/")
    if level:
        base = caller_path.parent
        for _ in range(max(0, level - 1)):
            base = base.parent
        relative = (base / module_path).as_posix().strip("/")
        candidates.extend(_module_keys(f"{relative}.py"))
    else:
        relative = (caller_path.parent / module_path).as_posix().strip("/")
        candidates.extend(_module_keys(f"{relative}.py"))
        candidates.extend(_module_keys(f"scripts/{module_path}.py"))
    matches: dict[str, dict[str, Any]] = {}
    for key in candidates:
        for function in graph["by_module_name"].get((key, name), []):
            matches[f"{function['path']}::{function['symbol']}"] = function
    return list(matches.values())


def _resolve_call(
    graph: dict[str, Any], caller: dict[str, Any], call: ast.Call
) -> tuple[dict[str, Any] | None, str | None]:
    file_record = caller["file_record"]
    candidates: list[dict[str, Any]] = []
    if isinstance(call.func, ast.Name):
        raw_name = call.func.id
        imported = file_record["imported_names"].get(raw_name)
        if imported is not None:
            candidates = _resolve_imported_function(graph, caller, imported)
        if not candidates:
            local = [
                row
                for row in graph["by_path_name"].get((caller["path"], raw_name), [])
                if row["parent_function_node"] is None
            ]
            candidates = local if len(local) == 1 else [
                row for row in graph["by_name"].get(raw_name, [])
                if row["parent_function_node"] is None
            ]
    elif isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
        base_name = call.func.value.id
        name = call.func.attr
        if base_name in {"self", "cls"} and caller["class_name"]:
            candidates = graph["by_method"].get(
                (caller["path"], caller["class_name"], name), []
            )
        elif base_name in file_record["imported_modules"]:
            imported = {
                **file_record["imported_modules"][base_name],
                "name": name,
            }
            candidates = _resolve_imported_function(graph, caller, imported)
    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) > 1:
        return None, "ambiguous_package_function_resolution"
    return None, None


def _calls_owned_by(function: dict[str, Any]) -> list[ast.Call]:
    node = function["node"]
    parents = function["file_record"]["parents"]
    return [
        value
        for value in ast.walk(node)
        if isinstance(value, ast.Call)
        and _nearest_parent(
            value,
            parents,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
        )
        is node
    ]


def _binding_rows_from_graph(
    graph: dict[str, Any], caller: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    abstentions: list[dict[str, Any]] = []
    caller_parameters = set(caller["parameters"])
    source = caller["file_record"]["source"]
    parents = caller["file_record"]["parents"]
    for call in _calls_owned_by(caller):
        callee, reason = _resolve_call(graph, caller, call)
        if reason:
            abstentions.append(
                {
                    "path": caller["path"],
                    "caller_symbol": caller["symbol"],
                    "line": int(call.lineno),
                    "reason": reason,
                }
            )
        if callee is None:
            continue
        mapped: list[tuple[str, ast.expr, str]] = []
        if not any(isinstance(value, ast.Starred) for value in call.args):
            for index, value in enumerate(call.args):
                if index < len(callee["positional"]):
                    mapped.append((callee["positional"][index], value, f"positional:{index}"))
        callee_parameters = set(callee["own_parameters"])
        for keyword in call.keywords:
            if keyword.arg is not None and keyword.arg in callee_parameters:
                mapped.append((keyword.arg, keyword.value, f"keyword:{keyword.arg}"))
        call_id = (
            f"{caller['path']}::{caller['symbol']}::"
            f"{int(call.lineno)}:{int(call.col_offset)}::{callee['symbol']}"
        )
        for formal, value, slot in mapped:
            if not isinstance(value, ast.Name) or not isinstance(value.ctx, ast.Load):
                continue
            actual = value.id
            if actual not in caller_parameters:
                continue
            parent = parents.get(value)
            rows.append(
                {
                    "path": caller["path"],
                    "caller_symbol": caller["symbol"],
                    "closure_root": caller["closure_root"],
                    "callee_symbol": callee["symbol"],
                    "call_id": call_id,
                    "call_line": int(call.lineno),
                    "call_source": ast.get_source_segment(source, call) or "",
                    "formal_parameter": formal,
                    "actual_parameter": actual,
                    "formal_available_as_caller_parameter": formal in caller_parameters,
                    "argument_slot": slot,
                    "is_identity": formal == actual,
                    "node": value,
                    "node_role": (
                        f"keyword:{parent.arg}" if isinstance(parent, ast.keyword) else slot
                    ),
                    "caller_parameter_count": len(caller_parameters),
                    "actual_parameter_scope": caller["parameter_scope"].get(actual, ""),
                    "formal_parameter_scope": caller["parameter_scope"].get(formal, ""),
                    "caller_formal_annotation": caller["parameter_annotations"].get(formal, ""),
                    "caller_actual_annotation": caller["parameter_annotations"].get(actual, ""),
                    "callee_formal_annotation": callee["own_annotations"].get(formal, ""),
                }
            )
    return rows, abstentions


def _binding_rows(package: Path) -> list[dict[str, Any]]:
    graph = _build_public_graph(package)
    rows: list[dict[str, Any]] = []
    for caller in graph["functions"]:
        current, _ = _binding_rows_from_graph(graph, caller)
        rows.extend(current)
    return rows


def _suppression_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    suppressions: dict[str, dict[str, Any]] = {}
    by_caller_callee: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    by_call: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_caller_callee[
            (row["path"], row["caller_symbol"], row["callee_symbol"])
        ].append(row)
        by_call[row["call_id"]].append(row)
    for row in rows:
        if row["is_identity"] or not row["formal_available_as_caller_parameter"]:
            continue
        callee_annotation = str(row["callee_formal_annotation"])
        actual_annotation = str(row["caller_actual_annotation"])
        same_name_annotation = str(row["caller_formal_annotation"])
        if (
            callee_annotation
            and actual_annotation
            and same_name_annotation
            and actual_annotation == callee_annotation
            and same_name_annotation != callee_annotation
        ):
            node_id = legacy._node_id(row["path"], row["node"])
            suppressions[node_id] = {
                "reason": "actual_parameter_type_matches_callee_while_same_name_parameter_does_not",
                "path": row["path"],
                "caller_symbol": row["caller_symbol"],
                "callee_symbol": row["callee_symbol"],
                "callee_formal_parameter": row["formal_parameter"],
                "observed_caller_parameter": row["actual_parameter"],
                "callee_annotation": callee_annotation,
                "actual_annotation": actual_annotation,
                "same_name_annotation": same_name_annotation,
            }
    for caller_rows in by_caller_callee.values():
        calls = {row["call_id"] for row in caller_rows}
        if len(calls) < 2:
            continue
        for call_id in calls:
            mismatches = [
                row
                for row in by_call[call_id]
                if not row["is_identity"] and row["formal_available_as_caller_parameter"]
            ]
            for row in mismatches:
                reciprocal = next(
                    (
                        other
                        for other in mismatches
                        if other["formal_parameter"] == row["actual_parameter"]
                        and other["actual_parameter"] == row["formal_parameter"]
                    ),
                    None,
                )
                if reciprocal is None:
                    continue
                identity_alternative = any(
                    other["call_id"] != call_id
                    and other["formal_parameter"] == row["formal_parameter"]
                    and other["actual_parameter"] == row["formal_parameter"]
                    for other in caller_rows
                ) and any(
                    other["call_id"] != call_id
                    and other["formal_parameter"] == row["actual_parameter"]
                    and other["actual_parameter"] == row["actual_parameter"]
                    for other in caller_rows
                )
                if not identity_alternative:
                    continue
                for target in (row, reciprocal):
                    node_id = legacy._node_id(target["path"], target["node"])
                    suppressions[node_id] = {
                        "reason": "same_caller_callee_has_identity_and_mirrored_argument_branches",
                        "path": target["path"],
                        "caller_symbol": target["caller_symbol"],
                        "callee_symbol": target["callee_symbol"],
                        "callee_formal_parameter": target["formal_parameter"],
                        "observed_caller_parameter": target["actual_parameter"],
                        "identity_alternative_present": True,
                    }
    return dict(sorted(suppressions.items()))


def _evidence_for_bindings(
    rows: list[dict[str, Any]], suppressions: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    by_call: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_caller: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    eligible: list[dict[str, Any]] = []
    for row in rows:
        by_call[row["call_id"]].append(row)
        by_caller[(row["path"], row["caller_symbol"])].append(row)
        node_id = legacy._node_id(row["path"], row["node"])
        if (
            not row["is_identity"]
            and row["formal_available_as_caller_parameter"]
            and node_id not in suppressions
        ):
            eligible.append(row)
    by_target_formal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        by_target_formal[row["formal_parameter"]].append(row)

    seeds: list[dict[str, Any]] = []
    for row in eligible:
        evidence: set[str] = set()
        same_call = by_call[row["call_id"]]
        same_caller = by_caller[(row["path"], row["caller_symbol"])]
        mismatches = [
            other
            for other in same_call
            if not other["is_identity"]
            and other["formal_available_as_caller_parameter"]
            and legacy._node_id(other["path"], other["node"]) not in suppressions
        ]
        anchors = [other for other in same_call if other["is_identity"]]
        if any(
            other["formal_parameter"] == row["actual_parameter"]
            and other["actual_parameter"] == row["formal_parameter"]
            for other in same_call
        ):
            evidence.add("reciprocal_same_call")
        if any(
            other["actual_parameter"] == row["actual_parameter"]
            and other["formal_parameter"] != row["formal_parameter"]
            for other in same_call
        ):
            evidence.add("duplicate_displacement")
        if any(
            other["formal_parameter"] == row["actual_parameter"]
            and other["actual_parameter"] == row["formal_parameter"]
            and other["call_id"] != row["call_id"]
            for other in same_caller
        ):
            evidence.add("reciprocal_across_calls")
        if sum(
            other["formal_parameter"] == row["formal_parameter"]
            and other["actual_parameter"] == row["actual_parameter"]
            for other in same_caller
        ) >= 2:
            evidence.add("repeated_pair")
        if len(mismatches) == 1 and anchors:
            evidence.add("anchored_single_outlier")
        if len(mismatches) >= 2 and anchors and any(
            a is not b
            and (
                a["formal_parameter"] == b["actual_parameter"]
                or a["actual_parameter"] == b["formal_parameter"]
            )
            for a in mismatches
            for b in mismatches
        ):
            evidence.add("anchored_displacement_chain")
        same_target = by_target_formal[row["formal_parameter"]]
        if (
            len({(other["path"], other["caller_symbol"]) for other in same_target}) >= 2
            and len({other["actual_parameter"] for other in same_target}) >= 2
        ):
            evidence.add("repeated_target_formal_across_callers")
        if not evidence:
            continue
        confidence = max(EVIDENCE_CONFIDENCE[value] for value in evidence)
        if confidence >= MIN_CONFIDENCE:
            seeds.append({**row, "evidence": sorted(evidence), "confidence": confidence})

    seed_roots = {str(row["closure_root"]) for row in seeds}
    seed_by_id = {
        legacy._node_id(row["path"], row["node"]): row for row in seeds
    }
    selected: list[dict[str, Any]] = []
    for row in eligible:
        node_id = legacy._node_id(row["path"], row["node"])
        if node_id in seed_by_id:
            selected.append(seed_by_id[node_id])
        elif str(row["closure_root"]) in seed_roots:
            selected.append(
                {
                    **row,
                    "evidence": ["caller_family_closure_completion"],
                    "confidence": EVIDENCE_CONFIDENCE[
                        "caller_family_closure_completion"
                    ],
                }
            )
    unique = {
        legacy._node_id(row["path"], row["node"]): row for row in selected
    }
    return sorted(
        unique.values(),
        key=lambda row: (
            row["path"],
            int(row["node"].lineno),
            int(row["node"].col_offset),
        ),
    )


def _editable_node(package: Path, row: dict[str, Any]) -> dict[str, Any]:
    editable = legacy._editable_node(package, row)
    editable["backend"] = "stdlib_ast_call_binding_v477"
    editable["facts"].update(
        {
            "closure_root": row["closure_root"],
            "actual_parameter_scope": row["actual_parameter_scope"],
            "formal_parameter_scope": row["formal_parameter_scope"],
        }
    )
    return editable


def _public_obligation(row: dict[str, Any], editable: dict[str, Any]) -> dict[str, Any]:
    return {
        "family": FAMILY,
        "confidence": float(row["confidence"]),
        "path": str(row["path"]),
        "caller_symbol": str(row["caller_symbol"]),
        "closure_root": str(row["closure_root"]),
        "callee_symbol": str(row["callee_symbol"]),
        "call_line": int(row["call_line"]),
        "argument_slot": str(row["argument_slot"]),
        "callee_formal_parameter": str(row["formal_parameter"]),
        "observed_caller_parameter": str(row["actual_parameter"]),
        "actual_parameter_scope": str(row["actual_parameter_scope"]),
        "formal_parameter_scope": str(row["formal_parameter_scope"]),
        "same_name_caller_parameter_available": True,
        "evidence": list(row["evidence"]),
        "editable_node_id": str(editable["node_id"]),
        "repair_goal": (
            "Restore the complete public caller-to-callee parameter binding closure "
            "while preserving signatures and unrelated call arguments."
        ),
        "semantic_correctness_inferred": False,
    }


def _derive_candidates(
    package: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, dict[str, Any]]]:
    graph = _build_public_graph(package)
    binding_rows: list[dict[str, Any]] = []
    abstentions: list[dict[str, Any]] = []
    for caller in graph["functions"]:
        current, skipped = _binding_rows_from_graph(graph, caller)
        binding_rows.extend(current)
        abstentions.extend(skipped)
    suppressions = _suppression_map(binding_rows)
    selected = _evidence_for_bindings(binding_rows, suppressions)
    diagnostics = {
        "python_file_count": len(graph["files"]),
        "function_count": len(graph["functions"]),
        "nested_function_count": sum(
            row["parent_function_node"] is not None for row in graph["functions"]
        ),
        "resolved_caller_parameter_binding_count": len(binding_rows),
        "lexical_parameter_binding_count": sum(
            row["actual_parameter_scope"] == "lexical"
            or row["formal_parameter_scope"] == "lexical"
            for row in binding_rows
        ),
        "identity_binding_count": sum(row["is_identity"] for row in binding_rows),
        "mismatch_binding_count": sum(not row["is_identity"] for row in binding_rows),
        "high_confidence_candidate_count": len(selected),
        "resolution_abstentions": abstentions[:64],
        "resolution_abstention_count": len(abstentions),
    }
    return selected, diagnostics, suppressions


def build_public_python_call_binding_closure(
    package_root: str | Path, request_text: str = ""
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    _assert_public_package(package)
    candidates, diagnostics, suppressions = _derive_candidates(package)
    caller_count = len(
        {(str(row["path"]), str(row["caller_symbol"])) for row in candidates}
    )
    root_count = len(
        {(str(row["path"]), str(row["closure_root"])) for row in candidates}
    )
    path_count = len({str(row["path"]) for row in candidates})
    abstain_reason: str | None = None
    if not candidates:
        abstain_reason = "no_high_confidence_call_binding_closure"
    elif len(candidates) > MAX_CLOSURE_EDITS:
        abstain_reason = "call_binding_closure_exceeds_node_bound"
    elif caller_count > MAX_CALLER_SYMBOLS:
        abstain_reason = "call_binding_closure_exceeds_caller_bound"
    elif root_count > MAX_CLOSURE_ROOTS:
        abstain_reason = "call_binding_closure_exceeds_root_bound"
    elif path_count > MAX_CHANGED_PATHS:
        abstain_reason = "call_binding_closure_exceeds_path_bound"
    selected = candidates if abstain_reason is None else []
    editable = [_editable_node(package, row) for row in selected]
    obligations = [
        _public_obligation(row, node)
        for row, node in zip(selected, editable, strict=True)
    ]
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "source_scope": "public_parent_python_scripts_only",
        "package_tree_hash": _tree_hash(package),
        "localization_decision": {
            "decision": "PROPOSE" if editable else "ABSTAIN",
            "selected_family": FAMILY if editable else None,
            "confidence": min(
                (float(row["confidence"]) for row in selected), default=0.0
            ),
            "candidate_count_before_bounds": len(candidates),
            "selected_obligation_count": len(obligations),
            "editable_node_count": len(editable),
            "caller_symbol_count": caller_count if editable else 0,
            "closure_root_count": root_count if editable else 0,
            "changed_path_upper_bound": path_count if editable else 0,
            "abstain_reason": abstain_reason,
        },
        "obligations": obligations,
        "editable_nodes": editable,
        "ambiguity_suppressions": [
            {"node_id": node_id, **value} for node_id, value in suppressions.items()
        ],
        "diagnostics": diagnostics,
        "evidence_counts": dict(
            sorted(
                Counter(
                    evidence
                    for row in obligations
                    for evidence in row.get("evidence") or []
                ).items()
            )
        ),
        "edit_contract": {
            "maximum_edits": MAX_CLOSURE_EDITS,
            "maximum_caller_symbols": MAX_CALLER_SYMBOLS,
            "maximum_closure_roots": MAX_CLOSURE_ROOTS,
            "maximum_changed_paths": MAX_CHANGED_PATHS,
            "required_edit_count": len(editable),
            "atomic_closure_required": len(editable) > 1,
            "exact_name_node_binding_required": True,
            "outside_selected_nodes_preserved_by_construction": True,
            "public_signatures_must_be_preserved": True,
            "residual_call_binding_check_required": True,
            "module_aware_resolution_required": True,
            "lexical_closure_capture_required": True,
            "ambiguity_suppression_required": True,
            "semantic_correctness_inferred": False,
        },
        "request_text_consumed": False,
        "skill_markdown_consumed": False,
        "benchmark_bundled_facts_consumed": False,
        "hidden_artifacts_consumed": False,
        "task_verifier_consumed": False,
        "gold_or_oracle_consumed": False,
        "reward_consumed": False,
        "semantic_correctness_inferred": False,
        "claim_boundary": (
            "The packet reports one bounded package-local call-binding family, including "
            "module resolution, lexical captures, and atomic caller-family completion. "
            "It does not certify intended runtime semantics."
        ),
    }
    result["facts_hash"] = canonical_json_hash(result)
    return result


def _all_name_nodes(package: Path) -> list[dict[str, Any]]:
    graph = _build_public_graph(package)
    rows: list[dict[str, Any]] = []
    for function in graph["functions"]:
        source = function["file_record"]["source"]
        parents = function["file_record"]["parents"]
        for node in ast.walk(function["node"]):
            if not isinstance(node, ast.Name) or not isinstance(node.ctx, ast.Load):
                continue
            if _nearest_parent(
                node,
                parents,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
            ) is not function["node"]:
                continue
            parent = parents.get(node)
            role = (
                f"keyword:{parent.arg}"
                if isinstance(parent, ast.keyword)
                else type(parent).__name__ if parent is not None else "expression"
            )
            fake = {
                "path": function["path"],
                "caller_symbol": function["symbol"],
                "closure_root": function["closure_root"],
                "callee_symbol": "",
                "formal_parameter": "",
                "actual_parameter": node.id,
                "argument_slot": role,
                "node_role": role,
                "node": node,
                "evidence": [],
                "confidence": 0.0,
                "actual_parameter_scope": "",
                "formal_parameter_scope": "",
            }
            rows.append(_editable_node(package, fake))
    unique = {str(row["node_id"]): row for row in rows}
    return sorted(
        unique.values(),
        key=lambda row: (
            str(row["path"]),
            int(row["byte_span"]["start"]),
            str(row["node_id"]),
        ),
    )


def validate_public_python_call_binding_closure(
    facts: dict[str, Any], package_root: str | Path
) -> str:
    body = dict(facts)
    expected = str(body.pop("facts_hash", ""))
    if not expected or canonical_json_hash(body) != expected:
        raise ValueError("call_binding_closure_v4_facts_hash_invalid")
    if facts.get("schema_version") != SCHEMA_VERSION or facts.get("method") != METHOD_ID:
        raise ValueError("call_binding_closure_v4_method_invalid")
    if any(
        facts.get(field) is not False
        for field in (
            "benchmark_bundled_facts_consumed",
            "hidden_artifacts_consumed",
            "task_verifier_consumed",
            "gold_or_oracle_consumed",
            "reward_consumed",
            "semantic_correctness_inferred",
        )
    ):
        raise ValueError("call_binding_closure_v4_scope_invalid")
    package = Path(package_root).resolve()
    _assert_public_package(package)
    if facts.get("package_tree_hash") != _tree_hash(package):
        raise ValueError("call_binding_closure_v4_package_changed")
    registry = {str(row["node_id"]): row for row in _all_name_nodes(package)}
    editable = list(facts.get("editable_nodes") or [])
    obligations = list(facts.get("obligations") or [])
    if len(editable) > MAX_CLOSURE_EDITS or len(editable) != len(obligations):
        raise ValueError("call_binding_closure_v4_editable_shape_invalid")
    if len({str(row.get("node_id")) for row in editable}) != len(editable):
        raise ValueError("call_binding_closure_v4_duplicate_node")
    for row in editable:
        current = registry.get(str(row.get("node_id")))
        if current is None or current.get("node_type") != "Name":
            raise ValueError("call_binding_closure_v4_unknown_name_node")
        if str(row.get("node_sha256")) != str(current.get("node_sha256")):
            raise ValueError("call_binding_closure_v4_node_hash_mismatch")
        if str(row.get("file_sha256")) != sha256_file(package / str(row["path"])):
            raise ValueError("call_binding_closure_v4_file_hash_mismatch")
    decision = str((facts.get("localization_decision") or {}).get("decision"))
    if decision == "PROPOSE" and not editable:
        raise ValueError("call_binding_closure_v4_empty_proposal")
    if decision == "ABSTAIN" and (editable or obligations):
        raise ValueError("call_binding_closure_v4_abstain_has_editable_content")
    return expected


def build_same_package_wrong_call_binding_closure(
    package_root: str | Path, real_facts: dict[str, Any]
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    validate_public_python_call_binding_closure(real_facts, package)
    result = copy.deepcopy(real_facts)
    result.pop("facts_hash", None)
    real_nodes = list(real_facts.get("editable_nodes") or [])
    registry = _all_name_nodes(package)
    forbidden = {str(row["node_id"]) for row in real_nodes}
    selected: list[dict[str, Any]] = []
    tiers: list[str] = []
    for real in real_nodes:
        used = {str(row["node_id"]) for row in selected}
        candidates = [
            row for row in registry if str(row["node_id"]) not in forbidden | used
        ]
        same_symbol_role = [
            row
            for row in candidates
            if row.get("symbol") == real.get("symbol")
            and row.get("role") == real.get("role")
        ]
        same_symbol = [
            row for row in candidates if row.get("symbol") == real.get("symbol")
        ]
        same_path = [row for row in candidates if row.get("path") == real.get("path")]
        if same_symbol_role:
            pool, tier = same_symbol_role, "same_symbol_and_argument_role"
        elif same_symbol:
            pool, tier = same_symbol, "same_symbol"
        elif same_path:
            pool, tier = same_path, "same_path"
        else:
            pool, tier = candidates, "same_package"
        pool.sort(
            key=lambda row: (
                abs(int(row["line"]) - int(real["line"])),
                abs(
                    len(str(row["observed_source"]))
                    - len(str(real["observed_source"]))
                ),
                str(row["node_id"]),
            )
        )
        if not pool:
            raise ValueError("call_binding_closure_v4_sham_decoy_unavailable")
        selected.append(copy.deepcopy(pool[0]))
        tiers.append(tier)
    result["editable_nodes"] = selected
    result["control_metadata"] = {
        "control": "same-package-wrong-call-binding-closure-v4",
        "construction": "deterministic_shape_matched_package_local_name_derangement",
        "real_node_overlap_count": len(
            forbidden & {str(row["node_id"]) for row in selected}
        ),
        "same_editable_node_count": len(selected) == len(real_nodes),
        "shape_matching_tiers": tiers,
        "hidden_or_verifier_feedback_used": False,
    }
    result["facts_hash"] = canonical_json_hash(result)
    validate_public_python_call_binding_closure(result, package)
    return result


def residual_obligations(
    candidate_package: str | Path, frozen_facts: dict[str, Any]
) -> list[dict[str, Any]]:
    current = build_public_python_call_binding_closure(candidate_package)
    target_keys = {
        (
            str(row["path"]),
            str(row["caller_symbol"]),
            str(row["callee_symbol"]),
            str(row["callee_formal_parameter"]),
            str(row["argument_slot"]),
        )
        for row in frozen_facts.get("obligations") or []
    }
    return [
        row
        for row in current.get("obligations") or []
        if (
            str(row["path"]),
            str(row["caller_symbol"]),
            str(row["callee_symbol"]),
            str(row["callee_formal_parameter"]),
            str(row["argument_slot"]),
        )
        in target_keys
    ]


__all__ = [
    "FAMILY",
    "MAX_CLOSURE_EDITS",
    "METHOD_ID",
    "build_public_python_call_binding_closure",
    "build_same_package_wrong_call_binding_closure",
    "residual_obligations",
    "validate_public_python_call_binding_closure",
]
