from __future__ import annotations

import ast
import copy
import io
import tokenize
from pathlib import Path
from typing import Any, Iterable

from bvi_skill_evo.balanced_hybrid_ast_v316 import _assert_public_package, _tree_hash
from skillscriptbench.io_utils import canonical_json_hash, sha256_bytes, sha256_file


SCHEMA_VERSION = "4.89-public-runtime-python-behavior-path-closure-v1"
METHOD_ID = "public_runtime_python_behavior_path_closure_v1"
FAMILY = "behavior_path_closure"
MAX_CLOSURE_EDITS = 2
MAX_CHANGED_PATHS = 2


def _python_paths(package: Path) -> list[Path]:
    scripts = package / "scripts"
    if not scripts.is_dir():
        return []
    return sorted(
        path
        for path in scripts.rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    )


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def _ancestor(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
    kinds: tuple[type[ast.AST], ...],
) -> ast.AST | None:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, kinds):
            return current
        current = parents.get(current)
    return None


def _enclosing_function(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    value = _ancestor(node, parents, (ast.FunctionDef, ast.AsyncFunctionDef))
    return value if isinstance(value, (ast.FunctionDef, ast.AsyncFunctionDef)) else None


def _exception_names(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return set()
    values = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    names: set[str] = set()
    for value in values:
        if isinstance(value, ast.Name):
            names.add(value.id)
        elif isinstance(value, ast.Attribute):
            names.add(value.attr)
    return names


def _structured_annotation(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    if function.returns is None:
        return False
    annotation = ast.unparse(function.returns).casefold()
    return any(
        token in annotation
        for token in ("list", "tuple", "dict", "mapping", "sequence", "set[")
    )


def _terminal_statement(statement: ast.stmt) -> bool:
    return isinstance(statement, (ast.Break, ast.Continue, ast.Return, ast.Raise))


def _node_bytes(source: str, node: ast.AST) -> bytes:
    segment = ast.get_source_segment(source, node)
    if segment is None:
        raise ValueError("behavior_path_node_source_unavailable")
    return segment.encode("utf-8")


def _function_name(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> str:
    function = _enclosing_function(node, parents)
    return function.name if function is not None else "<module>"


def _editable_ast_node(
    package: Path,
    path: Path,
    source: str,
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
    *,
    role: str,
    facts: dict[str, Any],
) -> dict[str, Any]:
    relative = path.relative_to(package).as_posix()
    node_type = type(node).__name__
    node_id = f"{relative}:{node.lineno}:{node.col_offset}:{node_type}"
    observed = ast.get_source_segment(source, node)
    if observed is None:
        raise ValueError("behavior_path_ast_segment_missing")
    return {
        "backend": "stdlib_ast_behavior_path_v489",
        "language": "python",
        "path": relative,
        "symbol": _function_name(node, parents),
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
        "node_sha256": sha256_bytes(_node_bytes(source, node)),
        "source_sha256": sha256_bytes(_node_bytes(source, node)),
        "file_sha256": sha256_file(path),
        "facts": facts,
    }


def _bool_operator_token(
    package: Path,
    path: Path,
    source: str,
    node: ast.BoolOp,
    parents: dict[ast.AST, ast.AST],
    *,
    role: str,
    facts: dict[str, Any],
) -> dict[str, Any] | None:
    if len(node.values) != 2:
        return None
    operator = "and" if isinstance(node.op, ast.And) else "or"
    left, right = node.values
    tokens = [
        token
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.NAME
        and token.string == operator
        and (token.start[0], token.start[1])
        >= (int(left.end_lineno), int(left.end_col_offset))
        and (token.end[0], token.end[1]) <= (int(right.lineno), int(right.col_offset))
    ]
    if len(tokens) != 1:
        return None
    token = tokens[0]
    relative = path.relative_to(package).as_posix()
    node_type = "BoolOpOperator"
    node_id = f"{relative}:{token.start[0]}:{token.start[1]}:{node_type}"
    encoded = operator.encode("utf-8")
    return {
        "backend": "stdlib_ast_token_behavior_path_v489",
        "language": "python",
        "path": relative,
        "symbol": _function_name(node, parents),
        "node_id": node_id,
        "site_id": node_id,
        "node_type": node_type,
        "role": role,
        "line": token.start[0],
        "column": token.start[1],
        "end_line": token.end[0],
        "end_column": token.end[1],
        "span": {
            "start_line": token.start[0],
            "start_column": token.start[1],
            "end_line": token.end[0],
            "end_column": token.end[1],
        },
        "observed_source": operator,
        "node_sha256": sha256_bytes(encoded),
        "source_sha256": sha256_bytes(encoded),
        "file_sha256": sha256_file(path),
        "facts": facts,
    }


def _same_expression(left: ast.AST, right: ast.AST) -> bool:
    return ast.dump(left, include_attributes=False) == ast.dump(
        right, include_attributes=False
    )


def _startswith_equality_contradiction(node: ast.BoolOp) -> bool:
    if not isinstance(node.op, ast.And) or len(node.values) != 2:
        return False
    for call, compare in (
        (node.values[0], node.values[1]),
        (node.values[1], node.values[0]),
    ):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "startswith"
            and len(call.args) == 1
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
            and isinstance(compare, ast.Compare)
            and len(compare.ops) == 1
            and isinstance(compare.ops[0], ast.Eq)
            and len(compare.comparators) == 1
            and isinstance(compare.comparators[0], ast.Constant)
            and isinstance(compare.comparators[0].value, str)
            and _same_expression(call.func.value, compare.left)
        ):
            continue
        prefix = call.args[0].value
        exact = compare.comparators[0].value
        if not exact.startswith(prefix):
            return True
    return False


class _ShapeNormalizer(ast.NodeTransformer):
    def visit_Name(self, node: ast.Name) -> ast.AST:
        return ast.copy_location(ast.Name(id="NAME", ctx=copy.deepcopy(node.ctx)), node)

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        value = self.visit(node.value)
        return ast.copy_location(ast.Attribute(value=value, attr="ATTR", ctx=node.ctx), node)

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        value: Any
        if isinstance(node.value, str):
            value = "STRING"
        elif isinstance(node.value, (int, float, complex)):
            value = 0
        else:
            value = node.value
        return ast.copy_location(ast.Constant(value=value), node)

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        values = [self.visit(value) for value in node.values]
        return ast.copy_location(ast.BoolOp(op=ast.And(), values=values), node)


def _normalized_shape(node: ast.AST) -> str:
    normalized = _ShapeNormalizer().visit(copy.deepcopy(node))
    return ast.dump(normalized, include_attributes=False)


def _name_load_count(node: ast.AST, name: str) -> int:
    return sum(
        isinstance(value, ast.Name) and isinstance(value.ctx, ast.Load) and value.id == name
        for value in ast.walk(node)
    )


def _sibling_guard_asymmetry(previous: ast.If, current: ast.If) -> bool:
    if not (
        isinstance(previous.test, ast.BoolOp)
        and isinstance(previous.test.op, ast.And)
        and len(previous.test.values) == 2
        and isinstance(current.test, ast.BoolOp)
        and isinstance(current.test.op, ast.Or)
        and len(current.test.values) == 2
        and previous.body
        and current.body
        and _terminal_statement(previous.body[-1])
        and _terminal_statement(current.body[-1])
        and isinstance(previous.test.values[0], ast.Name)
        and isinstance(current.test.values[0], ast.Name)
    ):
        return False
    previous_name = previous.test.values[0].id
    current_name = current.test.values[0].id
    if _name_load_count(previous.test.values[1], previous_name) == 0:
        return False
    if _name_load_count(current.test.values[1], current_name) == 0:
        return False
    return _normalized_shape(previous.test) == _normalized_shape(current.test)


def _direct_name_operand(node: ast.BoolOp) -> str | None:
    names = [value.id for value in node.values if isinstance(value, ast.Name)]
    return names[0] if len(names) == 1 else None


def _dominated_followup(previous: ast.If, current: ast.If) -> bool:
    if not (
        isinstance(previous.test, ast.BoolOp)
        and isinstance(previous.test.op, ast.Or)
        and previous.body
        and _terminal_statement(previous.body[-1])
    ):
        return False
    name = _direct_name_operand(previous.test)
    return name is not None and isinstance(current.test, ast.Name) and current.test.id == name


def _statement_lists(tree: ast.AST) -> Iterable[list[ast.stmt]]:
    for node in ast.walk(tree):
        for _, value in ast.iter_fields(node):
            if isinstance(value, list) and value and all(
                isinstance(item, ast.stmt) for item in value
            ):
                yield value


def _scan_file(package: Path, path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    parents = _parents(tree)
    obligations: list[dict[str, Any]] = []
    registry: list[dict[str, Any]] = []

    for handler in [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]:
        if not (
            len(handler.body) == 1
            and isinstance(handler.body[0], ast.Raise)
            and handler.body[0].exc is None
            and handler.body[0].cause is None
        ):
            continue
        raise_node = handler.body[0]
        function = _enclosing_function(handler, parents)
        caught = _exception_names(handler)
        parse_error = any(
            name.endswith("DecodeError") or name in {"ValueError", "ParseError"}
            for name in caught
        )
        loop = _ancestor(handler, parents, (ast.For, ast.AsyncFor, ast.While))
        subtype = ""
        confidence = 0.0
        evidence: list[str] = []
        if loop is not None and parse_error and any(
            isinstance(node, ast.Return) for node in ast.walk(loop)
        ):
            subtype = "loop_recovery_terminates_search"
            confidence = 0.995
            evidence = [
                "bare_reraise_inside_search_loop",
                "parse_failure_prevents_later_candidates",
                "loop_contains_success_return",
            ]
        elif (
            loop is None
            and parse_error
            and function is not None
            and _structured_annotation(function)
            and any(isinstance(node, ast.Return) for node in ast.walk(function))
        ):
            subtype = "structured_error_channel_bypassed"
            confidence = 0.98
            evidence = [
                "bare_reraise_in_structured_return_function",
                "parse_failure_bypasses_declared_return_channel",
            ]
        editable = _editable_ast_node(
            package,
            path,
            source,
            raise_node,
            parents,
            role="recovery_statement",
            facts={
                "caught_exceptions": sorted(caught),
                "inside_loop": loop is not None,
                "candidate_subtype": subtype,
                "confidence": confidence,
            },
        )
        registry.append(editable)
        if subtype:
            obligations.append(
                {
                    "family": FAMILY,
                    "subtype": subtype,
                    "confidence": confidence,
                    "path": editable["path"],
                    "function_symbol": editable["symbol"],
                    "editable_node_id": editable["node_id"],
                    "evidence": evidence,
                    "repair_goal": (
                        "Restore the interrupted recovery path without changing the public "
                        "interface or unrelated control flow."
                    ),
                    "semantic_correctness_inferred": False,
                }
            )

    registered_raise_ids = {str(row["node_id"]) for row in registry}
    for raise_node in [node for node in ast.walk(tree) if isinstance(node, ast.Raise)]:
        editable = _editable_ast_node(
            package,
            path,
            source,
            raise_node,
            parents,
            role="raise_statement",
            facts={"candidate_subtype": "", "confidence": 0.0},
        )
        if str(editable["node_id"]) not in registered_raise_ids:
            registry.append(editable)
            registered_raise_ids.add(str(editable["node_id"]))

    registered_control_ids = {str(row["node_id"]) for row in registry}
    for control_node in [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Return, ast.Break, ast.Continue))
    ]:
        editable = _editable_ast_node(
            package,
            path,
            source,
            control_node,
            parents,
            role="control_transfer_statement",
            facts={"candidate_subtype": "", "confidence": 0.0},
        )
        if str(editable["node_id"]) not in registered_control_ids:
            registry.append(editable)
            registered_control_ids.add(str(editable["node_id"]))

    bool_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.BoolOp)]
    proposed_bool_ids: set[str] = set()
    for node in bool_nodes:
        token = _bool_operator_token(
            package,
            path,
            source,
            node,
            parents,
            role="boolean_connector",
            facts={"candidate_subtype": "", "confidence": 0.0},
        )
        if token is not None:
            registry.append(token)
        if token is None or not _startswith_equality_contradiction(node):
            continue
        token["facts"] = {
            "candidate_subtype": "unsatisfiable_string_guard",
            "confidence": 0.995,
        }
        proposed_bool_ids.add(str(token["node_id"]))
        obligations.append(
            {
                "family": FAMILY,
                "subtype": "unsatisfiable_string_guard",
                "confidence": 0.995,
                "path": token["path"],
                "function_symbol": token["symbol"],
                "editable_node_id": token["node_id"],
                "evidence": [
                    "startswith_and_exact_equality_are_mutually_exclusive",
                    "branch_condition_is_unsatisfiable",
                ],
                "repair_goal": "Restore a satisfiable branch guard while preserving its predicates.",
                "semantic_correctness_inferred": False,
            }
        )

    token_by_position = {
        (str(row["path"]), int(row["line"]), int(row["column"])): row
        for row in registry
        if row["node_type"] == "BoolOpOperator"
    }
    for statements in _statement_lists(tree):
        for previous, current in zip(statements, statements[1:]):
            if not isinstance(previous, ast.If) or not isinstance(current, ast.If):
                continue
            subtype = ""
            confidence = 0.0
            evidence: list[str] = []
            target: ast.BoolOp | None = None
            if _sibling_guard_asymmetry(previous, current):
                target = current.test
                subtype = "sibling_guard_connector_asymmetry"
                confidence = 0.985
                evidence = [
                    "adjacent_terminal_guards_share_structure",
                    "one_guard_connector_breaks_sibling_shape",
                    "truthy_configuration_short_circuits_quantified_check",
                ]
            elif _dominated_followup(previous, current):
                target = previous.test
                subtype = "guard_dominates_followup_branch"
                confidence = 0.99
                evidence = [
                    "terminal_or_guard_contains_followup_guard",
                    "following_branch_is_unreachable_for_shared_state",
                ]
            if target is None:
                continue
            fresh = _bool_operator_token(
                package,
                path,
                source,
                target,
                parents,
                role="boolean_connector",
                facts={"candidate_subtype": subtype, "confidence": confidence},
            )
            if fresh is None or str(fresh["node_id"]) in proposed_bool_ids:
                continue
            key = (str(fresh["path"]), int(fresh["line"]), int(fresh["column"]))
            token = token_by_position[key]
            token["facts"] = dict(fresh["facts"])
            proposed_bool_ids.add(str(token["node_id"]))
            obligations.append(
                {
                    "family": FAMILY,
                    "subtype": subtype,
                    "confidence": confidence,
                    "path": token["path"],
                    "function_symbol": token["symbol"],
                    "editable_node_id": token["node_id"],
                    "evidence": evidence,
                    "repair_goal": (
                        "Restore the local branch-reachability invariant while preserving "
                        "the original predicates and public behavior outside this guard."
                    ),
                    "semantic_correctness_inferred": False,
                }
            )
    return obligations, registry


