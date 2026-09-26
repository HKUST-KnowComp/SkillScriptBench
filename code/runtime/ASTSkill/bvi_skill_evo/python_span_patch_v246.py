from __future__ import annotations

import ast
import copy
import itertools
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from bvi_skill_evo import python_span_patch_v237 as base
from bvi_skill_evo import python_span_patch_v241 as prior
from skillscriptbench.io_utils import canonical_json_hash, copy_tree_clean, hash_tree


ACCEPT_STRUCTURALLY = base.ACCEPT_STRUCTURALLY
MAX_EDITS = base.MAX_EDITS
PYTHON_SPAN_PATCH_TOOL = copy.deepcopy(prior.PYTHON_SPAN_PATCH_TOOL)
PYTHON_SPAN_PATCH_TOOL["function"]["description"] = (
    "Submit zero, one, or two bounded Python source-span replacements. Exact whole-function "
    "replacements are allowed only when declaration kind, name, signature, decorators, and return "
    "annotation are preserved. Statement indentation may be normalized mechanically."
)


def _single_declaration(value: str) -> ast.AST | None:
    try:
        module = ast.parse(value)
    except SyntaxError:
        return None
    if len(module.body) != 1:
        return None
    node = module.body[0]
    return node if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else None


def _declaration_contract(node: ast.AST) -> tuple[Any, ...]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return (
            type(node).__name__,
            node.name,
            ast.dump(node.args, include_attributes=False),
            ast.dump(node.returns, include_attributes=False) if node.returns else None,
            tuple(ast.dump(value, include_attributes=False) for value in node.decorator_list),
        )
    if isinstance(node, ast.ClassDef):
        return (
            type(node).__name__,
            node.name,
            tuple(ast.dump(value, include_attributes=False) for value in node.bases),
            tuple(ast.dump(value, include_attributes=False) for value in node.keywords),
            tuple(ast.dump(value, include_attributes=False) for value in node.decorator_list),
        )
    raise TypeError(type(node).__name__)


def _validate_declaration_replacement(observed: str, replacement: str, index: int) -> None:
    replacement_has_declaration = bool(
        re.search(r"(?m)^\s*(?:async\s+def|def|class)\s+", replacement)
    )
    if not replacement_has_declaration:
        return
    old = _single_declaration(observed)
    new = _single_declaration(replacement)
    if old is None or new is None or _declaration_contract(old) != _declaration_contract(new):
        raise ValueError(f"unsafe_declaration_replacement:{index}")


def _parse_response(content: str) -> tuple[dict[str, Any], list[str]]:
    raw_payload = json.loads(content)
    if not isinstance(raw_payload, dict):
        raise TypeError("patch_payload_must_be_object")
    ignored = sorted(set(raw_payload) - {"summary", "edits"})
    raw_edits = raw_payload.get("edits")
    if not isinstance(raw_edits, list) or len(raw_edits) > MAX_EDITS:
        raise ValueError("bounded_edits_required")
    edits: list[dict[str, str]] = []
    for index, raw in enumerate(raw_edits):
        if not isinstance(raw, dict):
            raise TypeError(f"edit_must_be_object:{index}")
        allowed = {
            "operation",
            "path",
            "symbol",
            "observed_source",
            "replacement_source",
            "replacement",
            "target_node_id",
            "expected_node_sha256",
            "node_id",
            "node_sha256",
        }
        ignored.extend(f"edits[{index}].{key}" for key in sorted(set(raw) - allowed))
        operation = str(raw.get("operation") or "replace_span")
        if operation not in {"replace_span", "replace_node", "replace_statement"}:
            raise ValueError(f"unsupported_operation:{operation}")
        observed = base._strip_fence(str(raw.get("observed_source") or ""))
        replacement = base._strip_fence(
            str(raw.get("replacement_source", raw.get("replacement")) or "")
        )
        edit = {
            "operation": "replace_span",
            "path": str(raw.get("path") or ""),
            "symbol": str(raw.get("symbol") or ""),
            "observed_source": observed,
            "replacement_source": replacement,
            "target_node_id": str(raw.get("target_node_id") or raw.get("node_id") or ""),
            "expected_node_sha256": str(
                raw.get("expected_node_sha256") or raw.get("node_sha256") or ""
            ),
        }
        if "node_id" in raw:
            ignored.append(f"edits[{index}].node_id_alias")
        if "node_sha256" in raw:
            ignored.append(f"edits[{index}].node_sha256_alias")
        if not edit["path"] or not observed:
            raise ValueError(f"edit_path_and_observed_source_required:{index}")
        if observed == replacement:
            continue
        if len(observed.splitlines()) > base.MAX_OBSERVED_LINES:
            raise ValueError(f"observed_span_too_large:{index}")
        if len(replacement.splitlines()) > base.MAX_REPLACEMENT_LINES:
            raise ValueError(f"replacement_span_too_large:{index}")
        if len(replacement.encode("utf-8")) > base.MAX_REPLACEMENT_BYTES:
            raise ValueError(f"replacement_bytes_too_large:{index}")
        _validate_declaration_replacement(observed, replacement, index)
        edits.append(edit)
    return {"summary": str(raw_payload.get("summary") or ""), "edits": edits}, sorted(
        set(ignored)
    )


