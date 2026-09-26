from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from typing import Any

from bvi_skill_evo import python_span_patch_v241 as prior_patch
from bvi_skill_evo import python_span_patch_v246 as python_patch
from skillscriptbench import d37_extension45_dual_path_experiment_v309 as patcher
from skillscriptbench.io_utils import canonical_json_hash, sha256_file


METHOD_ID = "node_bound_public_contract_flow_executor_v365"


def _node_coordinates(node_id: str) -> tuple[int, int, str] | None:
    parts = node_id.rsplit(":", 3)
    if len(parts) != 4:
        return None
    _, line, column, node_type = parts
    try:
        return int(line), int(column), node_type
    except ValueError:
        return None


def _node_at_identity(tree: ast.AST, node_id: str) -> ast.AST | None:
    coordinates = _node_coordinates(node_id)
    if coordinates is None:
        return None
    line, column, node_type = coordinates
    matches = [
        node
        for node in ast.walk(tree)
        if type(node).__name__ == node_type
        and getattr(node, "lineno", None) == line
        and getattr(node, "col_offset", None) == column
    ]
    return matches[0] if len(matches) == 1 else None


def canonical_editable_nodes(
    package_root: str | Path, facts: dict[str, Any]
) -> list[dict[str, Any]]:
    package = Path(package_root).resolve()
    rows = []
    seen: set[str] = set()
    for raw in facts.get("editable_nodes", []):
        node_id = str(raw.get("node_id") or "")
        path = str(raw.get("path") or raw.get("caller_path") or "")
        node_sha = str(raw.get("node_sha256") or "")
        if not node_id or not path or not node_sha or node_id in seen:
            continue
        target = (package / path).resolve()
        try:
            target.relative_to(package)
        except ValueError:
            continue
        if not target.is_file() or target.suffix != ".py":
            continue
        if raw.get("file_sha256") and sha256_file(target) != raw.get("file_sha256"):
            continue
        source = target.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError:
            continue
        node = _node_at_identity(tree, node_id)
        if node is None or not hasattr(node, "end_lineno"):
            continue
        observed = ast.get_source_segment(source, node)
        if not observed:
            continue
        rows.append(
            {
                **copy.deepcopy(raw),
                "path": path,
                "node_id": node_id,
                "node_sha256": node_sha,
                "line": int(node.lineno),
                "column": int(node.col_offset),
                "end_line": int(node.end_lineno),
                "end_column": int(node.end_col_offset),
                "node_type": type(node).__name__,
                "observed_source": observed,
            }
        )
        seen.add(node_id)
    return rows


def _matching_nodes(
    edit: dict[str, Any], nodes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    path = str(edit.get("path") or "")
    symbol = str(edit.get("symbol") or "")
    observed = str(edit.get("observed_source") or "")
    matches = []
    for node in nodes:
        if node["path"] != path:
            continue
        node_symbol = str(
            node.get("function_symbol") or node.get("caller_symbol") or ""
        )
        if symbol and node_symbol and symbol != node_symbol:
            continue
        anchor = str(node.get("observed_source") or "")
        if not anchor or anchor not in observed:
            continue
        matches.append(node)
    return matches


def bind_response_to_public_nodes(
    content: str, package_root: str | Path, facts: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    payload = json.loads(content)
    if not isinstance(payload, dict) or not isinstance(payload.get("edits"), list):
        raise ValueError("bounded_patch_payload_required")
    nodes = canonical_editable_nodes(package_root, facts)
    by_id = {row["node_id"]: row for row in nodes}
    bindings = []
    for index, edit in enumerate(payload["edits"]):
        if not isinstance(edit, dict):
            raise TypeError(f"edit_must_be_object:{index}")
        node_id = str(edit.get("target_node_id") or edit.get("node_id") or "")
        expected = str(
            edit.get("expected_node_sha256") or edit.get("node_sha256") or ""
        )
        if node_id or expected:
            node = by_id.get(node_id)
            if node is None or expected != node["node_sha256"]:
                raise ValueError(f"supplied_public_node_binding_invalid:{index}")
            matches = [node]
            mode = "model_supplied_public_node_binding"
        else:
            matches = _matching_nodes(edit, nodes)
            if len(matches) != 1:
                raise ValueError(
                    f"public_node_binding_not_unique:{index}:{len(matches)}"
                )
            node = matches[0]
            edit["target_node_id"] = node["node_id"]
            edit["expected_node_sha256"] = node["node_sha256"]
            mode = "deterministic_public_node_binding"
        observed = str(edit.get("observed_source") or "")
        replacement = str(
            edit.get("replacement_source", edit.get("replacement")) or ""
        )
        node_source = str(node["observed_source"])
        narrowed = False
        if observed != node_source:
            _, old_middle, new_middle, _ = prior_patch._minimal_difference(
                observed, replacement
            )
            if old_middle == node_source and old_middle != new_middle:
                edit["observed_source"] = node_source
                edit["replacement_source"] = new_middle
                narrowed = True
        bindings.append(
            {
                "edit_index": index,
                "mode": mode,
                "node_id": node["node_id"],
                "node_sha256": node["node_sha256"],
                "node_type": node["node_type"],
                "path": node["path"],
                "context_edit_narrowed_to_exact_node": narrowed,
            }
        )
    report = {
        "method": METHOD_ID,
        "edit_count": len(payload["edits"]),
        "available_public_node_count": len(nodes),
        "all_edits_bound": len(bindings) == len(payload["edits"]),
        "bindings": bindings,
        "hidden_or_verifier_feedback_used": False,
    }
    report["binding_hash"] = canonical_json_hash(report)
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")), report


def apply_bound_response(
    content: str,
    source_package: str | Path,
    candidate_package: str | Path,
    facts: dict[str, Any],
) -> dict[str, Any]:
    source = Path(source_package).resolve()
    canonical_facts = copy.deepcopy(facts)
    canonical_facts["editable_nodes"] = canonical_editable_nodes(source, facts)
    bound_content, binding = bind_response_to_public_nodes(content, source, facts)
    patcher._configure_edit_limit()
    application = python_patch.apply_python_span_patch(
        bound_content,
        source,
        Path(candidate_package).resolve(),
        visible_ast_facts=canonical_facts,
        require_ast_binding=True,
    )
    application["public_node_binding"] = binding
    application["method"] = METHOD_ID
    return application
