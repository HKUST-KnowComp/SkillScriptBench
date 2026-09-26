from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from bvi_skill_evo.balanced_hybrid_ast_v316 import _assert_public_package, _tree_hash
from skillscriptbench.io_utils import canonical_json_hash, sha256_bytes, sha256_file


SCHEMA_VERSION = "4.98-public-runtime-python-helper-edge-closure-v1"
METHOD_ID = "public_runtime_python_helper_edge_closure_v1"
FAMILY = "call_binding_closure"
SUBFAMILY = "package_local_helper_edge"
MAX_CLOSURE_EDITS = 2
MAX_CHANGED_PATHS = 2


@dataclass(frozen=True)
class HelperDef:
    module: str
    symbol: str
    path: str
    parameters: tuple[str, ...]
    return_annotation: str
    docstring: str


@dataclass
class ParsedFile:
    path: Path
    relative: str
    source: str
    tree: ast.Module
    parents: dict[ast.AST, ast.AST]


def _python_paths(package: Path) -> list[Path]:
    scripts = package / "scripts"
    if not scripts.is_dir():
        return []
    return sorted(
        path
        for path in scripts.rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    )


def _module_names(relative: str) -> set[str]:
    path = Path(relative)
    parts = list(path.with_suffix("").parts)
    if parts and parts[0] == "scripts":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return set()
    return {".".join(parts), parts[-1]}


def _parse_package(package: Path) -> list[ParsedFile]:
    files: list[ParsedFile] = []
    for path in _python_paths(package):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            continue
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        files.append(
            ParsedFile(
                path=path,
                relative=path.relative_to(package).as_posix(),
                source=source,
                tree=tree,
                parents=parents,
            )
        )
    return files


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parents.get(current)
    return "<module>"


