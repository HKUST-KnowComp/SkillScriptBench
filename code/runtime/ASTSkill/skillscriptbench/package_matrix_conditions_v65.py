from __future__ import annotations

import argparse
import ast
import base64
import getpass
import http.client
import json
import math
import os
import platform
import random
import re
import shutil
import subprocess
import ssl
import tempfile
import time
import uuid
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from skillscriptbench.adapters.openlux_v04_smoke import (
    _call_chat_completion,
    _model_content,
    _normalized_usage,
)
from skillscriptbench.adapters.openlux_v16_shell_operator_smoke import (
    _shell_ast,
    _tree_sitter_runtime,
)
from skillscriptbench.io_utils import (
    canonical_json_hash,
    copy_tree_clean,
    hash_tree,
    read_json,
    sha256_bytes,
    sha256_file,
    write_json,
)
from skillscriptbench.js_ts_discrimination import _node_check
from skillscriptbench.mutation_operators_v13 import (
    enumerate_shell_behavior_operators_v13,
)
from skillscriptbench.shell_discrimination import enumerate_shell_default_operators
from skillscriptbench.protocol_normalization_v89 import (
    classify_candidate_failure,
    normalize_patch_arguments,
)
from skillscriptbench.source_capsule_v89 import (
    create_source_capsule,
    source_capsule_plan_receipt,
    validate_source_capsule,
)
from skillscriptbench.structural_evolution_v65 import (
    apply_structured_edits,
    build_evolution_spec,
    exact_target_match,
    extract_markdown_sections,
    extract_python_role_sites,
    generic_structural_gate,
    localization_audit_row,
    opportunity_decision,
    rank_role_sites,
    select_gold_site,
    select_matched_sham,
)


SCHEMA_VERSION = "0.65-role-aware-ast-spec-node-editor-v1"
CONDITIONS = ("raw-package", "ast-spec", "matched-sham", "gold-node")
SCRIPT_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".sh", ".bash"}
JS_SUFFIXES = {".js", ".mjs", ".cjs", ".ts"}
SHELL_SUFFIXES = {".sh", ".bash"}
MAX_CONTEXT_SITES = 5
MAX_EDITS = 8
MAX_SINGLE_EDIT_BYTES = 16_000
MAX_TOTAL_EDIT_BYTES = 32_000
GENERIC_REQUEST_PREFIX = "Audit the complete skill package"
JS_SITE_HELPER = (
    Path(__file__).resolve().parent / "js_parser" / "extract_mutation_sites_v08.mjs"
)
CODE_PATHS = (
    "skillscriptbench/package_matrix_conditions_v65.py",
    "skillscriptbench/package_matrix_evaluate_conditions_v65.py",
    "skillscriptbench/structural_evolution_v65.py",
    "skillscriptbench/adapters/openlux_v04_smoke.py",
    "skillscriptbench/adapters/openlux_v16_shell_operator_smoke.py",
    "skillscriptbench/io_utils.py",
    "skillscriptbench/js_ts_discrimination.py",
    "skillscriptbench/js_parser/extract_mutation_sites_v08.mjs",
    "skillscriptbench/js_parser/package-lock.json",
    "skillscriptbench/mutation_operators_v13.py",
    "skillscriptbench/shell_discrimination.py",
    "skillscriptbench/protocol_normalization_v89.py",
    "skillscriptbench/source_capsule_v89.py",
)
STOPWORDS = {
    "a",
    "all",
    "and",
    "any",
    "are",
    "as",
    "be",
    "by",
    "complete",
    "do",
    "existing",
    "for",
    "from",
    "if",
    "in",
    "is",
    "it",
    "keep",
    "make",
    "no",
    "not",
    "of",
    "on",
    "only",
    "or",
    "package",
    "preserve",
    "skill",
    "that",
    "the",
    "this",
    "to",
    "unchanged",
    "use",
    "user",
    "with",
}
PATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_skill_patch",
        "description": "Submit the complete bounded edit decision for the visible Agent Skill package.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["edits", "summary"],
            "properties": {
                "edits": {
                    "type": "array",
                    "maxItems": MAX_EDITS,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "path",
                            "expected_file_sha256",
                            "operation",
                            "target_node_id",
                            "expected_node_sha256",
                            "symbol",
                            "start_line",
                            "end_line",
                            "replacement",
                        ],
                        "properties": {
                            "path": {
                                "type": "string",
                                "pattern": "^(SKILL\\.md|scripts/.+)$",
                            },
                            "expected_file_sha256": {
                                "type": "string",
                                "pattern": "^[0-9a-f]{64}$",
                            },
                            "operation": {
                                "type": "string",
                                "enum": [
                                    "replace_node",
                                    "insert_parameter",
                                    "replace_lines",
                                    "append_markdown",
                                ],
                            },
                            "target_node_id": {"type": "string"},
                            "expected_node_sha256": {
                                "type": "string",
                                "pattern": "^$|^[0-9a-f]{64}$",
                            },
                            "symbol": {"type": "string"},
                            "start_line": {"type": "integer", "minimum": 0},
                            "end_line": {"type": "integer", "minimum": 0},
                            "replacement": {"type": "string"},
                        },
                    },
                },
                "summary": {"type": "string"},
            },
        },
    },
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _camel_tokens(text: str) -> set[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {
        token
        for token in re.findall(r"[A-Za-z0-9]+", expanded.lower())
        if len(token) >= 2 and token not in STOPWORDS
    }


def _focus_request(request: str) -> str:
    marker = "## Required Use Case"
    if marker in request:
        return request.split(marker, 1)[1].strip()
    return request.strip()


def _quoted_anchors(text: str) -> list[str]:
    values = re.findall(r"`([^`\n]{2,160})`", text)
    values.extend(re.findall(r"\"([^\"\n]{3,120})\"", text))
    return sorted(set(values), key=lambda value: (-len(value), value))


def _line_number(source: str, offset: int) -> int:
    return source.count("\n", 0, max(0, offset)) + 1


def _line_window(source: str, line: int, radius: int = 6) -> str:
    lines = source.splitlines()
    if not lines:
        return ""
    start = max(0, line - radius - 1)
    end = min(len(lines), line + radius)
    return "\n".join(f"{index + 1}: {lines[index]}" for index in range(start, end))


def _symbol_for_line(source: str, line: int, language: str) -> str:
    lines = source.splitlines()
    if language == "python":
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return "<module>"
        matches = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.lineno <= line <= int(getattr(node, "end_lineno", node.lineno))
        ]
        if matches:
            return min(matches, key=lambda node: int(getattr(node, "end_lineno", 0)) - node.lineno).name
        return "<module>"
    if language in {"javascript", "typescript"}:
        return "<module>"
    current = "<script>"
    for index, value in enumerate(lines[:line], start=1):
        match = re.match(r"\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\)\s*\{", value)
        if match:
            current = match.group(1)
    return current


