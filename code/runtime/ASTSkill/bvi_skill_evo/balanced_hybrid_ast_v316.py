from __future__ import annotations

import ast
import json
import re
import shutil
import tempfile
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Iterable

from bvi_skill_evo.proposal_first_ast_gate_v280 import ACCEPT
from bvi_skill_evo.proposal_first_closure_gate_v292 import (
    build_proposal_first_closure_report,
)
from skillscriptbench.io_utils import canonical_json_hash, sha256_file
from skillscriptbench.multilang_structural_v66 import node_public_view
from skillscriptbench.multilang_structural_v76 import (
    enumerate_package_nodes,
    rank_script_nodes,
    select_editable_nodes,
)
from skillscriptbench.python_structural_anomaly_facts_v254 import (
    build_compact_python_structural_anomaly_facts,
)


SCHEMA_VERSION = "3.16-balanced-hybrid-ast-v1"
MAX_REVIEW_SITES = 10
MAX_HIGH_CONFIDENCE_FINDINGS = 8
HIGH_CONFIDENCE = 0.82

_FORBIDDEN_PATH_PARTS = {
    "_private",
    "tests",
    "test",
    "hidden",
    "oracle",
    "verifier",
}
_HIGH_PRECISION_FINDINGS = {
    "accumulated_value_absent_from_return_mapping",
    "direct_container_iteration_with_helper_available",
    "effectless_placeholder",
    "identity_assignment_bypasses_transform",
}


