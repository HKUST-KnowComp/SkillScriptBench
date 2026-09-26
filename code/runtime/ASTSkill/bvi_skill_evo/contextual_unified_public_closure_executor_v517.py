from __future__ import annotations

import ast
import copy
import json
import keyword
import subprocess
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from bvi_skill_evo import parent_projected_formal_origin_ast_v370 as formal_origin
from bvi_skill_evo import public_only_ast_final_v380 as d48_control
from bvi_skill_evo import public_runtime_cli_interface_closure_v453 as cli_interface
from bvi_skill_evo import public_runtime_multilang_controls_v412 as multilang_control
from bvi_skill_evo import public_runtime_multilang_dual_v410 as multilang
from bvi_skill_evo import public_runtime_python_behavior_path_closure_v489 as behavior_path
from bvi_skill_evo import public_runtime_python_behavior_path_closure_executor_v492 as behavior_executor
from bvi_skill_evo import public_runtime_python_call_binding_closure_v480 as call_binding
from bvi_skill_evo import public_runtime_python_helper_edge_closure_v498 as helper_edge
from bvi_skill_evo import public_runtime_python_helper_edge_closure_executor_v502 as helper_executor
from bvi_skill_evo import public_runtime_python_parameter_closure_scope_gate_v451 as parameter_flow
from bvi_skill_evo.coarse_source_flow import _node_hash
from bvi_skill_evo.public_runtime_python_parameter_effect_v424 import _public_signatures
from skillscriptbench.io_utils import (
    canonical_json_hash,
    copy_tree_clean,
    hash_tree,
    sha256_bytes,
    sha256_file,
)
from skillscriptbench.multilang_structural_v66 import enumerate_package_nodes


SCHEMA_VERSION = "5.17-contextual-unified-public-closure-executor-v1"
METHOD_ID = "contextual_unified_public_runtime_ast_closure_v2"
ACCEPT_STRUCTURALLY = "ACCEPT_STRUCTURALLY"
REVISE = "REVISE"
ABSTAIN_TO_RAW = "ABSTAIN_TO_RAW"
EDIT_ALL = "EDIT_ALL"
MAX_EDITABLE_NODES = 8
MAX_TOP_LEVEL_FAMILIES = 2

TOP_LEVEL_FAMILY = {
    "d48_typed_contract": "typed_contract",
    "multilang_typed_contract": "typed_contract",
    "parameter_flow": "interface_flow",
    "cli_interface": "interface_flow",
    "call_binding": "call_data_composition",
    "helper_edge": "call_data_composition",
    "behavior_path": "behavior_path",
}

# A newer exact def-use/call-site detector owns a node when it and the older D48
# packet normalize to the same source span. D48 remains recorded as corroboration.
COMPONENT_PRIORITY = {
    "parameter_flow": 0,
    "cli_interface": 0,
    "call_binding": 0,
    "behavior_path": 0,
    "helper_edge": 0,
    "multilang_typed_contract": 1,
    "d48_typed_contract": 2,
}

UNIFIED_NODE_PATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_skill_patch",
        "description": (
            "Review one bounded executable-skill closure. Choose EDIT_ALL only when the "
            "full public package supports the structural hypothesis; otherwise choose "
            "ABSTAIN_TO_RAW. EDIT_ALL must provide every listed exact target_node_id."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["decision", "edits", "summary"],
            "properties": {
                "decision": {"type": "string", "enum": [EDIT_ALL, ABSTAIN_TO_RAW]},
                "edits": {
                    "type": "array",
                    "maxItems": MAX_EDITABLE_NODES,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["target_node_id", "replacement"],
                        "properties": {
                            "target_node_id": {"type": "string"},
                            "replacement": {"type": "string"},
                        },
                    },
                },
                "summary": {"type": "string"},
            },
        },
    },
}
PYTHON_NODE_PATCH_TOOL = UNIFIED_NODE_PATCH_TOOL
MULTILANG_NODE_PATCH_TOOL = UNIFIED_NODE_PATCH_TOOL


def _tree_hash(package: str | Path) -> str:
    return canonical_json_hash(hash_tree(Path(package).resolve()))


def _embedded_hash_valid(payload: dict[str, Any], field: str = "facts_hash") -> bool:
    body = dict(payload)
    expected = str(body.pop(field, ""))
    return bool(expected) and canonical_json_hash(body) == expected