def _site(
    *,
    path: str,
    language: str,
    node_type: str,
    role: str,
    start: int,
    end: int,
    line: int,
    symbol: str,
    source: str,
    observed: str,
    backend: str,
    facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    end_line = _line_number(source, end)
    payload = {
        "path": path,
        "language": language,
        "backend": backend,
        "node_type": node_type,
        "role": role,
        "start": start,
        "end": end,
        "line": line,
        "end_line": end_line,
        "symbol": symbol,
        "observed_source": observed[:600],
        "window": _line_window(source, line),
        "facts": facts or {},
    }
    payload["site_id"] = f"site-{canonical_json_hash(payload)[:16]}"
    return payload


def _python_sites(path: str, source: str) -> list[dict[str, Any]]:
    return extract_python_role_sites(path, source)


def _js_sites(path: str, source: str) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [shutil.which("node") or "node", str(JS_SITE_HELPER)],
        input=json.dumps({"source": source, "filename": path}),
        text=True,
        capture_output=True,
        cwd=JS_SITE_HELPER.parent,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(f"babel_site_extraction_failed:{path}:{completed.stderr[-800:]}")
    payload = json.loads(completed.stdout)
    language = "typescript" if Path(path).suffix.lower() == ".ts" else "javascript"
    rows: list[dict[str, Any]] = []
    for opportunity in payload.get("opportunities", []):
        start = int(opportunity["start"])
        end = int(opportunity["end"])
        function = opportunity.get("enclosingFunction") or {}
        rows.append(
            _site(
                path=path,
                language=language,
                node_type=str(opportunity.get("nodeType") or opportunity.get("kind") or "Node"),
                role=str(opportunity.get("kind") or opportunity.get("family") or "behavior_node"),
                start=start,
                end=end,
                line=_line_number(source, start),
                symbol=str(function.get("name") or "<module>"),
                source=source,
                observed=source[start:end],
                backend="babel_ast",
                facts={
                    key: value
                    for key, value in opportunity.items()
                    if key
                    in {
                        "dimension",
                        "family",
                        "kind",
                        "logicalOperator",
                        "numericSide",
                        "numericValue",
                        "operator",
                        "originalLimit",
                        "originalValue",
                    }
                    and value is not None
                },
            )
        )
    deduped = {
        (row["path"], row["start"], row["end"], row["node_type"]): row for row in rows
    }
    return list(deduped.values())


def _shell_sites(path: str, source: str) -> list[dict[str, Any]]:
    source_hash = sha256_bytes(source.encode("utf-8"))
    operators = [
        *enumerate_shell_behavior_operators_v13(
            source,
            source_hash=source_hash,
            path=path,
            package_id="visible-package",
        ),
        *enumerate_shell_default_operators(
            source,
            source_hash=source_hash,
            path=path,
            package_id="visible-package",
        ),
    ]
    _tree, ast_records = _shell_ast(source)
    encoded = source.encode("utf-8")

    def containing_type(start: int, end: int) -> str:
        byte_start = len(source[:start].encode("utf-8"))
        byte_end = len(source[:end].encode("utf-8"))
        matches = [
            row
            for row in ast_records
            if int(row["start_byte"]) <= byte_start and int(row["end_byte"]) >= byte_end
        ]
        if not matches:
            return "shell_token"
        return min(matches, key=lambda row: int(row["end_byte"]) - int(row["start_byte"]))["type"]

    rows: list[dict[str, Any]] = []
    for operator in operators:
        start = int(operator["start"])
        end = int(operator["end"])
        role = (operator.get("structural_roles") or [operator.get("operator_subfamily")])[0]
        rows.append(
            _site(
                path=path,
                language="shell",
                node_type=containing_type(start, end),
                role=str(role),
                start=start,
                end=end,
                line=int(operator["line"]),
                symbol=_symbol_for_line(source, int(operator["line"]), "shell"),
                source=source,
                observed=source[start:end],
                backend="tree_sitter_bash",
            )
        )

    meaningful = {
        "command",
        "redirected_statement",
        "test_command",
        "binary_expression",
        "variable_assignment",
        "expansion",
        "if_statement",
        "function_definition",
    }
    for record in ast_records:
        if record["type"] not in meaningful:
            continue
        start_byte = int(record["start_byte"])
        end_byte = int(record["end_byte"])
        observed = encoded[start_byte:end_byte].decode("utf-8")
        if not observed.strip() or len(observed) > 800:
            continue
        start = len(encoded[:start_byte].decode("utf-8"))
        end = len(encoded[:end_byte].decode("utf-8"))
        line = _line_number(source, start)
        rows.append(
            _site(
                path=path,
                language="shell",
                node_type=str(record["type"]),
                role="syntax_node",
                start=start,
                end=end,
                line=line,
                symbol=_symbol_for_line(source, line, "shell"),
                source=source,
                observed=observed,
                backend="tree_sitter_bash",
            )
        )
    deduped = {
        (row["path"], row["start"], row["end"], row["node_type"]): row for row in rows
    }
    return list(deduped.values())


def enumerate_structural_sites(package: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for file in sorted((package / "scripts").rglob("*.py")) if (package / "scripts").is_dir() else []:
        relative = file.relative_to(package).as_posix()
        source = file.read_text(encoding="utf-8")
        try:
            rows.extend(_python_sites(relative, source))
        except SyntaxError:
            continue
    if not rows:
        raise ValueError(f"no_structural_sites:{package}")
    return rows


def _anomaly_score(site: dict[str, Any], focus: str) -> float:
    window = site["window"].lower()
    observed = site["observed_source"].strip().lower()
    role = site["role"].lower()
    node_type = site["node_type"].lower()
    focus_lower = focus.lower()
    score = 0.0
    if "exit_status" in role and observed == "0" and re.search(r"error|fail|invalid|required", window):
        score += 5.0
    if "output_stream" in role and observed in {"1>&1", ">&1"} and re.search(r"error|fail|invalid", window):
        score += 5.0
    if "path_kind" in role:
        if observed == "-f" and re.search(r"\bdir(?:ectory)?\b|_dir\b", window):
            score += 4.0
        if observed == "-d" and re.search(r"\bfile\b|_file\b|\.[a-z0-9]{1,6}\b", window):
            score += 4.0
    if "sort" in role and re.search(r"newest|oldest|relevant|ascending|descending|sorted", focus.lower()):
        score += 4.0
    if "fallback" in role and re.search(r"default|fallback|silent", focus.lower()):
        score += 4.0
    if "exit_status" in role and re.search(r"exit|status|non-zero|failure", focus_lower):
        score += 4.0
    if "output_stream" in role and re.search(r"stdout|stderr|json only|piping|parsing", focus_lower):
        score += 4.0
    if "path_kind" in role and re.search(r"file|directory|folder|path", focus_lower):
        score += 4.0
    if "extension_filter" in role and re.search(r"\.[a-z0-9]{2,6}\b", focus_lower):
        score += 4.0
    if "comparison_boundary" in role and re.search(r"\b[45][0-9]{2}\b|boundary|threshold", focus_lower):
        score += 4.0
    if "literal_branch" in role and re.search(r"--[a-z0-9-]+|\bget\b|\brecord\b", focus_lower):
        score += 3.0
    if node_type == "variable_assignment" and re.search(r"default|fallback|silent", focus_lower):
        if re.search(r"\$\{[^}]+(?::-|-)?.*\}", window):
            score += 5.0
    if observed and len(observed) >= 3 and window.count(observed) > 1:
        score += 1.5
    return score


def rank_structural_sites(
    sites: list[dict[str, Any]], request: str, *, package: Path | None = None
) -> list[dict[str, Any]]:
    focus = _focus_request(request)
    focus_terms = _camel_tokens(focus)
    anchors = _quoted_anchors(focus)
    focus_lines = [line.strip() for line in focus.splitlines() if line.strip()]
    explicit_paths = {
        match.rstrip(".,;:)")
        for match in re.findall(
            r"(?:[A-Za-z0-9_.~-]+/)*scripts/[A-Za-z0-9_./~-]+\.(?:py|js|mjs|cjs|ts|sh|bash)",
            focus,
            flags=re.IGNORECASE,
        )
    }
    observed_counts = Counter(
        (site["path"], site["role"], site["observed_source"].strip())
        for site in sites
        if site["observed_source"].strip()
    )
    file_cache: dict[str, str] = {}
    file_token_cache: dict[str, set[str]] = {}
    if package is not None:
        for path in {site["path"] for site in sites}:
            file_cache[path] = (package / path).read_text(encoding="utf-8")
            file_token_cache[path] = _camel_tokens(file_cache[path])
    document_frequency = Counter(
        token
        for terms in file_token_cache.values()
        for token in focus_terms & terms
    )
    file_count = max(1, len(file_token_cache))
    ranked: list[dict[str, Any]] = []
    for site in sites:
        searchable = " ".join(
            [
                site["path"],
                site["symbol"],
                site["role"],
                site["node_type"],
                site["observed_source"],
                site["window"],
                json.dumps(site.get("facts") or {}, sort_keys=True),
            ]
        )
        site_terms = _camel_tokens(searchable)
        overlap = focus_terms & site_terms
        exact_anchor_hits = sum(anchor in searchable for anchor in anchors)
        symbol_anchor_hits = sum(
            anchor == site["symbol"] or Path(anchor).stem == site["symbol"]
            for anchor in anchors
        )
        path_explicit = any(
            site["path"] == path
            or site["path"].endswith(path)
            or path.endswith(site["path"])
            for path in explicit_paths
        )
        file_source = file_cache.get(site["path"], "")
        file_terms = file_token_cache.get(site["path"], set())
        file_overlap = focus_terms & file_terms
        file_idf_score = sum(
            1.0 + math.log((file_count + 1) / (document_frequency[token] + 1))
            for token in file_overlap
        )
        file_anchor_hits = sum(anchor in file_source for anchor in anchors) if file_source else 0
        duplicate_observation = observed_counts[
            (site["path"], site["role"], site["observed_source"].strip())
        ]
        line_similarity = max(
            (
                SequenceMatcher(None, line.lower(), site["window"].lower()).ratio()
                for line in focus_lines
            ),
            default=0.0,
        )
        score = (
            len(overlap) * 1.5
            + exact_anchor_hits * 5.0
            + symbol_anchor_hits * 30.0
            + int(path_explicit) * 30.0
            + file_idf_score * 0.75
            + file_anchor_hits * 4.0
            + int(duplicate_observation > 1) * 4.0
            + line_similarity * 3.0
            + _anomaly_score(site, focus)
        )
        item = dict(site)
        item["localization_score"] = round(score, 6)
        item["matched_terms"] = sorted(overlap)
        item["exact_anchor_hit_count"] = exact_anchor_hits
        item["symbol_anchor_hit_count"] = symbol_anchor_hits
        item["explicit_path_match"] = path_explicit
        item["file_anchor_hit_count"] = file_anchor_hits
        item["file_idf_score"] = round(file_idf_score, 6)
        item["duplicate_observation_count"] = duplicate_observation
        ranked.append(item)
    return sorted(
        ranked,
        key=lambda row: (
            -float(row["localization_score"]),
            row["path"],
            int(row["line"]),
            row["site_id"],
        ),
    )


def _diverse_top_sites(ranked: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for site in ranked:
        identity = (site["path"], site["symbol"], int(site["line"]) // 3)
        if identity in seen:
            continue
        selected.append(site)
        seen.add(identity)
        if len(selected) >= count:
            break
    if len(selected) < count:
        for site in ranked:
            if site in selected:
                continue
            selected.append(site)
            if len(selected) >= count:
                break
    return selected


def _impact_sites(package: Path, sites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    symbols = {site["symbol"] for site in sites if site["symbol"] not in {"<module>", "<script>"}}
    impacts: list[dict[str, Any]] = []
    for file in sorted(path for path in package.rglob("*") if path.is_file()):
        relative = file.relative_to(package).as_posix()
        if not relative.startswith("scripts/") or file.suffix.lower() not in SCRIPT_SUFFIXES:
            continue
        source = file.read_text(encoding="utf-8")
        for symbol in sorted(symbols):
            for match in re.finditer(rf"\b{re.escape(symbol)}\s*\(", source):
                line = _line_number(source, match.start())
                if any(relative == site["path"] and line == site["line"] for site in sites):
                    continue
                impacts.append({"path": relative, "line": line, "symbol": symbol})
                if len(impacts) >= 12:
                    return impacts
    return impacts


def _structural_packet(package: Path, sites: list[dict[str, Any]]) -> dict[str, Any]:
    nodes = []
    for site in sites:
        path = package / site["path"]
        source = path.read_text(encoding="utf-8")
        selected_lines = source.splitlines()[
            int(site["line"]) - 1 : int(site["end_line"])
        ]
        nodes.append(
            {
                "id": site["site_id"],
                "type": site["node_type"],
                "role": site["role"],
                "path": site["path"],
                "symbol": site["symbol"],
                "span": {
                    "start_line": site["line"],
                    "end_line": site["end_line"],
                },
                "file_sha256": sha256_file(path),
                "selected_lines_sha256": canonical_json_hash(selected_lines),
                "observed_source": site["observed_source"],
                "context_source": site["window"],
                "facts": site.get("facts") or {},
                "parser_backend": site["backend"],
            }
        )
    edges: list[dict[str, str]] = []
    for left, right in zip(nodes, nodes[1:]):
        relation = "same_symbol" if left["symbol"] == right["symbol"] else "co_localized"
        edges.append({"source": left["id"], "relation": relation, "target": right["id"]})
    return {
        "schema_version": "0.64-visible-structural-facts-v1",
        "claim": "observed_package_structure_only",
        "nodes": nodes,
        "edges": edges,
        "impact_sites": _impact_sites(package, sites),
    }


def _text_location(sites: list[dict[str, Any]]) -> str:
    lines = ["Automated location candidates from the visible package:"]
    for index, site in enumerate(sites, start=1):
        lines.append(
            f"{index}. {site['path']}:{site['line']} in {site['symbol']} "
            f"({site['role']})."
        )
    lines.append("These are location hints only; independently verify whether any edit is needed.")
    return "\n".join(lines)


def _hidden_target(label: dict[str, Any]) -> dict[str, Any]:
    if label["track"] == "capability_evolution":
        target = label["target"]
        return {
            "path": target["source_path"],
            "line": int(target["line"]),
            "symbol": target["function_name"],
        }
    return {
        "path": label["target_path"],
        "line": int(label["operator"]["line"]),
        "symbol": None,
    }


def _matches_hidden_target(site: dict[str, Any], target: dict[str, Any]) -> bool:
    if site["path"] != target["path"]:
        return False
    if target.get("symbol") and site["symbol"] == target["symbol"]:
        return True
    return abs(int(site["line"]) - int(target["line"])) <= 3


def _select_sham_sites(
    ranked: list[dict[str, Any]],
    real_sites: list[dict[str, Any]],
    label: dict[str, Any],
    *,
    count: int,
) -> list[dict[str, Any]]:
    target = _hidden_target(label)
    eligible = [
        site
        for site in ranked
        if not _matches_hidden_target(site, target)
    ]
    eligible.sort(
        key=lambda site: (
            site["path"] == target["path"],
            abs(int(site["line"]) - int(target["line"]))
            if site["path"] == target["path"]
            else -1,
            site["site_id"],
        )
    )
    selected = _diverse_top_sites(eligible, count)
    if len(selected) != count:
        raise ValueError(f"cannot_construct_deranged_sham:{label['case_id']}")
    return selected


def build_case_contexts(
    public_case: Path,
    label: dict[str, Any],
) -> dict[str, Any]:
    package = public_case / "package"
    request = (public_case / "REQUEST.md").read_text(encoding="utf-8")
    sites = enumerate_structural_sites(package)
    ranked, contract = rank_role_sites(sites, request)
    decision = opportunity_decision(ranked)
    gold_site = select_gold_site(sites, label)
    if gold_site is None:
        raise ValueError(f"exact_gold_site_missing:{label['case_id']}")
    matched_sham_site = select_matched_sham(ranked, label)
    if matched_sham_site is None:
        raise ValueError(f"matched_sham_missing:{label['case_id']}")
    predicted_spec = build_evolution_spec(package, ranked, contract)
    matched_sham_spec = build_evolution_spec(
        package,
        ranked,
        contract,
        selected=matched_sham_site,
        force_decision="PROPOSE",
    )
    gold_spec = build_evolution_spec(
        package,
        ranked,
        contract,
        selected=gold_site,
        force_decision="PROPOSE",
    )
    node_registry = {
        site["site_id"]: site
        for site in sites
        if site.get("backend") == "python_ast_v65"
    }
    markdown_sections = extract_markdown_sections(
        (package / "SKILL.md").read_text(encoding="utf-8")
    )
    return {
        "case_id": label["case_id"],
        "predicted_spec": predicted_spec,
        "matched_sham_spec": matched_sham_spec,
        "gold_spec": gold_spec,
        "node_registry": node_registry,
        "markdown_sections": markdown_sections,
        "ranked_sites": ranked,
        "decision": decision,
        "candidate_site_count": len(ranked),
        "localization_audit": localization_audit_row(
            label["case_id"], ranked, label, decision
        ),
    }


def _package_payload(package: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in sorted(value for value in package.rglob("*") if value.is_file()):
        relative = path.relative_to(package).as_posix()
        data = path.read_bytes()
        try:
            content = data.decode("utf-8")
            encoding = "utf-8"
            editable = relative == "SKILL.md" or relative.startswith("scripts/")
            numbered_content = "\n".join(
                f"{index}: {line}"
                for index, line in enumerate(content.splitlines(), start=1)
            )
        except UnicodeDecodeError:
            numbered_content = base64.b64encode(data).decode("ascii")
            encoding = "base64"
            editable = False
        files.append(
            {
                "path": relative,
                "encoding": encoding,
                "editable": editable,
                "sha256": sha256_bytes(data),
                "numbered_content": numbered_content,
            }
        )
    return files


def build_prompt(public_case: Path, condition: str, contexts: dict[str, Any]) -> str:
    if condition not in CONDITIONS:
        raise ValueError(condition)
    request = (public_case / "REQUEST.md").read_text(encoding="utf-8")
    package = public_case / "package"
    structural = ""
    if condition in {"ast-spec", "matched-sham", "gold-node"}:
        packet = {
            "ast-spec": contexts["predicted_spec"],
            "matched-sham": contexts["matched_sham_spec"],
            "gold-node": contexts["gold_spec"],
        }[condition]
        structural = (
            "\n\nSTRUCTURAL_EVOLUTION_SPEC.json\n"
            + json.dumps(packet, indent=2, sort_keys=True, ensure_ascii=True)
        )
    edit_policy = (
        "Because a structural spec is supplied, every Python edit must use only replace_node or "
        "insert_parameter and must bind to the single target.node_id in that spec. Do not use "
        "replace_lines for scripts, do not edit anti-target nodes, and use append_markdown for "
        "SKILL.md. If the spec decision is ABSTAIN, return zero edits. "
        if condition != "raw-package"
        else "No structural node registry is supplied. Use replace_lines for script changes and "
        "append_markdown for SKILL.md; leave node hash fields empty. "
    )
    instruction = (
        "Inspect the complete visible Agent Skill package and satisfy the user request. "
        "Do not assume that a defect exists: if the package is already correct, return zero edits. "
        "If a repair or capability generalization is needed, make the smallest sufficient change, "
        "keep SKILL.md and scripts consistent, preserve unrelated behavior, and edit only SKILL.md "
        "or existing files below scripts/. Any supplied structural evolution spec is automatically "
        "derived from the visible package. "
        + edit_policy
        +
        "Otherwise verify the target against the visible source and obey its anti-target constraints. "
        "Never request or infer hidden tests, "
        "mutation labels, gold code, verifier output, or oracle behavior.\n\n"
        "Use the required submit_skill_patch tool exactly once. Its edits argument must contain zero "
        "to eight bounded edit objects. numbered_content prefixes every visible UTF-8 source "
        "line with a display-only '<line>: ' marker; never copy that marker into replacement. Copy the "
        "file's visible sha256 into expected_file_sha256. For structural edits, replace_node must copy "
        "target.node_id and target.source_sha256, while insert_parameter must copy function.node_id and "
        "function.source_sha256. For insert_parameter, replacement contains "
        "only the parameter declaration, such as 'threshold: int = 5'. append_markdown appends a compact "
        "SKILL.md section. replace_lines is a fallback for raw-source conditions and complete inclusive "
        "line ranges. Every edit must include all schema fields; use empty strings and zero line numbers "
        "for fields unused by that operation. Do not return a unified diff or complete files.\n\n"
    )
    return (
        instruction
        + "USER_REQUEST.md\n"
        + request
        + structural
        + "\n\nVISIBLE_PACKAGE.json\n"
        + json.dumps(_package_payload(package), indent=2, ensure_ascii=True)
        + "\n"
    )


def _freeze_hash_valid(payload: dict[str, Any]) -> bool:
    expected = str(payload.get("freeze_hash") or "")
    value = dict(payload)
    value.pop("freeze_hash", None)
    return bool(expected) and canonical_json_hash(value) == expected


def _verify_benchmark_freeze(benchmark_root: Path, expected_hash: str) -> dict[str, Any]:
    freeze = read_json(benchmark_root / "FREEZE_MANIFEST.json")
    public_manifest = read_json(benchmark_root / "public" / "manifest.json")
    private_manifest = read_json(benchmark_root / "_private" / "manifest.json")
    public_count = len(public_manifest.get("cases") or [])
    private_count = len(private_manifest.get("cases") or [])
    checks = {
        "freeze_hash": _freeze_hash_valid(freeze),
        "expected_hash": freeze.get("freeze_hash") == expected_hash,
        "status": freeze.get("status") == "frozen",
        "case_count": freeze.get("case_count")
        == public_manifest.get("case_count")
        == private_manifest.get("case_count")
        == public_count
        == private_count
        and public_count > 0,
        "public_tree": canonical_json_hash(hash_tree(benchmark_root / "public"))
        == freeze.get("public_tree_hash"),
        "private_tree": canonical_json_hash(hash_tree(benchmark_root / "_private"))
        == freeze.get("private_tree_hash"),
    }
    if not all(checks.values()):
        raise ValueError(
            "benchmark_freeze_verification_failed:"
            + ",".join(name for name, ok in checks.items() if not ok)
        )
    return {"freeze": freeze, "checks": checks}


def _localization_audit_row(contexts: dict[str, Any]) -> dict[str, Any]:
    target = contexts["hidden_target"]
    real = contexts["real_sites"]
    sham = contexts["sham_sites"]
    return {
        "case_id": contexts["case_id"],
        "target": target,
        "real_top1_match": _matches_hidden_target(real[0], target),
        "real_top3_match": any(_matches_hidden_target(site, target) for site in real[:3]),
        "real_topk_match": any(_matches_hidden_target(site, target) for site in real),
        "sham_target_match": any(_matches_hidden_target(site, target) for site in sham),
        "real_sites": [
            {key: site[key] for key in ("path", "line", "symbol", "node_type", "role", "localization_score")}
            for site in real
        ],
        "sham_sites": [
            {key: site[key] for key in ("path", "line", "symbol", "node_type", "role", "localization_score")}
            for site in sham
        ],
    }


def prepare_stage(
    benchmark_root: str | Path,
    experiment_root: str | Path,
    *,
    expected_freeze_hash: str,
    model: str = "gpt-5.5",
    base_url: str = "https://api.openlux.ai/v1",
    timeout: int = 600,
    max_attempts: int = 2,
) -> dict[str, Any]:
    benchmark = Path(benchmark_root).resolve()
    experiment = Path(experiment_root).resolve()
    if experiment.exists():
        raise FileExistsError(f"experiment_root_already_exists:{experiment}")
    verified = _verify_benchmark_freeze(benchmark, expected_freeze_hash)
    stage = experiment / "stage"
    audit_root = experiment / "_audit"
    stage.mkdir(parents=True)
    audit_root.mkdir()
    copy_tree_clean(benchmark / "public", stage / "public")
    public_manifest = read_json(stage / "public" / "manifest.json")
    private_manifest = read_json(benchmark / "_private" / "manifest.json")
    private_by_id = {row["case_id"]: row for row in private_manifest["cases"]}

    contexts_root = stage / "contexts"
    prompts_root = stage / "prompts"
    contexts_root.mkdir()
    prompts_root.mkdir()
    calls: list[dict[str, Any]] = []
    context_receipts: list[dict[str, Any]] = []
    localization_rows: list[dict[str, Any]] = []
    package_sizes: list[tuple[int, str]] = []
    context_decisions: dict[str, str] = {}
    capability_case_ids: set[str] = set()
    selected_public_rows = [
        row
        for row in sorted(public_manifest["cases"], key=lambda row: row["case_id"])
        if row.get("track") == "capability_evolution"
    ]
    for public_row in selected_public_rows:
        case_id = public_row["case_id"]
        if public_row.get("track") == "capability_evolution":
            capability_case_ids.add(case_id)
        public_case = stage / "public" / "cases" / case_id
        private_case = benchmark / "_private" / "cases" / case_id
        label_path = private_case / "label.json"
        if sha256_file(label_path) != private_by_id[case_id]["label_sha256"]:
            raise ValueError(f"private_label_hash_mismatch:{case_id}")
        label = read_json(label_path)
        contexts = build_case_contexts(public_case, label)
        context_decisions[case_id] = contexts["decision"]["decision"]
        case_context = contexts_root / case_id
        case_context.mkdir()
        write_json(case_context / "AST_SPEC.json", contexts["predicted_spec"])
        write_json(case_context / "MATCHED_SHAM_SPEC.json", contexts["matched_sham_spec"])
        write_json(case_context / "GOLD_NODE_SPEC.json", contexts["gold_spec"])
        write_json(case_context / "NODE_REGISTRY.json", contexts["node_registry"])
        write_json(case_context / "MARKDOWN_SECTIONS.json", contexts["markdown_sections"])
        localization_rows.append(contexts["localization_audit"])
        context_receipts.append(
            {
                "case_id": case_id,
                "candidate_site_count": contexts["candidate_site_count"],
                "ast_spec_sha256": sha256_file(case_context / "AST_SPEC.json"),
                "matched_sham_spec_sha256": sha256_file(
                    case_context / "MATCHED_SHAM_SPEC.json"
                ),
                "gold_node_spec_sha256": sha256_file(case_context / "GOLD_NODE_SPEC.json"),
                "node_registry_sha256": sha256_file(case_context / "NODE_REGISTRY.json"),
                "predicted_decision": contexts["decision"]["decision"],
            }
        )
        package_size = sum(
            path.stat().st_size
            for path in (public_case / "package").rglob("*")
            if path.is_file()
        )
        package_sizes.append((package_size, case_id))
        for condition in CONDITIONS:
            trial_id = f"{case_id}--{condition}--r1"
            prompt = build_prompt(public_case, condition, contexts)
            prompt_path = prompts_root / f"{trial_id}.txt"
            prompt_path.write_text(prompt, encoding="utf-8")
            calls.append(
                {
                    "trial_id": trial_id,
                    "case_id": case_id,
                    "condition": condition,
                    "repeat": 1,
                    "prompt_path": f"prompts/{trial_id}.txt",
                    "prompt_sha256": sha256_file(prompt_path),
                    "prompt_bytes": prompt_path.stat().st_size,
                    "public_package_hash": canonical_json_hash(
                        hash_tree(public_case / "package")
                    ),
                }
            )

    workspace = Path(__file__).resolve().parents[1]
    code_hashes = {
        relative: sha256_file(workspace / relative) for relative in CODE_PATHS
    }
    propose_sizes = sorted(
        (size, case_id)
        for size, case_id in package_sizes
        if context_decisions.get(case_id) == "PROPOSE"
    )
    abstain_sizes = sorted(
        (size, case_id)
        for size, case_id in package_sizes
        if context_decisions.get(case_id) == "ABSTAIN"
    )
    if not propose_sizes:
        raise ValueError("smoke_requires_at_least_one_propose_case")
    if abstain_sizes:
        smoke_case_ids = [propose_sizes[0][1], abstain_sizes[0][1]]
        smoke_selection_basis = "smallest_propose_and_smallest_abstain_public_packages"
    elif len(propose_sizes) >= 2:
        smoke_case_ids = [propose_sizes[0][1], propose_sizes[1][1]]
        smoke_selection_basis = "two_smallest_propose_public_packages"
    else:
        smoke_case_ids = [propose_sizes[0][1]]
        smoke_selection_basis = "single_available_propose_public_package"
    smoke_trial_ids = [
        call["trial_id"] for call in calls if call["case_id"] in smoke_case_ids
    ]
    write_json(
        stage / "SMOKE_TRIALS.json",
        {"case_ids": smoke_case_ids, "trial_ids": smoke_trial_ids},
    )
    matched_conditions = CONDITIONS
    capability_trial_ids = [
        call["trial_id"]
        for call in calls
        if call["case_id"] in capability_case_ids
        and call["condition"] in matched_conditions
    ]
    structured_smoke_case_ids = list(smoke_case_ids)
    structured_smoke_trial_ids = [
        call["trial_id"]
        for call in calls
        if call["case_id"] in structured_smoke_case_ids
        and call["condition"] in matched_conditions
    ]
    capability_selection = {
        "schema_version": SCHEMA_VERSION,
        "selection_basis": "public_manifest_track_capability_evolution",
        "conditions": list(matched_conditions),
        "case_ids": sorted(capability_case_ids),
        "trial_ids": capability_trial_ids,
    }
    capability_selection["selection_hash"] = canonical_json_hash(capability_selection)
    write_json(stage / "CAPABILITY_FOUR_CONDITION_TRIALS.json", capability_selection)
    structured_smoke = {
        "schema_version": SCHEMA_VERSION,
        "selection_basis": smoke_selection_basis,
        "conditions": list(matched_conditions),
        "case_ids": structured_smoke_case_ids,
        "trial_ids": structured_smoke_trial_ids,
    }
    structured_smoke["selection_hash"] = canonical_json_hash(structured_smoke)
    write_json(stage / "NODE_EDITOR_SMOKE_TRIALS.json", structured_smoke)

    create_source_capsule(
        workspace,
        stage,
        CODE_PATHS,
        entrypoint_module="skillscriptbench.package_matrix_conditions_v65",
    )

    plan = {
        "schema_version": SCHEMA_VERSION,
        "status": "frozen_before_model_calls",
        "created_at": _utc_now(),
        "benchmark_freeze_hash": expected_freeze_hash,
        "model": model,
        "base_url": base_url,
        "api_protocol": "chat-completions-forced-tool",
        "temperature": 0,
        "timeout": timeout,
        "max_attempts": max_attempts,
        "conditions": list(CONDITIONS),
        "case_count": len(selected_public_rows),
        "call_count": len(calls),
        "calls": calls,
        "public_tree_hash": canonical_json_hash(hash_tree(stage / "public")),
        "contexts_tree_hash": canonical_json_hash(hash_tree(contexts_root)),
        "prompts_tree_hash": canonical_json_hash(hash_tree(prompts_root)),
        "code_sha256": code_hashes,
        "source_capsule": source_capsule_plan_receipt(stage),
        "runtime": {
            "python": sys_version(),
            "platform": platform.platform(),
            "ast_backend": "python_stdlib_ast",
        },
        "response_mode": "forced_submit_skill_patch_role_aware_node_editor_v1",
        "structured_edit_policy": {
            "selected_target_node_only": True,
            "python_operations": ["insert_parameter", "replace_node"],
            "markdown_operations": ["append_markdown"],
            "predicted_abstain_has_no_editable_nodes": True,
        },
        "tool_schema_hash": canonical_json_hash(PATCH_TOOL),
        "capability_selection_sha256": sha256_file(
            stage / "CAPABILITY_FOUR_CONDITION_TRIALS.json"
        ),
        "structured_smoke_selection_sha256": sha256_file(
            stage / "NODE_EDITOR_SMOKE_TRIALS.json"
        ),
        "hidden_evaluator_loaded_during_model_run": False,
        "sham_derangement_uses_hidden_location_only": True,
        "protocol_normalization": {
            "schema_version": "0.89-condition-blind-patch-protocol-normalization-v1",
            "applied_before_candidate_parse": True,
            "semantic_fields_may_change": False,
            "automatic_line_shift": False,
        },
    }
    plan["plan_hash"] = canonical_json_hash(plan)
    write_json(stage / "FROZEN_PLAN.json", plan)

    localization = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete_post_context_private_audit",
        "case_count": len(localization_rows),
        "exact_recall_at_1_count": sum(row["exact_recall_at_1"] for row in localization_rows),
        "exact_recall_at_3_count": sum(row["exact_recall_at_3"] for row in localization_rows),
        "mrr": sum(float(row["mrr"]) for row in localization_rows)
        / max(1, len(localization_rows)),
        "family_top1_correct_count": sum(
            row["family_top1_correct"] for row in localization_rows
        ),
        "parameter_top1_correct_count": sum(
            row["parameter_top1_correct"] for row in localization_rows
        ),
        "propose_count": sum(
            row["decision"]["decision"] == "PROPOSE" for row in localization_rows
        ),
        "abstain_count": sum(
            row["decision"]["decision"] == "ABSTAIN" for row in localization_rows
        ),
        "rows": localization_rows,
    }
    localization["audit_hash"] = canonical_json_hash(localization)
    write_json(audit_root / "LOCALIZATION_AUDIT.json", localization)
    prepare = {
        "schema_version": SCHEMA_VERSION,
        "status": "ready_for_zero_call_preflight",
        "created_at": _utc_now(),
        "benchmark_checks": verified["checks"],
        "benchmark_freeze_hash": expected_freeze_hash,
        "case_count": len(selected_public_rows),
        "call_count": len(calls),
        "condition_counts": dict(Counter(call["condition"] for call in calls)),
        "context_receipts": context_receipts,
        "localization_audit_hash": localization["audit_hash"],
        "plan_hash": plan["plan_hash"],
        "smoke_trial_count": len(smoke_trial_ids),
        "model_calls": 0,
        "credential_persisted": False,
    }
    prepare["prepare_hash"] = canonical_json_hash(prepare)
    write_json(audit_root / "PREPARE_RECORD.json", prepare)
    return {"plan": plan, "prepare": prepare, "localization": localization}


def sys_version() -> str:
    import sys

    return sys.version


def _embedded_hash_valid(payload: dict[str, Any], field: str) -> bool:
    expected = str(payload.get(field) or "")
    value = dict(payload)
    value.pop(field, None)
    return bool(expected) and canonical_json_hash(value) == expected


def _validate_plan(stage: Path) -> dict[str, Any]:
    plan = read_json(stage / "FROZEN_PLAN.json")
    workspace = Path(__file__).resolve().parents[1]
    capsule_receipt = plan.get("source_capsule")
    capsule_validation = (
        validate_source_capsule(stage, capsule_receipt, plan.get("code_sha256") or {})
        if isinstance(capsule_receipt, dict)
        else None
    )
    checks = {
        "plan_hash": _embedded_hash_valid(plan, "plan_hash"),
        "status": plan.get("status") == "frozen_before_model_calls",
        "model": plan.get("model") == "gpt-5.5",
        "call_count": plan.get("call_count")
        == plan.get("case_count", 0) * len(CONDITIONS)
        == len(plan.get("calls", [])),
        "condition_counts": dict(Counter(row["condition"] for row in plan.get("calls", [])))
        == {condition: plan.get("case_count", 0) for condition in CONDITIONS},
        "public_tree": canonical_json_hash(hash_tree(stage / "public"))
        == plan.get("public_tree_hash"),
        "contexts_tree": canonical_json_hash(hash_tree(stage / "contexts"))
        == plan.get("contexts_tree_hash"),
        "prompts_tree": canonical_json_hash(hash_tree(stage / "prompts"))
        == plan.get("prompts_tree_hash"),
        "capability_selection": sha256_file(
            stage / "CAPABILITY_FOUR_CONDITION_TRIALS.json"
        )
        == plan.get("capability_selection_sha256"),
        "structured_smoke_selection": sha256_file(
            stage / "NODE_EDITOR_SMOKE_TRIALS.json"
        )
        == plan.get("structured_smoke_selection_sha256"),
        "code_hashes": (
            capsule_validation is not None
            and capsule_validation["status"] == "pass"
        )
        if capsule_receipt is not None
        else all(
            (workspace / relative).is_file()
            and sha256_file(workspace / relative) == expected
            for relative, expected in (plan.get("code_sha256") or {}).items()
        ),
        "source_capsule": capsule_validation is not None
        and capsule_validation["status"] == "pass"
        if capsule_receipt is not None
        else True,
    }
    prompt_checks: list[dict[str, Any]] = []
    for call in plan.get("calls", []):
        prompt_path = stage / call["prompt_path"]
        prompt = prompt_path.read_text(encoding="utf-8")
        condition = call["condition"]
        isolation = (
            condition != "raw-package"
            or "STRUCTURAL_EVOLUTION_SPEC.json" not in prompt
        ) and (
            condition == "raw-package"
            or "STRUCTURAL_EVOLUTION_SPEC.json" in prompt
        )
        condition_hidden = condition not in prompt
        prompt_checks.append(
            {
                "trial_id": call["trial_id"],
                "hash_ok": sha256_file(prompt_path) == call["prompt_sha256"],
                "isolation_ok": isolation,
                "condition_hidden": condition_hidden,
            }
        )
    checks["all_prompt_hashes"] = all(row["hash_ok"] for row in prompt_checks)
    checks["all_prompt_views_isolated"] = all(row["isolation_ok"] for row in prompt_checks)
    checks["all_condition_names_hidden"] = all(row["condition_hidden"] for row in prompt_checks)
    return {"plan": plan, "checks": checks, "prompt_checks": prompt_checks}


def _parse_and_apply_package_edits_v64_legacy(
    content: str,
    source_package: Path,
    candidate_package: Path,
    *,
    structural_nodes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    parsed = _parse_final_edit_json(content)
    if not isinstance(parsed, dict) or set(parsed) != {"edits", "summary"}:
        raise ValueError("response_must_contain_exactly_edits_and_summary")
    edits = parsed["edits"]
    summary = parsed["summary"]
    if not isinstance(edits, list) or len(edits) > MAX_EDITS:
        raise ValueError(f"edits_must_contain_zero_to_{MAX_EDITS}_items")
    if not isinstance(summary, str):
        raise TypeError("summary_must_be_string")
    copy_tree_clean(source_package, candidate_package)
    available_nodes = structural_nodes or {}
    receipts: list[dict[str, Any]] = []
    total_bytes = 0
    changed_paths: set[str] = set()
    no_op_count = 0
    duplicate_count = 0
    accepted: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_edits: set[str] = set()
    for index, edit in enumerate(edits):
        required = {
            "path",
            "expected_file_sha256",
            "operation",
            "start_line",
            "end_line",
            "target_node_id",
            "replacement",
        }
        if not isinstance(edit, dict) or set(edit) != required:
            raise ValueError(f"edit_{index}_schema_invalid")
        relative = str(edit["path"])
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError(f"edit_{index}_unsafe_path")
        if relative != "SKILL.md" and not relative.startswith("scripts/"):
            raise ValueError(f"edit_{index}_path_outside_allowed_scope:{relative}")
        target = source_package / pure
        if not target.is_file():
            raise FileNotFoundError(f"edit_{index}_target_missing:{relative}")
        source = target.read_text(encoding="utf-8")
        actual_file_sha256 = sha256_file(target)
        expected_file_sha256 = edit["expected_file_sha256"]
        if not isinstance(expected_file_sha256, str):
            raise TypeError(f"edit_{index}_expected_file_sha256_must_be_string")
        if expected_file_sha256 != actual_file_sha256:
            raise ValueError(f"edit_{index}_file_sha256_mismatch:{relative}")

        operation = edit["operation"]
        if operation not in {"replace_lines", "insert_before", "insert_after"}:
            raise ValueError(f"edit_{index}_operation_invalid:{operation}")
        start_line = edit["start_line"]
        end_line = edit["end_line"]
        if (
            not isinstance(start_line, int)
            or isinstance(start_line, bool)
            or not isinstance(end_line, int)
            or isinstance(end_line, bool)
        ):
            raise TypeError(f"edit_{index}_line_numbers_must_be_integers")
        source_lines = source.splitlines()
        line_count = len(source_lines)
        if not (1 <= start_line <= end_line <= line_count):
            raise ValueError(
                f"edit_{index}_line_range_out_of_bounds:{start_line}:{end_line}:{line_count}"
            )
        target_node_id = edit["target_node_id"]
        if not isinstance(target_node_id, str):
            raise TypeError(f"edit_{index}_target_node_id_must_be_string")
        node_binding = {
            "status": "not_supplied",
            "node_exists": None,
            "path_match": None,
            "file_hash_match": None,
            "range_overlap": None,
        }
        if target_node_id:
            node = available_nodes.get(target_node_id)
            if node is None:
                node_binding.update(status="unknown_node", node_exists=False)
            else:
                node_start = int(node["span"]["start_line"])
                node_end = int(node["span"]["end_line"])
                node_binding.update(
                    status="matched"
                    if node.get("path") == relative
                    and node.get("file_sha256") == actual_file_sha256
                    and not (end_line < node_start or start_line > node_end)
                    else "advisory_mismatch",
                    node_exists=True,
                    path_match=node.get("path") == relative,
                    file_hash_match=node.get("file_sha256") == actual_file_sha256,
                    range_overlap=not (end_line < node_start or start_line > node_end),
                )

        replacement = edit["replacement"]
        if not isinstance(replacement, str):
            raise TypeError(f"edit_{index}_replacement_must_be_string")
        normalized_replacement = replacement.replace("\r\n", "\n")
        if "\r" in normalized_replacement:
            raise ValueError(f"edit_{index}_replacement_contains_bare_carriage_return")
        if not normalized_replacement:
            new_lines: list[str] = []
        else:
            new_lines = normalized_replacement.split("\n")
            if new_lines[-1] == "":
                new_lines.pop()
        old_lines = (
            source_lines[start_line - 1 : end_line]
            if operation == "replace_lines"
            else []
        )
        if (operation == "replace_lines" and old_lines == new_lines) or (
            operation != "replace_lines" and not new_lines
        ):
            no_op_count += 1
            continue

        old_bytes = len("\n".join(old_lines).encode("utf-8"))
        new_bytes = len("\n".join(new_lines).encode("utf-8"))
        if max(old_bytes, new_bytes) > MAX_SINGLE_EDIT_BYTES:
            raise ValueError(f"edit_{index}_too_large")
        total_bytes += max(old_bytes, new_bytes)
        if total_bytes > MAX_TOTAL_EDIT_BYTES:
            raise ValueError("total_edit_span_too_large")

        if operation == "replace_lines":
            start_index, end_index = start_line - 1, end_line
        elif operation == "insert_before":
            start_index = end_index = start_line - 1
        else:
            start_index = end_index = end_line
        normalized = {
            "index": index,
            "path": relative,
            "expected_file_sha256": expected_file_sha256,
            "operation": operation,
            "start_line": start_line,
            "end_line": end_line,
            "target_node_id": target_node_id,
            "node_binding": node_binding,
            "new_lines": new_lines,
            "old_lines": old_lines,
            "start_index": start_index,
            "end_index": end_index,
            "old_bytes": old_bytes,
            "new_bytes": new_bytes,
        }
        identity = canonical_json_hash(
            {key: value for key, value in normalized.items() if key != "index"}
        )
        if identity in seen_edits:
            duplicate_count += 1
            continue
        seen_edits.add(identity)
        accepted[relative].append(normalized)

    for relative, file_edits in accepted.items():
        ordered = sorted(
            file_edits,
            key=lambda row: (row["start_index"], row["end_index"], row["index"]),
        )
        for left, right in zip(ordered, ordered[1:]):
            left_is_insert = left["start_index"] == left["end_index"]
            right_is_insert = right["start_index"] == right["end_index"]
            if left_is_insert and right_is_insert:
                conflict = left["start_index"] == right["start_index"]
            elif left_is_insert:
                conflict = right["start_index"] <= left["start_index"] <= right["end_index"]
            elif right_is_insert:
                conflict = left["start_index"] <= right["start_index"] <= left["end_index"]
            else:
                conflict = right["start_index"] < left["end_index"]
            if conflict:
                raise ValueError(
                    f"overlapping_edits:{relative}:edit_{left['index']}:edit_{right['index']}"
                )

        source_path = source_package / relative
        source = source_path.read_text(encoding="utf-8")
        newline = "\r\n" if "\r\n" in source and source.count("\r\n") == source.count("\n") else "\n"
        trailing_newline = source.endswith(("\n", "\r"))
        result_lines = source.splitlines()
        for edit in sorted(
            file_edits,
            key=lambda row: (row["start_index"], row["end_index"], row["index"]),
            reverse=True,
        ):
            result_lines[edit["start_index"] : edit["end_index"]] = edit["new_lines"]
        result_source = newline.join(result_lines)
        if trailing_newline:
            result_source += newline
        if result_source == source:
            no_op_count += len(file_edits)
            continue
        target = candidate_package / relative
        target.write_text(result_source, encoding="utf-8", newline="")
        changed_paths.add(relative)
        for edit in file_edits:
            receipts.append(
                {
                    "index": edit["index"],
                    "path": relative,
                    "operation": edit["operation"],
                    "start_line": edit["start_line"],
                    "end_line": edit["end_line"],
                    "target_node_id": edit["target_node_id"],
                    "node_binding": edit["node_binding"],
                    "expected_file_sha256": edit["expected_file_sha256"],
                    "old_lines_sha256": canonical_json_hash(edit["old_lines"]),
                    "new_lines_sha256": canonical_json_hash(edit["new_lines"]),
                    "old_bytes": edit["old_bytes"],
                    "new_bytes": edit["new_bytes"],
                }
            )
    syntax = validate_candidate_package(candidate_package, changed_paths)
    return {
        "response_mode": "bounded_multifile_line_addressed_replacement_v3",
        "summary": summary,
        "edit_count": len(receipts),
        "ignored_no_op_count": no_op_count,
        "ignored_duplicate_count": duplicate_count,
        "changed_paths": sorted(changed_paths),
        "total_edit_span_upper_bound_bytes": total_bytes,
        "edits": receipts,
        "syntax": syntax,
    }


def _parse_final_edit_json(content: str) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate_json_key:{key}")
            value[key] = item
        return value

    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines)
    try:
        return json.loads(stripped, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as original_error:
        starts = [match.start() for match in re.finditer(r'\{\s*"edits"\s*:', stripped)]
        decoder = json.JSONDecoder(object_pairs_hook=reject_duplicate_keys)
        for start in reversed(starts):
            try:
                value, end = decoder.raw_decode(stripped[start:])
            except json.JSONDecodeError:
                continue
            if stripped[start + end :].strip():
                continue
            return value
        raise original_error


def _unwrap_return_expression(replacement: str) -> str | None:
    stripped = replacement.strip()
    try:
        parsed = ast.parse(stripped)
    except SyntaxError:
        return None
    if len(parsed.body) != 1 or not isinstance(parsed.body[0], ast.Return):
        return None
    value = parsed.body[0].value
    return ast.unparse(value) if value is not None else None


def _unwrap_assignment_expression(replacement: str) -> str | None:
    stripped = replacement.strip()
    try:
        parsed = ast.parse(stripped)
    except SyntaxError:
        return None
    if len(parsed.body) != 1:
        return None
    statement = parsed.body[0]
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        return ast.unparse(statement.value)
    if isinstance(statement, ast.AnnAssign) and statement.value is not None:
        return ast.unparse(statement.value)
    return None


def parse_and_apply_package_edits(
    content: str,
    source_package: Path,
    candidate_package: Path,
    *,
    structural_nodes: dict[str, dict[str, Any]] | None = None,
    request: str | None = None,
    allow_function_scope_replacement: bool = False,
    recover_structural_node_id_by_hash: bool = False,
    normalize_return_value_replacement: bool = False,
    normalize_alias_value_replacement: bool = False,
) -> dict[str, Any]:
    parsed = _parse_final_edit_json(content)
    if not isinstance(parsed, dict) or set(parsed) != {"edits", "summary"}:
        raise ValueError("response_must_contain_exactly_edits_and_summary")
    edits = parsed["edits"]
    summary = parsed["summary"]
    if not isinstance(edits, list) or len(edits) > MAX_EDITS:
        raise ValueError(f"edits_must_contain_zero_to_{MAX_EDITS}_items")
    if not isinstance(summary, str):
        raise TypeError("summary_must_be_string")
    required = {
        "path",
        "expected_file_sha256",
        "operation",
        "target_node_id",
        "expected_node_sha256",
        "symbol",
        "start_line",
        "end_line",
        "replacement",
    }
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict) or set(edit) != required:
            raise ValueError(f"edit_{index}_schema_invalid")
    structured_mode = structural_nodes is not None
    structural_node_id_recovery_count = 0
    return_value_replacement_normalization_count = 0
    alias_value_replacement_normalization_count = 0
    if structured_mode:
        allowed_node_ids = set(structural_nodes or {})
        for index, edit in enumerate(edits):
            if edit["path"] == "SKILL.md":
                if edit["operation"] != "append_markdown":
                    raise ValueError(f"edit_{index}_structured_markdown_operation_invalid")
                continue
            if edit["operation"] not in {"replace_node", "insert_parameter"}:
                raise ValueError(f"edit_{index}_structured_script_operation_invalid")
            if edit["target_node_id"] not in allowed_node_ids:
                matches = [
                    node_id
                    for node_id, node in (structural_nodes or {}).items()
                    if recover_structural_node_id_by_hash
                    and node.get("path") == edit["path"]
                    and node.get("node_source_sha256")
                    == edit["expected_node_sha256"]
                    and (
                        not edit["symbol"]
                        or not node.get("symbol")
                        or node.get("symbol") == edit["symbol"]
                    )
                ]
                if len(matches) != 1:
                    raise ValueError(f"edit_{index}_structured_target_not_selected")
                edit["target_node_id"] = matches[0]
                structural_node_id_recovery_count += 1
            selected_node = (structural_nodes or {})[edit["target_node_id"]]
            selected_role = selected_node["role"]
            if edit["operation"] == "insert_parameter" and selected_role != "function_scope":
                raise ValueError(f"edit_{index}_insert_parameter_requires_function_node")
            if (
                edit["operation"] == "replace_node"
                and selected_role == "function_scope"
                and not allow_function_scope_replacement
            ):
                raise ValueError(f"edit_{index}_replace_node_requires_target_node")
            if (
                normalize_return_value_replacement
                and edit["operation"] == "replace_node"
                and selected_role == "resolver_return_value"
            ):
                expression = _unwrap_return_expression(edit["replacement"])
                if expression is not None and expression != edit["replacement"]:
                    edit["replacement"] = expression
                    return_value_replacement_normalization_count += 1
            if (
                normalize_alias_value_replacement
                and edit["operation"] == "replace_node"
                and selected_role == "alias_source_value"
            ):
                expression = _unwrap_assignment_expression(edit["replacement"])
                if expression is not None and expression != edit["replacement"]:
                    edit["replacement"] = expression
                    alias_value_replacement_normalization_count += 1
    application = apply_structured_edits(
        edits,
        source_package,
        candidate_package,
        node_registry=structural_nodes or {},
    )
    syntax = validate_candidate_package(
        candidate_package, application["changed_paths"]
    )
    gate = (
        generic_structural_gate(
            source_package,
            candidate_package,
            request,
            application["changed_paths"],
        )
        if request is not None and edits
        else {
            "decision": "ABSTAIN" if not edits else "NOT_RUN",
            "semantic_correctness": "unknown",
            "items": [],
        }
    )
    return {
        **application,
        "summary": summary,
        "syntax": syntax,
        "structural_gate": gate,
        "structural_node_id_recovery_count": structural_node_id_recovery_count,
        "return_value_replacement_normalization_count": (
            return_value_replacement_normalization_count
        ),
        "alias_value_replacement_normalization_count": (
            alias_value_replacement_normalization_count
        ),
    }


def validate_candidate_package(package: Path, changed_paths: Iterable[str]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for relative in sorted(set(changed_paths)):
        path = package / relative
        suffix = path.suffix.lower()
        source = path.read_text(encoding="utf-8")
        if suffix == ".py":
            ast.parse(source, filename=relative)
            detail = "python_ast_parse"
        elif suffix in JS_SUFFIXES:
            ok, detail = _node_check(source, suffix)
            if not ok:
                raise SyntaxError(f"js_ts_parse_failed:{relative}:{detail}")
        elif suffix in SHELL_SUFFIXES:
            completed = subprocess.run(
                ["bash", "--noprofile", "--norc", "-n"],
                input=source,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            if completed.returncode != 0:
                raise SyntaxError(f"shell_parse_failed:{relative}:{completed.stderr[-800:]}")
            _shell_ast(source)
            detail = "bash_n_plus_tree_sitter_bash"
        else:
            detail = "text_file"
        rows.append({"path": relative, "status": "pass", "detail": detail})
    return {"status": "pass", "files": rows}


def zero_call_preflight(experiment_root: str | Path) -> dict[str, Any]:
    experiment = Path(experiment_root).resolve()
    stage = experiment / "stage"
    validated = _validate_plan(stage)
    checks = dict(validated["checks"])
    with tempfile.TemporaryDirectory(prefix="ssb-v65-zero-call-") as directory:
        fixture = Path(directory)
        source = fixture / "source"
        source.mkdir()
        (source / "SKILL.md").write_text("Use `classify`.\n", encoding="utf-8")
        (source / "scripts").mkdir()
        (source / "scripts" / "tool.py").write_text(
            "def classify(score: int) -> str:\n"
            "    return 'high' if score >= 5 else 'low'\n",
            encoding="utf-8",
        )
        fixture_request = (
            "Generalize the public `classify` helper through an optional `threshold` "
            "parameter, using `5` as the compatibility default."
        )
        fixture_sites = extract_python_role_sites(
            "scripts/tool.py", (source / "scripts" / "tool.py").read_text(encoding="utf-8")
        )
        fixture_target = next(
            site for site in fixture_sites if site["role"] == "comparison_boundary"
        )
        fixture_function = next(
            site for site in fixture_sites if site["role"] == "function_scope"
        )
        fixture_registry = {site["site_id"]: site for site in fixture_sites}
        candidate = fixture / "candidate"
        application = parse_and_apply_package_edits(
            json.dumps(
                {
                    "edits": [
                        {
                            "path": "scripts/tool.py",
                            "expected_file_sha256": sha256_file(source / "scripts" / "tool.py"),
                            "operation": "insert_parameter",
                            "target_node_id": fixture_function["site_id"],
                            "expected_node_sha256": fixture_function["node_source_sha256"],
                            "symbol": "classify",
                            "start_line": 0,
                            "end_line": 0,
                            "replacement": "threshold: int = 5",
                        },
                        {
                            "path": "scripts/tool.py",
                            "expected_file_sha256": sha256_file(source / "scripts" / "tool.py"),
                            "operation": "replace_node",
                            "target_node_id": fixture_target["site_id"],
                            "expected_node_sha256": fixture_target["node_source_sha256"],
                            "symbol": "classify",
                            "start_line": 0,
                            "end_line": 0,
                            "replacement": "threshold",
                        },
                        {
                            "path": "SKILL.md",
                            "expected_file_sha256": sha256_file(source / "SKILL.md"),
                            "operation": "append_markdown",
                            "target_node_id": "",
                            "expected_node_sha256": "",
                            "symbol": "",
                            "start_line": 0,
                            "end_line": 0,
                            "replacement": "## Threshold\n\n`classify` accepts `threshold` (default `5`).",
                        }
                    ],
                    "summary": "fixture",
                }
            ),
            source,
            candidate,
            structural_nodes=fixture_registry,
            request=fixture_request,
        )
        candidate_source = (candidate / "scripts" / "tool.py").read_text(encoding="utf-8")
        checks["production_node_editor_smoke"] = (
            application["edit_count"] == 3
            and "threshold: int = 5" in candidate_source
            and "score >= threshold" in candidate_source
            and application["structural_gate"]["decision"] == "ACCEPT_STRUCTURALLY"
        )
        no_op_candidate = fixture / "no-op"
        no_op = parse_and_apply_package_edits(
            json.dumps({"edits": [], "summary": "already correct"}),
            source,
            no_op_candidate,
            request=fixture_request,
        )
        checks["clean_no_op_parser_smoke"] = (
            no_op["edit_count"] == 0 and hash_tree(source) == hash_tree(no_op_candidate)
        )
        ignored_candidate = fixture / "ignored-no-op"
        ignored = parse_and_apply_package_edits(
            json.dumps(
                {
                    "edits": [
                        {
                            "path": "SKILL.md",
                            "expected_file_sha256": sha256_file(source / "SKILL.md"),
                            "operation": "replace_lines",
                            "target_node_id": "",
                            "expected_node_sha256": "",
                            "symbol": "",
                            "start_line": 1,
                            "end_line": 1,
                            "replacement": "Use `classify`.",
                        }
                    ],
                    "summary": "redundant edit",
                }
            ),
            source,
            ignored_candidate,
            request=fixture_request,
        )
        checks["redundant_no_op_is_ignored"] = (
            ignored["edit_count"] == 0
            and ignored["ignored_no_op_count"] == 1
            and hash_tree(source) == hash_tree(ignored_candidate)
        )
    localization = read_json(experiment / "_audit" / "LOCALIZATION_AUDIT.json")
    case_count = int(localization.get("case_count") or 0)
    checks["localization_audit_hash"] = _embedded_hash_valid(localization, "audit_hash")
    checks["exact_recall_at_1_gate"] = localization.get("exact_recall_at_1_count", 0) >= math.ceil(0.75 * case_count)
    checks["exact_recall_at_3_gate"] = localization.get("exact_recall_at_3_count", 0) >= math.ceil(0.90 * case_count)
    checks["family_recovery_gate"] = localization.get("family_top1_correct_count", 0) >= math.ceil(0.75 * case_count)
    checks["parameter_recovery_gate"] = localization.get("parameter_top1_correct_count", 0) >= math.ceil(0.75 * case_count)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if all(checks.values()) else "fail",
        "created_at": _utc_now(),
        "checks": checks,
        "case_count": validated["plan"]["case_count"],
        "call_count": validated["plan"]["call_count"],
        "model_calls": 0,
        "credential_persisted": False,
    }
    result["preflight_hash"] = canonical_json_hash(result)
    write_json(experiment / "_audit" / "ZERO_CALL_PREFLIGHT.json", result)
    if result["status"] != "pass":
        raise ValueError(
            "zero_call_preflight_failed:"
            + ",".join(name for name, ok in checks.items() if not ok)
        )
    return result


def _credential_absent(root: Path, api_key: str) -> bool:
    needle = api_key.encode("utf-8")
    return all(needle not in path.read_bytes() for path in root.rglob("*") if path.is_file())


def _call_patch_completion(
    *,
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout: int,
    max_attempts: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any]]:
    request_payload: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "tools": [PATCH_TOOL],
        "tool_choice": {
            "type": "function",
            "function": {"name": "submit_skill_patch"},
        },
    }
    encoded = json.dumps(request_payload).encode("utf-8")
    attempts: list[dict[str, Any]] = []
    response_payload: dict[str, Any] | None = None
    for attempt in range(1, max_attempts + 1):
        started = time.monotonic()
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/chat/completions",
            data=encoded,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
                status = response.status
            attempts.append(
                {
                    "attempt": attempt,
                    "status": "success",
                    "http_status": status,
                    "elapsed_seconds": time.monotonic() - started,
                }
            )
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:2000]
            attempts.append(
                {
                    "attempt": attempt,
                    "status": "http_error",
                    "http_status": exc.code,
                    "elapsed_seconds": time.monotonic() - started,
                    "error": body,
                }
            )
            if exc.code not in {408, 409, 429, 500, 502, 503, 504}:
                break
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionResetError,
            http.client.RemoteDisconnected,
            ssl.SSLError,
        ) as exc:
            attempts.append(
                {
                    "attempt": attempt,
                    "status": "transport_error",
                    "elapsed_seconds": time.monotonic() - started,
                    "error": f"{type(exc).__name__}:{exc}",
                }
            )
        if attempt < max_attempts:
            time.sleep(5)
    return response_payload, attempts, request_payload


def _patch_arguments(response: dict[str, Any]) -> tuple[str, str]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("provider_response_choices_missing")
    message = choices[0].get("message") or {}
    tool_calls = message.get("tool_calls") or []
    matching = [
        call
        for call in tool_calls
        if (call.get("function") or {}).get("name") == "submit_skill_patch"
    ]
    if len(matching) == 1:
        arguments = (matching[0].get("function") or {}).get("arguments")
        if not isinstance(arguments, str):
            raise TypeError("submit_skill_patch_arguments_not_string")
        return arguments, "forced_tool_call"
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content, "content_fallback"
    raise ValueError(f"submit_skill_patch_tool_call_count:{len(matching)}")


def _structural_nodes_for_call(
    stage: Path, call: dict[str, Any]
) -> dict[str, dict[str, Any]] | None:
    if call["condition"] == "raw-package":
        return None
    spec_name = {
        "ast-spec": "AST_SPEC.json",
        "matched-sham": "MATCHED_SHAM_SPEC.json",
        "gold-node": "GOLD_NODE_SPEC.json",
    }[call["condition"]]
    context_root = stage / "contexts" / call["case_id"]
    spec = read_json(context_root / spec_name)
    if spec.get("decision") == "ABSTAIN":
        return {}
    node_ids = {
        str((spec.get("target") or {}).get("node_id") or ""),
        str((spec.get("function") or {}).get("node_id") or ""),
    }
    registry = read_json(context_root / "NODE_REGISTRY.json")
    missing = sorted(node_id for node_id in node_ids if node_id not in registry)
    if missing:
        raise ValueError(
            f"selected_structural_node_missing:{call['trial_id']}:{','.join(missing)}"
        )
    return {node_id: registry[node_id] for node_id in sorted(node_ids)}


def _run_one(
    stage: Path,
    call: dict[str, Any],
    output: Path,
    *,
    api_key: str,
    plan: dict[str, Any],
) -> dict[str, Any]:
    final_root = output / call["trial_id"]
    if final_root.exists():
        raise FileExistsError(final_root)
    temporary_root = output / f".{call['trial_id']}.partial-{uuid.uuid4().hex}"
    temporary_root.mkdir()
    prompt_path = stage / call["prompt_path"]
    prompt = prompt_path.read_text(encoding="utf-8")
    started = time.perf_counter()
    response, attempts, request_payload = _call_patch_completion(
        api_key=api_key,
        base_url=plan["base_url"],
        model=plan["model"],
        system_prompt=(
            "You repair or safely generalize one visible Agent Skill package. You have exactly one "
            "available function and must call submit_skill_patch exactly once, including an empty "
            "edits list when no change is needed. Never request hidden evaluation artifacts."
        ),
        user_prompt=prompt,
        timeout=int(plan["timeout"]),
        max_attempts=int(plan["max_attempts"]),
    )
    if response is not None:
        write_json(temporary_root / "provider_response.json", response)
    content_error: str | None = None
    response_transport_mode: str | None = None
    try:
        if response is None:
            content = ""
        else:
            content, response_transport_mode = _patch_arguments(response)
    except (KeyError, TypeError, ValueError) as exc:
        content = ""
        content_error = f"{type(exc).__name__}:{exc}"
    (temporary_root / "raw_response.txt").write_text(content, encoding="utf-8")
    parse_error = content_error
    application: dict[str, Any] | None = None
    candidate_hashes: dict[str, str] | None = None
    failure_class = "protocol_invalid" if content_error else None
    normalized_content = None
    normalization = None
    if response is not None and content_error is None:
        public_package = stage / "public" / "cases" / call["case_id"] / "package"
        try:
            normalized_content, normalization = normalize_patch_arguments(
                content, public_package
            )
            (temporary_root / "normalized_response.txt").write_text(
                normalized_content, encoding="utf-8"
            )
            write_json(
                temporary_root / "PROTOCOL_NORMALIZATION.json", normalization
            )
        except (json.JSONDecodeError, FileNotFoundError, TypeError, ValueError) as exc:
            parse_error = f"{type(exc).__name__}:{exc}"
            failure_class = "protocol_invalid"
    if normalized_content is not None:
        try:
            candidate_package = temporary_root / "candidate" / "package"
            application = parse_and_apply_package_edits(
                normalized_content,
                public_package,
                candidate_package,
                structural_nodes=_structural_nodes_for_call(stage, call),
                request=(
                    stage / "public" / "cases" / call["case_id"] / "REQUEST.md"
                ).read_text(encoding="utf-8"),
            )
            candidate_hashes = hash_tree(candidate_package)
            write_json(temporary_root / "RESPONSE_APPLICATION.json", application)
        except (
            json.JSONDecodeError,
            FileNotFoundError,
            KeyError,
            TypeError,
            ValueError,
            SyntaxError,
        ) as exc:
            parse_error = f"{type(exc).__name__}:{exc}"
            failure_class = classify_candidate_failure(exc)
            shutil.rmtree(temporary_root / "candidate", ignore_errors=True)
    status = (
        "provider_unavailable_no_candidate"
        if response is None
        else "invalid_response_frozen"
        if application is None
        else "candidate_frozen"
    )
    freeze = {
        "schema_version": SCHEMA_VERSION,
        "trial_id": call["trial_id"],
        "status": status,
        "created_at": _utc_now(),
        "plan_hash": plan["plan_hash"],
        "prompt_sha256": sha256_file(prompt_path),
        "raw_response_sha256": sha256_file(temporary_root / "raw_response.txt"),
        "normalized_response_sha256": sha256_file(
            temporary_root / "normalized_response.txt"
        )
        if normalized_content is not None
        else None,
        "protocol_normalization_sha256": sha256_file(
            temporary_root / "PROTOCOL_NORMALIZATION.json"
        )
        if normalization is not None
        else None,
        "provider_response_sha256": sha256_file(temporary_root / "provider_response.json")
        if response is not None
        else None,
        "response_application_sha256": sha256_file(
            temporary_root / "RESPONSE_APPLICATION.json"
        )
        if application is not None
        else None,
        "candidate_tree_hash": canonical_json_hash(candidate_hashes)
        if candidate_hashes is not None
        else None,
        "protocol_status": (
            normalization["status"]
            if normalization is not None
            else "protocol_invalid"
            if response is not None
            else "provider_unavailable"
        ),
        "failure_class": failure_class,
        "hidden_evaluation_loaded": False,
    }
    freeze["candidate_freeze_hash"] = canonical_json_hash(freeze)
    write_json(temporary_root / "CANDIDATE_FREEZE.json", freeze)
    record = {
        "schema_version": SCHEMA_VERSION,
        "trial_id": call["trial_id"],
        "case_id": call["case_id"],
        "condition": call["condition"],
        "repeat": 1,
        "status": status,
        "created_at": _utc_now(),
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "provider": plan["base_url"],
        "model": plan["model"],
        "transport_attempt_count": len(attempts),
        "completed_model_response_count": int(response is not None),
        "model_calls": int(response is not None),
        "attempts": attempts,
        "response_id": response.get("id") if response else None,
        "usage": _normalized_usage(response.get("usage")) if response else None,
        "request_hash": canonical_json_hash(request_payload),
        "prompt_sha256": call["prompt_sha256"],
        "parse_error": parse_error,
        "failure_class": failure_class,
        "protocol_status": (
            normalization["status"]
            if normalization is not None
            else "protocol_invalid"
            if response is not None
            else "provider_unavailable"
        ),
        "candidate_validation_status": (
            "valid" if application is not None else failure_class
        ),
        "protocol_correction_count": (
            normalization["correction_count"] if normalization is not None else None
        ),
        "response_transport_mode": response_transport_mode,
        "edit_count": application.get("edit_count") if application else None,
        "changed_paths": application.get("changed_paths") if application else None,
        "candidate_freeze_hash": freeze["candidate_freeze_hash"],
        "hidden_evaluation_loaded": False,
        "credential_persisted": False,
    }
    write_json(temporary_root / "RUN_RECORD.json", record)
    if not _credential_absent(temporary_root, api_key):
        raise RuntimeError("api_credential_persisted")
    os.replace(temporary_root, final_root)
    return record


def _existing_trial_ids(run_roots: Iterable[Path]) -> set[str]:
    ids: set[str] = set()
    for root in run_roots:
        if not root.exists():
            continue
        for record_path in root.glob("*/RUN_RECORD.json"):
            record = read_json(record_path)
            ids.add(str(record["trial_id"]))
    return ids


def run_stage(
    experiment_root: str | Path,
    output_root: str | Path,
    *,
    api_key: str,
    workers: int = 6,
    trial_ids: set[str] | None = None,
    exclude_run_roots: Iterable[str | Path] = (),
) -> dict[str, Any]:
    experiment = Path(experiment_root).resolve()
    stage = experiment / "stage"
    output = Path(output_root).resolve()
    if output.exists():
        raise FileExistsError(f"run_output_already_exists:{output}")
    preflight = read_json(experiment / "_audit" / "ZERO_CALL_PREFLIGHT.json")
    if preflight.get("status") != "pass" or not _embedded_hash_valid(preflight, "preflight_hash"):
        raise ValueError("valid_zero_call_preflight_required")
    validated = _validate_plan(stage)
    if not all(validated["checks"].values()):
        raise ValueError("stage_changed_after_preflight")
    plan = validated["plan"]
    calls = list(plan["calls"])
    if trial_ids is not None:
        known = {call["trial_id"] for call in calls}
        if not trial_ids <= known:
            raise ValueError("unknown_trial_ids")
        calls = [call for call in calls if call["trial_id"] in trial_ids]
    excluded = _existing_trial_ids(Path(root).resolve() for root in exclude_run_roots)
    calls = [call for call in calls if call["trial_id"] not in excluded]
    if not calls:
        raise ValueError("no_trials_selected")
    output.mkdir(parents=True)
    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(calls)))) as executor:
        futures = {
            executor.submit(
                _run_one,
                stage,
                call,
                output,
                api_key=api_key,
                plan=plan,
            ): call
            for call in calls
        }
        for future in as_completed(futures):
            records.append(future.result())
    records.sort(key=lambda row: row["trial_id"])
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "all_candidates_frozen"
        if all(row["status"] == "candidate_frozen" for row in records)
        else "completed_with_invalid_trials",
        "created_at": _utc_now(),
        "plan_hash": plan["plan_hash"],
        "selected_trial_count": len(records),
        "candidate_frozen_count": sum(row["status"] == "candidate_frozen" for row in records),
        "invalid_response_count": sum(row["status"] == "invalid_response_frozen" for row in records),
        "provider_unavailable_count": sum(
            row["status"] == "provider_unavailable_no_candidate" for row in records
        ),
        "transport_outcome_unknown_count": sum(
            any(attempt.get("status") == "transport_error" for attempt in row["attempts"])
            and row["status"] == "provider_unavailable_no_candidate"
            for row in records
        ),
        "transport_attempt_count": sum(row["transport_attempt_count"] for row in records),
        "completed_model_response_count": sum(row["completed_model_response_count"] for row in records),
        "model_calls": sum(row["model_calls"] for row in records),
        "usage": {
            key: sum(int((row.get("usage") or {}).get(key, 0)) for row in records)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "condition_counts": dict(Counter(row["condition"] for row in records)),
        "credential_persisted": False,
        "hidden_evaluation_loaded": False,
        "trials": records,
    }
    summary["run_hash"] = canonical_json_hash(summary)
    write_json(output / "BATCH_RUN_SUMMARY.json", summary)
    if not _credential_absent(output, api_key):
        raise RuntimeError("api_credential_persisted_in_batch")
    return summary


