from __future__ import annotations

import difflib
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from bvi_skill_evo.balanced_hybrid_ast_v316 import (
    _assert_public_package,
    _tree_hash,
)
from bvi_skill_evo.multilang_node_patch_v4 import (
    ACCEPT_STRUCTURALLY,
    apply_multilang_node_patch,
)
from skillscriptbench.io_utils import canonical_json_hash, sha256_file
from skillscriptbench.multilang_structural_v66 import (
    JS_SUFFIXES,
    SHELL_SUFFIXES,
    extract_javascript_nodes,
    extract_shell_nodes,
    node_public_view,
    rank_script_nodes,
)


SCHEMA_VERSION = "4.10-public-runtime-multilang-dual-v3"
METHOD_ID = "public_runtime_typed_node_bound_dual_v3"
NON_PYTHON_LANGUAGES = {"javascript", "typescript", "shell"}
MAX_EDITABLE_NODES = 4

_FAMILY_PRIORITY = {
    "sibling_default_asymmetry": 6,
    "sort_direction_contract": 5,
    "failure_exit_status_contract": 5,
    "path_kind_contract": 5,
    "omitted_default_contract": 4,
    "literal_contract_conflict": 3,
}
_FALLBACK_RE = re.compile(
    r"(?P<base>[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+)"
    r"\s*(?:\|\||\?\?)\s*"
    r"(?P<default>[\"'][^\"']+[\"']|-?[0-9]+(?:\.[0-9]+)?)"
)
_CONTEXT_STOP = {
    "and",
    "args",
    "default",
    "for",
    "from",
    "if",
    "the",
    "this",
    "user",
    "with",
}


def _enumerate_non_python_nodes(package: Path) -> list[dict[str, Any]]:
    scripts = package / "scripts"
    rows: list[dict[str, Any]] = []
    if not scripts.is_dir():
        return rows
    for path in sorted(scripts.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(package)
        lowered_parts = {part.casefold() for part in relative.parts}
        if (
            lowered_parts & {"test", "tests", "__pycache__"}
            or path.name.startswith("test_")
            or ".test." in path.name
            or "_test." in path.name
        ):
            continue
        suffix = path.suffix.casefold()
        source = path.read_text(encoding="utf-8")
        if suffix in JS_SUFFIXES:
            rows.extend(extract_javascript_nodes(relative.as_posix(), source))
        elif suffix in SHELL_SUFFIXES:
            rows.extend(extract_shell_nodes(relative.as_posix(), source))
    return rows


def _focus_request(request_text: str) -> str:
    marker = re.search(r"^##\s+Required Use Case\s*$", request_text, flags=re.MULTILINE)
    return request_text[marker.end() :] if marker else request_text


def _tokens(value: str) -> set[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]*", expanded)
        if len(token) >= 2
    }


