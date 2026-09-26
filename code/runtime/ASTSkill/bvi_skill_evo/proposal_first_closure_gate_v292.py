from __future__ import annotations

import ast
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable

from bvi_skill_evo.proposal_first_ast_gate_v280 import (
    ACCEPT,
    REVISE,
    _call_graph,
    _parse_package,
)
from bvi_skill_evo.proposal_first_structural_gate_v287 import (
    build_posthoc_structural_gate,
)
from skillscriptbench.io_utils import canonical_json_hash


SCHEMA_VERSION = "2.92-proposal-first-closure-gate-v2"

_DROP_FACT_KEYS = {
    "facts_hash",
    "package_tree_hash",
    "skill_sha256",
    "gold_or_oracle_used",
    "task_verifier_feedback_used",
    "hidden_artifacts_consumed",
    "selected_files",
    "local_context",
    "context_source",
    "numbered_content",
}
_OBSERVED_KEYS = (
    "observed_source",
    "call_source",
    "observed_expression",
)


def _public_python_contracts(package: Path) -> dict[str, list[str]]:
    contracts: dict[str, list[str]] = {}
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(package)
        if "tests" in relative.parts or path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative.as_posix())
        rows: list[str] = []
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if node.name.startswith("_"):
                continue
            if isinstance(node, ast.ClassDef):
                rows.append(f"class:{node.name}")
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and not child.name.startswith("_"):
                        rows.append(
                            f"method:{node.name}.{child.name}:"
                            f"{ast.dump(child.args, include_attributes=False)}"
                        )
            else:
                rows.append(
                    f"function:{node.name}:{ast.dump(node.args, include_attributes=False)}"
                )
        contracts[relative.as_posix()] = sorted(rows)
    return contracts