def _bound_span(
    source: str,
    edit: dict[str, str],
    facts: dict[str, Any],
) -> tuple[int, int, str, dict[str, Any]]:
    raw_matches = [
        row
        for row in facts.get("editable_nodes", [])
        if row.get("node_id") == edit["target_node_id"]
    ]
    unique: dict[str, dict[str, Any]] = {
        canonical_json_hash(row): row for row in raw_matches
    }
    matches = list(unique.values())
    if len(matches) != 1:
        raise ValueError(f"ast_node_binding_not_unique:{len(matches)}")
    node = matches[0]
    if node["path"] != edit["path"]:
        raise ValueError("ast_node_path_mismatch")
    if node["node_sha256"] != edit["expected_node_sha256"]:
        raise ValueError("ast_node_sha256_mismatch")
    node_start = base._line_col_offset(source, int(node["line"]), int(node["column"]))
    node_end = base._line_col_offset(source, int(node["end_line"]), int(node["end_column"]))
    node_source = source[node_start:node_end]
    if node_source != node["observed_source"]:
        raise ValueError("visible_ast_node_source_drift")
    if edit["observed_source"] == node_source:
        return (
            node_start,
            node_end,
            edit["replacement_source"],
            {
                "mode": "exact_visible_ast_node",
                "node_id": node["node_id"],
                "duplicate_identical_fact_count": len(raw_matches),
                "node_type": node.get("node_type"),
            },
        )
    start, end, _ = base._unbound_span(source, edit["observed_source"])
    if not (start <= node_start < node_end <= end):
        raise ValueError("ast_context_span_does_not_contain_bound_node")
    return (
        start,
        end,
        edit["replacement_source"],
        {
            "mode": "exact_context_span_anchored_by_visible_ast_node",
            "node_id": node["node_id"],
            "duplicate_identical_fact_count": len(raw_matches),
            "node_type": node.get("node_type"),
        },
    )


def _statement_indentation_variant(source: str, start: int, replacement: str) -> str:
    line_start = source.rfind("\n", 0, start) + 1
    prefix = source[line_start:start]
    if prefix.strip() or not prefix or "\n" not in replacement:
        if prefix.strip() or not prefix:
            return replacement
    lines = replacement.splitlines()
    if not lines:
        return replacement
    first_indent = len(lines[0]) - len(lines[0].lstrip(" \t"))
    remove_prefix = first_indent >= len(prefix)
    relative: list[str] = []
    for line in lines:
        if remove_prefix and line.startswith(prefix):
            relative.append(line[len(prefix) :])
        else:
            relative.append(line)
    relative[0] = relative[0].lstrip(" \t")
    normalized = [relative[0]]
    normalized.extend(prefix + line if line else "" for line in relative[1:])
    return "\n".join(normalized)


def _materialize_parseable_source(
    source: str,
    edits: list[tuple[int, int, str, dict[str, str], dict[str, Any]]],
    *,
    filename: str,
) -> tuple[str, list[str]]:
    ordered = sorted(edits, key=lambda row: (row[0], row[1]), reverse=True)
    for index, row in enumerate(ordered):
        if index + 1 < len(ordered) and ordered[index + 1][1] > row[0]:
            raise ValueError("overlapping_edits_forbidden")
    variants: list[list[str]] = []
    for start, _, replacement, _, _ in ordered:
        normalized = _statement_indentation_variant(source, start, replacement)
        variants.append([replacement] if normalized == replacement else [replacement, normalized])
    combinations = list(itertools.product(*variants))
    combinations.sort(key=lambda values: sum(value != ordered[index][2] for index, value in enumerate(values)))
    first_error: SyntaxError | None = None
    for values in combinations:
        candidate = source
        for (start, end, _, _, _), replacement in zip(ordered, values):
            candidate = candidate[:start] + replacement + candidate[end:]
        try:
            ast.parse(candidate, filename=filename)
        except SyntaxError as exc:
            first_error = first_error or exc
            continue
        modes = [
            "raw" if value == ordered[index][2] else "statement_indentation_normalized"
            for index, value in enumerate(values)
        ]
        return candidate, modes
    if first_error is not None:
        raise first_error
    raise ValueError("no_replacement_combination")


