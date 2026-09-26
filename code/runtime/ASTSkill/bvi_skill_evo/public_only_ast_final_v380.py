from __future__ import annotations

import ast
import copy
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from bvi_skill_evo import node_bound_contract_flow_executor_v365 as node_executor
from bvi_skill_evo import parent_projected_formal_origin_ast_v370 as formal_projection
from bvi_skill_evo import public_contract_flow_ast_v358 as contract_flow
from bvi_skill_evo.proposal_first_closure_gate_v292 import (
    ACCEPT,
    build_proposal_first_closure_report,
)
from skillscriptbench.io_utils import canonical_json_hash, copy_tree_clean, sha256_file


SCHEMA_VERSION = "3.80-public-only-ast-final-v1"
METHOD_ID = "selective_public_only_ast_executor_v1"
REAL_CONTROL = "real-public-structure"
SHAM_CONTROL = "same-package-wrong-structure"


def _node_id(relative: str, node: ast.AST) -> str:
    return (
        f"{relative}:{int(node.lineno)}:{int(node.col_offset)}:"
        f"{type(node).__name__}"
    )


def _node_source(source: str, node: ast.AST) -> str:
    return ast.get_source_segment(source, node) or ast.unparse(node)


def _function_parameters(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[str]:
    return [
        argument.arg
        for argument in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        )
    ]


class _FunctionSiteCollector(ast.NodeVisitor):
    def __init__(self, source: str, relative: str) -> None:
        self.source = source
        self.relative = relative
        self.scope: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self.call_sites: list[dict[str, Any]] = []
        self.constant_sites: list[dict[str, Any]] = []

    def _visit_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        self.scope.append(node)
        for statement in node.body:
            self.visit(statement)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_AsyncFunctionDef(  # noqa: N802
        self, node: ast.AsyncFunctionDef
    ) -> None:
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        for statement in node.body:
            self.visit(statement)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        if self.scope:
            function = self.scope[-1]
            parameters = _function_parameters(function)
            candidates: list[tuple[str, str | int, ast.Name]] = []
            candidates.extend(
                ("positional", index, argument)
                for index, argument in enumerate(node.args)
                if isinstance(argument, ast.Name)
            )
            candidates.extend(
                ("keyword", str(keyword.arg), keyword.value)
                for keyword in node.keywords
                if keyword.arg is not None and isinstance(keyword.value, ast.Name)
            )
            for call_kind, call_slot, argument in candidates:
                alternatives = [
                    name
                    for name in parameters
                    if name != argument.id and name not in {"self", "cls"}
                ]
                if not alternatives:
                    continue
                self.call_sites.append(
                    {
                        "path": self.relative,
                        "caller_path": self.relative,
                        "caller_symbol": function.name,
                        "callee_path": self.relative,
                        "callee_symbol": ast.unparse(node.func),
                        "call_kind": call_kind,
                        "call_slot": call_slot,
                        "call_line": int(node.lineno),
                        "call_column": int(node.col_offset),
                        "call_source": _node_source(self.source, node),
                        "observed_origin": argument.id,
                        "replacement_origin": alternatives[0],
                        "node_id": _node_id(self.relative, node),
                        "node_sha256": contract_flow._node_hash(node),
                    }
                )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802
        if self.scope and isinstance(node.value, (str, int, float, bool)):
            function = self.scope[-1]
            self.constant_sites.append(
                {
                    "path": self.relative,
                    "function_symbol": function.name,
                    "node_id": _node_id(self.relative, node),
                    "node_sha256": contract_flow._node_hash(node),
                    "node_type": "Constant",
                    "start_line": int(node.lineno),
                    "start_column": int(node.col_offset),
                    "end_line": int(node.end_lineno),
                    "end_column": int(node.end_col_offset),
                    "observed_expression": _node_source(self.source, node),
                    "value_type": type(node.value).__name__,
                }
            )