def _dedupe_nodes(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen: set[tuple[str, int, int]] = set()
    for row in rows:
        key = (
            str(row["path"]),
            int(row["byte_span"]["start"]),
            int(row["byte_span"]["end"]),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _node_evidence(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_overlap": list(row.get("v66_overlap_terms") or []),
        "explicit_path": bool(row.get("v66_explicit_path")),
        "document_linked_path": bool(row.get("v66_document_linked_path")),
        "contract_mismatch_bonus": float(
            row.get("v66_contract_mismatch_bonus") or 0.0
        ),
        "generic_anomaly_score": float(
            row.get("v66_generic_anomaly_score") or 0.0
        ),
        "parameter_binding_match": bool(row.get("v66_parameter_binding_match")),
        "exact_binding_match": bool(row.get("v66_exact_binding_match")),
    }


def _editable_view(
    row: dict[str, Any],
    *,
    package: Path,
    family: str,
    rank: int,
    reasons: list[str],
) -> dict[str, Any]:
    result = node_public_view(row)
    result.update(
        {
            "file_sha256": sha256_file(package / str(row["path"])),
            "byte_span": {
                "start": int(row["byte_span"]["start"]),
                "end": int(row["byte_span"]["end"]),
            },
            "rank": rank,
            "family": family,
            "family_priority": _FAMILY_PRIORITY[family],
            "localization_score": float(row.get("localization_score") or 0.0),
            "selection_reasons": sorted(set(reasons)),
            "selection_evidence": _node_evidence(row),
        }
    )
    return result


def _context_view(row: dict[str, Any], *, relation: str) -> dict[str, Any]:
    result = node_public_view(row)
    result["relation"] = relation
    return result


def _sibling_default_obligations(
    nodes: list[dict[str, Any]], request_text: str, skill_text: str
) -> list[dict[str, Any]]:
    focus = _focus_request(request_text)
    contract_text = focus + "\n" + skill_text
    focus_tokens = _tokens(contract_text)
    obligations: list[dict[str, Any]] = []
    seen_targets: set[str] = set()
    fallback_rows = [row for row in nodes if row.get("role") == "fallback_expression"]
    for reference in fallback_rows:
        match = _FALLBACK_RE.search(str(reference.get("observed_source") or ""))
        if not match:
            continue
        base = match.group("base")
        default_source = match.group("default")
        default_value = default_source.strip("\"'")
        leaf = base.rsplit(".", 1)[-1].casefold()
        default_in_request = bool(
            re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(default_value)}(?![A-Za-z0-9_])",
                focus,
                flags=re.IGNORECASE,
            )
        )
        if not default_in_request or leaf not in focus_tokens:
            continue
        candidates = []
        for node in nodes:
            observed = str(node.get("observed_source") or "")
            if (
                node.get("path") != reference.get("path")
                or node.get("symbol") != reference.get("symbol")
                or node.get("role") != "call_expression"
                or base not in observed
                or re.search(rf"{re.escape(base)}\s*(?:\|\||\?\?)", observed)
            ):
                continue
            candidates.append(node)
        if not candidates:
            continue
        target = min(
            candidates,
            key=lambda row: (
                len(str(row.get("observed_source") or "")),
                int(row["byte_span"]["start"]),
                str(row["site_id"]),
            ),
        )
        if str(target["site_id"]) in seen_targets:
            continue
        seen_targets.add(str(target["site_id"]))
        obligations.append(
            {
                "family": "sibling_default_asymmetry",
                "confidence": 0.96,
                "repair_goal": (
                    "Review the bare use of the same option binding against the visible sibling "
                    "default and the documented compatibility default."
                ),
                "binding_expression": base,
                "documented_default": default_value,
                "editable": [target],
                "context": [reference],
                "reasons": [
                    "same_binding_has_visible_sibling_fallback",
                    "default_value_present_in_request",
                    "binding_name_present_in_request",
                ],
            }
        )
    return obligations