def _scan_package(package: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    obligations: list[dict[str, Any]] = []
    registry: list[dict[str, Any]] = []
    for path in _python_paths(package):
        try:
            found, nodes = _scan_file(package, path)
        except (SyntaxError, UnicodeDecodeError, tokenize.TokenError):
            continue
        obligations.extend(found)
        registry.extend(nodes)
    by_id = {str(row["node_id"]): row for row in registry}
    unique_obligations = {
        str(row["editable_node_id"]): row for row in obligations
    }
    return list(unique_obligations.values()), list(by_id.values())


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
            "The packet reports bounded public control-flow reachability discrepancies. "
            "It does not certify replacement semantics or end-to-end correctness."
        ),
    }


def _finalize(payload: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    result.pop("facts_hash", None)
    result["facts_hash"] = canonical_json_hash(result)
    return result


def build_public_python_behavior_path_closure(
    package_root: str | Path, request_text: str = ""
) -> dict[str, Any]:
    del request_text
    package = Path(package_root).resolve()
    _assert_public_package(package)
    obligations, registry = _scan_package(package)
    obligations.sort(
        key=lambda row: (
            str(row["path"]),
            str(row["function_symbol"]),
            str(row["editable_node_id"]),
        )
    )
    abstain_reason: str | None = None
    if not obligations:
        abstain_reason = "no_high_confidence_behavior_path_discrepancy"
    elif len(obligations) > MAX_CLOSURE_EDITS:
        abstain_reason = "behavior_path_closure_exceeds_node_bound"
    selected = [] if abstain_reason else obligations
    node_by_id = {str(row["node_id"]): row for row in registry}
    editable = [node_by_id[str(row["editable_node_id"])] for row in selected]
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
            "selected_obligation_count": len(selected),
            "editable_node_count": len(editable),
            "confidence": min(
                (float(row["confidence"]) for row in selected), default=0.0
            ),
        },
        "edit_contract": {
            "atomic_closure_required": True,
            "exact_node_binding_required": True,
            "maximum_edits": MAX_CLOSURE_EDITS,
            "maximum_changed_paths": MAX_CHANGED_PATHS,
            "outside_selected_nodes_preserved_by_construction": True,
            "public_signatures_must_be_preserved": True,
            "residual_behavior_path_check_required": True,
            "semantic_correctness_inferred": False,
        },
        "obligations": selected,
        "editable_nodes": editable,
    }
    return _finalize(facts)


