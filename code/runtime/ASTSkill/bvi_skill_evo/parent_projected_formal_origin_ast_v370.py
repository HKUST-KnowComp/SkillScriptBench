from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path
from typing import Any

from bvi_skill_evo import public_contract_flow_ast_v358 as base
from bvi_skill_evo.balanced_hybrid_ast_v316 import _assert_public_package, _tree_hash
from bvi_skill_evo.coarse_source_flow import (
    _call_target,
    _compatible,
    _node_hash,
    _parameters,
    _resolve_imports,
)
from skillscriptbench.io_utils import canonical_json_hash, copy_tree_clean


SCHEMA_VERSION = "3.70-parent-projected-formal-origin-ast-v1"
METHOD_ID = "parent_projected_formal_origin_ast_v1"


def _function_parameter_names(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
) -> set[str]:
    arguments = node.args
    return {
        argument.arg
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        )
    } | ({arguments.vararg.arg} if arguments.vararg else set()) | (
        {arguments.kwarg.arg} if arguments.kwarg else set()
    )


class _ScopeBindings(ast.NodeVisitor):
    def __init__(self, root: ast.AST) -> None:
        self.root = root
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        if node is self.root:
            self.generic_visit(node)

    def visit_AsyncFunctionDef(  # noqa: N802
        self, node: ast.AsyncFunctionDef
    ) -> None:
        if node is self.root:
            self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        if node is self.root:
            self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        return


def _scope_bindings(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
) -> set[str]:
    visitor = _ScopeBindings(node)
    visitor.visit(node)
    return _function_parameter_names(node) | visitor.names