def _family_candidates(
    ranked: list[dict[str, Any]], request_text: str
) -> list[dict[str, Any]]:
    focus = _focus_request(request_text)
    lower = focus.casefold()
    rows = _dedupe_nodes(ranked)
    families: list[dict[str, Any]] = []

    if re.search(r"\b(newest|oldest|relevant|ascending|descending|sorted|sort)\b", lower):
        candidates = [row for row in rows if row.get("role") == "sort_comparator"][:3]
        if candidates:
            families.append(
                {
                    "family": "sort_direction_contract",
                    "confidence": 0.92,
                    "editable": candidates,
                    "context": [],
                    "reasons": ["typed_sort_role", "sorting_direction_in_request"],
                }
            )

    if re.search(r"\bexit(?:s|ed)?\b|non[- ]?zero|status", lower):
        exit_rows = [
            row
            for row in rows
            if row.get("role") == "exit_status"
            and str(row.get("observed_source") or "").strip() == "0"
        ]
        failure_rows = [
            row
            for row in exit_rows
            if re.search(
                r"invalid|fail|error|cannot|missing|does not|no session|malformed",
                str(row.get("window") or ""),
                flags=re.IGNORECASE,
            )
        ]
        candidates = (failure_rows or exit_rows)[:3]
        if candidates:
            families.append(
                {
                    "family": "failure_exit_status_contract",
                    "confidence": 0.94 if failure_rows else 0.86,
                    "editable": candidates,
                    "context": [],
                    "reasons": [
                        "typed_exit_status_role",
                        "nonzero_failure_contract_in_request",
                        *( ["failure_context_near_zero_status"] if failure_rows else [] ),
                    ],
                }
            )

    if re.search(r"\b(validate|validation)\b", lower) and re.search(
        r"\b(file|directory|folder|path)\b", lower
    ):
        candidates = [row for row in rows if row.get("role") == "path_kind_guard"][:2]
        if candidates:
            families.append(
                {
                    "family": "path_kind_contract",
                    "confidence": 0.92,
                    "editable": candidates,
                    "context": [],
                    "reasons": ["typed_path_guard_role", "validation_path_contract"],
                }
            )

    if re.search(r"\b(default|fallback|omitted)\b", lower):
        default_rows = []
        for row in rows:
            role = str(row.get("role") or "")
            evidence = _node_evidence(row)
            observed = str(row.get("observed_source") or "")
            if role not in {
                "binding_assignment",
                "binding_initializer",
                "fallback_expression",
                "shell_environment_fallback",
                "shell_positional_fallback",
            }:
                continue
            if not evidence["request_overlap"]:
                continue
            simple_positional_without_default = bool(
                re.search(r"=\s*[\"']?\$\{[0-9]+\}[\"']?\s*$", observed)
            )
            if (
                evidence["generic_anomaly_score"] >= 4.0
                or evidence["exact_binding_match"]
                or evidence["parameter_binding_match"]
                or simple_positional_without_default
            ):
                if role != "binding_assignment" or simple_positional_without_default:
                    default_rows.append(row)
        if default_rows:
            families.append(
                {
                    "family": "omitted_default_contract",
                    "confidence": 0.90,
                    "editable": default_rows[:2],
                    "context": [],
                    "reasons": ["default_contract_in_request", "binding_or_fallback_role"],
                }
            )

    if rows:
        top = rows[0]
        evidence = _node_evidence(top)
        observed = str(top.get("observed_source") or "").strip().strip("\"'")
        request_extensions = {
            value.casefold() for value in re.findall(r"\.[A-Za-z0-9]{2,6}\b", focus)
        }
        observed_extensions = {
            value.casefold()
            for value in re.findall(r"\.[A-Za-z0-9]{2,6}\b", observed)
        }
        extension_conflict = bool(
            request_extensions
            and observed_extensions
            and observed_extensions.isdisjoint(request_extensions)
        )
        literal_conflict = (
            top.get("role") == "literal"
            and bool(evidence["request_overlap"])
            and bool(evidence["explicit_path"] or evidence["document_linked_path"])
            and (evidence["contract_mismatch_bonus"] > 0 or extension_conflict)
        )
        context = " ".join(
            str((top.get("facts") or {}).get(key) or "")
            for key in ("parentSource", "grandparentSource")
        )
        domain_overlap = (
            _tokens(context) & _tokens(focus)
        ) - _CONTEXT_STOP
        literal_conflict = literal_conflict and (
            extension_conflict or len(domain_overlap) >= 2
        )
        if literal_conflict:
            families.append(
                {
                    "family": "literal_contract_conflict",
                    "confidence": 0.94 if extension_conflict else 0.90,
                    "editable": [top],
                    "context": [],
                    "reasons": [
                        "top_ranked_literal",
                        "request_linked_script_path",
                        *(
                            ["request_extension_conflicts_with_literal"]
                            if extension_conflict
                            else ["visible_contract_mismatch"]
                        ),
                    ],
                }
            )
    return families


def _obligation_score(row: dict[str, Any]) -> tuple[int, float, float]:
    editable = list(row.get("editable") or [])
    return (
        _FAMILY_PRIORITY[str(row["family"])],
        float(row.get("confidence") or 0.0),
        max((float(node.get("localization_score") or 0.0) for node in editable), default=0.0),
    )