def _safe_path(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or not relative.startswith("scripts/"):
        raise ValueError("unified_closure_path_outside_scripts")
    target = (root / pure).resolve()
    if root.resolve() not in target.parents:
        raise ValueError("unified_closure_path_escape")
    return target


def _byte_span_from_ast(source: bytes, node: ast.AST) -> dict[str, int]:
    if not all(
        hasattr(node, field)
        for field in ("lineno", "col_offset", "end_lineno", "end_col_offset")
    ):
        raise ValueError("unified_closure_ast_node_missing_span")
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    start = offsets[int(node.lineno) - 1] + int(node.col_offset)
    end = offsets[int(node.end_lineno) - 1] + int(node.end_col_offset)
    if not 0 <= start < end <= len(source):
        raise ValueError("unified_closure_ast_byte_span_invalid")
    return {"start": start, "end": end}


def _span_from_row(package: Path, row: dict[str, Any]) -> dict[str, int]:
    if row.get("byte_span"):
        value = row["byte_span"]
        return {"start": int(value["start"]), "end": int(value["end"])}
    relative = str(row.get("path") or row.get("caller_path") or "")
    source = _safe_path(package, relative).read_bytes()
    line = int(row.get("line") or row.get("start_line") or 0)
    column = int(row.get("column") or row.get("start_column") or 0)
    end_line = int(row.get("end_line") or line)
    end_column = int(row.get("end_column") or 0)
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for item in lines:
        offsets.append(offsets[-1] + len(item))
    start = offsets[line - 1] + column
    end = offsets[end_line - 1] + end_column
    if not 0 <= start < end <= len(source):
        raise ValueError("unified_closure_row_byte_span_invalid")
    return {"start": start, "end": end}


def _node_id(path: str, node: ast.AST) -> str:
    return f"{path}:{node.lineno}:{node.col_offset}:{type(node).__name__}"


def _d48_validate(facts: dict[str, Any], package: Path) -> str:
    if not _embedded_hash_valid(facts):
        raise ValueError("unified_closure_d48_hash_invalid")
    expected_tree = str(facts.get("current_tree_hash") or facts.get("parent_tree_hash") or "")
    if expected_tree != formal_origin._tree_hash(package):
        raise ValueError("unified_closure_d48_tree_changed")
    if any(
        facts.get(field) is not False
        for field in (
            "benchmark_bundled_visible_ast_facts_consumed",
            "hidden_artifacts_consumed",
            "task_verifier_consumed",
            "gold_or_oracle_consumed",
            "reward_consumed",
        )
    ):
        raise ValueError("unified_closure_d48_scope_invalid")
    return str(facts["facts_hash"])


def _validate_component(component: str, facts: dict[str, Any], package: Path) -> str:
    if component == "d48_typed_contract":
        return _d48_validate(facts, package)
    validators = {
        "multilang_typed_contract": multilang.validate_public_multilang_contract_set,
        "parameter_flow": parameter_flow.validate_public_python_parameter_closure,
        "cli_interface": cli_interface.validate_public_cli_interface_closure,
        "call_binding": call_binding.validate_public_python_call_binding_closure,
        "behavior_path": behavior_path.validate_public_python_behavior_path_closure,
        "helper_edge": helper_edge.validate_public_python_helper_edge_closure,
    }
    return str(validators[component](facts, package))


def _obligation_map(component: str, facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    nodes = list(facts.get("editable_nodes") or [])
    obligations = list(facts.get("obligations") or [])
    if component == "multilang_typed_contract":
        obligation = copy.deepcopy(facts.get("obligation") or {})
        return {str(row["node_id"]): obligation for row in nodes}
    by_id = {
        str(row["editable_node_id"]): copy.deepcopy(row)
        for row in obligations
        if row.get("editable_node_id")
    }
    if by_id:
        return by_id
    if len(nodes) == len(obligations):
        return {
            str(node["node_id"]): copy.deepcopy(obligation)
            for node, obligation in zip(nodes, obligations, strict=True)
        }
    if len(nodes) == 1 and facts.get("obligation"):
        return {str(nodes[0]["node_id"]): copy.deepcopy(facts["obligation"])}
    return {str(row["node_id"]): {} for row in nodes}


def _bounded_text(value: Any, *, maximum_bytes: int = 6000) -> str:
    text = str(value or "")
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return text
    clipped = encoded[:maximum_bytes].decode("utf-8", errors="ignore")
    return clipped + "\n[context clipped]"


def _compact_source_context(
    target: Path, line: int, end_line: int, *, radius: int = 5
) -> str:
    lines = target.read_text(encoding="utf-8").splitlines()
    start = max(1, int(line or 1) - radius)
    stop = min(len(lines), max(int(end_line or line or 1), int(line or 1)) + radius)
    return _bounded_text(
        "\n".join(f"{number}: {lines[number - 1]}" for number in range(start, stop + 1))
    )


def _public_node_context(source: dict[str, Any], target: Path) -> dict[str, Any]:
    line = int(source.get("line") or source.get("start_line") or 0)
    end_line = int(source.get("end_line") or line)
    supplied = source.get("context_source")
    context: dict[str, Any] = {
        "context_source": _bounded_text(supplied)
        if supplied
        else _compact_source_context(target, line, end_line),
        "context_origin": "component_source"
        if supplied
        else "package_local_line_window",
    }
    for key in ("facts", "selection_evidence", "selection_reasons"):
        if source.get(key) not in (None, [], {}):
            context[key] = copy.deepcopy(source[key])
    return context


def _regular_records(
    component: str, facts: dict[str, Any], package: Path
) -> list[dict[str, Any]]:
    obligations = _obligation_map(component, facts)
    records = []
    for source in facts.get("editable_nodes") or []:
        relative = str(source.get("path") or source.get("caller_path") or "")
        target = _safe_path(package, relative)
        span = _span_from_row(package, source)
        encoded = target.read_bytes()
        observed = str(
            source.get("observed_source")
            or source.get("observed_expression")
            or encoded[span["start"] : span["end"]].decode("utf-8")
        )
        if encoded[span["start"] : span["end"]].decode("utf-8") != observed:
            raise ValueError(f"unified_closure_observed_source_drift:{component}:{relative}")
        node_id = str(source["node_id"])
        records.append(
            {
                "target_node_id": node_id,
                "source_component_node_id": node_id,
                "component": component,
                "top_level_family": TOP_LEVEL_FAMILY[component],
                "path": relative,
                "language": str(source.get("language") or "python"),
                "node_type": str(source.get("node_type") or ""),
                "role": str(source.get("role") or ""),
                "symbol": str(source.get("symbol") or source.get("function_symbol") or ""),
                "line": int(source.get("line") or source.get("start_line") or 0),
                "column": int(source.get("column") or source.get("start_column") or 0),
                "end_line": int(source.get("end_line") or source.get("line") or 0),
                "end_column": int(source.get("end_column") or 0),
                "byte_span": span,
                "file_sha256": sha256_file(target),
                "node_sha256": str(
                    source.get("node_sha256")
                    or source.get("source_sha256")
                    or sha256_bytes(observed.encode("utf-8"))
                ),
                "observed_source": observed,
                "public_evidence": obligations.get(node_id) or {},
                "public_node_context": _public_node_context(source, target),
                "expected_replacement": None,
            }
        )
    return records


def _d48_records(facts: dict[str, Any], package: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    editable = {str(row["node_id"]): row for row in facts.get("editable_nodes") or []}
    formal_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    resolver_findings: list[dict[str, Any]] = []
    for finding in facts.get("findings") or []:
        kind = str(finding.get("kind") or "")
        if kind == "formal_origin_mismatch" and str(finding.get("node_id")) in editable:
            formal_groups[str(finding["node_id"])].append(finding)
        elif kind == "argument_to_resolver_return_disconnect":
            candidate_ids = {
                str(row.get("node_id")) for row in finding.get("candidate_sites") or []
            }
            if candidate_ids & set(editable):
                resolver_findings.append(finding)
        elif kind not in {"formal_origin_mismatch"}:
            raise ValueError(f"unified_closure_d48_family_unsupported:{kind}")
    for source_node_id, group in sorted(formal_groups.items()):
        relative = str(group[0]["caller_path"])
        target = _safe_path(package, relative)
        source_text = target.read_text(encoding="utf-8")
        source_bytes = target.read_bytes()
        tree = ast.parse(source_text, filename=relative)
        call = formal_origin._locate_call(tree, group[0])
        if len(group) == 1:
            finding = group[0]
            argument = formal_origin._argument_node(call, finding)
            observed = ast.get_source_segment(source_text, argument) or ""
            span = _byte_span_from_ast(source_bytes, argument)
            records.append(
                {
                    "target_node_id": _node_id(relative, argument),
                    "source_component_node_id": source_node_id,
                    "component": "d48_typed_contract",
                    "top_level_family": "typed_contract",
                    "path": relative,
                    "language": "python",
                    "node_type": type(argument).__name__,
                    "role": f"{finding['call_kind']}:{finding['call_slot']}",
                    "symbol": str(finding["caller_symbol"]),
                    "line": int(argument.lineno),
                    "column": int(argument.col_offset),
                    "end_line": int(argument.end_lineno),
                    "end_column": int(argument.end_col_offset),
                    "byte_span": span,
                    "file_sha256": sha256_file(target),
                    "node_sha256": _node_hash(argument),
                    "observed_source": observed,
                    "public_evidence": copy.deepcopy(finding),
                    "public_node_context": _public_node_context(
                        editable[source_node_id], target
                    ),
                    "expected_replacement": str(finding["callee_formal_parameter"]),
                    "expected_replacement_ast": None,
                }
            )
            continue
        rewritten = copy.deepcopy(call)
        for finding in group:
            argument = formal_origin._argument_node(rewritten, finding)
            replacement = ast.copy_location(
                ast.Name(id=str(finding["callee_formal_parameter"]), ctx=ast.Load()),
                argument,
            )
            if finding["call_kind"] == "positional":
                rewritten.args[int(finding["call_slot"])] = replacement
            else:
                for item in rewritten.keywords:
                    if item.arg == str(finding["call_slot"]):
                        item.value = replacement
        observed = ast.get_source_segment(source_text, call) or ""
        records.append(
            {
                "target_node_id": _node_id(relative, call),
                "source_component_node_id": source_node_id,
                "component": "d48_typed_contract",
                "top_level_family": "typed_contract",
                "path": relative,
                "language": "python",
                "node_type": "Call",
                "role": "multi_argument_formal_origin_closure",
                "symbol": str(group[0]["caller_symbol"]),
                "line": int(call.lineno),
                "column": int(call.col_offset),
                "end_line": int(call.end_lineno),
                "end_column": int(call.end_col_offset),
                "byte_span": _byte_span_from_ast(source_bytes, call),
                "file_sha256": sha256_file(target),
                "node_sha256": _node_hash(call),
                "observed_source": observed,
                "public_evidence": {
                    "kind": "multi_argument_formal_origin_closure",
                    "repair_goal": group[0]["repair_goal"],
                    "call_source": observed,
                    "argument_obligations": copy.deepcopy(group),
                },
                "public_node_context": _public_node_context(
                    editable[source_node_id], target
                ),
                "expected_replacement": ast.unparse(rewritten),
                "expected_replacement_ast": ast.dump(
                    rewritten, include_attributes=False
                ),
            }
        )
    for finding in resolver_findings:
        sites = list(finding.get("candidate_sites") or [])
        if len(sites) != 1:
            raise ValueError("unified_closure_d48_resolver_site_not_unique")
        site = editable.get(str(sites[0]["node_id"]))
        if site is None:
            raise ValueError("unified_closure_d48_resolver_site_missing")
        records.extend(_regular_records("d48_typed_contract", {"editable_nodes": [site]}, package))
        records[-1]["public_evidence"] = copy.deepcopy(finding)
        records[-1]["expected_replacement"] = str(finding["callee_formal"])
        records[-1]["expected_replacement_ast"] = None
    if len(records) != len(editable):
        raise ValueError("unified_closure_d48_record_count_mismatch")
    return records


def _component_records(
    component: str, facts: dict[str, Any], package: Path
) -> list[dict[str, Any]]:
    _validate_component(component, facts, package)
    if component == "d48_typed_contract":
        return _d48_records(facts, package)
    return _regular_records(component, facts, package)


def _canonicalize_records(
    records_by_component: dict[str, list[dict[str, Any]]]
) -> tuple[list[dict[str, Any]], list[str], list[str], list[dict[str, Any]]]:
    canonical: list[dict[str, Any]] = []
    overlap_receipts: list[dict[str, Any]] = []
    flattened = [copy.deepcopy(row) for records in records_by_component.values() for row in records]
    flattened.sort(
        key=lambda row: (
            COMPONENT_PRIORITY[row["component"]],
            row["path"],
            int(row["byte_span"]["start"]),
            int(row["byte_span"]["end"]) - int(row["byte_span"]["start"]),
            row["component"],
            row["target_node_id"],
        )
    )
    for candidate in flattened:
        overlaps = [
            selected
            for selected in canonical
            if selected["path"] == candidate["path"]
            and int(selected["byte_span"]["start"]) < int(candidate["byte_span"]["end"])
            and int(candidate["byte_span"]["start"]) < int(selected["byte_span"]["end"])
        ]
        if not overlaps:
            candidate["corroborating_components"] = []
            candidate["corroborating_node_ids"] = []
            candidate["corroborating_public_evidence"] = []
            canonical.append(candidate)
            continue
        for selected in overlaps:
            selected["corroborating_components"] = sorted(
                set(selected["corroborating_components"]) | {candidate["component"]}
            )
            selected["corroborating_node_ids"] = sorted(
                set(selected["corroborating_node_ids"]) | {candidate["target_node_id"]}
            )
            corroboration = {
                "component": candidate["component"],
                "source_component_node_id": candidate["source_component_node_id"],
                "node_type": candidate["node_type"],
                "role": candidate["role"],
                "symbol": candidate["symbol"],
                "observed_source": candidate["observed_source"],
                "public_structural_evidence": copy.deepcopy(
                    candidate.get("public_evidence") or {}
                ),
                "public_node_context": copy.deepcopy(
                    candidate.get("public_node_context") or {}
                ),
            }
            selected["corroborating_public_evidence"].append(corroboration)
            overlap_receipts.append(
                {
                    "path": candidate["path"],
                    "primary_node_id": selected["target_node_id"],
                    "primary_component": selected["component"],
                    "corroborating_node_id": candidate["target_node_id"],
                    "corroborating_component": candidate["component"],
                    "corroborating_evidence_hash": canonical_json_hash(corroboration),
                }
            )
    primary_components = {str(row["component"]) for row in canonical}
    all_components = set(records_by_component)
    ordered = sorted(
        canonical,
        key=lambda row: (row["path"], row["byte_span"]["start"], row["target_node_id"]),
    )
    for index, row in enumerate(ordered, start=1):
        row["rank"] = index
        row["corroborating_public_evidence"] = sorted(
            row["corroborating_public_evidence"],
            key=lambda item: (
                item["component"],
                item["source_component_node_id"],
            ),
        )
    return ordered, sorted(primary_components), sorted(all_components), overlap_receipts


def build_unified_public_closure(
    package_root: str | Path,
    request_text: str,
    source_components: Iterable[dict[str, Any]],
    *,
    control_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    components = [copy.deepcopy(row) for row in source_components]
    by_component = {str(row["component"]): row for row in components}
    if len(by_component) != len(components) or not components:
        raise ValueError("unified_closure_component_set_invalid")
    records = {
        component: _component_records(component, row["facts"], package)
        for component, row in by_component.items()
    }
    nodes, retained, contributing, overlap_receipts = _canonicalize_records(records)
    retained_payloads = [by_component[name] for name in retained]
    corroborating_payloads = [
        by_component[name] for name in contributing if name not in set(retained)
    ]
    families = sorted({TOP_LEVEL_FAMILY[name] for name in contributing})
    if not 1 <= len(nodes) <= MAX_EDITABLE_NODES:
        raise ValueError(f"unified_closure_node_bound_exceeded:{len(nodes)}")
    if len(families) > MAX_TOP_LEVEL_FAMILIES:
        raise ValueError(f"unified_closure_family_bound_exceeded:{families}")
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "status": "public_unified_closure_available",
        "package_tree_hash": _tree_hash(package),
        "request_text": request_text,
        "request_hash": canonical_json_hash({"request": request_text}),
        "localization_decision": {
            "decision": "PROPOSE",
            "editable_node_count": len(nodes),
            "component_count": len(contributing),
            "primary_component_count": len(retained),
            "corroborating_component_count": len(corroborating_payloads),
            "top_level_family_count": len(families),
            "top_level_families": families,
        },
        "editable_nodes": nodes,
        "components": retained_payloads,
        "component_names": retained,
        "corroborating_components": corroborating_payloads,
        "corroborating_component_names": [
            str(row["component"]) for row in corroborating_payloads
        ],
        "all_source_component_names": contributing,
        "overlap_normalization_receipts": overlap_receipts,
        "edit_contract": {
            "atomic_closure_required": True,
            "atomic_decision_required": True,
            "allowed_decisions": [EDIT_ALL, ABSTAIN_TO_RAW],
            "abstain_when_full_package_contradicts_hypothesis": True,
            "all_listed_target_node_ids_required": True,
            "exact_node_binding_required": True,
            "maximum_edits": MAX_EDITABLE_NODES,
            "maximum_top_level_families": MAX_TOP_LEVEL_FAMILIES,
            "outside_selected_nodes_preserved_by_construction": True,
            "public_signatures_must_be_preserved": True,
            "all_component_residual_checks_required": True,
            "semantic_correctness_inferred": False,
        },
        "control_metadata": copy.deepcopy(control_metadata),
        "source_scope": (
            "public request, public SKILL.md/scripts, frozen D39 raw proposal, and "
            "runtime-generated structural components only"
        ),
        "benchmark_bundled_facts_consumed": False,
        "hidden_artifacts_consumed": False,
        "task_verifier_consumed": False,
        "gold_or_oracle_consumed": False,
        "reward_consumed": False,
        "semantic_correctness_inferred": False,
        "claim_boundary": (
            "The packet localizes and composes public structural obligations. It does not "
            "establish task-level semantic correctness."
        ),
    }
    result["facts_hash"] = canonical_json_hash(result)
    validate_unified_public_closure(result, package)
    return result


def _component_sham(
    component: str, package: Path, real_facts: dict[str, Any]
) -> dict[str, Any]:
    if component == "d48_typed_contract":
        return d48_control.build_same_package_wrong_structure_packet(package, real_facts)
    builders = {
        "multilang_typed_contract": multilang_control.build_same_package_wrong_node_set,
        "parameter_flow": parameter_flow.build_same_package_wrong_closure,
        "cli_interface": cli_interface.build_same_package_wrong_cli_closure,
        "call_binding": call_binding.build_same_package_wrong_call_binding_closure,
        "behavior_path": behavior_path.build_same_package_wrong_behavior_path_closure,
        "helper_edge": helper_edge.build_same_package_wrong_helper_edge_closure,
    }
    return builders[component](package, real_facts)


def build_same_package_wrong_unified_closure(
    package_root: str | Path, real_facts: dict[str, Any]
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    validate_unified_public_closure(real_facts, package)
    sham_components = []
    for row in real_facts["components"]:
        component = str(row["component"])
        sham_components.append(
            {
                **{key: copy.deepcopy(value) for key, value in row.items() if key != "facts"},
                "facts": _component_sham(component, package, row["facts"]),
            }
        )
    sham = build_unified_public_closure(
        package,
        str(real_facts["request_text"]),
        sham_components,
        control_metadata={
            "control": "same-package-wrong-contextual-unified-closure-v2",
            "source_real_facts_hash": real_facts["facts_hash"],
            "same_package": True,
            "selection_uses_hidden_or_verifier": False,
        },
    )
    decoy_payloads: list[dict[str, Any]] = []
    decoy_records: dict[str, list[dict[str, Any]]] = {}
    for row in real_facts.get("corroborating_components") or []:
        component = str(row["component"])
        wrong_facts = _component_sham(component, package, row["facts"])
        decoy_payloads.append(
            {
                **{
                    key: copy.deepcopy(value)
                    for key, value in row.items()
                    if key != "facts"
                },
                "facts": wrong_facts,
            }
        )
        decoy_records[component] = _component_records(component, wrong_facts, package)
    real_nodes = {
        (row["path"], row["byte_span"]["start"], row["byte_span"]["end"])
        for row in real_facts["editable_nodes"]
    }
    sham_nodes = {
        (row["path"], row["byte_span"]["start"], row["byte_span"]["end"])
        for row in sham["editable_nodes"]
    }
    if len(real_nodes) != len(sham_nodes) or real_nodes & sham_nodes:
        raise ValueError(
            "unified_closure_sham_shape_invalid:"
            f"real={len(real_nodes)}:sham={len(sham_nodes)}:overlap={len(real_nodes & sham_nodes)}"
        )
    body = copy.deepcopy(sham)
    body.pop("facts_hash", None)
    body["corroborating_components"] = decoy_payloads
    body["corroborating_component_names"] = [
        str(row["component"]) for row in decoy_payloads
    ]
    body["all_source_component_names"] = sorted(
        set(body["component_names"]) | set(body["corroborating_component_names"])
    )
    body["overlap_normalization_receipts"] = []
    for index, (real_node, sham_node) in enumerate(
        zip(real_facts["editable_nodes"], body["editable_nodes"], strict=True)
    ):
        attached: list[dict[str, Any]] = []
        attached_ids: list[str] = []
        attached_components: list[str] = []
        for evidence_index, real_evidence in enumerate(
            real_node.get("corroborating_public_evidence") or []
        ):
            component = str(real_evidence["component"])
            choices = decoy_records.get(component) or []
            if not choices:
                raise ValueError(
                    f"unified_closure_sham_corroboration_missing:{component}"
                )
            decoy = choices[(index + evidence_index) % len(choices)]
            corroboration = {
                "component": component,
                "source_component_node_id": decoy["source_component_node_id"],
                "node_type": decoy["node_type"],
                "role": decoy["role"],
                "symbol": decoy["symbol"],
                "observed_source": decoy["observed_source"],
                "public_structural_evidence": copy.deepcopy(
                    decoy.get("public_evidence") or {}
                ),
                "public_node_context": copy.deepcopy(
                    decoy.get("public_node_context") or {}
                ),
            }
            attached.append(corroboration)
            attached_ids.append(str(decoy["target_node_id"]))
            attached_components.append(component)
            body["overlap_normalization_receipts"].append(
                {
                    "path": sham_node["path"],
                    "primary_node_id": sham_node["target_node_id"],
                    "primary_component": sham_node["component"],
                    "corroborating_node_id": decoy["target_node_id"],
                    "corroborating_component": component,
                    "corroborating_evidence_hash": canonical_json_hash(corroboration),
                    "control_relation": "same_package_wrong_corroboration",
                }
            )
        sham_node["corroborating_public_evidence"] = attached
        sham_node["corroborating_node_ids"] = sorted(set(attached_ids))
        sham_node["corroborating_components"] = sorted(set(attached_components))
    families = sorted(
        {TOP_LEVEL_FAMILY[name] for name in body["all_source_component_names"]}
    )
    body["localization_decision"].update(
        {
            "component_count": len(body["all_source_component_names"]),
            "primary_component_count": len(body["component_names"]),
            "corroborating_component_count": len(
                body["corroborating_component_names"]
            ),
            "top_level_family_count": len(families),
            "top_level_families": families,
        }
    )
    body["control_metadata"] = {
        **(body.get("control_metadata") or {}),
        "same_editable_node_count": True,
        "real_sham_node_sets_disjoint": True,
        "real_node_count": len(real_nodes),
        "sham_node_count": len(sham_nodes),
    }
    body["facts_hash"] = canonical_json_hash(body)
    validate_unified_public_closure(body, package)
    return body


def validate_unified_public_closure(
    facts: dict[str, Any], package_root: str | Path
) -> str:
    package = Path(package_root).resolve()
    if not _embedded_hash_valid(facts):
        raise ValueError("unified_closure_facts_hash_invalid")
    if facts.get("schema_version") != SCHEMA_VERSION or facts.get("method") != METHOD_ID:
        raise ValueError("unified_closure_schema_invalid")
    if facts.get("package_tree_hash") != _tree_hash(package):
        raise ValueError("unified_closure_package_changed")
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
        raise ValueError("unified_closure_scope_invalid")
    nodes = list(facts.get("editable_nodes") or [])
    if not 1 <= len(nodes) <= MAX_EDITABLE_NODES:
        raise ValueError("unified_closure_node_count_invalid")
    if len({str(row["target_node_id"]) for row in nodes}) != len(nodes):
        raise ValueError("unified_closure_duplicate_target_id")
    spans: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for row in nodes:
        target = _safe_path(package, str(row["path"]))
        if sha256_file(target) != row["file_sha256"]:
            raise ValueError("unified_closure_file_hash_changed")
        encoded = target.read_bytes()
        start, end = int(row["byte_span"]["start"]), int(row["byte_span"]["end"])
        if encoded[start:end].decode("utf-8") != row["observed_source"]:
            raise ValueError("unified_closure_node_source_changed")
        if any(start < prior_end and prior_start < end for prior_start, prior_end in spans[row["path"]]):
            raise ValueError("unified_closure_overlapping_canonical_nodes")
        spans[row["path"]].append((start, end))
    components = list(facts.get("components") or [])
    component_names = [str(row["component"]) for row in components]
    if component_names != list(facts.get("component_names") or []):
        raise ValueError("unified_closure_component_names_invalid")
    for row in components:
        _validate_component(str(row["component"]), row["facts"], package)
    corroborating = list(facts.get("corroborating_components") or [])
    corroborating_names = [str(row["component"]) for row in corroborating]
    if corroborating_names != list(facts.get("corroborating_component_names") or []):
        raise ValueError("unified_closure_corroborating_component_names_invalid")
    if set(component_names) & set(corroborating_names):
        raise ValueError("unified_closure_component_roles_overlap")
    for row in corroborating:
        _validate_component(str(row["component"]), row["facts"], package)
    all_names = sorted(set(component_names) | set(corroborating_names))
    if all_names != list(facts.get("all_source_component_names") or []):
        raise ValueError("unified_closure_all_component_names_invalid")
    for row in nodes:
        context = row.get("public_node_context") or {}
        if not str(context.get("context_source") or "").strip():
            raise ValueError("unified_closure_public_context_missing")
        evidence = list(row.get("corroborating_public_evidence") or [])
        if sorted({str(item["component"]) for item in evidence}) != list(
            row.get("corroborating_components") or []
        ):
            raise ValueError("unified_closure_corroborating_evidence_shape_invalid")
    families = {TOP_LEVEL_FAMILY[name] for name in all_names}
    if len(families) > MAX_TOP_LEVEL_FAMILIES:
        raise ValueError("unified_closure_too_many_families")
    return str(facts["facts_hash"])


def prompt_packet_view(facts: dict[str, Any]) -> dict[str, Any]:
    nodes = []
    for row in facts.get("editable_nodes") or []:
        evidence = copy.deepcopy(row.get("public_evidence") or {})
        evidence.pop("finding_hash", None)
        evidence.pop("source_finding_hashes", None)
        nodes.append(
            {
                "target_node_id": row["target_node_id"],
                "path": row["path"],
                "language": row["language"],
                "node_type": row["node_type"],
                "role": row["role"],
                "symbol": row["symbol"],
                "line": row["line"],
                "column": row["column"],
                "observed_source": row["observed_source"],
                "top_level_family": row["top_level_family"],
                "component": row["component"],
                "corroborating_components": row["corroborating_components"],
                "public_structural_evidence": evidence,
                "public_node_context": copy.deepcopy(row["public_node_context"]),
                "corroborating_public_evidence": copy.deepcopy(
                    row.get("corroborating_public_evidence") or []
                ),
            }
        )
    return {
        "method": METHOD_ID,
        "localization_decision": facts["localization_decision"],
        "editable_nodes": nodes,
        "edit_contract": facts["edit_contract"],
        "locator_protocol": {
            "model_submits": ["decision", "target_node_id", "replacement"],
            "framework_completes_all_spans_and_hashes": True,
            "all_listed_target_node_ids_required_exactly_once_for_edit_all": True,
            "abstain_to_raw_requires_zero_edits": True,
            "structural_packet_is_rejectable_hypothesis": True,
            "semantic_replacement_completed_or_rewritten": False,
            "unknown_or_overlapping_nodes_rejected": True,
        },
        "claim_boundary": facts["claim_boundary"],
    }


def _parse_payload(content: str) -> tuple[dict[str, Any], list[str]]:
    duplicate_fields: list[str] = []

    def object_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                if result[key] != value:
                    raise ValueError(f"conflicting_duplicate_json_key:{key}")
                duplicate_fields.append(key)
                continue
            result[key] = value
        return result

    payload = json.loads(content, object_pairs_hook=object_hook)
    if not isinstance(payload, dict) or set(payload) != {
        "decision",
        "edits",
        "summary",
    }:
        raise ValueError("unified_closure_response_schema_invalid")
    if payload["decision"] not in {EDIT_ALL, ABSTAIN_TO_RAW}:
        raise ValueError("unified_closure_decision_invalid")
    if not isinstance(payload["summary"], str):
        raise TypeError("unified_closure_summary_must_be_string")
    if not isinstance(payload["edits"], list) or len(payload["edits"]) > MAX_EDITABLE_NODES:
        raise ValueError("unified_closure_edits_outside_bound")
    return payload, sorted(duplicate_fields)


def _python_expression(source: str) -> ast.expr:
    try:
        return ast.parse(source, mode="eval").body
    except SyntaxError as exc:
        raise ValueError("unified_closure_replacement_not_python_expression") from exc


def _validate_replacement(row: dict[str, Any], replacement: Any) -> str:
    if not isinstance(replacement, str) or not replacement.strip():
        raise ValueError("unified_closure_replacement_empty")
    value = replacement.strip()
    if "\n" in value or len(value.encode("utf-8")) > 4096:
        raise ValueError("unified_closure_replacement_not_single_line")
    if value == str(row["observed_source"]).strip():
        raise ValueError("unified_closure_noop_replacement")
    component = str(row["component"])
    if component == "d48_typed_contract":
        parsed = _python_expression(value)
        expected_ast = row.get("expected_replacement_ast")
        if expected_ast:
            if ast.dump(parsed, include_attributes=False) != expected_ast:
                raise ValueError("unified_closure_d48_expected_flow_not_restored")
        elif value != str(row["expected_replacement"]):
            raise ValueError("unified_closure_d48_expected_flow_not_restored")
    elif component == "call_binding":
        if not value.isidentifier() or keyword.iskeyword(value):
            raise ValueError("unified_closure_call_binding_not_identifier")
    elif component == "behavior_path":
        if row["node_type"] == "BoolOp":
            if value not in {"and", "or"}:
                raise ValueError("unified_closure_bool_operator_invalid")
        else:
            behavior_executor._one_safe_statement(value)
    elif component == "helper_edge":
        helper_executor._validate_direct_call(value, row["public_evidence"])
    elif row["language"] == "python":
        _python_expression(value)
    return value


def _normalize_edits(
    payload: dict[str, Any], facts: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if payload["decision"] == ABSTAIN_TO_RAW:
        if payload["edits"]:
            raise ValueError("unified_closure_abstain_requires_zero_edits")
        return [], []
    if payload["decision"] != EDIT_ALL:
        raise ValueError("unified_closure_edit_decision_invalid")
    registry = {str(row["target_node_id"]): row for row in facts["editable_nodes"]}
    required = set(registry)
    seen: set[str] = set()
    edits: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    for index, raw in enumerate(payload["edits"]):
        if not isinstance(raw, dict) or set(raw) != {"target_node_id", "replacement"}:
            raise ValueError(f"unified_closure_compact_edit_invalid:{index}")
        node_id = str(raw["target_node_id"])
        if node_id not in registry or node_id in seen:
            raise ValueError("unified_closure_unknown_or_duplicate_target")
        row = registry[node_id]
        replacement = _validate_replacement(row, raw["replacement"])
        seen.add(node_id)
        edits.append({**copy.deepcopy(row), "replacement": replacement})
        receipts.append(
            {
                "edit_index": index,
                "target_node_id": node_id,
                "component": row["component"],
                "locator_fields_completed_from_frozen_packet": True,
                "semantic_replacement_completed_or_rewritten": False,
            }
        )
    if seen != required:
        raise ValueError(
            f"unified_closure_atomic_target_set_required:missing={sorted(required-seen)}"
        )
    return edits, receipts


def _apply_exact_edits(source: Path, candidate: Path, edits: list[dict[str, Any]]) -> list[str]:
    copy_tree_clean(source, candidate)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edit in edits:
        grouped[str(edit["path"])].append(edit)
    changed: list[str] = []
    for relative, rows in grouped.items():
        source_file = _safe_path(source, relative)
        target_file = _safe_path(candidate, relative)
        if any(sha256_file(source_file) != row["file_sha256"] for row in rows):
            raise ValueError("unified_closure_source_file_hash_mismatch")
        original = source_file.read_bytes()
        rendered = original
        prior_start = len(original) + 1
        for row in sorted(rows, key=lambda item: item["byte_span"]["start"], reverse=True):
            start = int(row["byte_span"]["start"])
            end = int(row["byte_span"]["end"])
            if not 0 <= start < end <= len(original) or end > prior_start:
                raise ValueError("unified_closure_invalid_or_overlapping_spans")
            if original[start:end].decode("utf-8") != row["observed_source"]:
                raise ValueError("unified_closure_observed_source_mismatch")
            rendered = rendered[:start] + row["replacement"].encode("utf-8") + rendered[end:]
            prior_start = start
        target_file.write_bytes(rendered)
        changed.append(relative)
    return sorted(changed)


def _analyze_exact_candidate_scope(
    source: Path, candidate: Path, edits: list[dict[str, Any]]
) -> dict[str, Any]:
    parent_hashes = hash_tree(source)
    candidate_hashes = hash_tree(candidate)
    changed_paths = sorted(
        path
        for path in set(parent_hashes) | set(candidate_hashes)
        if parent_hashes.get(path) != candidate_hashes.get(path)
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edit in edits:
        grouped[str(edit["path"])].append(edit)
    outside: list[dict[str, Any]] = []
    touched: list[str] = []
    intervals: list[dict[str, Any]] = []
    for relative in sorted(set(changed_paths) | set(grouped)):
        rows = grouped.get(relative, [])
        parent_file = source / relative
        candidate_file = candidate / relative
        if not parent_file.is_file() or not candidate_file.is_file():
            outside.append({"path": relative, "reason": "file_added_or_removed"})
            continue
        if not rows:
            outside.append({"path": relative, "reason": "changed_path_without_frozen_edit"})
            continue
        original = parent_file.read_bytes()
        expected = original
        for row in sorted(rows, key=lambda item: item["byte_span"]["start"], reverse=True):
            start = int(row["byte_span"]["start"])
            end = int(row["byte_span"]["end"])
            replacement = str(row["replacement"]).encode("utf-8")
            expected = expected[:start] + replacement + expected[end:]
            touched.append(str(row["target_node_id"]))
            intervals.append(
                {
                    "path": relative,
                    "parent_byte_start": start,
                    "parent_byte_end": end,
                    "target_node_id": row["target_node_id"],
                    "replacement_sha256": sha256_bytes(replacement),
                }
            )
        if candidate_file.read_bytes() != expected:
            outside.append(
                {"path": relative, "reason": "candidate_differs_from_exact_span_reconstruction"}
            )
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "parent_tree_hash": canonical_json_hash(parent_hashes),
        "candidate_tree_hash": canonical_json_hash(candidate_hashes),
        "changed_paths": changed_paths,
        "changed_intervals": sorted(
            intervals,
            key=lambda row: (row["path"], row["parent_byte_start"], row["target_node_id"]),
        ),
        "touched_editable_node_ids": sorted(touched),
        "outside_selected_node_changes": outside,
        "checks": {
            "candidate_changed": bool(changed_paths),
            "all_changes_equal_exact_span_reconstruction": not outside,
        },
        "hidden_artifacts_consumed": False,
        "task_verifier_consumed": False,
        "semantic_correctness_inferred": False,
    }
    result["scope_hash"] = canonical_json_hash(result)
    return result


def _syntax_findings(candidate: Path, changed_paths: list[str]) -> list[str]:
    findings: list[str] = []
    for path in sorted((candidate / "scripts").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError, ValueError) as exc:
            findings.append(f"{path.relative_to(candidate)}:{type(exc).__name__}:{exc}")
    if any(Path(path).suffix.casefold() in {".js", ".mjs", ".cjs", ".ts"} for path in changed_paths):
        try:
            enumerate_package_nodes(candidate, include_markdown=False)
        except Exception as exc:
            findings.append(f"non_python_parser:{type(exc).__name__}:{exc}")
    for relative in changed_paths:
        if Path(relative).suffix.casefold() not in {".sh", ".bash"}:
            continue
        completed = subprocess.run(
            ["/bin/bash", "-n", str(candidate / relative)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode:
            findings.append(f"{relative}:bash_n:{completed.stderr[-600:]}")
    return findings


def _d48_candidate_gate(candidate: Path, component_facts: dict[str, Any]) -> dict[str, Any]:
    # D48 replacement equivalence is checked against the frozen public flow contract
    # before application. Exact-node scope, syntax, and signatures are checked globally.
    return {
        "decision": ACCEPT_STRUCTURALLY,
        "expected_flow_equivalence_validated_before_application": True,
        "residual": [],
    }


def _component_gate(
    component: str,
    source: Path,
    candidate: Path,
    request_text: str,
    component_facts: dict[str, Any],
) -> dict[str, Any]:
    if component == "d48_typed_contract":
        return _d48_candidate_gate(candidate, component_facts)
    if component == "parameter_flow":
        gate = parameter_flow.analyze_candidate_closure(
            source, candidate, request_text, frozen_facts=component_facts
        )
        return {"decision": gate["decision"], "details": gate}
    if component == "cli_interface":
        gate = cli_interface.analyze_candidate_cli_closure(
            source, candidate, request_text, frozen_facts=component_facts
        )
        return {"decision": gate["decision"], "details": gate}
    if component == "call_binding":
        residual = call_binding.residual_obligations(candidate, component_facts)
    elif component == "behavior_path":
        residual = behavior_path.residual_obligations(candidate, component_facts)
    elif component == "helper_edge":
        residual = helper_edge.residual_obligations(candidate, component_facts)
    elif component == "multilang_typed_contract":
        current = multilang.build_public_multilang_contract_set(candidate, request_text)
        target_family = str(component_facts["localization_decision"]["selected_family"])
        residual = (
            list(current.get("editable_nodes") or [])
            if (current.get("localization_decision") or {}).get("decision") == "PROPOSE"
            and (current.get("localization_decision") or {}).get("selected_family")
            == target_family
            else []
        )
    else:
        raise ValueError(f"unified_closure_unknown_component:{component}")
    return {
        "decision": ACCEPT_STRUCTURALLY if not residual else REVISE,
        "residual": residual,
    }


def apply_unified_public_closure_patch(
    content: str,
    source_package: str | Path,
    candidate_package: str | Path,
    *,
    frozen_facts: dict[str, Any],
) -> dict[str, Any]:
    source = Path(source_package).resolve()
    candidate = Path(candidate_package).resolve()
    validate_unified_public_closure(frozen_facts, source)
    payload, duplicates = _parse_payload(content)
    edits, receipts = _normalize_edits(payload, frozen_facts)
    parent_hashes = hash_tree(source)
    if payload["decision"] == ABSTAIN_TO_RAW:
        copy_tree_clean(source, candidate)
        candidate_hashes = hash_tree(candidate)
        checks = {
            "explicit_abstain_decision": True,
            "zero_edits_submitted": not edits,
            "candidate_exactly_matches_source": candidate_hashes == parent_hashes,
            "public_packet_treated_as_rejectable_hypothesis": True,
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "method": METHOD_ID,
            "model_decision": ABSTAIN_TO_RAW,
            "summary": payload["summary"],
            "normalized_edits": [],
            "protocol_normalization": {
                "compact_locator_receipts": [],
                "identical_duplicate_json_fields_collapsed": duplicates,
                "semantic_replacement_completed_or_rewritten": False,
            },
            "structural_gate": {"decision": ABSTAIN_TO_RAW, "checks": checks},
            "scope_gate": {
                "changed_paths": [],
                "outside_selected_node_changes": [],
                "touched_editable_node_ids": [],
            },
            "component_gates": {},
            "changed_paths": [],
            "syntax_findings": [],
            "parent_tree_hash": canonical_json_hash(parent_hashes),
            "candidate_tree_hash": canonical_json_hash(candidate_hashes),
            "hidden_artifacts_consumed": False,
            "task_verifier_consumed": False,
            "gold_or_oracle_consumed": False,
            "reward_consumed": False,
            "semantic_correctness_inferred": False,
            "claim_boundary": (
                "The model rejected the public structural hypothesis; the frozen Raw "
                "package is preserved exactly."
            ),
        }
    changed_paths = _apply_exact_edits(source, candidate, edits)
    syntax_findings = _syntax_findings(candidate, changed_paths)
    candidate_hashes = hash_tree(candidate)
    actual_changed = sorted(
        path
        for path in set(parent_hashes) | set(candidate_hashes)
        if parent_hashes.get(path) != candidate_hashes.get(path)
    )
    scope_gate = _analyze_exact_candidate_scope(source, candidate, edits)
    component_gates = {
        str(row["component"]): _component_gate(
            str(row["component"]),
            source,
            candidate,
            str(frozen_facts["request_text"]),
            row["facts"],
        )
        for row in frozen_facts["components"]
    }
    checks = {
        "candidate_exists": candidate.is_dir(),
        "tree_scope_preserved": set(parent_hashes) == set(candidate_hashes),
        "syntax_valid": not syntax_findings,
        "public_python_signatures_preserved": _public_signatures(source)
        == _public_signatures(candidate),
        "all_required_node_ids_submitted": len(edits)
        == len(frozen_facts["editable_nodes"]),
        "changed_paths_match_materialized_edits": actual_changed == changed_paths,
        "all_changes_inside_frozen_exact_nodes": not scope_gate[
            "outside_selected_node_changes"
        ],
        "all_frozen_nodes_touched": set(scope_gate["touched_editable_node_ids"])
        == {str(row["target_node_id"]) for row in frozen_facts["editable_nodes"]},
        "all_component_residual_gates_accept": all(
            row["decision"] == ACCEPT_STRUCTURALLY for row in component_gates.values()
        ),
        "changed_paths_within_bound": len(actual_changed) <= MAX_EDITABLE_NODES
        and all(path.startswith("scripts/") for path in actual_changed),
    }
    decision = ACCEPT_STRUCTURALLY if all(checks.values()) else REVISE
    return {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "model_decision": EDIT_ALL,
        "summary": payload["summary"],
        "normalized_edits": edits,
        "protocol_normalization": {
            "compact_locator_receipts": receipts,
            "identical_duplicate_json_fields_collapsed": duplicates,
            "semantic_replacement_completed_or_rewritten": False,
        },
        "structural_gate": {"decision": decision, "checks": checks},
        "scope_gate": scope_gate,
        "component_gates": component_gates,
        "changed_paths": actual_changed,
        "syntax_findings": syntax_findings,
        "parent_tree_hash": canonical_json_hash(parent_hashes),
        "candidate_tree_hash": canonical_json_hash(candidate_hashes),
        "hidden_artifacts_consumed": False,
        "task_verifier_consumed": False,
        "gold_or_oracle_consumed": False,
        "reward_consumed": False,
        "semantic_correctness_inferred": False,
        "claim_boundary": (
            "Acceptance establishes exact-node scope, parse/API safety, and closure of "
            "the frozen public structural components only."
        ),
    }


build_public_python_parameter_effect_set = build_unified_public_closure
build_same_package_wrong_node_set = build_same_package_wrong_unified_closure
validate_public_python_parameter_effect_set = validate_unified_public_closure
apply_public_node_bound_patch = apply_unified_public_closure_patch


__all__ = [
    "ACCEPT_STRUCTURALLY",
    "MAX_EDITABLE_NODES",
    "MAX_TOP_LEVEL_FAMILIES",
    "METHOD_ID",
    "MULTILANG_NODE_PATCH_TOOL",
    "PYTHON_NODE_PATCH_TOOL",
    "apply_public_node_bound_patch",
    "apply_unified_public_closure_patch",
    "build_same_package_wrong_node_set",
    "build_same_package_wrong_unified_closure",
    "build_unified_public_closure",
    "prompt_packet_view",
    "validate_unified_public_closure",
]