def _package_sites(package: Path) -> dict[str, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    constants: list[dict[str, Any]] = []
    scripts = package / "scripts"
    for path in sorted(scripts.rglob("*.py")) if scripts.is_dir() else []:
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        relative = path.relative_to(package).as_posix()
        source = path.read_text(encoding="utf-8", errors="strict")
        tree = ast.parse(source, filename=relative)
        collector = _FunctionSiteCollector(source, relative)
        collector.visit(tree)
        calls.extend(collector.call_sites)
        constants.extend(collector.constant_sites)
    return {
        "calls": sorted(calls, key=lambda row: (row["path"], row["node_id"])),
        "constants": sorted(
            constants, key=lambda row: (row["path"], row["node_id"])
        ),
    }


def typed_families(facts: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(row.get("kind"))
                for row in facts.get("findings") or []
                if row.get("finding_class") == "contract_backed_obligation"
            }
        )
    )


def _typed_findings(facts: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in facts.get("findings") or []
        if row.get("finding_class") == "contract_backed_obligation"
    ]


def _typed_editable_nodes(facts: dict[str, Any]) -> list[dict[str, Any]]:
    typed_node_ids = {
        str(row.get("node_id"))
        for finding in _typed_findings(facts)
        for row in [finding, *(finding.get("candidate_sites") or [])]
        if row.get("node_id")
    }
    return [
        row
        for row in facts.get("editable_nodes") or []
        if str(row.get("node_id")) in typed_node_ids
    ]


def primary_typed_family(facts: dict[str, Any]) -> str | None:
    families = typed_families(facts)
    if not families:
        return None
    if len(families) != 1:
        raise ValueError(f"public_only_ast_mixed_typed_families:{families}")
    family = families[0]
    if family not in {
        "formal_origin_mismatch",
        "argument_to_resolver_return_disconnect",
    }:
        raise ValueError(f"public_only_ast_unsupported_typed_family:{family}")
    return family