def _ancestors(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[ast.AST]:
    result: list[ast.AST] = []
    current = parents.get(node)
    while current is not None:
        result.append(current)
        current = parents.get(current)
    return result


def _simple_not_name(test: ast.AST) -> str | None:
    if (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and isinstance(test.operand, ast.Name)
    ):
        return test.operand.id
    return None


def _function_index(files: list[ParsedFile]) -> dict[tuple[str, str], HelperDef]:
    result: dict[tuple[str, str], HelperDef] = {}
    for file in files:
        modules = _module_names(file.relative)
        for node in file.tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            positional = [*node.args.posonlyargs, *node.args.args]
            parameters = tuple(arg.arg for arg in positional)
            annotation = ast.unparse(node.returns) if node.returns is not None else ""
            helper = HelperDef(
                module=sorted(modules)[0] if modules else "",
                symbol=node.name,
                path=file.relative,
                parameters=parameters,
                return_annotation=annotation,
                docstring=ast.get_docstring(node) or "",
            )
            for module in modules:
                result[(module, node.name)] = helper
    return result


def _imported_helpers(
    file: ParsedFile, index: dict[tuple[str, str], HelperDef]
) -> dict[str, HelperDef]:
    result: dict[str, HelperDef] = {}
    for node in ast.walk(file.tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        modules = {node.module, node.module.rsplit(".", 1)[-1]}
        for alias in node.names:
            matches = {
                index[(module, alias.name)]
                for module in modules
                if (module, alias.name) in index
            }
            if len(matches) == 1:
                result[alias.asname or alias.name] = matches.pop()
    return result


def _helper_calls(
    files: list[ParsedFile], index: dict[tuple[str, str], HelperDef]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for file in files:
        imported = _imported_helpers(file, index)
        for call in [node for node in ast.walk(file.tree) if isinstance(node, ast.Call)]:
            if not isinstance(call.func, ast.Name) or call.func.id not in imported:
                continue
            helper = imported[call.func.id]
            parent = file.parents.get(call)
            target_name = None
            if isinstance(parent, ast.Assign) and len(parent.targets) == 1 and isinstance(parent.targets[0], ast.Name):
                target_name = parent.targets[0].id
            rows.append(
                {
                    "file": file,
                    "call": call,
                    "helper": helper,
                    "alias": call.func.id,
                    "target_name": target_name,
                    "argument_names": [arg.id for arg in call.args if isinstance(arg, ast.Name)],
                    "source": ast.get_source_segment(file.source, call),
                    "function_symbol": _enclosing_function(call, file.parents),
                }
            )
    return rows


def _editable_expr(
    package: Path,
    file: ParsedFile,
    node: ast.expr,
    *,
    role: str,
    facts: dict[str, Any],
) -> dict[str, Any]:
    observed = ast.get_source_segment(file.source, node)
    if observed is None:
        raise ValueError("helper_edge_source_segment_missing")
    encoded = observed.encode("utf-8")
    node_type = type(node).__name__
    node_id = f"{file.relative}:{node.lineno}:{node.col_offset}:{node_type}"
    return {
        "backend": "stdlib_ast_package_helper_edge_v498",
        "language": "python",
        "path": file.relative,
        "symbol": _enclosing_function(node, file.parents),
        "node_id": node_id,
        "site_id": node_id,
        "node_type": node_type,
        "role": role,
        "line": int(node.lineno),
        "column": int(node.col_offset),
        "end_line": int(node.end_lineno),
        "end_column": int(node.end_col_offset),
        "span": {
            "start_line": int(node.lineno),
            "start_column": int(node.col_offset),
            "end_line": int(node.end_lineno),
            "end_column": int(node.end_col_offset),
        },
        "observed_source": observed,
        "node_sha256": sha256_bytes(encoded),
        "source_sha256": sha256_bytes(encoded),
        "file_sha256": sha256_file(file.path),
        "facts": facts,
    }


def _sibling_fallback_obligations(
    package: Path,
    files: list[ParsedFile],
    index: dict[tuple[str, str], HelperDef],
    helper_calls: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    result: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for file in files:
        imported = _imported_helpers(file, index)
        for assign in [node for node in ast.walk(file.tree) if isinstance(node, ast.Assign)]:
            if not (
                len(assign.targets) == 1
                and isinstance(assign.targets[0], ast.Name)
                and isinstance(assign.value, ast.Name)
            ):
                continue
            target_name = assign.targets[0].id
            value_name = assign.value.id
            ancestors = _ancestors(assign, file.parents)
            if not any(
                isinstance(parent, ast.If) and _simple_not_name(parent.test) == target_name
                for parent in ancestors
            ):
                continue
            for alias, helper in imported.items():
                if len(helper.parameters) != 1:
                    continue
                analogs = [
                    row
                    for row in helper_calls
                    if row["helper"].path == helper.path
                    and row["helper"].symbol == helper.symbol
                    and row["file"].relative != file.relative
                    and row["target_name"] == target_name
                    and row["argument_names"] == [value_name]
                ]
                if not analogs:
                    continue
                analog = sorted(analogs, key=lambda row: row["file"].relative)[0]
                editable = _editable_expr(
                    package,
                    file,
                    assign.value,
                    role="fallback_assignment_rhs",
                    facts={
                        "candidate_subtype": "sibling_fallback_helper_edge",
                        "confidence": 0.998,
                        "expected_helper_symbol": alias,
                        "expected_argument_source": value_name,
                    },
                )
                obligation = {
                    "family": FAMILY,
                    "subfamily": SUBFAMILY,
                    "subtype": "sibling_fallback_helper_edge",
                    "confidence": 0.998,
                    "path": file.relative,
                    "function_symbol": editable["symbol"],
                    "editable_node_id": editable["node_id"],
                    "edge_role": "fallback_assignment_rhs",
                    "expected_helper_module": helper.module,
                    "expected_helper_path": helper.path,
                    "expected_helper_symbol": alias,
                    "expected_helper_formal_parameter": helper.parameters[0],
                    "expected_argument_source": value_name,
                    "helper_return_annotation": helper.return_annotation,
                    "public_sibling_analog": {
                        "path": analog["file"].relative,
                        "function_symbol": analog["function_symbol"],
                        "call_source": analog["source"],
                    },
                    "evidence": [
                        "same_package_sibling_uses_imported_helper_in_isomorphic_fallback",
                        "raw_rhs_bypasses_helper_return_transformation",
                        "target_and_argument_names_match_sibling_edge",
                    ],
                    "repair_goal": "Restore the package-local helper edge while preserving the raw value as its argument.",
                    "semantic_correctness_inferred": False,
                }
                result.append((obligation, editable))
    return result


def _is_missing_detail_helper(helper: HelperDef) -> bool:
    name = helper.symbol.casefold()
    annotation = helper.return_annotation.casefold()
    doc = helper.docstring.casefold()
    return (
        len(helper.parameters) == 1
        and "missing" in name
        and "detail" in name
        and ("str" in annotation or "detail string" in doc)
    )


def _raw_detail_exprs(branch: ast.If) -> list[tuple[ast.expr, str]]:
    rows: list[tuple[ast.expr, str]] = []
    for node in ast.walk(ast.Module(body=branch.body, type_ignores=[])):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple) and len(node.value.elts) >= 2:
            value = node.value.elts[-1]
            if isinstance(value, (ast.Name, ast.Constant)) and (
                isinstance(value, ast.Name) or isinstance(value.value, str)
            ):
                rows.append((value, "structured_return_detail"))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "add"
            and len(node.args) >= 4
            and any(isinstance(arg, ast.Constant) and arg.value is False for arg in node.args)
        ):
            value = node.args[-1]
            if isinstance(value, (ast.Name, ast.Constant)) and (
                isinstance(value, ast.Name) or isinstance(value.value, str)
            ):
                rows.append((value, "structured_check_detail_argument"))
    return rows


def _missing_detail_obligations(
    package: Path,
    files: list[ParsedFile],
    index: dict[tuple[str, str], HelperDef],
    helper_calls: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    result: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for file in files:
        imported = _imported_helpers(file, index)
        for alias, helper in imported.items():
            if not _is_missing_detail_helper(helper):
                continue
            correct_uses = sum(
                row["helper"].path == helper.path
                and row["helper"].symbol == helper.symbol
                and row["file"].relative == file.relative
                for row in helper_calls
            )
            for branch in [node for node in ast.walk(file.tree) if isinstance(node, ast.If)]:
                state_name = _simple_not_name(branch.test)
                if state_name is None or "binary" not in state_name.casefold():
                    continue
                for raw, role in _raw_detail_exprs(branch):
                    observed = ast.get_source_segment(file.source, raw)
                    if observed is None:
                        continue
                    editable = _editable_expr(
                        package,
                        file,
                        raw,
                        role=role,
                        facts={
                            "candidate_subtype": "missing_detail_helper_edge",
                            "confidence": 0.997 if correct_uses else 0.99,
                            "expected_helper_symbol": alias,
                            "expected_argument_source": observed,
                        },
                    )
                    obligation = {
                        "family": FAMILY,
                        "subfamily": SUBFAMILY,
                        "subtype": "missing_detail_helper_edge",
                        "confidence": 0.997 if correct_uses else 0.99,
                        "path": file.relative,
                        "function_symbol": editable["symbol"],
                        "editable_node_id": editable["node_id"],
                        "edge_role": role,
                        "expected_helper_module": helper.module,
                        "expected_helper_path": helper.path,
                        "expected_helper_symbol": alias,
                        "expected_helper_formal_parameter": helper.parameters[0],
                        "expected_argument_source": observed,
                        "helper_return_annotation": helper.return_annotation,
                        "public_same_file_helper_call_count": correct_uses,
                        "evidence": [
                            "package_local_helper_declares_uniform_missing_detail_contract",
                            "missing_binary_branch_emits_raw_name_or_literal",
                            "helper_accepts_the_observed_raw_value_and_returns_detail_text",
                        ],
                        "repair_goal": "Restore the package-local detail helper edge around the existing raw value.",
                        "semantic_correctness_inferred": False,
                    }
                    result.append((obligation, editable))
    return result


def _registry(files: list[ParsedFile], package: Path) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for file in files:
        for node in ast.walk(file.tree):
            if not isinstance(node, (ast.Name, ast.Constant)):
                continue
            if isinstance(node, ast.Constant) and not isinstance(node.value, str):
                continue
            observed = ast.get_source_segment(file.source, node)
            if observed is None or len(observed.encode("utf-8")) > 256:
                continue
            editable = _editable_expr(
                package,
                file,
                node,
                role="package_expression_decoy",
                facts={"candidate_subtype": "", "confidence": 0.0},
            )
            rows[str(editable["node_id"])] = editable
    return list(rows.values())


def _scan_package_uncached(package: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    files = _parse_package(package)
    index = _function_index(files)
    calls = _helper_calls(files, index)
    pairs = [
        *_sibling_fallback_obligations(package, files, index, calls),
        *_missing_detail_obligations(package, files, index, calls),
    ]
    obligations: dict[str, dict[str, Any]] = {}
    editable: dict[str, dict[str, Any]] = {}
    for obligation, node in pairs:
        node_id = str(node["node_id"])
        obligations[node_id] = obligation
        editable[node_id] = node
    registry = {str(row["node_id"]): row for row in _registry(files, package)}
    registry.update(editable)
    return list(obligations.values()), list(registry.values())


@lru_cache(maxsize=8)
def _scan_package_cached(
    package_path: str, package_tree_hash: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    del package_tree_hash
    return _scan_package_uncached(Path(package_path))


def _scan_package(package: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    resolved = package.resolve()
    obligations, registry = _scan_package_cached(str(resolved), _tree_hash(resolved))
    return copy.deepcopy(obligations), copy.deepcopy(registry)


def _base_facts(package: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "source_scope": "public_parent_python_scripts_only",
        "package_tree_hash": _tree_hash(package),
        "request_text_consumed": False,
        "skill_markdown_consumed": False,
        "benchmark_bundled_facts_consumed": False,
        "hidden_artifacts_consumed": False,
        "task_verifier_consumed": False,
        "gold_or_oracle_consumed": False,
        "reward_consumed": False,
        "semantic_correctness_inferred": False,
        "task_id_specific_rule_used": False,
        "claim_boundary": (
            "The packet reports bounded package-local helper-edge discrepancies from public "
            "definitions, imports, sibling calls, and typed sinks. It does not certify semantic correctness."
        ),
    }


def _finalize(payload: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    result.pop("facts_hash", None)
    result["facts_hash"] = canonical_json_hash(result)
    return result


def build_public_python_helper_edge_closure(
    package_root: str | Path, request_text: str = ""
) -> dict[str, Any]:
    del request_text
    package = Path(package_root).resolve()
    _assert_public_package(package)
    obligations, registry = _scan_package(package)
    obligations.sort(key=lambda row: (row["path"], row["function_symbol"], row["editable_node_id"]))
    abstain_reason: str | None = None
    if not obligations:
        abstain_reason = "no_high_confidence_package_local_helper_edge"
    elif len(obligations) > MAX_CLOSURE_EDITS:
        abstain_reason = "helper_edge_closure_exceeds_node_bound"
    selected = [] if abstain_reason else obligations
    by_id = {str(row["node_id"]): row for row in registry}
    editable = [by_id[str(row["editable_node_id"])] for row in selected]
    facts = {
        **_base_facts(package),
        "diagnostics": {
            "python_file_count": len(_python_paths(package)),
            "registry_node_count": len(registry),
            "high_confidence_obligation_count": len(obligations),
        },
        "localization_decision": {
            "decision": "ABSTAIN" if abstain_reason else "PROPOSE",
            "abstain_reason": abstain_reason,
            "selected_family": FAMILY if selected else None,
            "selected_subfamily": SUBFAMILY if selected else None,
            "selected_obligation_count": len(selected),
            "editable_node_count": len(editable),
            "confidence": min((float(row["confidence"]) for row in selected), default=0.0),
        },
        "edit_contract": {
            "atomic_closure_required": True,
            "exact_node_binding_required": True,
            "maximum_edits": MAX_CLOSURE_EDITS,
            "maximum_changed_paths": MAX_CHANGED_PATHS,
            "replacement_must_be_direct_expected_helper_call": True,
            "existing_raw_value_must_be_preserved_as_argument": True,
            "public_signatures_must_be_preserved": True,
            "residual_helper_edge_check_required": True,
            "semantic_correctness_inferred": False,
        },
        "obligations": selected,
        "editable_nodes": editable,
    }
    return _finalize(facts)


def build_same_package_wrong_helper_edge_closure(
    package_root: str | Path, real_facts: dict[str, Any]
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    validate_public_python_helper_edge_closure(real_facts, package)
    if real_facts["localization_decision"]["decision"] != "PROPOSE":
        raise ValueError("helper_edge_sham_requires_proposal")
    _, registry = _scan_package(package)
    real_ids = {str(row["node_id"]) for row in real_facts["editable_nodes"]}
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    for real in real_facts["editable_nodes"]:
        candidates = [
            row
            for row in registry
            if str(row["node_id"]) not in real_ids | used
            and row["node_type"] == real["node_type"]
        ]
        candidates.sort(
            key=lambda row: (
                row["path"] != real["path"],
                row["symbol"] != real["symbol"],
                row["observed_source"] != real["observed_source"],
                abs(len(str(row["observed_source"])) - len(str(real["observed_source"]))),
                str(row["node_id"]),
            )
        )
        if not candidates:
            raise ValueError("helper_edge_sham_decoy_unavailable")
        selected.append(candidates[0])
        used.add(str(candidates[0]["node_id"]))
    obligations = []
    for source, decoy in zip(real_facts["obligations"], selected):
        row = copy.deepcopy(source)
        row["path"] = decoy["path"]
        row["function_symbol"] = decoy["symbol"]
        row["editable_node_id"] = decoy["node_id"]
        row["edge_role"] = "same_package_wrong_expression_control"
        obligations.append(row)
    result = {
        **_base_facts(package),
        "diagnostics": copy.deepcopy(real_facts["diagnostics"]),
        "localization_decision": copy.deepcopy(real_facts["localization_decision"]),
        "edit_contract": copy.deepcopy(real_facts["edit_contract"]),
        "obligations": obligations,
        "editable_nodes": selected,
    }
    return _finalize(result)


def validate_public_python_helper_edge_closure(
    facts: dict[str, Any], package_root: str | Path
) -> str:
    body = dict(facts)
    expected = str(body.pop("facts_hash", ""))
    if not expected or canonical_json_hash(body) != expected:
        raise ValueError("helper_edge_facts_hash_invalid")
    if facts.get("schema_version") != SCHEMA_VERSION or facts.get("method") != METHOD_ID:
        raise ValueError("helper_edge_method_invalid")
    if any(
        facts.get(field) is not False
        for field in (
            "benchmark_bundled_facts_consumed",
            "hidden_artifacts_consumed",
            "task_verifier_consumed",
            "gold_or_oracle_consumed",
            "reward_consumed",
            "semantic_correctness_inferred",
            "task_id_specific_rule_used",
        )
    ):
        raise ValueError("helper_edge_scope_invalid")
    package = Path(package_root).resolve()
    _assert_public_package(package)
    if facts.get("package_tree_hash") != _tree_hash(package):
        raise ValueError("helper_edge_package_changed")
    editable = list(facts.get("editable_nodes") or [])
    obligations = list(facts.get("obligations") or [])
    if len(editable) != len(obligations) or len(editable) > MAX_CLOSURE_EDITS:
        raise ValueError("helper_edge_editable_shape_invalid")
    if len({str(row.get("node_id")) for row in editable}) != len(editable):
        raise ValueError("helper_edge_duplicate_node")
    _, registry_rows = _scan_package(package)
    registry = {str(row["node_id"]): row for row in registry_rows}
    for row in editable:
        current = registry.get(str(row.get("node_id")))
        if current is None or row.get("node_type") not in {"Name", "Constant"}:
            raise ValueError("helper_edge_unknown_expression_node")
        if str(row.get("node_sha256")) != str(current.get("node_sha256")):
            raise ValueError("helper_edge_node_hash_mismatch")
        if str(row.get("file_sha256")) != sha256_file(package / str(row["path"])):
            raise ValueError("helper_edge_file_hash_mismatch")
    decision = str((facts.get("localization_decision") or {}).get("decision"))
    if decision == "PROPOSE" and not editable:
        raise ValueError("helper_edge_empty_proposal")
    if decision == "ABSTAIN" and (editable or obligations):
        raise ValueError("helper_edge_abstain_has_editable_content")
    return expected


def residual_obligations(
    candidate_package: str | Path, frozen_facts: dict[str, Any]
) -> list[dict[str, Any]]:
    current = build_public_python_helper_edge_closure(candidate_package)
    target = {
        (
            str(row["path"]),
            str(row["function_symbol"]),
            str(row["subtype"]),
            str(row["expected_helper_symbol"]),
            str(row["edge_role"]),
        )
        for row in frozen_facts.get("obligations") or []
    }
    return [
        row
        for row in current.get("obligations") or []
        if (
            str(row["path"]),
            str(row["function_symbol"]),
            str(row["subtype"]),
            str(row["expected_helper_symbol"]),
            str(row["edge_role"]),
        )
        in target
    ]


__all__ = [
    "FAMILY",
    "MAX_CHANGED_PATHS",
    "MAX_CLOSURE_EDITS",
    "METHOD_ID",
    "SUBFAMILY",
    "build_public_python_helper_edge_closure",
    "build_same_package_wrong_helper_edge_closure",
    "residual_obligations",
    "validate_public_python_helper_edge_closure",
]