def build_public_multilang_contract_set(
    package_root: str | Path, request_text: str
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    _assert_public_package(package)
    nodes = _enumerate_non_python_nodes(package)
    ranked = rank_script_nodes(nodes, request_text, package)
    skill_text = (package / "SKILL.md").read_text(encoding="utf-8")
    obligations = [
        *_sibling_default_obligations(nodes, request_text, skill_text),
        *_family_candidates(ranked, request_text),
    ]
    obligations.sort(key=_obligation_score, reverse=True)
    selected = obligations[0] if obligations else None
    selected_family = str(selected["family"]) if selected else None
    selected_nodes = _dedupe_nodes(list(selected.get("editable") or [])) if selected else []
    selected_nodes = selected_nodes[:MAX_EDITABLE_NODES]
    ranked_ids = {str(row["site_id"]): index + 1 for index, row in enumerate(ranked)}
    editable = [
        _editable_view(
            row,
            package=package,
            family=selected_family or "literal_contract_conflict",
            rank=ranked_ids.get(str(row["site_id"]), len(ranked) + 1),
            reasons=list(selected.get("reasons") or []) if selected else [],
        )
        for row in selected_nodes
    ]
    context = [
        _context_view(row, relation="visible_sibling_contract_reference")
        for row in _dedupe_nodes(list(selected.get("context") or []))
    ] if selected else []
    decision = "PROPOSE" if editable else "ABSTAIN"
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "source_scope": "public_parent_request_skill_markdown_and_runtime_scripts_only",
        "package_tree_hash": _tree_hash(package),
        "localization_decision": {
            "decision": decision,
            "selected_family": selected_family,
            "confidence": float(selected.get("confidence") or 0.0) if selected else 0.0,
            "editable_node_count": len(editable),
            "alternative_family_count": max(0, len(obligations) - 1),
            "abstain_reason": None if editable else "no_typed_public_contract_set",
        },
        "obligation": (
            {
                "family": selected_family,
                "confidence": float(selected.get("confidence") or 0.0),
                "repair_goal": selected.get(
                    "repair_goal",
                    "Review only the listed nodes against the visible request and package contract.",
                ),
                "reasons": list(selected.get("reasons") or []),
                "binding_expression": selected.get("binding_expression"),
                "documented_default": selected.get("documented_default"),
            }
            if selected
            else None
        ),
        "editable_nodes": editable,
        "context_nodes": context,
        "alternative_families": [
            {
                "family": row["family"],
                "confidence": float(row.get("confidence") or 0.0),
                "editable_node_count": len(_dedupe_nodes(row.get("editable") or [])),
            }
            for row in obligations[1:6]
        ],
        "counts": {
            "non_python_script_node_count": len(nodes),
            "ranked_node_count": len(ranked),
            "typed_obligation_family_count": len(obligations),
            "editable_node_count": len(editable),
            "context_node_count": len(context),
        },
        "edit_contract": {
            "maximum_edits": 1,
            "exact_node_binding_required": True,
            "outside_selected_nodes_preserved_by_construction": True,
            "semantic_correctness_inferred": False,
        },
        "benchmark_bundled_facts_consumed": False,
        "hidden_artifacts_consumed": False,
        "task_verifier_consumed": False,
        "gold_or_oracle_consumed": False,
        "reward_consumed": False,
        "semantic_correctness_inferred": False,
        "claim_boundary": (
            "The packet identifies a small public, request-conditioned structural contract set. "
            "It constrains edit location but does not determine the correct replacement semantics."
        ),
    }
    result["facts_hash"] = canonical_json_hash(result)
    return result