def _sham_formal_findings(
    real: dict[str, Any], sites: dict[str, list[dict[str, Any]]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    real_findings = [
        row
        for row in real.get("findings") or []
        if row.get("kind") == "formal_origin_mismatch"
    ]
    real_ids = {str(row.get("node_id")) for row in real_findings}
    alternatives = [
        row for row in sites["calls"] if str(row.get("node_id")) not in real_ids
    ]
    if len(alternatives) < len(real_findings):
        raise ValueError(
            "public_only_ast_formal_sham_insufficient_same_package_decoys:"
            f"{len(alternatives)}:{len(real_findings)}"
        )
    findings: list[dict[str, Any]] = []
    editable: list[dict[str, Any]] = []
    for index, (source, decoy) in enumerate(
        zip(real_findings, alternatives[: len(real_findings)], strict=True), start=1
    ):
        finding = copy.deepcopy(source)
        finding.update(
            {
                "caller_path": decoy["caller_path"],
                "caller_symbol": decoy["caller_symbol"],
                "callee_path": decoy["callee_path"],
                "callee_symbol": decoy["callee_symbol"],
                "call_kind": decoy["call_kind"],
                "call_slot": decoy["call_slot"],
                "call_line": decoy["call_line"],
                "call_column": decoy["call_column"],
                "call_source": decoy["call_source"],
                "observed_origin": decoy["observed_origin"],
                "callee_formal_parameter": decoy["replacement_origin"],
                "same_named_origin_available": True,
                "node_id": decoy["node_id"],
                "node_sha256": decoy["node_sha256"],
                "obligation_occurrence_index": index,
                "semantic_correctness_inferred": False,
            }
        )
        finding.pop("finding_hash", None)
        finding["finding_hash"] = canonical_json_hash(finding)
        findings.append(finding)
        editable.append(copy.deepcopy(finding))
    return findings, editable


def _resolver_real_site_type(finding: dict[str, Any]) -> str | None:
    sites = list(finding.get("candidate_sites") or [])
    if len(sites) != 1:
        return None
    return str(sites[0].get("value_type") or "") or None


def _sham_resolver_findings(
    real: dict[str, Any], sites: dict[str, list[dict[str, Any]]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    real_findings = [
        row
        for row in real.get("findings") or []
        if row.get("kind") == "argument_to_resolver_return_disconnect"
    ]
    real_ids = {
        str(site.get("node_id"))
        for finding in real_findings
        for site in finding.get("candidate_sites") or []
    }
    alternatives = [
        row
        for row in sites["constants"]
        if str(row.get("node_id")) not in real_ids
    ]
    selected: list[dict[str, Any]] = []
    remaining = list(alternatives)
    for finding in real_findings:
        wanted_type = _resolver_real_site_type(finding)
        compatible = [
            row
            for row in remaining
            if wanted_type is None or row.get("value_type") == wanted_type
        ]
        pool = compatible or remaining
        if not pool:
            raise ValueError(
                "public_only_ast_resolver_sham_insufficient_same_package_decoys"
            )
        chosen = pool[0]
        selected.append(chosen)
        remaining.remove(chosen)

    findings: list[dict[str, Any]] = []
    editable: list[dict[str, Any]] = []
    for source, decoy in zip(real_findings, selected, strict=True):
        finding = copy.deepcopy(source)
        finding.update(
            {
                "path": decoy["path"],
                "caller_symbol": decoy["function_symbol"],
                "return_source": f"return {decoy['observed_expression']}",
                "candidate_sites": [copy.deepcopy(decoy)],
                "semantic_correctness_inferred": False,
            }
        )
        finding.pop("finding_hash", None)
        finding["finding_hash"] = canonical_json_hash(finding)
        findings.append(finding)
        editable.append(copy.deepcopy(decoy))
    return findings, editable


def build_same_package_wrong_structure_packet(
    package_root: str | Path,
    real_facts: dict[str, Any],
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    family = primary_typed_family(real_facts)
    result = copy.deepcopy(real_facts)
    result.pop("facts_hash", None)
    real_ids = {
        str(row.get("node_id"))
        for row in real_facts.get("editable_nodes") or []
        if row.get("node_id")
    }
    if family is None:
        findings: list[dict[str, Any]] = []
        editable: list[dict[str, Any]] = []
    else:
        sites = _package_sites(package)
        if family == "formal_origin_mismatch":
            findings, editable = _sham_formal_findings(real_facts, sites)
        else:
            findings, editable = _sham_resolver_findings(real_facts, sites)
        editable = editable[: len(_typed_editable_nodes(real_facts))]
    sham_ids = {
        str(row.get("node_id"))
        for row in editable
        if row.get("node_id")
    }
    result.update(
        {
            "schema_version": SCHEMA_VERSION,
            "method": METHOD_ID,
            "status": (
                "same_package_wrong_structure_available"
                if family is not None
                else "no_typed_signal_matched_empty_control"
            ),
            "findings": findings,
            "def_use_findings": [
                copy.deepcopy(row)
                for row in findings
                if row.get("kind") == "formal_origin_mismatch"
            ],
            "editable_nodes": editable,
            "obligation_identities": [
                [
                    row.get("kind"),
                    row.get("caller_path") or row.get("path"),
                    row.get("caller_symbol"),
                    row.get("node_id")
                    or (row.get("candidate_sites") or [{}])[0].get("node_id"),
                ]
                for row in findings
            ],
            "all_obligation_identities": [
                [
                    row.get("kind"),
                    row.get("caller_path") or row.get("path"),
                    row.get("caller_symbol"),
                    row.get("node_id")
                    or (row.get("candidate_sites") or [{}])[0].get("node_id"),
                ]
                for row in findings
            ],
            "localization_decision": {
                "decision": (
                    "review_typed_obligation_subgraph"
                    if findings
                    else "abstain_no_typed_obligation"
                ),
                "contract_obligation_count": len(findings),
                "contract_obligation_count_untruncated": len(findings),
                "generic_hypothesis_count": 0,
            },
            "control_metadata": {
                "control": SHAM_CONTROL,
                "construction": "deterministic_same_package_same_family_node_derangement",
                "real_family": family,
                "real_finding_count": len(_typed_findings(real_facts)),
                "sham_finding_count": len(findings),
                "real_editable_node_count": len(
                    _typed_editable_nodes(real_facts)
                ),
                "sham_editable_node_count": len(editable),
                "real_node_overlap_count": len(real_ids & sham_ids),
                "same_finding_count": len(findings)
                == len(_typed_findings(real_facts)),
                "same_editable_node_count": len(editable)
                == len(_typed_editable_nodes(real_facts)),
                "package_local": True,
                "hidden_or_verifier_feedback_used": False,
            },
            "hidden_artifacts_consumed": False,
            "task_verifier_consumed": False,
            "gold_or_oracle_consumed": False,
            "reward_consumed": False,
            "semantic_correctness_inferred": False,
        }
    )
    metadata = result["control_metadata"]
    if (
        not metadata["same_finding_count"]
        or not metadata["same_editable_node_count"]
        or metadata["real_node_overlap_count"] != 0
    ):
        raise ValueError("public_only_ast_sham_shape_or_derangement_invalid")
    result["facts_hash"] = canonical_json_hash(result)
    return result


def _site_still_present(package: Path, site: dict[str, Any]) -> bool:
    relative = str(site.get("path") or site.get("caller_path") or "")
    node_id = str(site.get("node_id") or "")
    target = (package / relative).resolve()
    try:
        target.relative_to(package)
    except ValueError:
        return True
    if not target.is_file() or target.suffix != ".py":
        return False
    source = target.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=relative)
    except SyntaxError:
        return True
    node = node_executor._node_at_identity(tree, node_id)
    if node is None:
        return False
    return contract_flow._node_hash(node) == site.get("node_sha256")


def build_wrong_structure_closure_report(
    parent_package: str | Path,
    candidate_package: str | Path,
    request_text: str,
    *,
    frozen_sham_facts: dict[str, Any],
) -> dict[str, Any]:
    parent = Path(parent_package).resolve()
    candidate = Path(candidate_package).resolve()
    base = build_proposal_first_closure_report(
        parent,
        candidate,
        request_text,
        visible_structural_facts=frozen_sham_facts,
    )
    sites = list(frozen_sham_facts.get("editable_nodes") or [])
    unresolved = [site for site in sites if _site_still_present(candidate, site)]
    checks = dict(base.get("checks") or {})
    if sites:
        checks["all_supplied_structural_sites_changed"] = not unresolved
    result = dict(base)
    result.pop("gate_hash", None)
    result.update(
        {
            "schema_version": SCHEMA_VERSION,
            "method": METHOD_ID,
            "control": SHAM_CONTROL,
            "checks": checks,
            "failed_checks": sorted(
                name for name, passed in checks.items() if not passed
            ),
            "decision": ACCEPT if all(checks.values()) else "REVISE",
            "frozen_wrong_structure_facts_hash": frozen_sham_facts["facts_hash"],
            "supplied_structural_site_count": len(sites),
            "residual_supplied_structural_site_count": len(unresolved),
            "hidden_or_verifier_feedback_used": False,
        }
    )
    result["gate_hash"] = canonical_json_hash(result)
    return result


def apply_frozen_formal_origin_packet(
    parent_package: str | Path,
    destination_package: str | Path,
    request_text: str,
    *,
    frozen_facts: dict[str, Any],
    control: str,
) -> dict[str, Any]:
    if control not in {REAL_CONTROL, SHAM_CONTROL}:
        raise ValueError(f"public_only_ast_unknown_control:{control}")
    parent = Path(parent_package).resolve()
    destination = Path(destination_package).resolve()
    findings = [
        row
        for row in frozen_facts.get("findings") or []
        if row.get("kind") == "formal_origin_mismatch"
    ]
    if not findings:
        raise ValueError("public_only_ast_formal_packet_empty")
    copy_tree_clean(parent, destination)
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for finding in findings:
        by_path[str(finding["caller_path"])].append(finding)
    receipts: list[dict[str, Any]] = []
    for relative, path_findings in sorted(by_path.items()):
        path = destination / relative
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
        replacements: list[tuple[int, int, str, dict[str, Any]]] = []
        for finding in path_findings:
            call = formal_projection._locate_call(tree, finding)
            argument = formal_projection._argument_node(call, finding)
            observed = str(finding["observed_origin"])
            replacement = str(finding["callee_formal_parameter"])
            if not isinstance(argument, ast.Name) or argument.id != observed:
                raise ValueError("public_only_ast_formal_observed_origin_mismatch")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", replacement):
                raise ValueError("public_only_ast_formal_replacement_invalid")
            start, end = formal_projection._source_offsets(source, argument)
            if source[start:end] != observed:
                raise ValueError("public_only_ast_formal_argument_span_mismatch")
            replacements.append((start, end, replacement, finding))
        updated = source
        for start, end, replacement, finding in sorted(
            replacements, key=lambda row: (row[0], row[1]), reverse=True
        ):
            updated = updated[:start] + replacement + updated[end:]
            receipts.append(
                {
                    "path": relative,
                    "node_id": finding["node_id"],
                    "call_slot": finding["call_slot"],
                    "observed_origin": finding["observed_origin"],
                    "replacement_origin": replacement,
                    "finding_hash": finding["finding_hash"],
                }
            )
        ast.parse(updated, filename=relative)
        path.write_text(updated, encoding="utf-8")

    if control == REAL_CONTROL:
        gate = contract_flow.build_public_contract_flow_report(
            parent,
            destination,
            request_text,
            frozen_raw_facts=frozen_facts,
        )
    else:
        gate = build_wrong_structure_closure_report(
            parent,
            destination,
            request_text,
            frozen_sham_facts=frozen_facts,
        )
    result = {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "control": control,
        "status": "candidate_materialized",
        "decision": gate["decision"],
        "edit_count": len(receipts),
        "finding_count": len(findings),
        "one_edit_per_finding": len(receipts) == len(findings),
        "edit_receipts": sorted(
            receipts, key=lambda row: (row["path"], row["node_id"])
        ),
        "candidate_tree_hash": contract_flow._tree_hash(destination),
        "frozen_facts_hash": frozen_facts["facts_hash"],
        "gate_hash": gate["gate_hash"],
        "gate_failed_checks": gate.get("failed_checks") or [],
        "public_inputs_only": True,
        "hidden_or_verifier_feedback_used": False,
    }
    result["projection_hash"] = canonical_json_hash(result)
    return result


def apply_node_bound_response(
    response_arguments: str,
    source_package: str | Path,
    destination_package: str | Path,
    facts: dict[str, Any],
) -> dict[str, Any]:
    return node_executor.apply_bound_response(
        response_arguments,
        source_package,
        destination_package,
        facts,
    )


def public_method_source_audit(path: str | Path) -> dict[str, Any]:
    source_path = Path(path).resolve()
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))
    imported_modules = sorted(
        {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        | {
            str(node.module or "")
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
    )
    forbidden_import_fragments = (
        "hidden_" + "evaluator",
        "task_" + "verifier",
        "oracle_" + "package",
        "gold_" + "package",
        "reward_" + "model",
    )
    forbidden_imports = [
        module
        for module in imported_modules
        if any(fragment in module for fragment in forbidden_import_fragments)
    ]
    task_id_literals = re.findall(r"[\"']d\d{1,3}-[A-Za-z0-9_-]+[\"']", source)
    result = {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "source_path": str(source_path),
        "source_sha256": sha256_file(source_path),
        "imported_modules": imported_modules,
        "forbidden_imports": forbidden_imports,
        "task_id_literal_count": len(task_id_literals),
        "task_id_literals": sorted(set(task_id_literals)),
        "passes": not forbidden_imports and not task_id_literals,
    }
    result["audit_hash"] = canonical_json_hash(result)
    return result
