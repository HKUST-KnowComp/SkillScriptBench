from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any

from skillscriptbench.d23_structural_facts_v96 import build_d23_fact_packet_v96
from skillscriptbench.io_utils import canonical_json_hash, hash_tree, sha256_file

from .unused_parameter_ast import _node_hash, _node_id


SCHEMA_VERSION = "bvi.visible_parameter_flow_ast.v1"
ACCEPT_STRUCTURALLY = "ACCEPT_STRUCTURALLY"
REJECT_STRUCTURALLY = "REJECT_STRUCTURALLY"


def _top_level_function(
    tree: ast.Module, symbol: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    matches = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == symbol
    ]
    if len(matches) != 1:
        raise ValueError(f"function_locator_not_unique:{symbol}:{len(matches)}")
    return matches[0]


def _expression_at_span(
    source: str, span: dict[str, Any], *, containing_symbol: str
) -> ast.expr:
    tree = ast.parse(source)
    function = _top_level_function(tree, containing_symbol)
    key = (
        int(span["start_line"]),
        int(span["start_column"]),
        int(span["end_line"]),
        int(span["end_column"]),
    )
    matches = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.expr)
        and (
            int(getattr(node, "lineno", -1)),
            int(getattr(node, "col_offset", -1)),
            int(getattr(node, "end_lineno", -1)),
            int(getattr(node, "end_col_offset", -1)),
        )
        == key
    ]
    if len(matches) != 1:
        raise ValueError(f"expression_span_not_unique:{containing_symbol}:{key}:{len(matches)}")
    return matches[0]


def _candidate_site(
    package: Path,
    *,
    node_view: dict[str, Any],
    containing_symbol: str,
) -> dict[str, Any]:
    relative = str(node_view["path"])
    path = package / relative
    source = path.read_text(encoding="utf-8")
    span = dict(node_view["span"])
    node = _expression_at_span(source, span, containing_symbol=containing_symbol)
    observed = ast.get_source_segment(source, node) or ast.unparse(node)
    return {
        "path": relative,
        "function_symbol": containing_symbol,
        "start_line": int(span["start_line"]),
        "start_column": int(span["start_column"]),
        "end_line": int(span["end_line"]),
        "end_column": int(span["end_column"]),
        "node_type": type(node).__name__,
        "node_id": _node_id(relative, node),
        "node_sha256": _node_hash(node),
        "observed_expression": observed,
        "file_sha256": sha256_file(path),
    }


def _compact_finding(
    package: Path, packet: dict[str, Any]
) -> dict[str, Any]:
    interprocedural = list(
        (packet.get("interprocedural_argument_flow") or {}).get("findings") or []
    )
    aliases = list(
        (packet.get("local_alias_argument_flow") or {}).get("findings") or []
    )
    if len(interprocedural) + len(aliases) != 1:
        raise ValueError(
            f"expected_one_parameter_flow_finding:{len(interprocedural)}:{len(aliases)}"
        )
    if interprocedural:
        row = interprocedural[0]
        containing_symbol = (
            str(row["caller"])
            if row["callee_scope"] == "nested"
            else str(row["callee"])
        )
        site = _candidate_site(
            package,
            node_view=dict(row["editable_return_node"]),
            containing_symbol=containing_symbol,
        )
        return {
            "finding_id": f"flow-{canonical_json_hash(row)[:16]}",
            "category": "argument_to_resolver_return_disconnect",
            "path": row["path"],
            "caller_symbol": row["caller"],
            "callee_symbol": row["callee"],
            "callee_scope": row["callee_scope"],
            "requested_parameter": row["requested_parameter"],
            "callee_formal": row["callee_formal"],
            "call_source": row["call_source"],
            "return_source": row["return_source"],
            "formal_load_count": int(row["formal_load_count"]),
            "argument_reaches_return": bool(row["argument_reaches_return"]),
            "candidate_sites": [site],
        }
    row = aliases[0]
    site = _candidate_site(
        package,
        node_view=dict(row["editable_alias_node"]),
        containing_symbol=str(row["function"]),
    )
    return {
        "finding_id": f"flow-{canonical_json_hash(row)[:16]}",
        "category": "parameter_to_local_alias_disconnect",
        "path": row["path"],
        "function_symbol": row["function"],
        "requested_parameter": row["requested_parameter"],
        "alias_symbol": row["alias"],
        "assignment_source": row["assignment_source"],
        "parameter_load_count": int(row["parameter_load_count"]),
        "alias_load_count": int(row["alias_load_count"]),
        "argument_reaches_alias": bool(row["argument_reaches_alias"]),
        "candidate_sites": [site],
    }


def build_visible_parameter_flow_facts(
    package_root: str | Path,
    request_text: str,
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    packet, audit, _registry = build_d23_fact_packet_v96(package, request_text)
    finding = _compact_finding(package, packet)
    contract = dict(packet.get("request_contract") or {})
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "structural_disconnect_detected",
        "finding_count": 1,
        "request_contract": contract,
        "findings": [finding],
        "derivation": {
            "inputs": ["SKILL.md", "scripts/**/*.py", "user_request"],
            "python_parser": "stdlib_ast",
            "uses_task_verdict": False,
            "uses_expected_output": False,
            "uses_gold_package": False,
            "uses_hidden_oracle": False,
            "uses_reward": False,
            "uses_mutation_label": False,
        },
        "audit": {
            "source_packet_hash": canonical_json_hash(packet),
            "structural_disconnect_count": int(
                audit.get("structural_disconnect_count") or 0
            ),
            "editable_node_count": int(audit.get("editable_node_count") or 0),
        },
        "disclaimer": (
            "The finding establishes a visible parameter-flow disconnect and an editable AST location. "
            "It does not prescribe the replacement expression or certify semantic correctness."
        ),
    }
    result["facts_hash"] = canonical_json_hash(result)
    return result


