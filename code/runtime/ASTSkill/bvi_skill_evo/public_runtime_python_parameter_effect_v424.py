from __future__ import annotations

import ast
import copy
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from bvi_skill_evo.balanced_hybrid_ast_v316 import _assert_public_package, _tree_hash
from bvi_skill_evo.python_span_patch_v246 import (
    ACCEPT_STRUCTURALLY,
    PYTHON_SPAN_PATCH_TOOL,
    apply_python_span_patch,
)
from bvi_skill_evo.public_contract_flow_ast_v358 import (
    _documented_unused_parameter_facts,
    _parameter_flow_facts,
)
from bvi_skill_evo.public_runtime_multilang_dual_v410 import (
    analyze_candidate_scope,
    choose_public_runtime_dual_source,
)
from bvi_skill_evo.unused_parameter_ast import _node_hash, _node_id
from skillscriptbench.io_utils import canonical_json_hash, sha256_file


SCHEMA_VERSION = "4.24-public-runtime-python-parameter-effect-v1"
METHOD_ID = "public_runtime_python_parameter_effect_node_bound_v1"
FAMILY = "parameter_effect_contract"
SOURCE_FINDING_KINDS = {
    "documented_optional_parameter_unused",
    "parameter_to_local_alias_disconnect",
}
MIN_CONFIDENCE = 0.90

PYTHON_NODE_PATCH_TOOL = copy.deepcopy(PYTHON_SPAN_PATCH_TOOL)
PYTHON_NODE_PATCH_TOOL["function"]["description"] = (
    "Submit zero or one exact Python AST-node replacement for a visible executable Agent "
    "Skill package. The listed node is a public structural location hypothesis, not a "
    "semantic answer. Preserve the public signature and compatibility default."
)
PYTHON_NODE_PATCH_TOOL["function"]["parameters"]["properties"]["edits"][
    "maxItems"
] = 1


def _byte_offset(source: str, line: int, byte_column: int) -> int:
    lines = source.splitlines(keepends=True)
    if not 1 <= line <= len(lines):
        raise ValueError("python_node_line_out_of_range")
    prefix = lines[line - 1].encode("utf-8")[:byte_column].decode("utf-8")
    return len("".join(lines[: line - 1]).encode("utf-8")) + len(
        prefix.encode("utf-8")
    )


