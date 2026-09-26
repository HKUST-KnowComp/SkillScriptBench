from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from typing import Any

from bvi_skill_evo import public_runtime_multilang_dual_v410 as real
from skillscriptbench.io_utils import canonical_json_hash


SCHEMA_VERSION = "4.12-public-runtime-multilang-controls-v1"
CONTROL_ID = "same-package-role-compatible-wrong-node-v1"

# Exact-role derangements are preferred. These fallbacks are fixed before any
# behavioral evaluation and are used only when a package has no unused node of
# the exact role.
ROLE_COMPATIBILITY: dict[str, tuple[str, ...]] = {
    "sort_comparator": ("comparison_expression", "return_expression"),
    "path_kind_guard": (
        "string_route_guard",
        "empty_value_guard",
        "numeric_guard",
        "guard_expression",
    ),
}


def _span(row: dict[str, Any]) -> tuple[int, int]:
    value = row["byte_span"]
    return int(value["start"]), int(value["end"])


def _overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if str(left["path"]) != str(right["path"]):
        return False
    left_start, left_end = _span(left)
    right_start, right_end = _span(right)
    return left_start < right_end and right_start < left_end


def _role_tier(candidate: dict[str, Any], target: dict[str, Any]) -> int | None:
    role = str(target.get("role") or "")
    observed = str(candidate.get("role") or "")
    if observed == role:
        return 0
    compatible = ROLE_COMPATIBILITY.get(role, ())
    return compatible.index(observed) + 1 if observed in compatible else None


def _surface_class(row: dict[str, Any]) -> tuple[str, str]:
    source = str(row.get("observed_source") or "").strip()
    if re.fullmatch(r"[\"'].*[\"']", source, flags=re.DOTALL):
        return "quoted_literal", ""
    if re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", source):
        return "numeric_literal", ""
    if row.get("role") == "call_expression":
        match = re.match(r"([A-Za-z_$][A-Za-z0-9_$.]*)\s*\(", source)
        return "call_expression", match.group(1) if match else ""
    if row.get("role") == "binding_assignment":
        rhs = source.split("=", 1)[1].strip() if "=" in source else source
        if re.search(r"\$\{[0-9]+(?::[-+?=])?", rhs):
            return "positional_parameter", ""
        if "$(" in rhs:
            return "command_substitution", ""
        return "binding_assignment", ""
    return str(row.get("role") or "unknown"), ""


def _decoy_key(candidate: dict[str, Any], target: dict[str, Any]) -> tuple[Any, ...]:
    tier = _role_tier(candidate, target)
    if tier is None:
        raise ValueError("decoy_role_not_compatible")
    target_start, target_end = _span(target)
    start, end = _span(candidate)
    target_surface, target_detail = _surface_class(target)
    candidate_surface, candidate_detail = _surface_class(candidate)
    return (
        tier,
        candidate_surface != target_surface,
        bool(target_detail) and candidate_detail != target_detail,
        str(candidate.get("path")) != str(target.get("path")),
        str(candidate.get("symbol")) != str(target.get("symbol")),
        abs((end - start) - (target_end - target_start)),
        abs(start - target_start),
        str(candidate.get("site_id")),
    )


def _choose_decoys(
    package: Path,
    real_facts: dict[str, Any],
) -> list[dict[str, Any]]:
    nodes = real._enumerate_non_python_nodes(package)
    real_nodes = list(real_facts.get("editable_nodes") or [])
    excluded_ids = {
        str(row.get("node_id"))
        for row in [
            *real_nodes,
            *(real_facts.get("context_nodes") or []),
        ]
        if row.get("node_id")
    }
    registry = {str(row["site_id"]): row for row in nodes}
    excluded_spans = [
        {
            "path": row["path"],
            "byte_span": row["byte_span"],
        }
        for node_id in excluded_ids
        for row in [registry.get(node_id)]
        if row is not None
    ]
    chosen: list[dict[str, Any]] = []
    chosen_ids: set[str] = set()
    for target in real_nodes:
        candidates = [
            row
            for row in nodes
            if str(row["site_id"]) not in excluded_ids | chosen_ids
            and _role_tier(row, target) is not None
            and not any(_overlaps(row, blocked) for blocked in excluded_spans)
        ]
        if not candidates:
            raise ValueError(
                "public_multilang_sham_no_role_compatible_decoy:"
                f"{target.get('path')}:{target.get('role')}"
            )
        selected = min(candidates, key=lambda row: _decoy_key(row, target))
        chosen.append(selected)
        chosen_ids.add(str(selected["site_id"]))
    return chosen