def _removed_declaration_classes(
    parent: Path, removed: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    top_level_classes: dict[str, set[str]] = defaultdict(set)
    for path in sorted(parent.rglob("*.py")):
        relative = path.relative_to(parent).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except SyntaxError:
            continue
        top_level_classes[relative].update(
            node.name for node in tree.body if isinstance(node, ast.ClassDef)
        )

    public: list[dict[str, Any]] = []
    private: list[dict[str, Any]] = []
    for row in removed:
        symbol = str(row.get("symbol") or "")
        parts = symbol.split(".") if symbol else []
        is_public_top_level = len(parts) == 1 and not parts[0].startswith("_")
        is_public_method = (
            len(parts) == 2
            and parts[0] in top_level_classes.get(str(row.get("path") or ""), set())
            and not parts[1].startswith("_")
        )
        (public if is_public_top_level or is_public_method else private).append(row)
    return public, private


def _changed_symbols(gate: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (str(row["path"]), str(row["symbol"]))
        for row in (*gate.get("changed_nodes_before", []), *gate.get("changed_nodes_after", []))
        if str(row.get("symbol") or "") != "<module>"
    }


def _two_hop_closure(
    changed: set[tuple[str, str]], edges: list[dict[str, Any]]
) -> dict[str, Any]:
    unique_edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str, str]] = set()
    for edge in edges:
        identity = (
            str(edge["caller_path"]),
            str(edge["caller_symbol"]),
            str(edge["callee_path"]),
            str(edge["callee_symbol"]),
        )
        if identity not in seen_edges:
            seen_edges.add(identity)
            unique_edges.append(edge)
    adjacency: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for edge in unique_edges:
        caller = (str(edge["caller_path"]), str(edge["caller_symbol"]))
        callee = (str(edge["callee_path"]), str(edge["callee_symbol"]))
        adjacency[caller].add(callee)
        adjacency[callee].add(caller)

    distance = {node: 0 for node in changed}
    queue = deque(changed)
    while queue:
        node = queue.popleft()
        if distance[node] >= 2:
            continue
        for neighbor in adjacency.get(node, set()):
            if neighbor not in distance:
                distance[neighbor] = distance[node] + 1
                queue.append(neighbor)

    closure = set(distance)
    return {
        "changed_symbols": [
            {"path": path, "symbol": symbol} for path, symbol in sorted(changed)
        ],
        "one_hop_symbols": [
            {"path": path, "symbol": symbol}
            for (path, symbol), depth in sorted(distance.items())
            if depth == 1
        ],
        "two_hop_symbols": [
            {"path": path, "symbol": symbol}
            for (path, symbol), depth in sorted(distance.items())
            if depth == 2
        ],
        "relevant_edges": [
            edge
            for edge in unique_edges
            if (str(edge["caller_path"]), str(edge["caller_symbol"])) in closure
            or (str(edge["callee_path"]), str(edge["callee_symbol"])) in closure
        ],
    }


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize(item)
            for key, item in value.items()
            if str(key) not in _DROP_FACT_KEYS
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _fact_payload(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not raw:
        return {"status": "not_available", "findings": [], "editable_nodes": []}
    keep = {
        "schema_version",
        "status",
        "method",
        "generic_invariant",
        "confidence_policy",
        "localization_decision",
        "constraints",
        "findings",
        "def_use_findings",
        "editable_nodes",
    }
    payload = {key: raw[key] for key in keep if key in raw}
    if "findings" in payload:
        payload["findings"] = list(payload["findings"] or [])[:16]
    if "editable_nodes" in payload:
        limit = 6 if not payload.get("findings") else 3
        payload["editable_nodes"] = list(payload["editable_nodes"] or [])[:limit]
    if "caller_views" in payload:
        payload["caller_views"] = list(payload["caller_views"] or [])[:8]
    return _sanitize(payload)


def _observed_fragment(row: dict[str, Any]) -> str:
    for key in _OBSERVED_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _annotate_candidate_status(value: Any, candidate: Path) -> Any:
    if isinstance(value, list):
        return [_annotate_candidate_status(item, candidate) for item in value]
    if not isinstance(value, dict):
        return value
    result = {
        key: _annotate_candidate_status(item, candidate) for key, item in value.items()
    }
    relative = str(value.get("path") or value.get("caller_path") or "")
    observed = _observed_fragment(value)
    if relative and observed:
        target = candidate / relative
        if not target.is_file():
            status = "path_absent"
            count = 0
        else:
            count = target.read_text(encoding="utf-8").count(observed)
            status = (
                "changed_or_absent"
                if count == 0
                else "still_present_unique"
                if count == 1
                else "still_present_ambiguous"
            )
        result["candidate_site_status"] = status
        result["candidate_occurrence_count"] = count
    return result


def _iter_sites(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _iter_sites(item)
    elif isinstance(value, dict):
        if "candidate_site_status" in value:
            yield value
        for item in value.values():
            yield from _iter_sites(item)


def _candidate_aware_visible_facts(
    candidate: Path, visible_facts: dict[str, Any] | None
) -> dict[str, Any]:
    annotated = _annotate_candidate_status(_fact_payload(visible_facts), candidate)
    sites: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in _iter_sites(annotated):
        identity = (
            str(row.get("path") or row.get("caller_path") or ""),
            str(row.get("node_id") or row.get("line") or row.get("call_line") or ""),
            _observed_fragment(row),
        )
        sites.setdefault(identity, row)
    counts: dict[str, int] = defaultdict(int)
    for row in sites.values():
        counts[str(row["candidate_site_status"])] += 1
    finding_counts: dict[str, int] = defaultdict(int)
    for row in annotated.get("findings", []) if isinstance(annotated, dict) else []:
        if not isinstance(row, dict):
            continue
        status = str(row.get("candidate_site_status") or "")
        if not status:
            nested = {
                str(site.get("candidate_site_status") or "")
                for site in row.get("candidate_sites", [])
                if isinstance(site, dict)
            }
            status = (
                "all_candidate_sites_changed_or_absent"
                if nested == {"changed_or_absent"}
                else "candidate_sites_still_present"
                if any(value.startswith("still_present") for value in nested)
                else "unmapped"
            )
        finding_counts[status] += 1
    return {
        "source": "frozen_visible_package_structural_facts",
        "facts": annotated,
        "site_status_counts": dict(sorted(counts.items())),
        "finding_status_counts": dict(sorted(finding_counts.items())),
        "unique_site_count": len(sites),
        "interpretation": (
            "A still-present site is a structural review item, not proof of an error. "
            "A changed-or-absent site means the proposal touched or removed the original expression, "
            "not that the resulting behavior is correct."
        ),
    }


def _local_def_use(gate: dict[str, Any], closure: dict[str, Any]) -> list[dict[str, Any]]:
    residual = gate.get("candidate_residual_structural_facts") or {}
    symbols = {
        (str(row["path"]), str(row["symbol"]))
        for key in ("changed_symbols", "one_hop_symbols", "two_hop_symbols")
        for row in closure.get(key, [])
    }
    return [
        row
        for row in residual.get("def_use_findings", [])
        if (str(row.get("path") or ""), str(row.get("symbol") or "")) in symbols
    ][:16]


def build_proposal_first_closure_report(
    parent_package: str | Path,
    candidate_package: str | Path,
    request_text: str,
    *,
    visible_structural_facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parent = Path(parent_package).resolve()
    candidate = Path(candidate_package).resolve()
    base = build_posthoc_structural_gate(parent, candidate, request_text)
    checks = dict(base.get("checks") or {})

    removed_public: list[dict[str, Any]] = []
    removed_private: list[dict[str, Any]] = []
    if any(path.endswith(".py") for path in base.get("changed_paths", [])):
        removed_public, removed_private = _removed_declaration_classes(
            parent, list(base.get("removed_declarations") or [])
        )
        try:
            public_contracts_preserved = (
                _public_python_contracts(parent) == _public_python_contracts(candidate)
            )
        except SyntaxError:
            public_contracts_preserved = False
        checks.pop("declarations_not_removed", None)
        checks["public_python_contracts_preserved"] = public_contracts_preserved
        checks["public_declarations_not_removed"] = not removed_public

    changed_path_limit = 4
    changed_symbol_limit = 8
    if "changed_path_count_bounded" in checks:
        checks["changed_path_count_bounded"] = (
            len(base.get("changed_paths", [])) <= changed_path_limit
        )
    if "changed_symbol_count_bounded" in checks:
        checks["changed_symbol_count_bounded"] = (
            len(_changed_symbols(base)) <= changed_symbol_limit
        )

    closure = dict(base.get("impact_closure") or {})
    if any(path.endswith(".py") for path in base.get("changed_paths", [])):
        parent_parsed, parent_failures = _parse_package(parent)
        candidate_parsed, candidate_failures = _parse_package(candidate)
        if not parent_failures and not candidate_failures:
            closure = _two_hop_closure(
                _changed_symbols(base),
                _call_graph(parent_parsed) + _call_graph(candidate_parsed),
            )

    result = dict(base)
    result.pop("gate_hash", None)
    result.update(
        {
            "schema_version": SCHEMA_VERSION,
            "checks": checks,
            "failed_checks": [name for name, passed in checks.items() if not passed],
            "decision": ACCEPT if all(checks.values()) else REVISE,
            "impact_closure": closure,
            "local_def_use_findings": _local_def_use(base, closure),
            "removed_public_declarations": removed_public,
            "removed_private_implementation_declarations": removed_private,
            "candidate_aware_visible_structural_facts": _candidate_aware_visible_facts(
                candidate, visible_structural_facts
            ),
            "limits": {
                "changed_path_limit": changed_path_limit,
                "changed_symbol_limit": changed_symbol_limit,
                "call_closure_depth": 2,
            },
            "task_verifier_consumed": False,
            "gold_or_oracle_consumed": False,
            "hidden_artifacts_consumed": False,
            "claim_boundary": (
                "The report maps a free proposal to concrete syntax nodes, protects public Python "
                "contracts, permits closed private implementation refactors, and exposes a two-hop "
                "package-local impact closure plus answer-free visible structural review items. "
                "It does not determine task or semantic correctness."
            ),
        }
    )
    result["gate_hash"] = canonical_json_hash(result)
    return result