def _nearest_function(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parents.get(current)
    return None


def _inside_signature(
    node: ast.AST,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    current: ast.AST | None = node
    while current is not None and current is not function:
        parent = parents.get(current)
        if parent is function:
            return current is function.args or current in function.decorator_list or current is function.returns
        current = parent
    return False


def _python_nodes(package: Path) -> list[dict[str, Any]]:
    scripts = package / "scripts"
    rows: list[dict[str, Any]] = []
    if not scripts.is_dir():
        return rows
    for path in sorted(scripts.rglob("*.py")):
        relative = path.relative_to(package).as_posix()
        lowered = {part.casefold() for part in path.relative_to(package).parts}
        if lowered & {"test", "tests", "__pycache__"} or path.name.startswith("test_"):
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.expr) or not all(
                hasattr(node, field)
                for field in ("lineno", "col_offset", "end_lineno", "end_col_offset")
            ):
                continue
            function = _nearest_function(node, parents)
            if function is None or _inside_signature(node, function, parents):
                continue
            observed = ast.get_source_segment(source, node)
            if not observed:
                continue
            start = _byte_offset(source, int(node.lineno), int(node.col_offset))
            end = _byte_offset(source, int(node.end_lineno), int(node.end_col_offset))
            rows.append(
                {
                    "site_id": _node_id(relative, node),
                    "node_id": _node_id(relative, node),
                    "path": relative,
                    "language": "python",
                    "backend": "stdlib_ast_expression_v424",
                    "node_type": type(node).__name__,
                    "role": type(parents.get(node)).__name__ if parents.get(node) else "expression",
                    "symbol": function.name,
                    "span": {
                        "start_line": int(node.lineno),
                        "start_column": int(node.col_offset),
                        "end_line": int(node.end_lineno),
                        "end_column": int(node.end_col_offset),
                    },
                    "byte_span": {"start": start, "end": end},
                    "observed_source": observed,
                    "node_source_sha256": _node_hash(node),
                    "facts": {
                        "literal_type": (
                            type(node.value).__name__
                            if isinstance(node, ast.Constant)
                            else None
                        ),
                    },
                }
            )
    unique = {str(row["site_id"]): row for row in rows}
    return sorted(
        unique.values(),
        key=lambda row: (
            str(row["path"]),
            int(row["byte_span"]["start"]),
            int(row["byte_span"]["end"]),
            str(row["node_type"]),
        ),
    )


def _finding_parameter(row: dict[str, Any]) -> str:
    return str(row.get("parameter") or row.get("requested_parameter") or "")


def _finding_function(row: dict[str, Any]) -> str:
    return str(row.get("function_symbol") or row.get("caller_symbol") or "")


def _finding_path(row: dict[str, Any]) -> str:
    return str(row.get("path") or row.get("caller_path") or "")


def _finding_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return _finding_path(row), _finding_function(row), _finding_parameter(row)


def _candidate_sites(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(site) for site in row.get("candidate_sites") or []]


def _site_span(site: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        int(site.get("start_line") or 0),
        int(site.get("start_column") or 0),
        int(site.get("end_line") or 0),
        int(site.get("end_column") or 0),
    )


def _node_span(node: dict[str, Any]) -> tuple[int, int, int, int]:
    span = node.get("span") or {}
    return (
        int(span.get("start_line") or 0),
        int(span.get("start_column") or 0),
        int(span.get("end_line") or 0),
        int(span.get("end_column") or 0),
    )


def _map_site_to_runtime_node(
    site: dict[str, Any], nodes: Iterable[dict[str, Any]]
) -> dict[str, Any] | None:
    path = str(site.get("path") or "")
    node_type = str(site.get("node_type") or "")
    observed = str(site.get("observed_expression") or site.get("observed_source") or "")
    matches = [
        row
        for row in nodes
        if str(row.get("path")) == path
        and str(row.get("node_type")) == node_type
        and _node_span(row) == _site_span(site)
        and str(row.get("observed_source")) == observed
    ]
    return copy.deepcopy(matches[0]) if len(matches) == 1 else None


def _editable_view(package: Path, node: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": str(node["path"]),
        "language": "python",
        "backend": str(node.get("backend") or "python_ast_v66"),
        "node_id": str(node["site_id"]),
        "source_sha256": str(node["node_source_sha256"]),
        "node_sha256": str(node["node_source_sha256"]),
        "file_sha256": sha256_file(package / str(node["path"])),
        "node_type": str(node["node_type"]),
        "role": str(node["role"]),
        "symbol": str(node.get("symbol") or ""),
        "span": copy.deepcopy(node["span"]),
        "byte_span": copy.deepcopy(node["byte_span"]),
        "line": int(node["span"]["start_line"]),
        "column": int(node["span"]["start_column"]),
        "end_line": int(node["span"]["end_line"]),
        "end_column": int(node["span"]["end_column"]),
        "observed_source": str(node["observed_source"]),
        "facts": copy.deepcopy(node.get("facts") or {}),
    }


def _group_findings(
    findings: Iterable[dict[str, Any]], nodes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in findings:
        if row.get("kind") not in SOURCE_FINDING_KINDS:
            continue
        key = _finding_key(row)
        if not all(key):
            continue
        grouped[key].append(dict(row))

    obligations: list[dict[str, Any]] = []
    for (path, function, parameter), rows in sorted(grouped.items()):
        rows.sort(
            key=lambda row: (
                row.get("kind") != "parameter_to_local_alias_disconnect",
                -float(row.get("confidence") or 0.0),
                str(row.get("finding_hash") or ""),
            )
        )
        mapped: list[tuple[float, int, dict[str, Any], dict[str, Any]]] = []
        for finding in rows:
            for site in _candidate_sites(finding):
                runtime = _map_site_to_runtime_node(site, nodes)
                if runtime is None:
                    continue
                mapped.append(
                    (
                        float(finding.get("confidence") or site.get("confidence") or 0.0),
                        int(site.get("rank") or 1),
                        finding,
                        runtime,
                    )
                )
        if not mapped:
            continue
        mapped.sort(
            key=lambda item: (
                item[2].get("kind") != "parameter_to_local_alias_disconnect",
                -item[0],
                item[1],
                str(item[3].get("site_id")),
            )
        )
        confidence, _, primary, node = mapped[0]
        if confidence < MIN_CONFIDENCE:
            continue
        defaults = sorted(
            {
                str(row.get("default_expression"))
                for row in rows
                if row.get("default_expression") is not None
            }
        )
        obligations.append(
            {
                "family": FAMILY,
                "confidence": round(confidence, 4),
                "path": path,
                "function_symbol": function,
                "parameter": parameter,
                "compatibility_defaults": defaults,
                "source_finding_kinds": sorted(
                    {str(row.get("kind")) for row in rows}
                ),
                "source_finding_hashes": sorted(
                    str(row.get("finding_hash"))
                    for row in rows
                    if row.get("finding_hash")
                ),
                "repair_goal": (
                    "Make the documented optional parameter influence the listed behavior "
                    "while preserving the public signature and compatibility default."
                ),
                "editable_node": node,
                "primary_source_finding_kind": str(primary.get("kind")),
                "semantic_correctness_inferred": False,
            }
        )
    return obligations


def build_public_python_parameter_effect_set(
    package_root: str | Path, request_text: str
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    _assert_public_package(package)
    documented = _documented_unused_parameter_facts(package)
    parameter_flow = _parameter_flow_facts(package, request_text)
    source_packet = {
        "derivation": "public_document_signature_and_request_parameter_flow_only",
        "documented_unused_parameter": documented,
        "request_parameter_flow": parameter_flow,
    }
    source_packet_hash = canonical_json_hash(source_packet)
    contract_findings = [
        row
        for row in [
            *(documented.get("findings") or []),
            *(parameter_flow.get("findings") or []),
        ]
        if row.get("finding_class") == "contract_backed_obligation"
    ]
    nodes = _python_nodes(package)
    obligations = _group_findings(contract_findings, nodes)
    selected = obligations[0] if len(obligations) == 1 else None
    editable = (
        [_editable_view(package, selected["editable_node"])] if selected else []
    )
    decision = "PROPOSE" if editable else "ABSTAIN"
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "source_scope": "public_parent_request_skill_markdown_and_python_scripts_only",
        "package_tree_hash": _tree_hash(package),
        "localization_decision": {
            "decision": decision,
            "selected_family": FAMILY if selected else None,
            "confidence": float(selected.get("confidence") or 0.0) if selected else 0.0,
            "eligible_obligation_count": len(obligations),
            "editable_node_count": len(editable),
            "abstain_reason": (
                None
                if selected
                else "no_supported_parameter_effect_contract"
                if not obligations
                else "multiple_parameter_effect_contracts"
            ),
        },
        "obligation": (
            {key: copy.deepcopy(value) for key, value in selected.items() if key != "editable_node"}
            if selected
            else None
        ),
        "editable_nodes": editable,
        "alternative_obligations": [
            {
                key: copy.deepcopy(value)
                for key, value in row.items()
                if key != "editable_node"
            }
            for row in obligations[:8]
        ],
        "counts": {
            "python_script_node_count": len(nodes),
            "source_contract_finding_count": len(contract_findings),
            "eligible_parameter_effect_count": len(obligations),
            "editable_node_count": len(editable),
        },
        "edit_contract": {
            "maximum_edits": 1,
            "exact_node_binding_required": True,
            "outside_selected_node_preserved_by_construction": True,
            "public_signature_must_be_preserved": True,
            "semantic_correctness_inferred": False,
        },
        "source_public_contract_packet_hash": source_packet_hash,
        "source_component_counts": {
            "documented_unused_parameter": int(documented.get("finding_count") or 0),
            "request_parameter_flow": int(parameter_flow.get("finding_count") or 0),
            "request_parameter_flow_abstention": len(
                parameter_flow.get("abstentions") or []
            ),
        },
        "benchmark_bundled_facts_consumed": False,
        "hidden_artifacts_consumed": False,
        "task_verifier_consumed": False,
        "gold_or_oracle_consumed": False,
        "reward_consumed": False,
        "semantic_correctness_inferred": False,
        "claim_boundary": (
            "The packet localizes a documented parameter-effect disconnect. It does not "
            "supply or certify the correct replacement semantics."
        ),
    }
    result["facts_hash"] = canonical_json_hash(result)
    return result


def validate_public_python_parameter_effect_set(
    facts: dict[str, Any], package_root: str | Path
) -> str:
    body = dict(facts)
    expected = str(body.pop("facts_hash", ""))
    if not expected or canonical_json_hash(body) != expected:
        raise ValueError("python_parameter_effect_facts_hash_invalid")
    if facts.get("method") != METHOD_ID:
        raise ValueError("python_parameter_effect_method_invalid")
    if any(
        facts.get(field) is not False
        for field in (
            "benchmark_bundled_facts_consumed",
            "hidden_artifacts_consumed",
            "task_verifier_consumed",
            "gold_or_oracle_consumed",
            "reward_consumed",
        )
    ):
        raise ValueError("python_parameter_effect_scope_invalid")
    package = Path(package_root).resolve()
    _assert_public_package(package)
    if facts.get("package_tree_hash") != _tree_hash(package):
        raise ValueError("python_parameter_effect_package_changed")
    registry = {str(row["site_id"]): row for row in _python_nodes(package)}
    editable = list(facts.get("editable_nodes") or [])
    if len(editable) > 1:
        raise ValueError("python_parameter_effect_too_many_editable_nodes")
    for row in editable:
        node = registry.get(str(row.get("node_id")))
        if node is None:
            raise ValueError("python_parameter_effect_unknown_node")
        if str(row.get("source_sha256")) != str(node.get("node_source_sha256")):
            raise ValueError("python_parameter_effect_node_hash_mismatch")
        if str(row.get("file_sha256")) != sha256_file(package / str(row["path"])):
            raise ValueError("python_parameter_effect_file_hash_mismatch")
    decision = (facts.get("localization_decision") or {}).get("decision")
    if decision == "PROPOSE" and len(editable) != 1:
        raise ValueError("python_parameter_effect_proposal_shape_invalid")
    if decision == "ABSTAIN" and editable:
        raise ValueError("python_parameter_effect_abstain_has_editable_node")
    return expected


def build_same_package_wrong_node_set(
    package_root: str | Path, real_facts: dict[str, Any]
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    validate_public_python_parameter_effect_set(real_facts, package)
    result = copy.deepcopy(real_facts)
    result.pop("facts_hash", None)
    real_nodes = list(real_facts.get("editable_nodes") or [])
    if not real_nodes:
        result["control_metadata"] = {
            "control": "same-package-wrong-python-node",
            "real_node_overlap_count": 0,
            "same_editable_node_count": True,
        }
        result["facts_hash"] = canonical_json_hash(result)
        return result
    real = real_nodes[0]
    real_literal_type = str((real.get("facts") or {}).get("literal_type") or "")
    shape_candidates = [
        row
        for row in _python_nodes(package)
        if str(row.get("site_id")) != str(real["node_id"])
        and str(row.get("node_type")) == str(real["node_type"])
        and str((row.get("facts") or {}).get("literal_type") or "")
        == real_literal_type
    ]
    strict = [
        row
        for row in shape_candidates
        if str(row.get("role")) == str(real.get("role"))
    ]
    same_symbol = [
        row
        for row in shape_candidates
        if str(row.get("symbol")) == str(real.get("symbol"))
    ]
    if strict:
        candidates = strict
        shape_tier = "same_node_type_parent_role_and_literal_type"
    elif same_symbol:
        candidates = same_symbol
        shape_tier = "same_node_and_literal_type_same_symbol"
    else:
        candidates = shape_candidates
        shape_tier = "same_node_and_literal_type_same_package"
    candidates.sort(
        key=lambda row: (
            str(row.get("symbol")) != str(real.get("symbol")),
            abs(
                len(str(row.get("observed_source") or ""))
                - len(str(real.get("observed_source") or ""))
            ),
            str(row.get("path")),
            int((row.get("byte_span") or {}).get("start") or 0),
            str(row.get("site_id")),
        )
    )
    if not candidates:
        raise ValueError("python_parameter_effect_sham_decoy_unavailable")
    decoy = _editable_view(package, candidates[0])
    result["editable_nodes"] = [decoy]
    result["control_metadata"] = {
        "control": "same-package-wrong-python-node",
        "construction": "deterministic_same_package_same_node_type_derangement",
        "real_node_overlap_count": int(decoy["node_id"] == real["node_id"]),
        "same_editable_node_count": True,
        "same_node_type": decoy["node_type"] == real["node_type"],
        "same_parent_role": decoy["role"] == real["role"],
        "same_literal_type": (
            (decoy.get("facts") or {}).get("literal_type")
            == (real.get("facts") or {}).get("literal_type")
        ),
        "shape_matching_tier": shape_tier,
        "hidden_or_verifier_feedback_used": False,
    }
    result["facts_hash"] = canonical_json_hash(result)
    return result


def apply_public_python_node_bound_patch(
    content: str,
    source_package: str | Path,
    candidate_package: str | Path,
    *,
    frozen_facts: dict[str, Any],
) -> dict[str, Any]:
    validate_public_python_parameter_effect_set(frozen_facts, source_package)
    if (frozen_facts.get("localization_decision") or {}).get("decision") != "PROPOSE":
        raise ValueError("python_parameter_effect_contract_abstained")
    result = apply_python_span_patch(
        content,
        source_package,
        candidate_package,
        visible_ast_facts=frozen_facts,
        require_ast_binding=True,
    )
    decision = (result.get("structural_gate") or {}).get("decision")
    if decision not in {ACCEPT_STRUCTURALLY, "ABSTAIN"}:
        raise ValueError("python_parameter_effect_patch_structurally_rejected")
    return result


def _public_signatures(package: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    scripts = package / "scripts"
    for path in sorted(scripts.rglob("*.py")) if scripts.is_dir() else []:
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=path.relative_to(package).as_posix())
        result[path.relative_to(package).as_posix()] = [
            ast.dump(node.args, include_attributes=False)
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
    return result


def analyze_generic_scope(
    parent_package: str | Path,
    generic_package: str | Path,
    *,
    frozen_facts: dict[str, Any],
) -> dict[str, Any]:
    validate_public_python_parameter_effect_set(frozen_facts, parent_package)
    return analyze_candidate_scope(
        parent_package,
        generic_package,
        frozen_facts=frozen_facts,
        facts_already_validated=True,
    )


def choose_source(
    *,
    generic_scope: dict[str, Any],
    ast_candidate_exists: bool,
    ast_application_decision: str | None,
) -> tuple[str, str]:
    return choose_public_runtime_dual_source(
        generic_scope=generic_scope,
        ast_candidate_exists=ast_candidate_exists,
        ast_application_decision=ast_application_decision,
    )
