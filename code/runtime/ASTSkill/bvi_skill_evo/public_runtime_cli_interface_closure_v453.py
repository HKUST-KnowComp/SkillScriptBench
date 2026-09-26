from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from bvi_skill_evo.multilang_node_patch_v4 import ACCEPT_STRUCTURALLY
from bvi_skill_evo.multilang_scalar_patch_v5 import (
    MULTILANG_NODE_PATCH_TOOL,
    apply_multilang_node_patch,
)
from skillscriptbench.io_utils import canonical_json_hash, hash_tree, sha256_file
from skillscriptbench.multilang_structural_v66 import (
    JS_SUFFIXES,
    extract_javascript_nodes,
    node_public_view,
)


SCHEMA_VERSION = "4.53-public-runtime-cli-interface-closure-v1"
METHOD_ID = "public_runtime_cli_option_binding_closure_v1"
MAX_CLOSURE_EDITS = 2
_FLAG_RE = re.compile(r"(?<![A-Za-z0-9_-])--[A-Za-z0-9][A-Za-z0-9-]*")
_BRANCH_RE = re.compile(
    r"(?:^|\b)(?:else\s+)?if\s*\((?P<condition>.+?)\)\s*"
    r"(?:\{\s*)?(?P<binding>[A-Za-z_$][A-Za-z0-9_$]*"
    r"(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+)\s*=",
)


def _tree_hash(root: Path) -> str:
    return canonical_json_hash(hash_tree(root))


def _documented_flags(request_text: str, skill_text: str) -> set[str]:
    return set(_FLAG_RE.findall(request_text + "\n" + skill_text))


def _camel_to_kebab(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value)
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", value)
    return value.replace("_", "-").casefold()


def _expected_flag(binding: str) -> str:
    return "--" + _camel_to_kebab(binding.rsplit(".", 1)[-1])