def apply_python_span_patch(
    content: str,
    source_package: str | Path,
    candidate_package: str | Path,
    *,
    visible_ast_facts: dict[str, Any] | None,
    require_ast_binding: bool,
) -> dict[str, Any]:
    source_root = Path(source_package).resolve()
    candidate_root = Path(candidate_package).resolve()
    if require_ast_binding and not isinstance(visible_ast_facts, dict):
        raise ValueError("visible_ast_facts_required")
    parsed, ignored_fields = _parse_response(content)
    before_api = base._api_signatures(source_root)
    copy_tree_clean(source_root, candidate_root)
    grouped: dict[str, list[tuple[int, int, str, dict[str, str], dict[str, Any]]]] = defaultdict(list)
    for edit in parsed["edits"]:
        target = base._safe_path(candidate_root, edit["path"])
        if not target.is_file():
            raise FileNotFoundError(edit["path"])
        source = target.read_text(encoding="utf-8")
        if require_ast_binding:
            span = _bound_span(source, edit, visible_ast_facts or {})
        else:
            if edit["target_node_id"] or edit["expected_node_sha256"]:
                ignored_fields.extend(["unbound_target_node_id", "unbound_expected_node_sha256"])
            span = prior._unbound_span(source, edit)
        grouped[edit["path"]].append((*span[:3], edit, span[3]))
    receipts: list[dict[str, Any]] = []
    normalization_used = False
    for relative, edits in grouped.items():
        target = base._safe_path(candidate_root, relative)
        original = target.read_text(encoding="utf-8")
        materialized, modes = _materialize_parseable_source(original, edits, filename=relative)
        ordered = sorted(edits, key=lambda row: (row[0], row[1]), reverse=True)
        for (start, end, replacement, edit, locator), mode in zip(ordered, modes):
            applied = (
                replacement
                if mode == "raw"
                else _statement_indentation_variant(original, start, replacement)
            )
            normalization_used |= mode != "raw"
            receipts.append(
                {
                    "path": relative,
                    "symbol": edit["symbol"],
                    "locator": locator,
                    "applied_observed_sha256": canonical_json_hash(original[start:end]),
                    "applied_replacement_sha256": canonical_json_hash(applied),
                    "replacement_normalization": mode,
                }
            )
        target.write_text(materialized, encoding="utf-8")
    after_api = base._api_signatures(candidate_root)
    if before_api != after_api:
        raise ValueError("public_api_signature_changed")
    source_hashes = hash_tree(source_root)
    candidate_hashes = hash_tree(candidate_root)
    changed_paths = sorted(
        path
        for path in set(source_hashes) | set(candidate_hashes)
        if source_hashes.get(path) != candidate_hashes.get(path)
    )
    if len(changed_paths) > MAX_EDITS or any(
        not path.endswith(".py") or "scripts/" not in path for path in changed_paths
    ):
        raise ValueError(f"changed_path_scope_invalid:{changed_paths}")
    return {
        "status": "candidate_materialized",
        "summary": parsed["summary"],
        "edit_count": len(parsed["edits"]),
        "changed_paths": changed_paths,
        "ignored_response_fields": sorted(set(ignored_fields)),
        "edit_receipts": receipts,
        "candidate_tree_hash": canonical_json_hash(candidate_hashes),
        "structural_gate": {
            "decision": ACCEPT_STRUCTURALLY,
            "checks": {
                "python_parse": True,
                "public_api_signatures_preserved": True,
                "changed_paths_within_scripts": True,
                "bounded_edit_count": len(parsed["edits"]) <= MAX_EDITS,
                "safe_same_contract_declaration_replacement": True,
                "normalization_was_mechanical_only": True,
                "hidden_semantic_correctness_checked": False,
            },
            "statement_indentation_normalized": normalization_used,
            "claim_boundary": "Structural acceptance and mechanical normalization are not task correctness.",
        },
    }
