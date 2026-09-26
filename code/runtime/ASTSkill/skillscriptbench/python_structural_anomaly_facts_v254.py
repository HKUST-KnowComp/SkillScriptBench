from __future__ import annotations

from pathlib import Path
from typing import Any

from .io_utils import canonical_json_hash
from .python_structural_anomaly_facts_v235 import (
    build_python_structural_anomaly_facts,
)


SCHEMA_VERSION = "2.54-compact-python-structural-anomaly-facts-v1"


def _node_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    """Identify a source node without using presentation-only rank fields."""
    return (
        row.get("path"),
        row.get("line"),
        row.get("column"),
        row.get("end_line"),
        row.get("end_column"),
        row.get("node_sha256"),
        row.get("observed_source"),
    )


def _edge_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("caller_path"),
        row.get("caller_symbol"),
        row.get("callee_path"),
        row.get("callee_symbol"),
        row.get("line"),
        row.get("call_source"),
    )


def compact_structural_facts(
    raw: dict[str, Any],
    *,
    maximum_findings: int = 12,
    maximum_local_edges: int = 48,
    maximum_def_use: int = 24,
) -> dict[str, Any]:
    if maximum_findings < 1:
        raise ValueError("maximum_findings_must_be_positive")

    deduplicated: list[dict[str, Any]] = []
    seen_nodes: set[tuple[Any, ...]] = set()
    ordered = sorted(
        raw.get("findings", []),
        key=lambda row: (int(row.get("rank", 10**9)), -float(row.get("confidence", 0.0))),
    )
    for row in ordered:
        identity = _node_identity(row)
        if identity in seen_nodes:
            continue
        seen_nodes.add(identity)
        copied = dict(row)
        copied["rank"] = len(deduplicated) + 1
        deduplicated.append(copied)
        if len(deduplicated) >= maximum_findings:
            break

    editable_keys = {
        "path",
        "symbol",
        "line",
        "column",
        "end_line",
        "end_column",
        "node_type",
        "node_id",
        "node_sha256",
        "observed_source",
        "rank",
    }
    editable_nodes = [
        {key: row.get(key) for key in editable_keys}
        for row in deduplicated
    ]

    seeds = {(str(row.get("path")), str(row.get("symbol"))) for row in deduplicated}
    local_edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[Any, ...]] = set()
    for row in sorted(
        raw.get("package_call_edges", []),
        key=lambda value: (
            str(value.get("caller_path")),
            str(value.get("caller_symbol")),
            int(value.get("line", 0)),
            str(value.get("callee_path")),
            str(value.get("callee_symbol")),
        ),
    ):
        caller = (str(row.get("caller_path")), str(row.get("caller_symbol")))
        callee = (str(row.get("callee_path")), str(row.get("callee_symbol")))
        if caller not in seeds and callee not in seeds:
            continue
        identity = _edge_identity(row)
        if identity in seen_edges:
            continue
        seen_edges.add(identity)
        local_edges.append(dict(row))
        if len(local_edges) >= maximum_local_edges:
            break

    local_def_use: list[dict[str, Any]] = []
    seen_def_use: set[str] = set()
    for row in raw.get("def_use_findings", []):
        if (str(row.get("path")), str(row.get("symbol"))) not in seeds:
            continue
        identity = canonical_json_hash(row)
        if identity in seen_def_use:
            continue
        seen_def_use.add(identity)
        local_def_use.append(dict(row))
        if len(local_def_use) >= maximum_def_use:
            break

    compact = {
        key: value
        for key, value in raw.items()
        if key
        not in {
            "facts_hash",
            "findings",
            "editable_nodes",
            "package_call_edges",
            "def_use_findings",
            "finding_count",
        }
    }
    compact.update(
        {
            "schema_version": SCHEMA_VERSION,
            "method": (
                "Request-conditioned Python AST anomaly ranking with rank-independent node "
                "deduplication and package-local one-hop call/def-use closure."
            ),
            "findings": deduplicated,
            "editable_nodes": editable_nodes,
            "package_call_edges": local_edges,
            "def_use_findings": local_def_use,
            "finding_count": len(deduplicated),
            "compaction": {
                "maximum_findings": maximum_findings,
                "maximum_local_edges": maximum_local_edges,
                "maximum_def_use": maximum_def_use,
                "raw_finding_count": len(raw.get("findings", [])),
                "raw_editable_node_count": len(raw.get("editable_nodes", [])),
                "raw_package_call_edge_count": len(raw.get("package_call_edges", [])),
                "raw_def_use_count": len(raw.get("def_use_findings", [])),
                "rank_is_not_node_identity": True,
                "package_wide_edge_dump_removed": True,
            },
            "hidden_artifacts_consumed": False,
        }
    )
    compact["facts_hash"] = canonical_json_hash(compact)
    return compact


def build_compact_python_structural_anomaly_facts(
    package_root: str | Path,
    request_text: str,
    *,
    maximum_findings: int = 12,
    maximum_local_edges: int = 48,
    maximum_def_use: int = 24,
) -> dict[str, Any]:
    raw = build_python_structural_anomaly_facts(
        package_root,
        request_text,
        maximum_findings=max(20, maximum_findings * 2),
    )
    return compact_structural_facts(
        raw,
        maximum_findings=maximum_findings,
        maximum_local_edges=maximum_local_edges,
        maximum_def_use=maximum_def_use,
    )