def build_same_package_wrong_behavior_path_closure(
    package_root: str | Path, real_facts: dict[str, Any]
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    validate_public_python_behavior_path_closure(real_facts, package)
    if real_facts["localization_decision"]["decision"] != "PROPOSE":
        raise ValueError("behavior_path_sham_requires_proposal")
    _, registry = _scan_package(package)
    real_ids = {str(row["node_id"]) for row in real_facts["editable_nodes"]}
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    for real in real_facts["editable_nodes"]:
        allowed_types = {str(real["node_type"])}
        if real["node_type"] == "Raise":
            allowed_types.update({"Return", "Break", "Continue"})
        candidates = [
            row
            for row in registry
            if str(row["node_id"]) not in real_ids | used
            and row["node_type"] in allowed_types
        ]
        candidates.sort(
            key=lambda row: (
                row["node_type"] != real["node_type"],
                row["path"] != real["path"],
                row["symbol"] != real["symbol"],
                row["observed_source"] != real["observed_source"],
                str(row["node_id"]),
            )
        )
        if not candidates:
            raise ValueError("behavior_path_sham_decoy_unavailable")
        selected.append(candidates[0])
        used.add(str(candidates[0]["node_id"]))

    obligations = []
    for source, decoy in zip(real_facts["obligations"], selected):
        row = copy.deepcopy(source)
        row["path"] = decoy["path"]
        row["function_symbol"] = decoy["symbol"]
        row["editable_node_id"] = decoy["node_id"]
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


def _registry(package: Path) -> dict[str, dict[str, Any]]:
    _, rows = _scan_package(package)
    return {str(row["node_id"]): row for row in rows}


def validate_public_python_behavior_path_closure(
    facts: dict[str, Any], package_root: str | Path
) -> str:
    body = dict(facts)
    expected = str(body.pop("facts_hash", ""))
    if not expected or canonical_json_hash(body) != expected:
        raise ValueError("behavior_path_facts_hash_invalid")
    if facts.get("schema_version") != SCHEMA_VERSION or facts.get("method") != METHOD_ID:
        raise ValueError("behavior_path_method_invalid")
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
        raise ValueError("behavior_path_scope_invalid")
    package = Path(package_root).resolve()
    _assert_public_package(package)
    if facts.get("package_tree_hash") != _tree_hash(package):
        raise ValueError("behavior_path_package_changed")
    editable = list(facts.get("editable_nodes") or [])
    obligations = list(facts.get("obligations") or [])
    if len(editable) != len(obligations) or len(editable) > MAX_CLOSURE_EDITS:
        raise ValueError("behavior_path_editable_shape_invalid")
    if len({str(row.get("node_id")) for row in editable}) != len(editable):
        raise ValueError("behavior_path_duplicate_node")
    registry = _registry(package)
    for row in editable:
        current = registry.get(str(row.get("node_id")))
        if current is None or row.get("node_type") not in {
            "Raise",
            "Return",
            "Break",
            "Continue",
            "BoolOpOperator",
        }:
            raise ValueError("behavior_path_unknown_node")
        if str(row.get("node_sha256")) != str(current.get("node_sha256")):
            raise ValueError("behavior_path_node_hash_mismatch")
        if str(row.get("file_sha256")) != sha256_file(package / str(row["path"])):
            raise ValueError("behavior_path_file_hash_mismatch")
    decision = str((facts.get("localization_decision") or {}).get("decision"))
    if decision == "PROPOSE" and not editable:
        raise ValueError("behavior_path_empty_proposal")
    if decision == "ABSTAIN" and (editable or obligations):
        raise ValueError("behavior_path_abstain_has_editable_content")
    return expected


def residual_obligations(
    candidate_package: str | Path, frozen_facts: dict[str, Any]
) -> list[dict[str, Any]]:
    current = build_public_python_behavior_path_closure(candidate_package)
    target = {
        (str(row["path"]), str(row["function_symbol"]), str(row["subtype"]))
        for row in frozen_facts.get("obligations") or []
    }
    return [
        row
        for row in current.get("obligations") or []
        if (str(row["path"]), str(row["function_symbol"]), str(row["subtype"]))
        in target
    ]


__all__ = [
    "FAMILY",
    "MAX_CHANGED_PATHS",
    "MAX_CLOSURE_EDITS",
    "METHOD_ID",
    "build_public_python_behavior_path_closure",
    "build_same_package_wrong_behavior_path_closure",
    "residual_obligations",
    "validate_public_python_behavior_path_closure",
]