def build_same_package_wrong_node_set(
    package_root: str | Path,
    real_facts: dict[str, Any],
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    real_hash = real.validate_public_multilang_contract_set(real_facts, package)
    decision = (real_facts.get("localization_decision") or {}).get("decision")
    if decision != "PROPOSE":
        raise ValueError("public_multilang_sham_requires_real_proposal")
    real_nodes = list(real_facts.get("editable_nodes") or [])
    decoys = _choose_decoys(package, real_facts)
    family = str((real_facts.get("localization_decision") or {})["selected_family"])
    sham_nodes = [
        real._editable_view(
            row,
            package=package,
            family=family,
            rank=int(source.get("rank") or 0),
            reasons=list(source.get("selection_reasons") or []),
        )
        for source, row in zip(real_nodes, decoys, strict=True)
    ]
    result = deepcopy(real_facts)
    result.pop("facts_hash", None)
    result["schema_version"] = SCHEMA_VERSION
    result["editable_nodes"] = sham_nodes
    result["context_nodes"] = []
    result["counts"] = {
        **(result.get("counts") or {}),
        "editable_node_count": len(sham_nodes),
        "context_node_count": 0,
    }
    result["control"] = {
        "control_id": CONTROL_ID,
        "source_real_facts_hash": real_hash,
        "same_package": True,
        "same_editable_node_count": len(sham_nodes) == len(real_nodes),
        "real_node_ids": [str(row["node_id"]) for row in real_nodes],
        "control_node_ids": [str(row["node_id"]) for row in sham_nodes],
        "node_sets_disjoint": not (
            {str(row["node_id"]) for row in real_nodes}
            & {str(row["node_id"]) for row in sham_nodes}
        ),
        "selection_uses_behavioral_outcome": False,
        "selection_uses_hidden_or_verifier": False,
    }
    result["facts_hash"] = canonical_json_hash(result)
    validate_same_package_wrong_node_set(result, package, real_facts)
    return result


def validate_same_package_wrong_node_set(
    sham_facts: dict[str, Any],
    package_root: str | Path,
    real_facts: dict[str, Any],
) -> str:
    package = Path(package_root).resolve()
    real_hash = real.validate_public_multilang_contract_set(real_facts, package)
    sham_hash = real.validate_public_multilang_contract_set(sham_facts, package)
    control = sham_facts.get("control") or {}
    real_ids = {str(row["node_id"]) for row in real_facts.get("editable_nodes") or []}
    sham_ids = {str(row["node_id"]) for row in sham_facts.get("editable_nodes") or []}
    checks = (
        sham_facts.get("schema_version") == SCHEMA_VERSION,
        control.get("control_id") == CONTROL_ID,
        control.get("source_real_facts_hash") == real_hash,
        control.get("same_package") is True,
        control.get("same_editable_node_count") is True,
        control.get("node_sets_disjoint") is True,
        control.get("selection_uses_behavioral_outcome") is False,
        control.get("selection_uses_hidden_or_verifier") is False,
        len(real_ids) == len(sham_ids) > 0,
        not (real_ids & sham_ids),
        (real_facts.get("localization_decision") or {}).get("selected_family")
        == (sham_facts.get("localization_decision") or {}).get("selected_family"),
    )
    if not all(checks):
        raise ValueError("public_multilang_sham_derangement_invalid")
    return sham_hash


def prompt_packet_view(facts: dict[str, Any]) -> dict[str, Any]:
    """Return the condition-blind packet shown to either real or control arm."""

    return {
        "localization_decision": facts["localization_decision"],
        "obligation": facts["obligation"],
        "editable_nodes": facts["editable_nodes"],
        "context_nodes": facts["context_nodes"],
        "edit_contract": facts["edit_contract"],
        "claim_boundary": facts["claim_boundary"],
    }