def validate_public_multilang_contract_set(
    facts: dict[str, Any], package_root: str | Path
) -> str:
    body = dict(facts)
    expected = str(body.pop("facts_hash", ""))
    if not expected or canonical_json_hash(body) != expected:
        raise ValueError("public_multilang_contract_set_hash_invalid")
    if facts.get("method") != METHOD_ID:
        raise ValueError("public_multilang_contract_set_method_invalid")
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
        raise ValueError("public_multilang_contract_set_scope_invalid")
    package = Path(package_root).resolve()
    _assert_public_package(package)
    if facts.get("package_tree_hash") != _tree_hash(package):
        raise ValueError("public_multilang_contract_set_package_changed")
    registry = {
        str(row["site_id"]): row
        for row in _enumerate_non_python_nodes(package)
    }
    editable = list(facts.get("editable_nodes") or [])
    if len(editable) > MAX_EDITABLE_NODES:
        raise ValueError("public_multilang_contract_set_too_many_nodes")
    for row in editable:
        node = registry.get(str(row.get("node_id")))
        if node is None:
            raise ValueError("public_multilang_contract_set_unknown_node")
        if str(row.get("source_sha256")) != str(node.get("node_source_sha256")):
            raise ValueError("public_multilang_contract_set_node_hash_mismatch")
    if (facts.get("localization_decision") or {}).get("decision") == "PROPOSE" and not editable:
        raise ValueError("public_multilang_contract_set_empty_proposal")
    return expected


def apply_public_node_bound_patch(
    content: str,
    source_package: str | Path,
    candidate_package: str | Path,
    *,
    frozen_facts: dict[str, Any],
) -> dict[str, Any]:
    validate_public_multilang_contract_set(frozen_facts, source_package)
    if (frozen_facts.get("localization_decision") or {}).get("decision") != "PROPOSE":
        raise ValueError("public_multilang_contract_set_abstained")
    result = apply_multilang_node_patch(
        content,
        source_package,
        candidate_package,
        visible_ast_facts=frozen_facts,
        require_ast_binding=True,
    )
    if (result.get("structural_gate") or {}).get("decision") not in {
        ACCEPT_STRUCTURALLY,
        "ABSTAIN",
    }:
        raise ValueError("public_node_bound_patch_structurally_rejected")
    return result