class _NestedClosureCalls(ast.NodeVisitor):
    def __init__(
        self,
        root: ast.FunctionDef | ast.AsyncFunctionDef,
        outer_parameters: set[str],
    ) -> None:
        self.root = root
        self.outer_parameters = outer_parameters
        self.depth = 0
        self.shadowed: set[str] = set()
        self.scope_path: list[str] = [root.name]
        self.calls: list[tuple[ast.Call, set[str], tuple[str, ...]]] = []

    def _visit_nested_scope(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
        name: str,
    ) -> None:
        previous_shadowed = self.shadowed
        self.shadowed = previous_shadowed | (
            _scope_bindings(node) & self.outer_parameters
        )
        self.depth += 1
        self.scope_path.append(name)
        for statement in node.body if not isinstance(node, ast.Lambda) else [node.body]:
            self.visit(statement)
        self.scope_path.pop()
        self.depth -= 1
        self.shadowed = previous_shadowed

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        if node is self.root:
            for statement in node.body:
                self.visit(statement)
            return
        self._visit_nested_scope(node, node.name)

    def visit_AsyncFunctionDef(  # noqa: N802
        self, node: ast.AsyncFunctionDef
    ) -> None:
        if node is self.root:
            for statement in node.body:
                self.visit(statement)
            return
        self._visit_nested_scope(node, node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        self._visit_nested_scope(node, "<lambda>")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        return

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        if self.depth > 0:
            self.calls.append((node, set(self.shadowed), tuple(self.scope_path)))
        self.generic_visit(node)


def _nested_closure_formal_origin_mismatches(
    package: Path, request_text: str
) -> list[dict[str, Any]]:
    index = base._python_index(package)
    skill_text = (package / "SKILL.md").read_text(encoding="utf-8")
    findings: list[dict[str, Any]] = []
    for caller_path, tree in index["trees"].items():
        imported_symbols, module_aliases = _resolve_imports(tree, index["modules"])
        top_level_functions = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for caller in top_level_functions:
            caller_parameters = {row["name"]: row for row in _parameters(caller)}
            if not caller_parameters:
                continue
            visitor = _NestedClosureCalls(caller, set(caller_parameters))
            visitor.visit(caller)
            relevance = base._caller_relevance(
                path=caller_path.as_posix(),
                symbol=caller.name,
                request_text=request_text,
                skill_text=skill_text,
                changed_symbols=set(),
            )
            for call, shadowed, scope_path in visitor.calls:
                target = _call_target(call, imported_symbols, module_aliases)
                if target is None:
                    continue
                callee_path, callee_symbol = target
                callee = index["functions"].get(callee_path, {}).get(callee_symbol)
                if callee is None:
                    continue
                callee_parameters = _parameters(callee)
                callee_by_name = {row["name"]: row for row in callee_parameters}
                slots: list[tuple[str, str | int, str | None, ast.expr]] = []
                for position, argument in enumerate(call.args):
                    formal = (
                        callee_parameters[position]["name"]
                        if position < len(callee_parameters)
                        else None
                    )
                    slots.append(("positional", position, formal, argument))
                slots.extend(
                    ("keyword", str(keyword.arg), str(keyword.arg), keyword.value)
                    for keyword in call.keywords
                    if keyword.arg is not None
                )
                for call_kind, call_slot, formal, argument in slots:
                    if (
                        formal is None
                        or formal not in caller_parameters
                        or formal not in callee_by_name
                        or not isinstance(argument, ast.Name)
                        or argument.id not in caller_parameters
                        or argument.id == formal
                        or formal in shadowed
                        or argument.id in shadowed
                    ):
                        continue
                    caller_annotation = caller_parameters[formal]["annotation"]
                    callee_annotation = callee_by_name[formal]["annotation"]
                    annotations_compatible = _compatible(
                        caller_annotation, callee_annotation
                    )
                    if (
                        caller_annotation
                        and callee_annotation
                        and not annotations_compatible
                    ):
                        continue
                    finding = {
                        "finding_class": "contract_backed_obligation",
                        "kind": "formal_origin_mismatch",
                        "confidence": 0.985 if call_kind == "keyword" else 0.975,
                        "caller_path": caller_path.as_posix(),
                        "caller_symbol": caller.name,
                        "lexical_scope_path": list(scope_path),
                        "captured_outer_parameter_evidence": True,
                        "shadowed_outer_parameters_at_call": sorted(shadowed),
                        "callee_path": callee_path.as_posix(),
                        "callee_symbol": callee_symbol,
                        "call_line": int(call.lineno),
                        "call_column": int(call.col_offset),
                        "call_kind": call_kind,
                        "call_slot": call_slot,
                        "callee_formal_parameter": formal,
                        "observed_origin": argument.id,
                        "same_named_origin_available": True,
                        "caller_annotation": caller_annotation,
                        "callee_annotation": callee_annotation,
                        "node_id": (
                            f"{caller_path.as_posix()}:{call.lineno}:"
                            f"{call.col_offset}:Call"
                        ),
                        "node_sha256": _node_hash(call),
                        "call_source": ast.get_source_segment(
                            index["sources"][caller_path], call
                        ),
                        "localization_evidence": relevance,
                        "repair_goal": (
                            "Reconnect the captured outer parameter with the same name as the "
                            "package-local callee formal without changing the public signature."
                        ),
                        "semantic_correctness_inferred": False,
                    }
                    finding["finding_hash"] = canonical_json_hash(finding)
                    findings.append(finding)
    findings.sort(
        key=lambda row: (
            str(row["caller_path"]),
            int(row["call_line"]),
            int(row.get("call_column") or 0),
            str(row["callee_formal_parameter"]),
        )
    )
    counts: Counter[tuple[Any, ...]] = Counter()
    for row in findings:
        identity = (
            row["caller_path"],
            row["caller_symbol"],
            row["callee_path"],
            row["callee_symbol"],
            row["call_kind"],
            row["call_slot"],
            row["callee_formal_parameter"],
            row["observed_origin"],
        )
        counts[identity] += 1
        row["obligation_occurrence_index"] = counts[identity]
        row["finding_hash"] = canonical_json_hash(
            {key: value for key, value in row.items() if key != "finding_hash"}
        )
    return findings


def collect_formal_origin_obligations(
    package_root: str | Path, request_text: str
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    _assert_public_package(package)
    direct = base._global_formal_origin_mismatches(
        package, request_text, changed_symbols=set()
    )
    nested = _nested_closure_formal_origin_mismatches(package, request_text)
    rows = [*direct["findings"], *nested]
    rows.sort(
        key=lambda row: (
            str(row["caller_path"]),
            int(row["call_line"]),
            int(str(row["node_id"]).rsplit(":", 2)[-2]),
            str(row["call_slot"]),
        )
    )
    identities = [
        [
            row["caller_path"],
            row["node_id"],
            row["call_kind"],
            row["call_slot"],
            row["observed_origin"],
            row["callee_formal_parameter"],
        ]
        for row in rows
    ]
    if len({tuple(row) for row in identities}) != len(rows):
        raise ValueError("parent_projected_duplicate_obligation_identity")
    result = {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "package_tree_hash": _tree_hash(package),
        "direct_obligation_count": len(direct["findings"]),
        "nested_closure_obligation_count": len(nested),
        "obligation_count": len(rows),
        "obligation_identities": identities,
        "findings": rows,
        "public_inputs_only": True,
        "hidden_or_verifier_feedback_used": False,
        "semantic_correctness_inferred": False,
    }
    result["facts_hash"] = canonical_json_hash(result)
    return result


def _byte_column_to_character(line: str, byte_column: int) -> int:
    encoded = line.encode("utf-8")
    if byte_column < 0 or byte_column > len(encoded):
        raise ValueError("parent_projected_byte_column_out_of_range")
    return len(encoded[:byte_column].decode("utf-8"))


def _source_offsets(source: str, node: ast.AST) -> tuple[int, int]:
    if not all(
        hasattr(node, field)
        for field in ("lineno", "col_offset", "end_lineno", "end_col_offset")
    ):
        raise ValueError("parent_projected_node_missing_source_span")
    lines = source.splitlines(keepends=True)
    start_line = int(node.lineno) - 1
    end_line = int(node.end_lineno) - 1
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    start = starts[start_line] + _byte_column_to_character(
        lines[start_line], int(node.col_offset)
    )
    end = starts[end_line] + _byte_column_to_character(
        lines[end_line], int(node.end_col_offset)
    )
    return start, end


def _locate_call(tree: ast.Module, finding: dict[str, Any]) -> ast.Call:
    node_id = str(finding["node_id"])
    parts = node_id.rsplit(":", 3)
    if len(parts) != 4:
        raise ValueError("parent_projected_node_id_invalid")
    line = int(parts[1])
    column = int(parts[2])
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and int(node.lineno) == line
        and int(node.col_offset) == column
        and _node_hash(node) == finding["node_sha256"]
    ]
    if len(matches) != 1:
        raise ValueError(
            f"parent_projected_call_not_unique:{finding['caller_path']}:{node_id}:{len(matches)}"
        )
    return matches[0]


def _argument_node(call: ast.Call, finding: dict[str, Any]) -> ast.expr:
    if finding["call_kind"] == "positional":
        slot = int(finding["call_slot"])
        if slot >= len(call.args):
            raise ValueError("parent_projected_positional_slot_missing")
        return call.args[slot]
    slot = str(finding["call_slot"])
    matches = [keyword.value for keyword in call.keywords if keyword.arg == slot]
    if len(matches) != 1:
        raise ValueError("parent_projected_keyword_slot_not_unique")
    return matches[0]


def apply_parent_projected_repair(
    parent_package: str | Path,
    destination_package: str | Path,
    request_text: str,
    *,
    frozen_raw_facts: dict[str, Any],
) -> dict[str, Any]:
    parent = Path(parent_package).resolve()
    destination = Path(destination_package).resolve()
    _assert_public_package(parent)
    facts = collect_formal_origin_obligations(parent, request_text)
    findings = list(facts["findings"])
    if not findings:
        raise ValueError("parent_projected_no_formal_origin_obligation")
    copy_tree_clean(parent, destination)

    by_path: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        by_path.setdefault(str(finding["caller_path"]), []).append(finding)
    receipts: list[dict[str, Any]] = []
    for relative, path_findings in sorted(by_path.items()):
        path = destination / relative
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
        replacements: list[tuple[int, int, str, dict[str, Any]]] = []
        for finding in path_findings:
            call = _locate_call(tree, finding)
            argument = _argument_node(call, finding)
            if (
                not isinstance(argument, ast.Name)
                or argument.id != finding["observed_origin"]
            ):
                raise ValueError("parent_projected_observed_origin_mismatch")
            replacement = str(finding["callee_formal_parameter"])
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", replacement):
                raise ValueError("parent_projected_replacement_identifier_invalid")
            start, end = _source_offsets(source, argument)
            if source[start:end] != argument.id:
                raise ValueError("parent_projected_argument_span_mismatch")
            replacements.append((start, end, replacement, finding))
        occupied: set[int] = set()
        for start, end, _, _ in replacements:
            if any(position in occupied for position in range(start, end)):
                raise ValueError("parent_projected_overlapping_edit")
            occupied.update(range(start, end))
        updated = source
        for start, end, replacement, finding in sorted(
            replacements, key=lambda row: (row[0], row[1]), reverse=True
        ):
            updated = updated[:start] + replacement + updated[end:]
            receipts.append(
                {
                    "path": relative,
                    "node_id": finding["node_id"],
                    "call_kind": finding["call_kind"],
                    "call_slot": finding["call_slot"],
                    "observed_origin": finding["observed_origin"],
                    "replacement_origin": replacement,
                    "captured_outer_parameter_evidence": bool(
                        finding.get("captured_outer_parameter_evidence")
                    ),
                    "finding_hash": finding["finding_hash"],
                }
            )
        ast.parse(updated, filename=relative)
        path.write_text(updated, encoding="utf-8")

    residual = collect_formal_origin_obligations(destination, request_text)
    base_report = base.build_public_contract_flow_report(
        parent,
        destination,
        request_text,
        frozen_raw_facts=frozen_raw_facts,
    )
    proven_closure_captures = {
        (
            str(finding["caller_path"]),
            ".".join(str(part) for part in finding.get("lexical_scope_path") or []),
            str(finding["callee_formal_parameter"]),
        )
        for finding in findings
        if finding.get("captured_outer_parameter_evidence") is True
    }
    unresolved_name_findings = list(base_report.get("unresolved_name_findings") or [])
    unresolved_names_are_proven_closure_captures = bool(unresolved_name_findings) and all(
        (
            str(row.get("path") or ""),
            str(row.get("symbol") or ""),
            str(name),
        )
        in proven_closure_captures
        for row in unresolved_name_findings
        for name in row.get("names") or []
    )
    base_failed_checks = list(base_report.get("failed_checks") or [])
    base_only_rejects_proven_closure_captures = (
        base_failed_checks == ["no_new_unresolved_names_in_changed_functions"]
        and unresolved_names_are_proven_closure_captures
    )
    changed_paths = sorted(by_path)
    checks = {
        "parent_projection_used": True,
        "one_edit_per_parent_obligation": len(receipts) == len(findings),
        "all_parent_formal_origin_obligations_resolved": residual["obligation_count"] == 0,
        "changed_paths_are_exact_obligation_paths": set(changed_paths) == set(by_path),
        "base_public_contract_flow_gate_accepts_or_only_rejects_proven_closure_captures": (
            base_report["decision"] == base.ACCEPT
            or base_only_rejects_proven_closure_captures
        ),
        "public_api_signatures_preserved": bool(
            base_report.get("checks", {}).get("public_api_signatures_preserved")
        ),
        "hidden_semantic_correctness_checked": False,
    }
    decision = "ACCEPT" if all(
        value for key, value in checks.items() if key != "hidden_semantic_correctness_checked"
    ) else "REVISE"
    result = {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "status": "candidate_materialized",
        "decision": decision,
        "checks": checks,
        "parent_tree_hash": facts["package_tree_hash"],
        "candidate_tree_hash": _tree_hash(destination),
        "parent_facts_hash": facts["facts_hash"],
        "frozen_raw_facts_hash": frozen_raw_facts["facts_hash"],
        "direct_obligation_count": facts["direct_obligation_count"],
        "nested_closure_obligation_count": facts["nested_closure_obligation_count"],
        "edit_count": len(receipts),
        "changed_paths": changed_paths,
        "edit_receipts": sorted(
            receipts, key=lambda row: (row["path"], row["node_id"], str(row["call_slot"]))
        ),
        "residual_obligation_count": residual["obligation_count"],
        "base_gate_hash": base_report["gate_hash"],
        "base_gate_failed_checks": base_failed_checks,
        "base_unresolved_name_findings": unresolved_name_findings,
        "proven_closure_capture_identities": sorted(
            [list(row) for row in proven_closure_captures]
        ),
        "base_only_rejects_proven_closure_captures": (
            base_only_rejects_proven_closure_captures
        ),
        "model_calls": 0,
        "public_inputs_only": True,
        "benchmark_bundled_visible_ast_facts_used": False,
        "hidden_verifier_gold_or_oracle_used": False,
        "semantic_correctness_inferred": False,
        "claim_boundary": (
            "This executor proves a deterministic public AST argument rewrite was projected onto "
            "the parent package and closed every detected formal-origin obligation. It does not "
            "prove task-level semantic correctness."
        ),
    }
    result["projection_hash"] = canonical_json_hash(result)
    return result