def recover_frozen_responses(
    experiment_root: str | Path,
    source_run_root: str | Path,
    output_root: str | Path,
) -> dict[str, Any]:
    experiment = Path(experiment_root).resolve()
    stage = experiment / "stage"
    source_root = Path(source_run_root).resolve()
    output = Path(output_root).resolve()
    if output.exists():
        raise FileExistsError(f"recovery_output_already_exists:{output}")
    preflight = read_json(experiment / "_audit" / "ZERO_CALL_PREFLIGHT.json")
    if preflight.get("status") != "pass" or not _embedded_hash_valid(preflight, "preflight_hash"):
        raise ValueError("valid_zero_call_preflight_required")
    validated = _validate_plan(stage)
    if not all(validated["checks"].values()):
        raise ValueError("stage_changed_before_response_recovery")
    plan = validated["plan"]
    calls = {call["trial_id"]: call for call in plan["calls"]}
    source_summary = read_json(source_root / "BATCH_RUN_SUMMARY.json")
    if not _embedded_hash_valid(source_summary, "run_hash"):
        raise ValueError("source_run_summary_hash_invalid")
    source_records = sorted(source_root.glob("*/RUN_RECORD.json"))
    if not source_records:
        raise ValueError("no_source_responses_to_recover")
    output.mkdir(parents=True)
    records: list[dict[str, Any]] = []
    for source_record_path in source_records:
        source_record = read_json(source_record_path)
        trial_id = source_record["trial_id"]
        if trial_id not in calls:
            raise ValueError(f"source_trial_not_in_new_plan:{trial_id}")
        call = calls[trial_id]
        source_trial = source_record_path.parent
        if source_record.get("completed_model_response_count") != 1:
            raise ValueError(f"source_trial_has_no_completed_response:{trial_id}")
        if source_record.get("model") != plan["model"] or source_record.get("provider") != plan["base_url"]:
            raise ValueError(f"source_provider_or_model_mismatch:{trial_id}")
        if source_record.get("prompt_sha256") != call["prompt_sha256"]:
            raise ValueError(f"source_prompt_hash_mismatch:{trial_id}")
        final_trial = output / trial_id
        temporary = output / f".{trial_id}.partial-{uuid.uuid4().hex}"
        temporary.mkdir()
        shutil.copy2(source_trial / "raw_response.txt", temporary / "raw_response.txt")
        if (source_trial / "provider_response.json").is_file():
            shutil.copy2(
                source_trial / "provider_response.json",
                temporary / "provider_response.json",
            )
        content = (temporary / "raw_response.txt").read_text(encoding="utf-8")
        parse_error: str | None = None
        application: dict[str, Any] | None = None
        candidate_hashes: dict[str, str] | None = None
        failure_class = None
        normalized_content = None
        normalization = None
        public_package = stage / "public" / "cases" / call["case_id"] / "package"
        try:
            normalized_content, normalization = normalize_patch_arguments(
                content, public_package
            )
            (temporary / "normalized_response.txt").write_text(
                normalized_content, encoding="utf-8"
            )
            write_json(temporary / "PROTOCOL_NORMALIZATION.json", normalization)
        except (json.JSONDecodeError, FileNotFoundError, TypeError, ValueError) as exc:
            parse_error = f"{type(exc).__name__}:{exc}"
            failure_class = "protocol_invalid"
        try:
            if normalized_content is None:
                raise ValueError("protocol_normalization_failed")
            candidate_package = temporary / "candidate" / "package"
            application = parse_and_apply_package_edits(
                normalized_content,
                public_package,
                candidate_package,
                structural_nodes=_structural_nodes_for_call(stage, call),
                request=(
                    stage / "public" / "cases" / call["case_id"] / "REQUEST.md"
                ).read_text(encoding="utf-8"),
            )
            candidate_hashes = hash_tree(candidate_package)
            write_json(temporary / "RESPONSE_APPLICATION.json", application)
        except (
            json.JSONDecodeError,
            FileNotFoundError,
            KeyError,
            TypeError,
            ValueError,
            SyntaxError,
        ) as exc:
            if parse_error is None:
                parse_error = f"{type(exc).__name__}:{exc}"
                failure_class = classify_candidate_failure(exc)
            shutil.rmtree(temporary / "candidate", ignore_errors=True)
        status = "candidate_frozen" if application is not None else "invalid_response_frozen"
        freeze = {
            "schema_version": SCHEMA_VERSION,
            "trial_id": trial_id,
            "status": status,
            "created_at": _utc_now(),
            "plan_hash": plan["plan_hash"],
            "prompt_sha256": call["prompt_sha256"],
            "raw_response_sha256": sha256_file(temporary / "raw_response.txt"),
            "normalized_response_sha256": sha256_file(
                temporary / "normalized_response.txt"
            )
            if normalized_content is not None
            else None,
            "protocol_normalization_sha256": sha256_file(
                temporary / "PROTOCOL_NORMALIZATION.json"
            )
            if normalization is not None
            else None,
            "provider_response_sha256": sha256_file(temporary / "provider_response.json"),
            "response_application_sha256": sha256_file(
                temporary / "RESPONSE_APPLICATION.json"
            )
            if application is not None
            else None,
            "candidate_tree_hash": canonical_json_hash(candidate_hashes)
            if candidate_hashes is not None
            else None,
            "protocol_status": (
                normalization["status"]
                if normalization is not None
                else "protocol_invalid"
            ),
            "failure_class": failure_class,
            "hidden_evaluation_loaded": False,
            "recovered_from_frozen_response": {
                "source_run_root": str(source_root),
                "source_run_record_sha256": sha256_file(source_record_path),
                "source_response_id": source_record.get("response_id"),
            },
        }
        freeze["candidate_freeze_hash"] = canonical_json_hash(freeze)
        write_json(temporary / "CANDIDATE_FREEZE.json", freeze)
        record = {
            **{key: value for key, value in source_record.items() if key != "parse_error"},
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "created_at": _utc_now(),
            "parse_error": parse_error,
            "failure_class": failure_class,
            "protocol_status": (
                normalization["status"]
                if normalization is not None
                else "protocol_invalid"
            ),
            "candidate_validation_status": (
                "valid" if application is not None else failure_class
            ),
            "protocol_correction_count": (
                normalization["correction_count"]
                if normalization is not None
                else None
            ),
            "edit_count": application.get("edit_count") if application else None,
            "changed_paths": application.get("changed_paths") if application else None,
            "candidate_freeze_hash": freeze["candidate_freeze_hash"],
            "hidden_evaluation_loaded": False,
            "response_recovery": True,
            "new_model_calls": 0,
            "inherited_completed_model_responses": 1,
        }
        write_json(temporary / "RUN_RECORD.json", record)
        os.replace(temporary, final_trial)
        records.append(record)
    records.sort(key=lambda row: row["trial_id"])
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "all_candidates_frozen"
        if all(row["status"] == "candidate_frozen" for row in records)
        else "completed_with_invalid_trials",
        "created_at": _utc_now(),
        "plan_hash": plan["plan_hash"],
        "selected_trial_count": len(records),
        "candidate_frozen_count": sum(row["status"] == "candidate_frozen" for row in records),
        "invalid_response_count": sum(row["status"] != "candidate_frozen" for row in records),
        "provider_unavailable_count": 0,
        "transport_outcome_unknown_count": 0,
        "transport_attempt_count": sum(row["transport_attempt_count"] for row in records),
        "completed_model_response_count": len(records),
        "model_calls": len(records),
        "new_model_calls": 0,
        "inherited_completed_model_responses": len(records),
        "usage": {
            key: sum(int((row.get("usage") or {}).get(key, 0)) for row in records)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "condition_counts": dict(Counter(row["condition"] for row in records)),
        "credential_persisted": False,
        "hidden_evaluation_loaded": False,
        "source_run_root": str(source_root),
        "trials": records,
    }
    summary["run_hash"] = canonical_json_hash(summary)
    write_json(output / "BATCH_RUN_SUMMARY.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare and run role-aware AST GPT-5.5 conditions on SkillScriptBench v0.65."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--benchmark-root", type=Path, required=True)
    prepare.add_argument("--experiment-root", type=Path, required=True)
    prepare.add_argument("--expected-freeze-hash", required=True)
    prepare.add_argument("--model", default="gpt-5.5")
    prepare.add_argument("--base-url", default="https://api.openlux.ai/v1")
    prepare.add_argument("--timeout", type=int, default=600)
    prepare.add_argument("--max-attempts", type=int, default=2)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--experiment-root", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--experiment-root", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument("--workers", type=int, default=6)
    run.add_argument("--trial-ids-file", type=Path)
    run.add_argument("--exclude-run-root", type=Path, action="append", default=[])
    recover = commands.add_parser("recover-responses")
    recover.add_argument("--experiment-root", type=Path, required=True)
    recover.add_argument("--source-run-root", type=Path, required=True)
    recover.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "prepare":
        result = prepare_stage(
            args.benchmark_root,
            args.experiment_root,
            expected_freeze_hash=args.expected_freeze_hash,
            model=args.model,
            base_url=args.base_url,
            timeout=args.timeout,
            max_attempts=args.max_attempts,
        )
        output = {
            "status": result["prepare"]["status"],
            "case_count": result["prepare"]["case_count"],
            "call_count": result["prepare"]["call_count"],
            "exact_recall_at_1": result["localization"]["exact_recall_at_1_count"],
            "exact_recall_at_3": result["localization"]["exact_recall_at_3_count"],
            "mrr": result["localization"]["mrr"],
            "family_top1_correct": result["localization"]["family_top1_correct_count"],
            "parameter_top1_correct": result["localization"]["parameter_top1_correct_count"],
            "abstain_count": result["localization"]["abstain_count"],
            "model_calls": 0,
        }
    elif args.command == "preflight":
        result = zero_call_preflight(args.experiment_root)
        output = {
            "status": result["status"],
            "case_count": result["case_count"],
            "call_count": result["call_count"],
            "model_calls": 0,
        }
    elif args.command == "run":
        selected = None
        if args.trial_ids_file:
            payload = read_json(args.trial_ids_file)
            selected = set(payload.get("trial_ids", payload))
        api_key = getpass.getpass("OpenLux API key: ")
        if not api_key:
            raise ValueError("api_key_required")
        result = run_stage(
            args.experiment_root,
            args.output_root,
            api_key=api_key,
            workers=args.workers,
            trial_ids=selected,
            exclude_run_roots=args.exclude_run_root,
        )
        output = {
            "status": result["status"],
            "selected_trial_count": result["selected_trial_count"],
            "candidate_frozen_count": result["candidate_frozen_count"],
            "invalid_response_count": result["invalid_response_count"],
            "provider_unavailable_count": result["provider_unavailable_count"],
            "model_calls": result["model_calls"],
            "usage": result["usage"],
        }
    else:
        result = recover_frozen_responses(
            args.experiment_root,
            args.source_run_root,
            args.output_root,
        )
        output = {
            "status": result["status"],
            "selected_trial_count": result["selected_trial_count"],
            "candidate_frozen_count": result["candidate_frozen_count"],
            "invalid_response_count": result["invalid_response_count"],
            "model_calls": result["model_calls"],
            "new_model_calls": result["new_model_calls"],
            "usage": result["usage"],
        }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if output["status"] in {
        "ready_for_zero_call_preflight",
        "pass",
        "all_candidates_frozen",
        "completed_with_invalid_trials",
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