def _signature(source: str, symbol: str) -> str:
    tree = ast.parse(source)
    function = _top_level_function(tree, symbol)
    return ast.dump(function.args, include_attributes=False)


def _names_loaded(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
    }


def _flow_restored(
    candidate: Path, finding: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    path = candidate / str(finding["path"])
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    if finding["category"] == "argument_to_resolver_return_disconnect":
        caller = _top_level_function(tree, str(finding["caller_symbol"]))
        callee_name = str(finding["callee_symbol"])
        if finding["callee_scope"] == "nested":
            matches = [
                node
                for node in caller.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == callee_name
            ]
        else:
            matches = [
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == callee_name
            ]
        returns = [
            node
            for function in matches
            for statement in function.body
            for node in ast.walk(statement)
            if isinstance(node, ast.Return)
        ]
        formal = str(finding["callee_formal"])
        helper_flow = len(matches) == 1 and any(
            formal in _names_loaded(node.value) for node in returns
        )
        direct_flow = len(matches) == 0 and str(
            finding["requested_parameter"]
        ) in _names_loaded(caller)
        restored = helper_flow or direct_flow
        return restored, {
            "topology": finding["callee_scope"],
            "callee_match_count": len(matches),
            "return_count": len(returns),
            "required_formal": formal,
            "return_loads_required_formal": helper_flow,
            "caller_loads_requested_parameter_directly": direct_flow,
        }

    function = _top_level_function(tree, str(finding["function_symbol"]))
    alias = str(finding["alias_symbol"])
    parameter = str(finding["requested_parameter"])
    assignments = [
        statement
        for statement in function.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == alias
            for target in (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
        )
    ]
    values = [statement.value for statement in assignments]
    alias_flow = len(assignments) == 1 and parameter in _names_loaded(values[0])
    direct_flow = len(assignments) == 0 and parameter in _names_loaded(function)
    restored = alias_flow or direct_flow
    return restored, {
        "topology": "local_alias",
        "alias_assignment_count": len(assignments),
        "required_parameter": parameter,
        "assignment_loads_required_parameter": alias_flow,
        "function_loads_requested_parameter_directly": direct_flow,
    }


def evaluate_parameter_flow_candidate(
    parent_package: str | Path,
    candidate_package: str | Path | None,
    activation: dict[str, Any],
) -> dict[str, Any]:
    parent = Path(parent_package).resolve()
    request_text = str(activation.get("request_text") or "")
    parent_facts = build_visible_parameter_flow_facts(parent, request_text)
    finding = parent_facts["findings"][0]
    if candidate_package is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "decision": REJECT_STRUCTURALLY,
            "reason": "candidate_unavailable",
            "parent_finding_count": 1,
            "candidate_finding_count": None,
        }
    candidate = Path(candidate_package).resolve()
    checks: dict[str, bool] = {
        "candidate_exists": candidate.is_dir(),
        "tree_scope_preserved": set(hash_tree(parent)) == set(hash_tree(candidate)),
    }
    syntax_errors: list[str] = []
    for path in sorted((candidate / "scripts").rglob("*.py")):
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError, ValueError) as exc:
            syntax_errors.append(f"{path.relative_to(candidate)}:{type(exc).__name__}:{exc}")
    checks["python_syntax"] = not syntax_errors

    target_path = str(finding["path"])
    parent_source = (parent / target_path).read_text(encoding="utf-8")
    candidate_source = (candidate / target_path).read_text(encoding="utf-8")
    symbols = [
        str(finding.get("caller_symbol") or finding.get("function_symbol"))
    ]
    checks["public_signatures_preserved"] = all(
        _signature(parent_source, symbol) == _signature(candidate_source, symbol)
        for symbol in symbols
    )
    flow_restored, flow_detail = _flow_restored(candidate, finding)
    checks["required_visible_flow_restored"] = flow_restored

    try:
        candidate_packet, candidate_audit, _registry = build_d23_fact_packet_v96(
            candidate, request_text
        )
        residual = int(candidate_audit.get("structural_disconnect_count") or 0)
    except (OSError, SyntaxError, TypeError, ValueError):
        candidate_packet = {}
        residual = -1
    checks["no_residual_parameter_flow_disconnect"] = residual == 0
    decision = ACCEPT_STRUCTURALLY if all(checks.values()) else REJECT_STRUCTURALLY
    return {
        "schema_version": SCHEMA_VERSION,
        "decision": decision,
        "reason": (
            "visible_parameter_flow_restored_with_scope_and_signature_preserved"
            if decision == ACCEPT_STRUCTURALLY
            else "visible_parameter_flow_gate_failed"
        ),
        "checks": checks,
        "syntax_errors": syntax_errors,
        "flow_detail": flow_detail,
        "parent_finding_count": 1,
        "candidate_finding_count": residual,
        "candidate_packet_hash": (
            canonical_json_hash(candidate_packet) if candidate_packet else None
        ),
        "claim_boundary": (
            "Acceptance establishes a visible AST/def-use repair and compatibility-preserving structure only; "
            "hidden differential evaluation is required for semantic correctness."
        ),
    }
