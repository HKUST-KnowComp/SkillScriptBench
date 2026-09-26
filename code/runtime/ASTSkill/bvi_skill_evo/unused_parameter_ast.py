from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from skillscriptbench.io_utils import canonical_json_hash, hash_tree, sha256_file


SCHEMA_VERSION = "bvi.documented_unused_parameter_ast.v1"
ACCEPT_STRUCTURALLY = "ACCEPT_STRUCTURALLY"
REJECT_STRUCTURALLY = "REJECT_STRUCTURALLY"


def _node_id(path: str, node: ast.AST) -> str:
    return f"{path}:{node.lineno}:{node.col_offset}:{type(node).__name__}"


def _node_hash(node: ast.AST) -> str:
    return canonical_json_hash(
        {
            "node_type": type(node).__name__,
            "ast": ast.dump(node, annotate_fields=True, include_attributes=False),
        }
    )


def _function(tree: ast.Module, symbol: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    matches = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == symbol
    ]
    if len(matches) != 1:
        raise ValueError(f"activated_function_count:{symbol}:{len(matches)}")
    return matches[0]


def _optional_parameters(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[tuple[str, ast.expr]]:
    positional = [*function.args.posonlyargs, *function.args.args]
    result: list[tuple[str, ast.expr]] = []
    if function.args.defaults:
        offset = len(positional) - len(function.args.defaults)
        result.extend(
            (positional[offset + index].arg, default)
            for index, default in enumerate(function.args.defaults)
        )
    result.extend(
        (argument.arg, default)
        for argument, default in zip(
            function.args.kwonlyargs, function.args.kw_defaults
        )
        if default is not None
    )
    return result


class _ParameterLoads(ast.NodeVisitor):
    def __init__(self, root: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> None:
        self.root = root
        self.name = name
        self.nodes: list[ast.Name] = []

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if isinstance(node.ctx, ast.Load) and node.id == self.name:
            self.nodes.append(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        if node is self.root:
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        if node is self.root:
            self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        return


def _parameter_loads(
    function: ast.FunctionDef | ast.AsyncFunctionDef, name: str
) -> list[ast.Name]:
    visitor = _ParameterLoads(function, name)
    visitor.visit(function)
    return visitor.nodes


def _documentation_evidence(
    skill_text: str, function_name: str, parameter: str
) -> dict[str, Any] | None:
    lines = skill_text.splitlines()
    for index, line in enumerate(lines):
        if function_name not in line:
            continue
        start = max(0, index - 1)
        end = min(len(lines), index + 3)
        excerpt = "\n".join(lines[start:end])
        if parameter in excerpt:
            return {
                "line": index + 1,
                "excerpt": excerpt[:800],
                "function_name_present": True,
                "parameter_present": True,
            }
    return None


def _parents(root: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(root) for child in ast.iter_child_nodes(parent)}


def _call_terminal(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _role_score(
    node: ast.expr,
    *,
    parameter: str,
    parent_map: dict[ast.AST, ast.AST],
) -> tuple[float, str]:
    parent = parent_map.get(node)
    grandparent = parent_map.get(parent) if parent is not None else None
    name = parameter.lower()

    if isinstance(parent, ast.Subscript) and parent.slice is node:
        return (0.99 if name in {"field", "key", "index"} else 0.94, "subscript_selector")
    if isinstance(parent, ast.Compare):
        return (
            0.99 if any(token in name for token in ("threshold", "limit", "index")) else 0.91,
            "comparison_boundary",
        )
    if isinstance(parent, ast.Call):
        terminal = _call_terminal(parent)
        if terminal in {"split", "rsplit", "partition", "rpartition"}:
            return (
                0.99 if any(token in name for token in ("delimiter", "separator", "marker")) else 0.94,
                f"{terminal}_argument",
            )
        if terminal == "get":
            return (0.99 if name in {"field", "key"} else 0.93, "mapping_get_key")
        return (0.84, "call_argument")
    if isinstance(parent, ast.keyword) and isinstance(grandparent, ast.Call):
        return (0.82, "keyword_argument")
    if isinstance(parent, (ast.BinOp, ast.BoolOp, ast.UnaryOp)):
        return (0.79, "computed_expression")
    if isinstance(parent, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
        return (0.72, "container_literal")
    return (0.65, type(parent).__name__ if parent is not None else "expression")


def _candidate_sites(
    source: str,
    relative: str,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    default_value: Any,
    parameter: str,
) -> list[dict[str, Any]]:
    parent_map = _parents(function)
    rows: list[dict[str, Any]] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Constant) or node.value != default_value:
            continue
        if not all(
            hasattr(node, name)
            for name in ("lineno", "col_offset", "end_lineno", "end_col_offset")
        ):
            continue
        score, role = _role_score(node, parameter=parameter, parent_map=parent_map)
        rows.append(
            {
                "path": relative,
                "function_symbol": function.name,
                "node_id": _node_id(relative, node),
                "node_sha256": _node_hash(node),
                "node_type": type(node).__name__,
                "start_line": int(node.lineno),
                "start_column": int(node.col_offset),
                "end_line": int(node.end_lineno),
                "end_column": int(node.end_col_offset),
                "observed_expression": ast.get_source_segment(source, node)
                or ast.unparse(node),
                "syntactic_role": role,
                "confidence": score,
            }
        )
    rows.sort(
        key=lambda row: (
            -float(row["confidence"]),
            int(row["start_line"]),
            int(row["start_column"]),
        )
    )
    for rank, row in enumerate(rows[:8], start=1):
        row["rank"] = rank
        row["site_hash"] = canonical_json_hash(row)
    return rows[:8]


def _signature_inventory(
    package: Path, activations: list[dict[str, str]]
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for activation in activations:
        relative = activation["path"]
        path = package / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        function = _function(tree, activation["symbol"])
        rows.append(
            {
                "path": relative,
                "symbol": function.name,
                "arguments_ast_sha256": canonical_json_hash(
                    ast.dump(function.args, annotate_fields=True, include_attributes=False)
                ),
            }
        )
    return rows


def build_documented_unused_parameter_facts(
    package_root: str | Path, activation: dict[str, Any]
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    skill_path = package / "SKILL.md"
    if not skill_path.is_file():
        raise FileNotFoundError(skill_path)
    skill_text = skill_path.read_text(encoding="utf-8")
    activations = [
        {"path": str(event["path"]), "symbol": str(event["symbol"])}
        for event in activation.get("events", [])
        if event.get("event_type") == "package_function_activated"
    ]
    if not activations:
        raise ValueError("activated_function_required")

    findings: list[dict[str, Any]] = []
    parse_errors: list[dict[str, str]] = []
    for activated in activations:
        relative = activated["path"]
        path = package / relative
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
            function = _function(tree, activated["symbol"])
        except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
            parse_errors.append(
                {"path": relative, "error": f"{type(exc).__name__}:{exc}"}
            )
            continue
        for parameter, default_node in _optional_parameters(function):
            try:
                default_value = ast.literal_eval(default_node)
            except (TypeError, ValueError):
                continue
            evidence = _documentation_evidence(
                skill_text, function.name, parameter
            )
            loads = _parameter_loads(function, parameter)
            sites = _candidate_sites(
                source, relative, function, default_value, parameter
            )
            if evidence is None or loads or not sites:
                continue
            finding: dict[str, Any] = {
                "kind": "documented_optional_parameter_unused",
                "path": relative,
                "function_symbol": function.name,
                "parameter": parameter,
                "default_expression": ast.unparse(default_node),
                "parameter_load_count": 0,
                "documentation_evidence": evidence,
                "candidate_site_count": len(sites),
                "candidate_sites": sites,
                "confidence": round(float(sites[0]["confidence"]), 4),
            }
            finding["finding_hash"] = canonical_json_hash(finding)
            findings.append(finding)

    findings.sort(
        key=lambda row: (
            -float(row["confidence"]), row["path"], row["function_symbol"], row["parameter"]
        )
    )
    for rank, finding in enumerate(findings, start=1):
        finding["rank"] = rank
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "ranked_documented_unused_parameters" if findings else "no_findings",
        "activation": {
            "task_id": activation.get("task_id"),
            "activated_functions": activations,
            "task_verdict_exposed": activation.get("task_verdict") is not None,
            "expected_output_exposed": activation.get("expected_output") is not None,
        },
        "generic_invariant": (
            "A documented optional public parameter should influence at least one expression in its activated "
            "function. When it has zero load uses, rank matching compatibility-default literals by their AST role."
        ),
        "finding_count": len(findings),
        "findings": findings,
        "signature_inventory": _signature_inventory(package, activations),
        "parse_errors": parse_errors,
        "derivation": {
            "inputs": [
                "visible_skill_markdown",
                "visible_package_python_ast",
                "answer_free_function_activation",
            ],
            "visible_inputs_only": True,
            "uses_task_verdict": False,
            "uses_expected_output": False,
            "uses_gold_package": False,
            "uses_hidden_oracle": False,
            "uses_reward": False,
            "uses_mutation_label": False,
        },
        "package_tree_hash": canonical_json_hash(hash_tree(package)),
        "skill_sha256": sha256_file(skill_path),
    }
    payload["facts_hash"] = canonical_json_hash(payload)
    return payload


def evaluate_documented_unused_parameter_candidate(
    parent_package: str | Path,
    candidate_package: str | Path | None,
    activation: dict[str, Any],
) -> dict[str, Any]:
    parent = Path(parent_package).resolve()
    parent_facts = build_documented_unused_parameter_facts(parent, activation)
    if candidate_package is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "decision": REJECT_STRUCTURALLY,
            "reason": "candidate_missing",
            "parent_finding_count": parent_facts["finding_count"],
            "candidate_finding_count": None,
        }
    candidate = Path(candidate_package).resolve()
    try:
        candidate_facts = build_documented_unused_parameter_facts(
            candidate, activation
        )
    except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "decision": REJECT_STRUCTURALLY,
            "reason": f"candidate_scan_failed:{type(exc).__name__}:{exc}",
            "parent_finding_count": parent_facts["finding_count"],
            "candidate_finding_count": None,
        }
    parent_hashes = hash_tree(parent)
    candidate_hashes = hash_tree(candidate)
    changed_paths = sorted(
        path
        for path in set(parent_hashes) | set(candidate_hashes)
        if parent_hashes.get(path) != candidate_hashes.get(path)
    )
    allowed_paths = {
        finding["path"] for finding in parent_facts.get("findings", [])
    }
    checks = {
        "parent_has_structural_finding": parent_facts["finding_count"] > 0,
        "candidate_has_no_residual_finding": candidate_facts["finding_count"] == 0,
        "activated_signatures_preserved": (
            parent_facts["signature_inventory"]
            == candidate_facts["signature_inventory"]
        ),
        "python_parse_ok": not candidate_facts["parse_errors"],
        "changes_within_ranked_script_paths": bool(changed_paths)
        and set(changed_paths) <= allowed_paths,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "decision": (
            ACCEPT_STRUCTURALLY if all(checks.values()) else REJECT_STRUCTURALLY
        ),
        "checks": checks,
        "changed_paths": changed_paths,
        "allowed_paths": sorted(allowed_paths),
        "parent_finding_count": parent_facts["finding_count"],
        "candidate_finding_count": candidate_facts["finding_count"],
        "parent_facts_hash": parent_facts["facts_hash"],
        "candidate_facts_hash": candidate_facts["facts_hash"],
        "semantic_correctness_checked": False,
    }