def _visible_files(root: Path) -> list[Path]:
    files = [root / "SKILL.md"]
    scripts = root / "scripts"
    if scripts.is_dir():
        for path in sorted(scripts.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            lowered_parts = {part.lower() for part in relative.parts}
            if (
                lowered_parts & {"tests", "test", "__pycache__"}
                or path.name.startswith("test_")
                or ".test." in path.name
                or "_test." in path.name
                or path.suffix.lower() in {".pyc", ".pyo"}
            ):
                continue
            files.append(path)
    return files


def _tree_hash(root: Path) -> str:
    return canonical_json_hash(
        {
            path.relative_to(root).as_posix(): sha256_file(path)
            for path in _visible_files(root)
        }
    )


def _assert_public_package(package: Path) -> None:
    lowered = {part.lower() for part in package.parts}
    if lowered & _FORBIDDEN_PATH_PARTS:
        raise ValueError(f"non_public_package_path_rejected:{package}")
    if not (package / "SKILL.md").is_file():
        raise FileNotFoundError(f"skill_markdown_missing:{package}")
    for path in _visible_files(package):
        relative = path.relative_to(package)
        if path.is_symlink():
            raise ValueError(f"visible_source_symlink_rejected:{relative.as_posix()}")
        if {part.lower() for part in relative.parts} & _FORBIDDEN_PATH_PARTS:
            raise ValueError(f"non_public_visible_path_rejected:{relative.as_posix()}")


def _copy_visible_package(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for path in _visible_files(source):
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _compact_site(row: dict[str, Any]) -> dict[str, Any]:
    public = node_public_view(row)
    public["localization_score"] = round(float(row.get("localization_score") or 0.0), 6)
    public["selection_channel"] = row.get("selection_channel")
    public["score_reasons"] = list(row.get("score_reasons") or [])[:8]
    public["context_source"] = str(public.get("context_source") or "")[:1200]
    return public


def _finding_view(row: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "finding_type",
        "confidence",
        "path",
        "symbol",
        "node_type",
        "node_id",
        "node_sha256",
        "line",
        "end_line",
        "observed_source",
        "explanation",
        "evidence",
    )
    result = {key: row.get(key) for key in keep}
    result["observed_source"] = str(result.get("observed_source") or "")[:1000]
    return result


def _relation_groups(
    all_nodes: list[dict[str, Any]], selected: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    selected_ids = {str(row["site_id"]) for row in selected}
    by_source: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    by_symbol: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in all_nodes:
        if row.get("language") == "markdown" or row.get("role") == "function_scope":
            continue
        observed = re.sub(r"\s+", " ", str(row.get("observed_source") or "")).strip()
        if observed:
            by_source[(str(row["path"]), str(row["role"]), observed)].append(row)
        by_symbol[(str(row["path"]), str(row["symbol"]))].append(row)

    relations: list[dict[str, Any]] = []
    for (path, role, observed), rows in sorted(by_source.items()):
        if len(rows) < 2 or not any(str(row["site_id"]) in selected_ids for row in rows):
            continue
        relations.append(
            {
                "relation": "repeated_structural_expression",
                "path": path,
                "role": role,
                "observed_source": observed[:500],
                "occurrence_count": len(rows),
                "site_ids": [str(row["site_id"]) for row in rows[:8]],
            }
        )
    for (path, symbol), rows in sorted(by_symbol.items()):
        members = [row for row in rows if str(row["site_id"]) in selected_ids]
        if not members or symbol == "<module>":
            continue
        relations.append(
            {
                "relation": "same_symbol_review_neighborhood",
                "path": path,
                "symbol": symbol,
                "site_ids": [str(row["site_id"]) for row in members[:8]],
                "roles": sorted({str(row["role"]) for row in rows})[:12],
            }
        )
    return relations[:16]


def build_runtime_advisory_packet(
    package_root: str | Path,
    request_text: str,
    *,
    maximum_sites: int = MAX_REVIEW_SITES,
) -> dict[str, Any]:
    """Build soft structural priors from only the visible package and request."""
    package = Path(package_root).resolve()
    _assert_public_package(package)
    nodes = enumerate_package_nodes(package, include_markdown=True)
    ranked = rank_script_nodes(nodes, request_text, package)
    selected = select_editable_nodes(ranked, max_nodes=maximum_sites)

    python_facts: dict[str, Any] | None = None
    if any(path.suffix == ".py" for path in (package / "scripts").rglob("*")):
        try:
            python_facts = build_compact_python_structural_anomaly_facts(
                package,
                request_text,
                maximum_findings=12,
                maximum_local_edges=24,
                maximum_def_use=16,
            )
        except (SyntaxError, ValueError):
            python_facts = None
    high_confidence = []
    for row in (python_facts or {}).get("findings", []):
        if (
            str(row.get("finding_type")) in _HIGH_PRECISION_FINDINGS
            and float(row.get("confidence") or 0.0) >= HIGH_CONFIDENCE
        ):
            high_confidence.append(_finding_view(row))
    high_confidence = high_confidence[:MAX_HIGH_CONFIDENCE_FINDINGS]

    call_edges = [
        {
            key: row.get(key)
            for key in (
                "caller_path",
                "caller_symbol",
                "callee_path",
                "callee_symbol",
                "line",
                "call_source",
            )
        }
        for row in (python_facts or {}).get("package_call_edges", [])[:24]
    ]
    def_use = [
        dict(row) for row in (python_facts or {}).get("def_use_findings", [])[:16]
    ]
    packet = {
        "schema_version": SCHEMA_VERSION,
        "method": "runtime_request_conditioned_soft_structural_prior",
        "source_scope": "visible_skill_markdown_and_runtime_scripts_only",
        "package_tree_hash": _tree_hash(package),
        "advisory_policy": {
            "is_edit_whitelist": False,
            "is_correctness_verdict": False,
            "model_must_read_complete_package": True,
            "model_may_ignore_any_site": True,
            "model_may_edit_unlisted_visible_script_nodes": True,
        },
        "ranked_review_sites": [_compact_site(row) for row in selected],
        "high_confidence_structural_findings": high_confidence,
        "package_local_call_edges": call_edges,
        "local_def_use_observations": def_use,
        "relation_groups": _relation_groups(nodes, selected),
        "shape_padding": "",
        "counts": {
            "script_node_count": sum(row.get("language") != "markdown" for row in nodes),
            "markdown_node_count": sum(row.get("language") == "markdown" for row in nodes),
            "ranked_script_node_count": len(ranked),
            "review_site_count": len(selected),
            "high_confidence_finding_count": len(high_confidence),
            "package_local_call_edge_count": len(call_edges),
            "local_def_use_observation_count": len(def_use),
            "relation_group_count": len(_relation_groups(nodes, selected)),
        },
        "hidden_artifacts_consumed": False,
        "task_verifier_consumed": False,
        "gold_or_oracle_consumed": False,
        "claim_boundary": (
            "Sites are request-conditioned package-local review hypotheses. Findings identify only "
            "generic structural risk patterns. Neither is evidence that a node is semantically wrong."
        ),
    }
    packet["packet_hash"] = canonical_json_hash(packet)
    return packet


def advisory_prompt_view(packet: dict[str, Any]) -> dict[str, Any]:
    """Return the condition-blind, compact view shown to the model."""
    return {
        key: packet.get(key)
        for key in (
            "schema_version",
            "advisory_policy",
            "ranked_review_sites",
            "high_confidence_structural_findings",
            "package_local_call_edges",
            "local_def_use_observations",
            "relation_groups",
            "counts",
            "claim_boundary",
            "shape_padding",
        )
    }


def _site_identity(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("path") or ""),
        str(row.get("symbol") or ""),
        str(row.get("role") or ""),
        re.sub(r"\s+", " ", str(row.get("observed_source") or "")).strip(),
    )


def build_sham_advisory_packet(
    package_root: str | Path,
    request_text: str,
    real_packet: dict[str, Any],
) -> dict[str, Any]:
    """Build a same-package low-ranked derangement without hidden labels."""
    package = Path(package_root).resolve()
    _assert_public_package(package)
    nodes = enumerate_package_nodes(package, include_markdown=True)
    ranked = rank_script_nodes(nodes, request_text, package)
    real_sites = list(real_packet.get("ranked_review_sites") or [])
    real_ids = {_site_identity(row) for row in real_sites}
    alternatives = [row for row in reversed(ranked) if _site_identity(row) not in real_ids]
    used: set[tuple[str, str, str, str]] = set()
    sham_sites: list[dict[str, Any]] = []
    for real in real_sites:
        candidates = [
            row
            for row in alternatives
            if _site_identity(row) not in used
            and row.get("language") == real.get("language")
        ]
        same_role = [row for row in candidates if row.get("role") == real.get("role")]
        selected = (same_role or candidates or alternatives)[0] if (same_role or candidates or alternatives) else None
        if selected is None:
            continue
        used.add(_site_identity(selected))
        view = _compact_site(selected)
        view["localization_score"] = real.get("localization_score")
        view["selection_channel"] = real.get("selection_channel")
        sham_sites.append(view)

    sham_findings = []
    for index, finding in enumerate(real_packet.get("high_confidence_structural_findings") or []):
        if not sham_sites:
            break
        site = sham_sites[index % len(sham_sites)]
        sham_findings.append(
            {
                **finding,
                "path": site.get("path"),
                "symbol": site.get("symbol"),
                "node_type": site.get("node_type"),
                "node_id": site.get("node_id"),
                "node_sha256": site.get("source_sha256"),
                "line": (site.get("span") or {}).get("start_line"),
                "end_line": (site.get("span") or {}).get("end_line"),
                "observed_source": site.get("observed_source"),
                "evidence": {"package_local_structural_role": site.get("role")},
            }
        )
    edge_keys = (
        "caller_path",
        "caller_symbol",
        "callee_path",
        "callee_symbol",
        "line",
        "call_source",
    )
    real_edges = list(real_packet.get("package_local_call_edges") or [])
    real_edge_ids = {
        canonical_json_hash({key: row.get(key) for key in edge_keys})
        for row in real_edges
    }
    graph_edges = [
        {key: row.get(key) for key in edge_keys}
        for row in _python_typed_graph(package)["call_edges"]
    ]
    alternative_edges = [
        row
        for row in graph_edges
        if canonical_json_hash(row) not in real_edge_ids
    ]
    edge_pool = alternative_edges or graph_edges or real_edges
    sham_edges = [dict(edge_pool[index % len(edge_pool)]) for index in range(len(real_edges))] if edge_pool else []

    real_def_use = list(real_packet.get("local_def_use_observations") or [])
    sham_def_use = []
    for index, row in enumerate(real_def_use):
        site = sham_sites[index % len(sham_sites)] if sham_sites else None
        if site is None:
            break
        relocated = dict(row)
        relocated.update(
            {
                "path": site.get("path"),
                "symbol": site.get("symbol"),
                "line": (site.get("span") or {}).get("start_line"),
            }
        )
        sham_def_use.append(relocated)

    selected_alternatives = [
        row for row in alternatives if _site_identity(row) in used
    ]
    sham_relations = _relation_groups(nodes, selected_alternatives)
    real_relations = list(real_packet.get("relation_groups") or [])
    if len(sham_relations) < len(real_relations) and sham_sites:
        for index in range(len(sham_relations), len(real_relations)):
            source = dict(real_relations[index])
            site = sham_sites[index % len(sham_sites)]
            if "path" in source:
                source["path"] = site.get("path")
            if "symbol" in source:
                source["symbol"] = site.get("symbol")
            if "observed_source" in source:
                source["observed_source"] = site.get("observed_source")
            if "site_ids" in source:
                source["site_ids"] = [site.get("node_id")]
            sham_relations.append(source)
    sham_relations = sham_relations[: len(real_relations)]
    packet = {
        **real_packet,
        "method": "runtime_request_conditioned_soft_structural_prior",
        "ranked_review_sites": sham_sites,
        "high_confidence_structural_findings": sham_findings,
        "package_local_call_edges": sham_edges,
        "local_def_use_observations": sham_def_use,
        "relation_groups": sham_relations,
        "shape_padding": "",
        "control_metadata": {
            "construction": "deterministic_low_rank_package_local_derangement",
            "hidden_labels_consumed": False,
            "real_site_overlap_count": sum(_site_identity(row) in real_ids for row in sham_sites),
            "real_edge_overlap_count": sum(
                canonical_json_hash(row) in real_edge_ids for row in sham_edges
            ),
            "same_field_counts": (
                len(sham_sites) == len(real_sites)
                and len(sham_findings)
                == len(real_packet.get("high_confidence_structural_findings") or [])
                and len(sham_edges) == len(real_edges)
                and len(sham_def_use) == len(real_def_use)
                and len(sham_relations) == len(real_relations)
            ),
        },
    }
    packet["counts"] = {
        **dict(real_packet.get("counts") or {}),
        "review_site_count": len(sham_sites),
        "high_confidence_finding_count": len(sham_findings),
        "package_local_call_edge_count": len(sham_edges),
        "local_def_use_observation_count": len(sham_def_use),
        "relation_group_count": len(sham_relations),
    }
    packet.pop("packet_hash", None)
    packet["packet_hash"] = canonical_json_hash(packet)
    return packet


def build_matched_advisory_pair(
    package_root: str | Path,
    request_text: str,
    *,
    maximum_sites: int = MAX_REVIEW_SITES,
) -> dict[str, Any]:
    """Build condition-blind real/sham packets with exact serialized byte parity."""
    real = build_runtime_advisory_packet(
        package_root, request_text, maximum_sites=maximum_sites
    )
    real["shape_padding"] = ""
    real.pop("packet_hash", None)
    real["packet_hash"] = canonical_json_hash(real)
    sham = build_sham_advisory_packet(package_root, request_text, real)
    dropped_unmatchable_edge_count = 0
    sham_metadata = sham.get("control_metadata") or {}
    if int(sham_metadata.get("real_edge_overlap_count") or 0) > 0:
        dropped_unmatchable_edge_count = len(real.get("package_local_call_edges") or [])
        real["package_local_call_edges"] = []
        sham["package_local_call_edges"] = []
        real["counts"]["package_local_call_edge_count"] = 0
        sham["counts"]["package_local_call_edge_count"] = 0
        sham_metadata["real_edge_overlap_count"] = 0
        sham_metadata["same_field_counts"] = True

    def prompt_bytes(packet: dict[str, Any]) -> int:
        return len(
            json.dumps(
                advisory_prompt_view(packet),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        )

    real_bytes = prompt_bytes(real)
    sham_bytes = prompt_bytes(sham)
    if real_bytes < sham_bytes:
        real["shape_padding"] = " " * (sham_bytes - real_bytes)
    elif sham_bytes < real_bytes:
        sham["shape_padding"] = " " * (real_bytes - sham_bytes)
    for packet in (real, sham):
        packet.pop("packet_hash", None)
        packet["packet_hash"] = canonical_json_hash(packet)
    real_bytes = prompt_bytes(real)
    sham_bytes = prompt_bytes(sham)
    audit = {
        "schema_version": SCHEMA_VERSION,
        "real_prompt_bytes": real_bytes,
        "sham_prompt_bytes": sham_bytes,
        "exact_prompt_byte_match": real_bytes == sham_bytes,
        "same_field_counts": bool(
            (sham.get("control_metadata") or {}).get("same_field_counts")
        ),
        "real_site_overlap_count": int(
            (sham.get("control_metadata") or {}).get("real_site_overlap_count") or 0
        ),
        "real_edge_overlap_count": int(
            (sham.get("control_metadata") or {}).get("real_edge_overlap_count") or 0
        ),
        "dropped_unmatchable_edge_count": dropped_unmatchable_edge_count,
        "hidden_labels_consumed": False,
    }
    audit["match_pass"] = (
        audit["exact_prompt_byte_match"]
        and audit["same_field_counts"]
        and audit["real_site_overlap_count"] == 0
        and audit["real_edge_overlap_count"] == 0
    )
    audit["audit_hash"] = canonical_json_hash(audit)
    return {"real": real, "sham": sham, "matching_audit": audit}


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _source(source: str, node: ast.AST) -> str:
    return ast.get_source_segment(source, node) or ""


def _scope_nodes(root: ast.AST) -> Iterable[ast.AST]:
    stack = list(ast.iter_child_nodes(root))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _python_typed_graph(package: Path) -> dict[str, Any]:
    definitions: list[dict[str, Any]] = []
    for file in sorted((package / "scripts").rglob("*.py")) if (package / "scripts").is_dir() else []:
        relative = file.relative_to(package).as_posix()
        source = file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError:
            continue
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        def visit(
            body: list[ast.stmt],
            prefix: str = "",
            owner_class: str | None = None,
        ) -> None:
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qualname = f"{prefix}.{node.name}" if prefix else node.name
                    positional_formals = [
                        arg.arg
                        for arg in (
                            list(node.args.posonlyargs)
                            + list(node.args.args)
                        )
                    ]
                    keyword_only_formals = [arg.arg for arg in node.args.kwonlyargs]
                    decorators = {
                        decorator.id
                        if isinstance(decorator, ast.Name)
                        else decorator.attr
                        if isinstance(decorator, ast.Attribute)
                        else ""
                        for decorator in node.decorator_list
                    }
                    descriptor = (
                        "staticmethod"
                        if owner_class and "staticmethod" in decorators
                        else "classmethod"
                        if owner_class and "classmethod" in decorators
                        else "instance_method"
                        if owner_class
                        else "function"
                    )
                    definitions.append(
                        {
                            "path": relative,
                            "symbol": qualname,
                            "simple": node.name,
                            "owner_class": owner_class,
                            "descriptor": descriptor,
                            "positional_formals": positional_formals,
                            "keyword_only_formals": keyword_only_formals,
                            "formals": positional_formals + keyword_only_formals,
                            "vararg": node.args.vararg.arg if node.args.vararg else None,
                            "kwarg": node.args.kwarg.arg if node.args.kwarg else None,
                            "node": node,
                            "source": source,
                        }
                    )
                    visit(node.body, qualname, None)
                elif isinstance(node, ast.ClassDef):
                    qualname = f"{prefix}.{node.name}" if prefix else node.name
                    visit(node.body, qualname, qualname)

        visit(tree.body)

    by_simple: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for definition in definitions:
        by_simple[str(definition["simple"])].append(definition)
    edges: list[dict[str, Any]] = []
    argument_flows: list[dict[str, Any]] = []
    unresolved_calls: list[dict[str, Any]] = []
    for caller in definitions:
        node = caller["node"]
        source = str(caller["source"])
        for child in _scope_nodes(node):
            if not isinstance(child, ast.Call):
                continue
            callee_name = _call_name(child)
            candidates = by_simple.get(callee_name, [])
            same_file = [row for row in candidates if row["path"] == caller["path"]]
            exact_owner: list[dict[str, Any]] = []
            receiver: ast.AST | None = None
            if isinstance(child.func, ast.Attribute):
                receiver = child.func.value
                if isinstance(receiver, ast.Name) and receiver.id in {"self", "cls"}:
                    owner = str(caller.get("owner_class") or "")
                    exact_owner = [
                        row
                        for row in same_file
                        if owner and row.get("owner_class") == owner
                    ]
                elif isinstance(receiver, ast.Name):
                    exact_owner = [
                        row
                        for row in same_file
                        if str(row.get("owner_class") or "").split(".")[-1]
                        == receiver.id
                    ]
            resolved = (
                exact_owner[0]
                if len(exact_owner) == 1
                else same_file[0]
                if len(same_file) == 1
                else candidates[0]
                if len(candidates) == 1
                else None
            )
            if resolved is None:
                if candidates:
                    unresolved_calls.append(
                        {
                            "caller_path": caller["path"],
                            "caller_symbol": caller["symbol"],
                            "callee_name": callee_name,
                            "line": int(getattr(child, "lineno", 0)),
                            "call_source": _source(source, child),
                            "reason": "ambiguous_package_definition",
                            "candidate_symbols": [
                                {"path": row["path"], "symbol": row["symbol"]}
                                for row in candidates
                            ],
                        }
                    )
                continue
            resolution = (
                "same_owner_exact"
                if exact_owner
                else "same_file_unique"
                if same_file
                else "package_unique"
            )
            edge = {
                "relation": "calls",
                "caller_path": caller["path"],
                "caller_symbol": caller["symbol"],
                "callee_path": resolved["path"],
                "callee_symbol": resolved["symbol"],
                "line": int(getattr(child, "lineno", 0)),
                "call_source": _source(source, child),
                "resolution": resolution,
            }
            edge_hash = canonical_json_hash(edge)
            edge["edge_hash"] = edge_hash
            edges.append(edge)
            positional_formals = list(resolved["positional_formals"])
            bound_receiver = False
            if isinstance(child.func, ast.Attribute):
                descriptor = str(resolved["descriptor"])
                if descriptor == "classmethod":
                    bound_receiver = True
                elif descriptor == "instance_method":
                    receiver_is_class = (
                        isinstance(receiver, ast.Name)
                        and receiver.id
                        == str(resolved.get("owner_class") or "").split(".")[-1]
                    )
                    bound_receiver = not receiver_is_class
            if bound_receiver and positional_formals:
                positional_formals = positional_formals[1:]
            for index, argument in enumerate(child.args):
                if isinstance(argument, ast.Starred):
                    continue
                if index >= len(positional_formals):
                    if resolved.get("vararg"):
                        formal = "*" + str(resolved["vararg"])
                    else:
                        break
                else:
                    formal = positional_formals[index]
                argument_flows.append(
                    {
                        **edge,
                        "relation": "argument_to_formal",
                        "edge_hash": edge_hash,
                        "argument_slot": index,
                        "argument_source": _source(source, argument),
                        "callee_formal": formal,
                    }
                )
            for keyword in child.keywords:
                if keyword.arg and (
                    keyword.arg in resolved["formals"] or resolved.get("kwarg")
                ):
                    argument_flows.append(
                        {
                            **edge,
                            "relation": "keyword_to_formal",
                            "edge_hash": edge_hash,
                            "argument_slot": keyword.arg,
                            "argument_source": _source(source, keyword.value),
                            "callee_formal": (
                                keyword.arg
                                if keyword.arg in resolved["formals"]
                                else "**" + str(resolved["kwarg"])
                            ),
                        }
                    )
    deduped_edges = {
        canonical_json_hash(row): row for row in edges
    }
    deduped_flows = {
        canonical_json_hash(row): row for row in argument_flows
    }
    return {
        "definitions": [
            {
                key: row[key]
                for key in (
                    "path",
                    "symbol",
                    "simple",
                    "owner_class",
                    "descriptor",
                    "positional_formals",
                    "keyword_only_formals",
                    "formals",
                    "vararg",
                    "kwarg",
                )
            }
            for row in definitions
        ],
        "call_edges": list(deduped_edges.values()),
        "argument_flows": list(deduped_flows.values()),
        "unresolved_calls": unresolved_calls,
    }


def _matches_seed(path: str, symbol: str, seeds: set[tuple[str, str]]) -> bool:
    return any(
        path == seed_path
        and (symbol == seed_symbol or symbol.endswith("." + seed_symbol) or seed_symbol.endswith("." + symbol))
        for seed_path, seed_symbol in seeds
    )


def _typed_closure(package: Path, changed_symbols: list[dict[str, Any]]) -> dict[str, Any]:
    graph = _python_typed_graph(package)
    seeds = {
        (str(row.get("path") or ""), str(row.get("symbol") or ""))
        for row in changed_symbols
        if str(row.get("symbol") or "") != "<module>"
    }
    outgoing: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    incoming: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for edge in graph["call_edges"]:
        caller = (str(edge["caller_path"]), str(edge["caller_symbol"]))
        callee = (str(edge["callee_path"]), str(edge["callee_symbol"]))
        outgoing[caller].append(edge)
        incoming[callee].append(edge)

    normalized_seeds = {
        (str(definition["path"]), str(definition["symbol"]))
        for definition in graph["definitions"]
        if _matches_seed(str(definition["path"]), str(definition["symbol"]), seeds)
    }
    queue = deque((seed, 0, "changed") for seed in normalized_seeds)
    visited: dict[tuple[str, str], tuple[int, set[str]]] = {
        seed: (0, {"changed"}) for seed in normalized_seeds
    }
    relevant_edges: dict[str, dict[str, Any]] = {}
    while queue:
        node, depth, _direction = queue.popleft()
        if depth >= 2:
            continue
        for direction, rows in (("downstream_callee", outgoing.get(node, [])), ("upstream_caller", incoming.get(node, []))):
            for edge in rows:
                neighbor = (
                    (str(edge["callee_path"]), str(edge["callee_symbol"]))
                    if direction == "downstream_callee"
                    else (str(edge["caller_path"]), str(edge["caller_symbol"]))
                )
                relevant_edges[canonical_json_hash(edge)] = edge
                old_depth, directions = visited.get(neighbor, (99, set()))
                new_depth = depth + 1
                directions = set(directions) | {direction}
                if new_depth < old_depth:
                    visited[neighbor] = (new_depth, directions)
                    queue.append((neighbor, new_depth, direction))
                else:
                    visited[neighbor] = (old_depth, directions)

    relevant_edge_values = list(relevant_edges.values())[:48]
    edge_hashes = {str(edge["edge_hash"]) for edge in relevant_edge_values}
    relevant_flows = [
        row
        for row in graph["argument_flows"]
        if str(row.get("edge_hash") or "") in edge_hashes
    ][:48]
    unsupported_seeds = [
        {"path": str(row.get("path") or ""), "symbol": str(row.get("symbol") or "")}
        for row in changed_symbols
        if str(row.get("path") or "").endswith(
            (".js", ".mjs", ".cjs", ".ts", ".tsx", ".sh", ".bash")
        )
    ]
    return {
        "seed_symbols": [
            {"path": path, "symbol": symbol} for path, symbol in sorted(normalized_seeds)
        ],
        "directional_symbols": [
            {
                "path": path,
                "symbol": symbol,
                "distance": depth,
                "directions": sorted(directions),
            }
            for (path, symbol), (depth, directions) in sorted(visited.items())
        ][:64],
        "typed_call_edges": relevant_edge_values,
        "argument_to_formal_flows": relevant_flows,
        "unresolved_calls": graph["unresolved_calls"][:48],
        "closure_depth": 2,
        "direction_preserved": True,
        "typed_backend": "python_ast",
        "unsupported_changed_symbols": unsupported_seeds,
        "coverage_status": "python_only" if unsupported_seeds else "complete_for_resolved_python_calls",
    }


def _finding_counter(
    packet: dict[str, Any],
) -> Counter[tuple[str, str, str, str, str]]:
    return Counter(
        (
            str(row.get("finding_type") or ""),
            str(row.get("path") or ""),
            str(row.get("symbol") or ""),
            str(row.get("node_type") or ""),
            re.sub(r"\s+", " ", str(row.get("observed_source") or "")).strip(),
        )
        for row in packet.get("high_confidence_structural_findings", [])
    )


def _counter_rows(
    counter: Counter[tuple[str, str, str, str, str]],
) -> list[dict[str, Any]]:
    return [
        {
            "finding_type": key[0],
            "path": key[1],
            "symbol": key[2],
            "node_type": key[3],
            "observed_source": key[4],
            "count": value,
        }
        for key, value in sorted(counter.items())
        if value > 0
    ]


def _document_contract_mentions(package: Path, symbols: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    skill = package / "SKILL.md"
    if not skill.is_file():
        return []
    names = {
        str(row.get("symbol") or "").split(".")[-1]
        for row in symbols
        if str(row.get("symbol") or "") not in {"", "<module>"}
    }
    rows = []
    for line_number, line in enumerate(skill.read_text(encoding="utf-8").splitlines(), start=1):
        matched = sorted(name for name in names if name and name in line)
        if matched:
            rows.append({"path": "SKILL.md", "line": line_number, "symbols": matched, "text": line[:500]})
    return rows[:16]


def build_balanced_posthoc_report(
    parent_package: str | Path,
    candidate_package: str | Path,
    request_text: str,
    *,
    parent_advisory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parent = Path(parent_package).resolve()
    candidate = Path(candidate_package).resolve()
    _assert_public_package(parent)
    _assert_public_package(candidate)
    with tempfile.TemporaryDirectory(prefix="balanced-public-projection-") as directory:
        projection = Path(directory)
        visible_parent = projection / "parent"
        visible_candidate = projection / "candidate"
        _copy_visible_package(parent, visible_parent)
        _copy_visible_package(candidate, visible_candidate)
        base = build_proposal_first_closure_report(
            visible_parent,
            visible_candidate,
            request_text,
            visible_structural_facts=None,
        )
    before = parent_advisory or build_runtime_advisory_packet(parent, request_text)
    after = build_runtime_advisory_packet(candidate, request_text)
    before_counter = _finding_counter(before)
    after_counter = _finding_counter(after)
    changed_symbols = list(base.get("impact_closure", {}).get("changed_symbols") or [])
    typed = _typed_closure(candidate, changed_symbols)
    result = dict(base)
    result.pop("gate_hash", None)
    result.update(
        {
            "schema_version": SCHEMA_VERSION,
            "runtime_advisory_before_hash": before.get("packet_hash"),
            "runtime_advisory_after_hash": after.get("packet_hash"),
            "candidate_runtime_advisory": advisory_prompt_view(after),
            "runtime_residual_comparison": {
                "resolved_high_confidence_findings": _counter_rows(before_counter - after_counter),
                "remaining_high_confidence_findings": _counter_rows(before_counter & after_counter),
                "new_high_confidence_findings": _counter_rows(after_counter - before_counter),
                "before_count": sum(before_counter.values()),
                "after_count": sum(after_counter.values()),
                "semantic_correctness_inferred": False,
            },
            "typed_impact_closure": typed,
            "document_contract_mentions": _document_contract_mentions(candidate, typed["directional_symbols"]),
            "benchmark_bundled_visible_facts_consumed": False,
            "task_verifier_consumed": False,
            "gold_or_oracle_consumed": False,
            "claim_boundary": (
                "This report checks syntax, public compatibility, actual changed nodes, directional call "
                "closure, argument-to-formal flow, and rerun generic anomalies. It cannot determine hidden "
                "behavior or semantic correctness."
            ),
        }
    )
    result["gate_hash"] = canonical_json_hash(result)
    return result


def _risk_profile(report: dict[str, Any] | None) -> dict[str, int | bool]:
    if not report:
        return {
            "accepted": False,
            "new_findings": 10**6,
            "parse_failures": 10**6,
            "removed_public": 10**6,
            "unresolved_names": 10**6,
            "changed_paths": 10**6,
            "changed_symbols": 10**6,
        }
    residual = report.get("runtime_residual_comparison") or {}
    parse = report.get("parse_failures") or {}
    return {
        "accepted": report.get("decision") == ACCEPT,
        "new_findings": sum(int(row.get("count") or 0) for row in residual.get("new_high_confidence_findings", [])),
        "parse_failures": sum(len(value or []) for value in parse.values()) if isinstance(parse, dict) else len(parse or []),
        "removed_public": len(report.get("removed_public_declarations") or []),
        "unresolved_names": len(report.get("unresolved_name_findings") or []),
        "changed_paths": len(report.get("changed_paths") or []),
        "changed_symbols": len((report.get("typed_impact_closure") or {}).get("seed_symbols") or []),
    }


def choose_balanced_source(
    *,
    first_exists: bool,
    revision_exists: bool,
    first_report: dict[str, Any] | None,
    revision_report: dict[str, Any] | None,
    same_tree: bool = False,
    first_application_error: str | None = None,
) -> tuple[str, str, bool]:
    """Prefer a safe revision, but reject visible structural regressions."""
    first = _risk_profile(first_report)
    revision = _risk_profile(revision_report)
    if revision_exists and revision["accepted"]:
        if not first_exists or not first["accepted"] or first_application_error:
            return "revision", "revision_recovers_missing_or_unsafe_first_candidate", False
        if same_tree:
            return "first", "revision_is_tree_equivalent_to_first", False
        hard_keys = ("parse_failures", "removed_public", "unresolved_names")
        if any(int(revision[key]) > int(first[key]) for key in hard_keys):
            return "first", "revision_rejected_visible_hard_risk_regression", False
        if int(revision["new_findings"]) > int(first["new_findings"]):
            return "first", "revision_rejected_new_high_confidence_anomaly", False
        if int(revision["changed_paths"]) > max(4, int(first["changed_paths"]) + 1):
            return "first", "revision_rejected_scope_expansion", False
        return "revision", "revision_structurally_safe_without_visible_regression", False
    if first_exists and first["accepted"]:
        return "first", "revision_unavailable_or_unsafe_fallback_first", False
    return "parent", "no_structurally_safe_candidate_abstain", True