def _visible_files(package: Path) -> dict[str, Path]:
    rows: dict[str, Path] = {}
    skill = package / "SKILL.md"
    if skill.is_file():
        rows["SKILL.md"] = skill
    scripts = package / "scripts"
    if scripts.is_dir():
        for path in sorted(scripts.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                rows[path.relative_to(package).as_posix()] = path
    return rows


def _byte_offsets(lines: list[bytes]) -> list[int]:
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return offsets


def _shrink_changed_block(left: bytes, right: bytes, base: int) -> tuple[int, int]:
    prefix = 0
    limit = min(len(left), len(right))
    while prefix < limit and left[prefix] == right[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < len(left) - prefix
        and suffix < len(right) - prefix
        and left[len(left) - suffix - 1] == right[len(right) - suffix - 1]
    ):
        suffix += 1
    return base + prefix, base + len(left) - suffix


def _changed_parent_intervals(parent: bytes, candidate: bytes) -> list[tuple[int, int, str]]:
    if parent == candidate:
        return []
    left_lines = parent.splitlines(keepends=True)
    right_lines = candidate.splitlines(keepends=True)
    left_offsets = _byte_offsets(left_lines)
    right_offsets = _byte_offsets(right_lines)
    intervals = []
    matcher = difflib.SequenceMatcher(a=left_lines, b=right_lines, autojunk=True)
    for tag, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        start = left_offsets[left_start]
        end = left_offsets[left_end]
        if tag == "replace":
            start, end = _shrink_changed_block(
                parent[start:end],
                candidate[right_offsets[right_start] : right_offsets[right_end]],
                start,
            )
        intervals.append((start, end, tag))
    return intervals


def _interval_inside_span(start: int, end: int, span_start: int, span_end: int) -> bool:
    if start == end:
        return span_start <= start <= span_end
    return span_start <= start and end <= span_end


def analyze_candidate_scope(
    parent_package: str | Path,
    candidate_package: str | Path,
    *,
    frozen_facts: dict[str, Any],
    facts_already_validated: bool = False,
) -> dict[str, Any]:
    parent = Path(parent_package).resolve()
    candidate = Path(candidate_package).resolve()
    if facts_already_validated:
        body = dict(frozen_facts)
        facts_hash = str(body.pop("facts_hash", ""))
        if not facts_hash or canonical_json_hash(body) != facts_hash:
            raise ValueError("public_multilang_contract_set_hash_invalid")
        if frozen_facts.get("package_tree_hash") != _tree_hash(parent):
            raise ValueError("public_multilang_contract_set_package_changed")
    else:
        facts_hash = validate_public_multilang_contract_set(frozen_facts, parent)
    allowed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in frozen_facts.get("editable_nodes") or []:
        allowed[str(row["path"])].append(row)
    parent_files = _visible_files(parent)
    candidate_files = _visible_files(candidate)
    changed_paths = sorted(
        path
        for path in set(parent_files) | set(candidate_files)
        if (
            path not in parent_files
            or path not in candidate_files
            or sha256_file(parent_files[path]) != sha256_file(candidate_files[path])
        )
    )
    touched_ids: set[str] = set()
    outside: list[dict[str, Any]] = []
    interval_rows: list[dict[str, Any]] = []
    for relative in changed_paths:
        if relative not in parent_files or relative not in candidate_files:
            outside.append({"path": relative, "reason": "file_added_or_removed"})
            continue
        left = parent_files[relative].read_bytes()
        right = candidate_files[relative].read_bytes()
        for start, end, tag in _changed_parent_intervals(left, right):
            containing = [
                row
                for row in allowed.get(relative, [])
                if _interval_inside_span(
                    start,
                    end,
                    int(row["byte_span"]["start"]),
                    int(row["byte_span"]["end"]),
                )
            ]
            if containing:
                touched_ids.update(str(row["node_id"]) for row in containing)
            else:
                outside.append(
                    {
                        "path": relative,
                        "reason": "changed_interval_outside_editable_nodes",
                        "parent_byte_start": start,
                        "parent_byte_end": end,
                        "diff_tag": tag,
                    }
                )
            interval_rows.append(
                {
                    "path": relative,
                    "parent_byte_start": start,
                    "parent_byte_end": end,
                    "diff_tag": tag,
                    "contained_by_node_ids": sorted(
                        str(row["node_id"]) for row in containing
                    ),
                }
            )
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "frozen_facts_hash": facts_hash,
        "parent_tree_hash": _tree_hash(parent),
        "candidate_tree_hash": _tree_hash(candidate),
        "changed_paths": changed_paths,
        "changed_intervals": interval_rows,
        "touched_editable_node_ids": sorted(touched_ids),
        "outside_selected_node_changes": outside,
        "checks": {
            "candidate_changed": bool(changed_paths),
            "at_least_one_editable_node_touched": bool(touched_ids),
            "all_changes_inside_editable_nodes": not outside,
        },
        "hidden_artifacts_consumed": False,
        "task_verifier_consumed": False,
        "semantic_correctness_inferred": False,
    }
    result["scope_hash"] = canonical_json_hash(result)
    return result


def choose_public_runtime_dual_source(
    *,
    generic_scope: dict[str, Any],
    ast_candidate_exists: bool,
    ast_application_decision: str | None,
) -> tuple[str, str]:
    if not ast_candidate_exists or ast_application_decision != ACCEPT_STRUCTURALLY:
        return "generic", "node_bound_candidate_unavailable_or_rejected"
    checks = generic_scope.get("checks") or {}
    if not checks.get("candidate_changed"):
        return "node-bound-ast", "generic_noop_with_typed_public_obligation"
    if not checks.get("all_changes_inside_editable_nodes"):
        return "node-bound-ast", "generic_changed_outside_typed_contract_set"
    if not checks.get("at_least_one_editable_node_touched"):
        return "node-bound-ast", "generic_missed_typed_contract_set"
    return "generic", "generic_already_confined_to_typed_contract_set"