def _canonical_flag(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _enumerate_js_nodes(package: Path) -> list[dict[str, Any]]:
    scripts = package / "scripts"
    rows: list[dict[str, Any]] = []
    if not scripts.is_dir():
        return rows
    for path in sorted(scripts.rglob("*")):
        if not path.is_file() or path.suffix.casefold() not in JS_SUFFIXES:
            continue
        relative = path.relative_to(package).as_posix()
        rows.extend(extract_javascript_nodes(relative, path.read_text(encoding="utf-8")))
    return rows


def _editable_view(package: Path, node: dict[str, Any]) -> dict[str, Any]:
    result = node_public_view(node)
    result.update(
        {
            "file_sha256": sha256_file(package / str(node["path"])),
            "node_sha256": str(node["node_source_sha256"]),
            "line": int(node["line"]),
            "column": int(node["column"]),
            "end_line": int(node["end_line"]),
            "end_column": int(node["end_column"]),
            "byte_span": {
                "start": int(node["byte_span"]["start"]),
                "end": int(node["byte_span"]["end"]),
            },
        }
    )
    return result


def _branch_obligations(
    package: Path,
    request_text: str,
    skill_text: str,
    nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    documented = _documented_flags(request_text, skill_text)
    by_location: dict[tuple[str, str, int, str], list[dict[str, Any]]] = {}
    for node in nodes:
        value = str((node.get("facts") or {}).get("value") or "")
        if (
            node.get("node_type") == "StringLiteral"
            and node.get("role") == "literal"
            and value.startswith("--")
        ):
            by_location.setdefault(
                (
                    str(node["path"]),
                    str(node.get("symbol") or ""),
                    int(node["line"]),
                    value,
                ),
                [],
            ).append(node)

    obligations: list[dict[str, Any]] = []
    for path in sorted({str(node["path"]) for node in nodes}):
        source = (package / path).read_text(encoding="utf-8")
        for line_number, line in enumerate(source.splitlines(), start=1):
            match = _BRANCH_RE.search(line)
            if not match:
                continue
            binding = match.group("binding")
            expected = _expected_flag(binding)
            if expected not in documented:
                continue
            observed = _FLAG_RE.findall(match.group("condition"))
            if expected in observed or not observed:
                continue
            candidates = []
            target_flags = [
                flag
                for flag in observed
                if _canonical_flag(flag) != _canonical_flag(expected)
            ]
            for flag in target_flags:
                candidates.extend(
                    by_location.get((path, "parseArgs", line_number, flag), [])
                )
            if len(candidates) != 1:
                continue
            node = candidates[0]
            obligations.append(
                {
                    "family": "cli_option_to_binding_mismatch",
                    "confidence": 0.99,
                    "path": path,
                    "function_symbol": "parseArgs",
                    "binding": binding,
                    "observed_long_option": str(
                        (node.get("facts") or {}).get("value") or ""
                    ),
                    "documented_binding_option": expected,
                    "repair_goal": (
                        "Make this parseArgs branch recognize the documented long option "
                        "that corresponds to its assigned result field; preserve aliases and "
                        "all other branches."
                    ),
                    "semantic_correctness_inferred": False,
                    "editable_node": _editable_view(package, node),
                }
            )
    obligations.sort(
        key=lambda row: (
            str(row["path"]),
            int(row["editable_node"]["byte_span"]["start"]),
        )
    )
    return obligations


def build_public_cli_interface_closure(
    package_root: str | Path,
    request_text: str,
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    skill_path = package / "SKILL.md"
    skill_text = skill_path.read_text(encoding="utf-8") if skill_path.is_file() else ""
    try:
        nodes = _enumerate_js_nodes(package)
        obligations = _branch_obligations(
            package, request_text, skill_text, nodes
        )
        parse_error = None
    except Exception as exc:
        nodes = []
        obligations = []
        parse_error = f"{type(exc).__name__}:{exc}"
    selected = obligations if 1 <= len(obligations) <= MAX_CLOSURE_EDITS else []
    if parse_error:
        decision = "ABSTAIN"
        abstain_reason = "javascript_parse_unavailable"
    elif len(obligations) > MAX_CLOSURE_EDITS:
        decision = "ABSTAIN"
        abstain_reason = "cli_interface_closure_exceeds_two_nodes"
    elif not obligations:
        decision = "ABSTAIN"
        abstain_reason = "no_public_cli_option_binding_mismatch"
    else:
        decision = "PROPOSE"
        abstain_reason = None
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "source_scope": "public_request_skill_markdown_and_javascript_scripts_only",
        "package_tree_hash": _tree_hash(package),
        "localization_decision": {
            "decision": decision,
            "selected_family": "cli_option_to_binding_mismatch"
            if decision == "PROPOSE"
            else None,
            "confidence": min((row["confidence"] for row in selected), default=0.0),
            "selected_obligation_count": len(selected),
            "editable_node_count": len(selected),
            "abstain_reason": abstain_reason,
        },
        "obligations": [
            {key: value for key, value in row.items() if key != "editable_node"}
            for row in selected
        ],
        "editable_nodes": [row["editable_node"] for row in selected],
        "alternative_obligation_count": max(0, len(obligations) - len(selected)),
        "counts": {
            "javascript_node_count": len(nodes),
            "cli_option_binding_mismatch_count": len(obligations),
            "editable_node_count": len(selected),
        },
        "edit_contract": {
            "exact_node_binding_required": True,
            "maximum_edits": MAX_CLOSURE_EDITS,
            "all_listed_obligations_must_be_resolved": True,
            "outside_selected_nodes_preserved_by_construction": True,
            "semantic_correctness_inferred": False,
        },
        "parse_error": parse_error,
        "hidden_artifacts_consumed": False,
        "task_verifier_consumed": False,
        "gold_or_oracle_consumed": False,
        "reward_consumed": False,
        "benchmark_bundled_facts_consumed": False,
        "claim_boundary": (
            "The packet links documented CLI options to parseArgs binding branches. It "
            "does not use or certify hidden behavioral correctness."
        ),
    }
    result["facts_hash"] = canonical_json_hash(result)
    validate_public_cli_interface_closure(result, package)
    return result


def validate_public_cli_interface_closure(
    facts: dict[str, Any], package_root: str | Path
) -> str:
    package = Path(package_root).resolve()
    expected = str(facts.get("facts_hash") or "")
    body = copy.deepcopy(facts)
    body.pop("facts_hash", None)
    if not expected or canonical_json_hash(body) != expected:
        raise ValueError("cli_interface_closure_facts_hash_invalid")
    if facts.get("package_tree_hash") != _tree_hash(package):
        raise ValueError("cli_interface_closure_package_changed")
    registry = {str(row["site_id"]): row for row in _enumerate_js_nodes(package)}
    for editable in facts.get("editable_nodes") or []:
        node = registry.get(str(editable.get("node_id")))
        if (
            node is None
            or node.get("node_type") != "StringLiteral"
            or node.get("role") != "literal"
            or str(node.get("node_source_sha256")) != str(editable.get("node_sha256"))
            or sha256_file(package / str(editable["path"]))
            != editable.get("file_sha256")
        ):
            raise ValueError("cli_interface_closure_node_binding_invalid")
    decision = str((facts.get("localization_decision") or {}).get("decision"))
    count = len(facts.get("editable_nodes") or [])
    if decision == "PROPOSE" and not (
        1 <= count == len(facts.get("obligations") or []) <= MAX_CLOSURE_EDITS
    ):
        raise ValueError("cli_interface_closure_proposal_shape_invalid")
    if decision == "ABSTAIN" and count:
        raise ValueError("cli_interface_closure_abstain_has_nodes")
    return expected


def build_same_package_wrong_cli_closure(
    package_root: str | Path,
    real_facts: dict[str, Any],
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    validate_public_cli_interface_closure(real_facts, package)
    result = copy.deepcopy(real_facts)
    result.pop("facts_hash", None)
    real_nodes = list(real_facts.get("editable_nodes") or [])
    registry = _enumerate_js_nodes(package)
    forbidden = {str(row["node_id"]) for row in real_nodes}
    selected: list[dict[str, Any]] = []
    for real in real_nodes:
        candidates = [
            row
            for row in registry
            if str(row["site_id"]) not in forbidden
            and str(row["site_id"]) not in {str(value["node_id"]) for value in selected}
            and row.get("node_type") == "StringLiteral"
            and row.get("role") == "literal"
            and str((row.get("facts") or {}).get("value") or "").startswith("--")
            and row.get("path") == real.get("path")
            and row.get("symbol") == real.get("symbol")
        ]
        candidates.sort(
            key=lambda row: (
                abs(int(row["line"]) - int(real["line"])),
                int(row["byte_span"]["start"]),
                str(row["site_id"]),
            )
        )
        if not candidates:
            raise ValueError("cli_interface_closure_sham_decoy_unavailable")
        selected.append(_editable_view(package, candidates[0]))
    result["editable_nodes"] = selected
    result["control_metadata"] = {
        "control": "same-package-wrong-cli-interface-closure",
        "construction": "deterministic_same_symbol_long_option_derangement",
        "real_node_overlap_count": len(
            forbidden & {str(row["node_id"]) for row in selected}
        ),
        "same_editable_node_count": len(selected) == len(real_nodes),
        "hidden_or_verifier_feedback_used": False,
    }
    result["facts_hash"] = canonical_json_hash(result)
    validate_public_cli_interface_closure(result, package)
    return result


def analyze_candidate_cli_closure(
    parent_package: str | Path,
    candidate_package: str | Path,
    request_text: str,
    *,
    frozen_facts: dict[str, Any],
) -> dict[str, Any]:
    parent = Path(parent_package).resolve()
    candidate = Path(candidate_package).resolve()
    validate_public_cli_interface_closure(frozen_facts, parent)
    try:
        residual_facts = build_public_cli_interface_closure(candidate, request_text)
        residual = list(residual_facts.get("obligations") or [])
        parse_error = residual_facts.get("parse_error")
    except Exception as exc:
        residual = []
        parse_error = f"{type(exc).__name__}:{exc}"
    parent_hashes = hash_tree(parent)
    candidate_hashes = hash_tree(candidate) if candidate.is_dir() else {}
    changed_paths = sorted(
        path
        for path in set(parent_hashes) | set(candidate_hashes)
        if parent_hashes.get(path) != candidate_hashes.get(path)
    )
    target_bindings = {
        (str(row["path"]), str(row["binding"]))
        for row in frozen_facts.get("obligations") or []
    }
    targeted_residual = [
        row
        for row in residual
        if (str(row["path"]), str(row["binding"])) in target_bindings
    ]
    checks = {
        "candidate_exists": candidate.is_dir(),
        "tree_scope_preserved": set(parent_hashes) == set(candidate_hashes),
        "javascript_parse": parse_error is None,
        "changed_paths_within_one_javascript_script": len(changed_paths) <= 1
        and all(Path(path).suffix.casefold() in JS_SUFFIXES for path in changed_paths),
        "all_cli_interface_obligations_resolved": not targeted_residual,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "decision": ACCEPT_STRUCTURALLY if all(checks.values()) else "REVISE",
        "checks": checks,
        "changed_paths": changed_paths,
        "parse_error": parse_error,
        "residual_obligations": targeted_residual,
        "parent_tree_hash": canonical_json_hash(parent_hashes),
        "candidate_tree_hash": canonical_json_hash(candidate_hashes),
        "hidden_semantic_correctness_checked": False,
    }


def apply_public_cli_closure_patch(
    content: str,
    source_package: str | Path,
    candidate_package: str | Path,
    *,
    frozen_facts: dict[str, Any],
    request_text: str,
) -> dict[str, Any]:
    validate_public_cli_interface_closure(frozen_facts, source_package)
    result = apply_multilang_node_patch(
        content,
        source_package,
        candidate_package,
        visible_ast_facts=frozen_facts,
        require_ast_binding=True,
    )
    gate = analyze_candidate_cli_closure(
        source_package,
        candidate_package,
        request_text,
        frozen_facts=frozen_facts,
    )
    result["cli_interface_closure_gate"] = gate
    result["structural_gate"]["decision"] = gate["decision"]
    result["structural_gate"]["checks"] = gate["checks"]
    return result


__all__ = [
    "MULTILANG_NODE_PATCH_TOOL",
    "analyze_candidate_cli_closure",
    "apply_public_cli_closure_patch",
    "build_public_cli_interface_closure",
    "build_same_package_wrong_cli_closure",
    "validate_public_cli_interface_closure",
]
