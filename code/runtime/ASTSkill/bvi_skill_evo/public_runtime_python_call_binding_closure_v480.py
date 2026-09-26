from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from bvi_skill_evo import public_runtime_python_call_binding_closure_v468 as core
from bvi_skill_evo import public_runtime_python_call_binding_closure_v477 as atomic
from bvi_skill_evo.balanced_hybrid_ast_v316 import _assert_public_package, _tree_hash
from skillscriptbench.io_utils import canonical_json_hash, sha256_file


SCHEMA_VERSION = "4.80-public-runtime-python-call-binding-closure-v5"
METHOD_ID = "public_runtime_python_call_binding_closure_v5"
FAMILY = atomic.FAMILY
MAX_CLOSURE_EDITS = atomic.MAX_CLOSURE_EDITS
MAX_CALLER_SYMBOLS = atomic.MAX_CALLER_SYMBOLS
MAX_CHANGED_PATHS = atomic.MAX_CHANGED_PATHS
MAX_PROPAGATED_CLOSURE_ROOTS = atomic.MAX_CLOSURE_ROOTS
MAX_CORE_CLOSURE_ROOTS = 4
MAX_CLOSURE_ROOTS = MAX_CORE_CLOSURE_ROOTS


def _convert(facts: dict[str, Any], *, origin: str) -> dict[str, Any]:
    result = copy.deepcopy(facts)
    result.pop("facts_hash", None)
    result["schema_version"] = SCHEMA_VERSION
    result["method"] = METHOD_ID
    result["detector_route"] = {
        "origin": origin,
        "family": FAMILY,
        "task_id_specific_rule_used": False,
        "core_fallback_requires_exact_candidate_set_equality": True,
        "propagated_closure_root_bound": MAX_PROPAGATED_CLOSURE_ROOTS,
        "core_closure_root_bound": MAX_CORE_CLOSURE_ROOTS,
    }
    result["facts_hash"] = canonical_json_hash(result)
    return result


def _candidate_ids(package: Path) -> set[str]:
    candidates, _, _ = atomic._derive_candidates(package)
    return {atomic.legacy._node_id(row["path"], row["node"]) for row in candidates}


def build_public_python_call_binding_closure(
    package_root: str | Path, request_text: str = ""
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    atomic_facts = atomic.build_public_python_call_binding_closure(package, request_text)
    decision = atomic_facts["localization_decision"]
    if decision["decision"] == "PROPOSE":
        return _convert(atomic_facts, origin="atomic_module_lexical_completion_v477")
    if decision.get("abstain_reason") == "call_binding_closure_exceeds_root_bound":
        core_facts = core.build_public_python_call_binding_closure(package, request_text)
        core_ids = {str(row["node_id"]) for row in core_facts.get("editable_nodes") or []}
        candidate_ids = _candidate_ids(package)
        if (
            core_facts["localization_decision"]["decision"] == "PROPOSE"
            and core_ids
            and core_ids == candidate_ids
            and len(
                {
                    (str(row["path"]), str(row["caller_symbol"]))
                    for row in core_facts.get("obligations") or []
                }
            )
            <= MAX_CORE_CLOSURE_ROOTS
        ):
            result = _convert(core_facts, origin="exact_core_v468_fallback")
            result.pop("facts_hash", None)
            result["detector_route"]["exact_candidate_set_equality"] = True
            result["detector_route"]["atomic_abstain_reason"] = str(
                decision["abstain_reason"]
            )
            result["edit_contract"]["maximum_edits"] = MAX_CLOSURE_EDITS
            result["edit_contract"]["maximum_changed_paths"] = MAX_CHANGED_PATHS
            result["facts_hash"] = canonical_json_hash(result)
            return result
    return _convert(atomic_facts, origin="atomic_module_lexical_completion_v477")


def _to_atomic(facts: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(facts)
    result.pop("facts_hash", None)
    result.pop("detector_route", None)
    result["schema_version"] = atomic.SCHEMA_VERSION
    result["method"] = atomic.METHOD_ID
    result["facts_hash"] = canonical_json_hash(result)
    return result


def build_same_package_wrong_call_binding_closure(
    package_root: str | Path, real_facts: dict[str, Any]
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    validate_public_python_call_binding_closure(real_facts, package)
    sham = atomic.build_same_package_wrong_call_binding_closure(
        package, _to_atomic(real_facts)
    )
    return _convert(
        sham,
        origin=f"sham_for_{real_facts['detector_route']['origin']}",
    )


def validate_public_python_call_binding_closure(
    facts: dict[str, Any], package_root: str | Path
) -> str:
    body = dict(facts)
    expected = str(body.pop("facts_hash", ""))
    if not expected or canonical_json_hash(body) != expected:
        raise ValueError("call_binding_closure_v5_facts_hash_invalid")
    if facts.get("schema_version") != SCHEMA_VERSION or facts.get("method") != METHOD_ID:
        raise ValueError("call_binding_closure_v5_method_invalid")
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
        raise ValueError("call_binding_closure_v5_scope_invalid")
    route = facts.get("detector_route") or {}
    if route.get("task_id_specific_rule_used") is not False or route.get("origin") not in {
        "atomic_module_lexical_completion_v477",
        "exact_core_v468_fallback",
        "sham_for_atomic_module_lexical_completion_v477",
        "sham_for_exact_core_v468_fallback",
    }:
        raise ValueError("call_binding_closure_v5_route_invalid")
    package = Path(package_root).resolve()
    _assert_public_package(package)
    if facts.get("package_tree_hash") != _tree_hash(package):
        raise ValueError("call_binding_closure_v5_package_changed")
    registry = {str(row["node_id"]): row for row in atomic._all_name_nodes(package)}
    editable = list(facts.get("editable_nodes") or [])
    obligations = list(facts.get("obligations") or [])
    if len(editable) > MAX_CLOSURE_EDITS or len(editable) != len(obligations):
        raise ValueError("call_binding_closure_v5_editable_shape_invalid")
    if len({str(row.get("node_id")) for row in editable}) != len(editable):
        raise ValueError("call_binding_closure_v5_duplicate_node")
    for row in editable:
        current = registry.get(str(row.get("node_id")))
        if current is None or current.get("node_type") != "Name":
            raise ValueError("call_binding_closure_v5_unknown_name_node")
        if str(row.get("node_sha256")) != str(current.get("node_sha256")):
            raise ValueError("call_binding_closure_v5_node_hash_mismatch")
        if str(row.get("file_sha256")) != sha256_file(package / str(row["path"])):
            raise ValueError("call_binding_closure_v5_file_hash_mismatch")
    decision = str((facts.get("localization_decision") or {}).get("decision"))
    if decision == "PROPOSE" and not editable:
        raise ValueError("call_binding_closure_v5_empty_proposal")
    if decision == "ABSTAIN" and (editable or obligations):
        raise ValueError("call_binding_closure_v5_abstain_has_editable_content")
    if route.get("origin") == "exact_core_v468_fallback" and (
        route.get("exact_candidate_set_equality") is not True
        or route.get("atomic_abstain_reason")
        != "call_binding_closure_exceeds_root_bound"
    ):
        raise ValueError("call_binding_closure_v5_core_fallback_unproven")
    return expected


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


_binding_rows = atomic._binding_rows


__all__ = [
    "FAMILY",
    "MAX_CLOSURE_EDITS",
    "METHOD_ID",
    "build_public_python_call_binding_closure",
    "build_same_package_wrong_call_binding_closure",
    "residual_obligations",
    "validate_public_python_call_binding_closure",
]
