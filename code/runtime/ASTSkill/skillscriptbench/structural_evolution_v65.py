from __future__ import annotations

import ast
import io
import math
import re
import tokenize
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from skillscriptbench.io_utils import (
    canonical_json_hash,
    copy_tree_clean,
    sha256_bytes,
    sha256_file,
)


ROLE_FAMILY = {
    "selection_index": "fixed_cardinality",
    "cardinality_limit": "fixed_cardinality",
    "comparison_boundary": "hardcoded_threshold",
    "schema_key": "fixed_schema",
    "delimiter": "fixed_format_path",
    "format_literal": "fixed_format_path",
    "path_suffix": "fixed_format_path",
    "marker": "fixed_format_path",
}

ROLE_PARAMETER = {
    "selection_index": "index",
    "cardinality_limit": "limit",
    "comparison_boundary": "threshold",
    "schema_key": "field",
    "delimiter": "delimiter",
    "format_literal": "format",
    "path_suffix": "suffix",
    "marker": "marker",
    "clamp_bound": "bound",
}

FAMILY_TERMS = {
    "fixed_cardinality": {"cardinality", "index", "limit", "position", "rank", "selected"},
    "hardcoded_threshold": {"boundary", "cutoff", "decision", "threshold", "tolerance"},
    "fixed_schema": {"field", "key", "mapping", "schema"},
    "fixed_format_path": {"delimiter", "format", "marker", "path", "suffix"},
}

ROLE_TERMS = {
    "selection_index": {"index", "position", "rank", "selected"},
    "cardinality_limit": {"cardinality", "count", "limit", "maximum"},
    "comparison_boundary": {"boundary", "cutoff", "decision", "threshold"},
    "schema_key": {"field", "key", "mapping", "schema"},
    "delimiter": {"delimiter", "separator", "split", "join"},
    "format_literal": {"format", "json", "csv", "yaml"},
    "path_suffix": {"extension", "path", "suffix"},
    "marker": {"marker", "prefix", "suffix", "truncation"},
    "clamp_bound": {"bound", "clamp", "maximum", "minimum"},
}


@dataclass(frozen=True)
class RequestContract:
    function: str | None
    parameter: str | None
    default: Any
    has_default: bool
    family: str | None


def _tokens(text: str) -> set[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return set(re.findall(r"[A-Za-z0-9_]+", expanded.lower()))


def _literal_from_text(value: str) -> tuple[Any, bool]:
    try:
        return ast.literal_eval(value), True
    except (SyntaxError, ValueError):
        stripped = value.strip()
        if re.fullmatch(r"-?\d+", stripped):
            return int(stripped), True
        if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)", stripped):
            return float(stripped), True
        return None, False


def parse_request_contract(request: str) -> RequestContract:
    function_match = re.search(r"public\s+`([A-Za-z_][A-Za-z0-9_]*)`", request)
    parameter_match = re.search(
        r"optional\s+`([A-Za-z_][A-Za-z0-9_]*)`\s+parameter", request
    )
    default_match = re.search(
        r"using\s+(`(?:[^`]|``)+`)\s+as\s+the\s+compatibility\s+default",
        request,
        flags=re.IGNORECASE,
    )
    default: Any = None
    has_default = False
    if default_match:
        default, has_default = _literal_from_text(default_match.group(1)[1:-1])
    terms = _tokens(request)
    family_scores = {
        family: len(terms & family_terms) for family, family_terms in FAMILY_TERMS.items()
    }
    best_family = max(family_scores, key=family_scores.get)
    family = best_family if family_scores[best_family] else None
    return RequestContract(
        function=function_match.group(1) if function_match else None,
        parameter=parameter_match.group(1) if parameter_match else None,
        default=default,
        has_default=has_default,
        family=family,
    )


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def _nearest_function(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parents.get(current)
    return None


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        parent = node.func.value
        prefix = parent.id if isinstance(parent, ast.Name) else ""
        return f"{prefix}.{node.func.attr}" if prefix else node.func.attr
    return ""


def _byte_offsets(
    source: str,
    line: int,
    column: int,
    end_line: int,
    end_column: int,
) -> tuple[int, int]:
    lines = source.splitlines(keepends=True)
    if not (1 <= line <= end_line <= len(lines)):
        raise ValueError("source_span_out_of_range")
    encoded_lines = [value.encode("utf-8") for value in lines]
    start = sum(len(value) for value in encoded_lines[: line - 1]) + column
    end = sum(len(value) for value in encoded_lines[: end_line - 1]) + end_column
    if not (0 <= start < end <= len(source.encode("utf-8"))):
        raise ValueError("source_span_invalid")
    return start, end


def _span(node: ast.AST) -> dict[str, int]:
    return {
        "start_line": int(node.lineno),
        "start_column": int(node.col_offset),
        "end_line": int(getattr(node, "end_lineno", node.lineno)),
        "end_column": int(getattr(node, "end_col_offset", node.col_offset)),
    }


def _source_for_span(source: str, span: dict[str, int]) -> str:
    start, end = _byte_offsets(
        source,
        span["start_line"],
        span["start_column"],
        span["end_line"],
        span["end_column"],
    )
    return source.encode("utf-8")[start:end].decode("utf-8")


def _line_window(source: str, line: int, radius: int = 4) -> str:
    values = source.splitlines()
    begin = max(1, line - radius)
    finish = min(len(values), line + radius)
    return "\n".join(
        f"{number}: {values[number - 1]}" for number in range(begin, finish + 1)
    )


def _compare_operator(compare: ast.Compare, literal: ast.Constant) -> str:
    if compare.left is literal and compare.ops:
        return type(compare.ops[0]).__name__
    for index, comparator in enumerate(compare.comparators):
        if comparator is literal and index < len(compare.ops):
            return type(compare.ops[index]).__name__
    return "Compare"


def _compare_uses_len(compare: ast.Compare) -> bool:
    return any(
        isinstance(child, ast.Call) and _call_name(child).rsplit(".", 1)[-1] == "len"
        for child in ast.walk(compare)
    )


def _if_chain_length(compare: ast.Compare, parents: dict[ast.AST, ast.AST]) -> int:
    current: ast.AST | None = parents.get(compare)
    while current is not None and not isinstance(current, ast.If):
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return 0
        current = parents.get(current)
    if not isinstance(current, ast.If):
        return 0
    root = current
    parent = parents.get(root)
    while isinstance(parent, ast.If) and root in parent.orelse:
        root = parent
        parent = parents.get(root)
    length = 0
    cursor: ast.If | None = root
    while cursor is not None:
        length += 1
        cursor = cursor.orelse[0] if cursor.orelse and isinstance(cursor.orelse[0], ast.If) else None
    return length


def _semantic_role(
    node: ast.Constant, parents: dict[ast.AST, ast.AST]
) -> tuple[str | None, ast.AST | None, dict[str, Any]]:
    parent = parents.get(node)
    value = node.value
    if isinstance(value, bool) or value is None:
        return None, None, {}
    if isinstance(parent, ast.Compare) and isinstance(value, (int, float)):
        role = "cardinality_limit" if _compare_uses_len(parent) else "comparison_boundary"
        return role, parent, {
            "operator": _compare_operator(parent, node),
            "compare_chain_length": len(parent.ops),
            "if_chain_length": _if_chain_length(parent, parents),
        }
    if isinstance(parent, ast.Subscript) and parent.slice is node:
        if isinstance(value, int):
            ancestor = parents.get(parent)
            while ancestor is not None and not isinstance(
                ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                if isinstance(ancestor, (ast.Lambda, ast.comprehension)):
                    return None, None, {}
                ancestor = parents.get(ancestor)
            return "selection_index", parent, {}
        if isinstance(value, str):
            return "schema_key", parent, {}
    if isinstance(parent, ast.Slice) and parent.upper is node and isinstance(value, int):
        grandparent = parents.get(parent)
        return "cardinality_limit", grandparent or parent, {"slice_bound": "upper"}
    if isinstance(parent, ast.Dict) and isinstance(value, str) and node in parent.keys:
        return "schema_key", parent, {"mapping_role": "literal_key"}
    if isinstance(parent, ast.Call):
        terminal = _call_name(parent).rsplit(".", 1)[-1]
        if terminal in {"min", "max"} and isinstance(value, (int, float)):
            return "clamp_bound", parent, {"call": terminal}
        if terminal == "get" and parent.args and parent.args[0] is node and isinstance(value, str):
            return "schema_key", parent, {"mapping_role": "get_key"}
        if terminal in {"join", "split", "rsplit"} and isinstance(value, str):
            return "delimiter", parent, {"call": terminal}
        if isinstance(value, str) and value.lower() in {"json", "csv", "yaml", "yml", "xml", "md", "markdown"}:
            return "format_literal", parent, {"call": terminal}
    if isinstance(value, str) and re.fullmatch(r"\.[A-Za-z0-9]{1,12}", value):
        return "path_suffix", parent or node, {}
    if (
        isinstance(value, str)
        and isinstance(parent, ast.BinOp)
        and isinstance(parent.op, ast.Add)
        and value in {"...", "---", "<<<", ">>>"}
    ):
        return "marker", parent, {"binary_operator": "Add"}
    return None, None, {}


def _function_site(path: str, source: str, function: ast.AST) -> dict[str, Any]:
    function_span = _span(function)
    header_line = source.splitlines()[function_span["start_line"] - 1]
    function_source = _source_for_span(source, function_span)
    identity = {
        "path": path,
        "role": "function_scope",
        "symbol": function.name,
        "span": function_span,
    }
    return {
        "site_id": f"node-{canonical_json_hash(identity)[:16]}",
        "path": path,
        "language": "python",
        "backend": "python_ast_v65",
        "node_type": type(function).__name__,
        "role": "function_scope",
        "family": None,
        "parameter_candidate": None,
        "default": None,
        "span": function_span,
        "parent_span": function_span,
        "line": function_span["start_line"],
        "end_line": function_span["start_line"],
        "column": function_span["start_column"],
        "end_column": len(header_line.encode("utf-8")),
        "symbol": function.name,
        "target_source": function_source,
        "observed_source": header_line.strip(),
        "window": _line_window(source, function_span["start_line"]),
        "node_source_sha256": sha256_bytes(function_source.encode("utf-8")),
        "parent_source_sha256": sha256_bytes(header_line.encode("utf-8")),
        "facts": {},
    }


def extract_python_role_sites(path: str, source: str) -> list[dict[str, Any]]:
    tree = ast.parse(source, filename=path)
    parents = _parent_map(tree)
    rows: list[dict[str, Any]] = []
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    function_ids: dict[str, str] = {}
    for function in functions:
        record = _function_site(path, source, function)
        rows.append(record)
        function_ids[function.name] = record["site_id"]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not hasattr(node, "lineno"):
            continue
        role, semantic_parent, facts = _semantic_role(node, parents)
        if role is None or semantic_parent is None:
            continue
        function = _nearest_function(node, parents)
        symbol = function.name if function is not None else "<module>"
        node_span = _span(node)
        parent_span = _span(semantic_parent)
        target_source = _source_for_span(source, node_span)
        parent_source = _source_for_span(source, parent_span)
        family = ROLE_FAMILY.get(role)
        parameter = ROLE_PARAMETER[role]
        identity = {
            "path": path,
            "role": role,
            "symbol": symbol,
            "span": node_span,
            "target_source": target_source,
        }
        rows.append(
            {
                "site_id": f"node-{canonical_json_hash(identity)[:16]}",
                "function_node_id": function_ids.get(symbol),
                "path": path,
                "language": "python",
                "backend": "python_ast_v65",
                "node_type": type(node).__name__,
                "semantic_node_type": type(semantic_parent).__name__,
                "role": role,
                "family": family,
                "parameter_candidate": parameter,
                "default": node.value,
                "span": node_span,
                "parent_span": parent_span,
                "line": node_span["start_line"],
                "end_line": node_span["end_line"],
                "column": node_span["start_column"],
                "end_column": node_span["end_column"],
                "symbol": symbol,
                "target_source": target_source,
                "observed_source": parent_source[:600],
                "window": _line_window(source, node_span["start_line"]),
                "node_source_sha256": sha256_bytes(target_source.encode("utf-8")),
                "parent_source_sha256": sha256_bytes(parent_source.encode("utf-8")),
                "facts": {
                    **facts,
                    "literal_type": type(node.value).__name__,
                    "literal_value": node.value,
                    "semantic_expression": parent_source[:600],
                    "semantic_ast": ast.dump(
                        semantic_parent, include_attributes=False
                    ),
                },
            }
        )
    unique = {row["site_id"]: row for row in rows}
    return sorted(
        unique.values(),
        key=lambda row: (
            row["path"],
            int(row["line"]),
            int(row["column"]),
            row["role"],
        ),
    )


def extract_markdown_sections(source: str) -> list[dict[str, Any]]:
    lines = source.splitlines()
    headings: list[tuple[int, int, str]] = []
    fenced = False
    fence_marker = ""
    for number, line in enumerate(lines, start=1):
        fence = re.match(r"^\s*(```+|~~~+)", line)
        if fence:
            marker = fence.group(1)[0]
            if not fenced:
                fenced = True
                fence_marker = marker
            elif marker == fence_marker:
                fenced = False
            continue
        if fenced:
            continue
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            headings.append((number, len(match.group(1)), match.group(2)))
    rows: list[dict[str, Any]] = []
    for index, (line, level, title) in enumerate(headings):
        end_line = len(lines)
        for next_line, next_level, _next_title in headings[index + 1 :]:
            if next_level <= level:
                end_line = next_line - 1
                break
        content = "\n".join(lines[line - 1 : end_line])
        identity = {"line": line, "level": level, "title": title, "content": content}
        rows.append(
            {
                "id": f"md-{canonical_json_hash(identity)[:16]}",
                "type": "markdown_section",
                "title": title,
                "level": level,
                "span": {"start_line": line, "end_line": end_line},
                "source_sha256": sha256_bytes(content.encode("utf-8")),
            }
        )
    return rows


def rank_role_sites(
    sites: Iterable[dict[str, Any]], request: str
) -> tuple[list[dict[str, Any]], RequestContract]:
    contract = parse_request_contract(request)
    request_terms = _tokens(request)
    ranked: list[dict[str, Any]] = []
    for original in sites:
        if original["role"] == "function_scope":
            continue
        site = dict(original)
        score = 0.0
        reasons: list[str] = []
        if contract.function and site["symbol"] == contract.function:
            score += 40.0
            reasons.append("function")
        elif contract.function:
            score -= 18.0
        if contract.parameter and site.get("parameter_candidate") == contract.parameter:
            score += 20.0
            reasons.append("parameter")
        if contract.family and site.get("family") == contract.family:
            score += 18.0
            reasons.append("family")
        if contract.has_default and site.get("default") == contract.default:
            score += 18.0
            reasons.append("default")
        role_terms = ROLE_TERMS.get(str(site["role"]), set())
        role_overlap = request_terms & role_terms
        score += 3.0 * len(role_overlap)
        if role_overlap:
            reasons.append("role_terms")
        if contract.family == "hardcoded_threshold":
            if site["role"] == "comparison_boundary":
                score += 12.0
                reasons.append("decision_boundary")
            elif site["role"] == "clamp_bound":
                score -= 24.0
                reasons.append("clamp_penalty")
        if contract.family == "fixed_cardinality" and site["role"] in {
            "selection_index",
            "cardinality_limit",
        }:
            score += 8.0
        if contract.family == "fixed_schema" and site["role"] == "schema_key":
            score += 8.0
        if contract.family == "fixed_format_path" and site["role"] in {
            "delimiter",
            "format_literal",
            "path_suffix",
        }:
            score += 8.0
        site["localization_score"] = round(score, 6)
        site["score_reasons"] = reasons
        ranked.append(site)
    ranked.sort(
        key=lambda row: (
            -float(row["localization_score"]),
            row["path"],
            int(row["line"]),
            int(row["column"]),
            row["site_id"],
        )
    )
    return ranked, contract


def opportunity_decision(
    ranked: list[dict[str, Any]], *, minimum_score: float = 70.0, minimum_margin: float = 6.0
) -> dict[str, Any]:
    if not ranked:
        return {
            "decision": "ABSTAIN",
            "confidence": 0.0,
            "margin": 0.0,
            "reason": "no_structural_opportunity",
        }
    first = float(ranked[0]["localization_score"])
    second = float(ranked[1]["localization_score"]) if len(ranked) > 1 else 0.0
    margin = first - second
    confidence = 1.0 / (1.0 + math.exp(-((first - 60.0) / 12.0)))
    decision = "PROPOSE" if first >= minimum_score and margin >= minimum_margin else "ABSTAIN"
    reason = "high_confidence_unique_top1" if decision == "PROPOSE" else "low_score_or_margin"
    return {
        "decision": decision,
        "confidence": round(confidence, 6),
        "margin": round(margin, 6),
        "top1_score": round(first, 6),
        "top2_score": round(second, 6),
        "reason": reason,
    }


def _call_sites(package: Path, target_symbol: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((package / "scripts").rglob("*.py")) if (package / "scripts").is_dir() else []:
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node).rsplit(".", 1)[-1] != target_symbol:
                continue
            rows.append(
                {
                    "path": path.relative_to(package).as_posix(),
                    "line": int(node.lineno),
                    "expression": (ast.get_source_segment(source, node) or "")[:240],
                }
            )
    return rows[:20]


def _document_references(package: Path, symbol: str) -> list[dict[str, Any]]:
    skill = package / "SKILL.md"
    if not skill.is_file():
        return []
    rows = []
    for line, value in enumerate(skill.read_text(encoding="utf-8").splitlines(), start=1):
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])", value):
            rows.append({"path": "SKILL.md", "line": line, "source": value[:300]})
    return rows[:20]


def ambiguity_reasons(
    target: dict[str, Any], ranked: Iterable[dict[str, Any]], source: str
) -> list[str]:
    siblings = [
        site
        for site in ranked
        if site["site_id"] != target["site_id"]
        and site["path"] == target["path"]
        and site["symbol"] == target["symbol"]
    ]
    reasons: list[str] = []
    if any(site.get("default") == target.get("default") for site in siblings):
        reasons.append("duplicate_literal_value_in_symbol")
    if int(target.get("facts", {}).get("compare_chain_length", 0)) > 1:
        reasons.append("chained_comparison")
    if int(target.get("facts", {}).get("if_chain_length", 0)) > 1:
        reasons.append("threshold_branch_chain")
    if target["role"] == "selection_index" and any(
        site["role"] == "selection_index" for site in siblings
    ):
        reasons.append("neighboring_selection_index")
    default_text = str(target.get("default"))
    try:
        tree = ast.parse(source, filename=target["path"])
    except SyntaxError:
        tree = None
    if tree is not None:
        parents = _parent_map(tree)
        function = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == target["symbol"]
            ),
            None,
        )
        docstring_node = (
            function.body[0].value
            if function is not None
            and function.body
            and isinstance(function.body[0], ast.Expr)
            and isinstance(function.body[0].value, ast.Constant)
            and isinstance(function.body[0].value.value, str)
            else None
        )
        if isinstance(target.get("default"), (int, float)):
            pattern = re.compile(rf"(?<![0-9.]){re.escape(default_text)}(?![0-9.])")
        else:
            pattern = re.compile(re.escape(default_text))
        for node in ast.walk(function) if function is not None else []:
            if (
                not isinstance(node, ast.Constant)
                or not isinstance(node.value, str)
                or node is docstring_node
                or (
                    int(getattr(node, "lineno", -1)) == int(target["line"])
                    and int(getattr(node, "col_offset", -1)) == int(target["column"])
                )
            ):
                continue
            if pattern.search(node.value):
                reasons.append("default_literal_appears_in_output_text")
                break
    return sorted(set(reasons))


def build_evolution_spec(
    package: Path,
    ranked: list[dict[str, Any]],
    contract: RequestContract,
    *,
    selected: dict[str, Any] | None = None,
    force_decision: str | None = None,
) -> dict[str, Any]:
    decision = opportunity_decision(ranked)
    target = selected or (ranked[0] if ranked else None)
    if target is None:
        return {
            "schema_version": "0.65-evolution-spec-v1",
            "decision": "ABSTAIN",
            "confidence": 0.0,
            "reason": "no_target",
        }
    if force_decision:
        decision = {**decision, "decision": force_decision, "reason": "controlled_condition"}
    source = (package / target["path"]).read_text(encoding="utf-8")
    function_site = next(
        (
            site
            for site in extract_python_role_sites(target["path"], source)
            if site["site_id"] == target.get("function_node_id")
            and site["role"] == "function_scope"
        ),
        None,
    )
    if function_site is None:
        raise ValueError(f"function_scope_missing:{target['path']}:{target['symbol']}")
    anti_targets = [
        {
            "id": site["site_id"],
            "role": site["role"],
            "span": site["span"],
            "source": site["target_source"],
            "source_sha256": site["node_source_sha256"],
        }
        for site in ranked
        if site["site_id"] != target["site_id"]
        and site["path"] == target["path"]
        and site["symbol"] == target["symbol"]
    ][:8]
    parameter = contract.parameter or target.get("parameter_candidate")
    default = contract.default if contract.has_default else target.get("default")
    family = contract.family or target.get("family")
    ambiguity = ambiguity_reasons(target, ranked, source)
    properties = {
        "fixed_cardinality": [
            "Omitting the optional parameter preserves every old call result.",
            "A non-default value changes only the selected index or cardinality relation at target.",
        ],
        "hardcoded_threshold": [
            "Omitting the optional parameter preserves every old decision.",
            "The parameter replaces only the target comparison boundary.",
        ],
        "fixed_schema": [
            "Omitting the optional parameter preserves the original mapping key and output.",
            "A non-default field redirects only the target schema access or assignment.",
        ],
        "fixed_format_path": [
            "Omitting the optional parameter preserves the original representation or path.",
            "A non-default value changes only the requested format or path choice.",
        ],
    }.get(str(family), [])
    payload = {
        "schema_version": "0.65-evolution-spec-v1",
        **decision,
        "target": {
            "node_id": target["site_id"],
            "function_node_id": target.get("function_node_id"),
            "path": target["path"],
            "symbol": target["symbol"],
            "role": target["role"],
            "family": target.get("family"),
            "span": target["span"],
            "source": target["target_source"],
            "source_sha256": target["node_source_sha256"],
            "semantic_expression": target["observed_source"],
            "semantic_expression_sha256": target["parent_source_sha256"],
            "facts": target.get("facts") or {},
        },
        "function": {
            "node_id": function_site["site_id"],
            "path": function_site["path"],
            "symbol": function_site["symbol"],
            "role": function_site["role"],
            "span": function_site["span"],
            "signature": function_site["observed_source"],
            "source_sha256": function_site["node_source_sha256"],
        },
        "mutation": {
            "operation": "add_optional_parameter_and_replace_target_node",
            "parameter": parameter,
            "compatibility_default": default,
            "replace_target_with": parameter,
        },
        "compatibility_contract": {
            "public_symbol_preserved": target["symbol"],
            "old_calls_omit_parameter": True,
            "default_behavior_equal": True,
            "unrelated_source_must_remain_byte_identical": True,
        },
        "anti_targets": anti_targets,
        "affected_call_sites": _call_sites(package, target["symbol"]),
        "document_references": _document_references(package, target["symbol"]),
        "metamorphic_properties": properties,
        "ambiguity": {
            "status": "ambiguous" if ambiguity else "unambiguous",
            "reasons": ambiguity,
            "exact_oracle_primary_eligible": not ambiguity,
        },
    }
    payload["spec_id"] = f"spec-{canonical_json_hash(payload)[:16]}"
    return payload


def exact_target_match(site: dict[str, Any], label: dict[str, Any]) -> bool:
    if label.get("track") != "capability_evolution":
        return False
    target = label["target"]
    candidate = label.get("candidate") or {}
    return (
        site["path"] == target["source_path"]
        and site["symbol"] == target["function_name"]
        and int(site["line"]) == int(target["line"])
        and int(site["column"]) == int(target["column"])
        and site.get("family") == candidate.get("family")
        and site.get("parameter_candidate") == target.get("parameter")
        and site.get("default") == target.get("default")
    )


def select_gold_site(
    sites: Iterable[dict[str, Any]], label: dict[str, Any]
) -> dict[str, Any] | None:
    matches = [site for site in sites if exact_target_match(site, label)]
    if len(matches) > 1:
        raise ValueError(f"multiple_exact_gold_sites:{label['case_id']}")
    return matches[0] if matches else None


def select_matched_sham(
    ranked: list[dict[str, Any]], label: dict[str, Any]
) -> dict[str, Any] | None:
    eligible = [site for site in ranked if not exact_target_match(site, label)]
    if not eligible:
        return None
    target = label["target"]
    family = (label.get("candidate") or {}).get("family")
    eligible.sort(
        key=lambda site: (
            site["path"] != target["source_path"],
            site["symbol"] != target["function_name"],
            site.get("family") != family,
            site.get("role") != ranked[0].get("role") if ranked else True,
            abs(int(site["line"]) - int(target["line"]))
            if site["path"] == target["source_path"]
            else 10**9,
            -float(site["localization_score"]),
        )
    )
    return eligible[0]


def localization_audit_row(
    case_id: str,
    ranked: list[dict[str, Any]],
    label: dict[str, Any],
    decision: dict[str, Any],
) -> dict[str, Any]:
    rank = next(
        (index for index, site in enumerate(ranked, start=1) if exact_target_match(site, label)),
        None,
    )
    top = ranked[0] if ranked else None
    target = label.get("target") or {}
    candidate = label.get("candidate") or {}
    return {
        "case_id": case_id,
        "exact_rank": rank,
        "exact_recall_at_1": rank == 1,
        "exact_recall_at_3": rank is not None and rank <= 3,
        "mrr": 1.0 / rank if rank else 0.0,
        "family_top1_correct": bool(top) and top.get("family") == candidate.get("family"),
        "parameter_top1_correct": bool(top)
        and top.get("parameter_candidate") == target.get("parameter"),
        "role_top1": top.get("role") if top else None,
        "decision": decision,
        "top_sites": [
            {
                key: site.get(key)
                for key in (
                    "site_id",
                    "path",
                    "line",
                    "column",
                    "symbol",
                    "role",
                    "family",
                    "parameter_candidate",
                    "default",
                    "localization_score",
                )
            }
            for site in ranked[:5]
        ],
    }


def _absolute_offsets(source: str, span: dict[str, int]) -> tuple[int, int]:
    return _byte_offsets(
        source,
        span["start_line"],
        span["start_column"],
        span["end_line"],
        span["end_column"],
    )


def _token_position_to_byte_offset(source: str, position: tuple[int, int]) -> int:
    line, character_column = position
    lines = source.splitlines(keepends=True)
    prefix = "".join(lines[: line - 1]) + lines[line - 1][:character_column]
    return len(prefix.encode("utf-8"))


def _parameter_insertion(
    source: str, symbol: str, parameter_source: str
) -> tuple[int, bytes]:
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    def_index: int | None = None
    for index, token in enumerate(tokens):
        if token.type == tokenize.NAME and token.string in {"def", "async"}:
            search = index + 1
            if token.string == "async":
                if search >= len(tokens) or tokens[search].string != "def":
                    continue
                search += 1
            while search < len(tokens) and tokens[search].type in {
                tokenize.NL,
                tokenize.NEWLINE,
                tokenize.INDENT,
                tokenize.DEDENT,
            }:
                search += 1
            if search < len(tokens) and tokens[search].type == tokenize.NAME and tokens[search].string == symbol:
                def_index = search
                break
    if def_index is None:
        raise ValueError(f"function_symbol_not_found:{symbol}")
    open_index = next(
        (
            index
            for index in range(def_index + 1, len(tokens))
            if tokens[index].type == tokenize.OP and tokens[index].string == "("
        ),
        None,
    )
    if open_index is None:
        raise ValueError(f"function_open_paren_not_found:{symbol}")
    depth = 0
    close_index: int | None = None
    for index in range(open_index, len(tokens)):
        token = tokens[index]
        if token.type != tokenize.OP:
            continue
        if token.string == "(":
            depth += 1
        elif token.string == ")":
            depth -= 1
            if depth == 0:
                close_index = index
                break
    if close_index is None:
        raise ValueError(f"function_close_paren_not_found:{symbol}")
    open_token = tokens[open_index]
    close_token = tokens[close_index]
    open_end = _token_position_to_byte_offset(source, open_token.end)
    close_start = _token_position_to_byte_offset(source, close_token.start)
    encoded = source.encode("utf-8")
    interior = encoded[open_end:close_start].decode("utf-8")
    newline = "\r\n" if "\r\n" in source else "\n"
    if "\n" not in interior and "\r" not in interior:
        prefix = ", " if interior.strip() else ""
        return close_start, f"{prefix}{parameter_source}".encode("utf-8")
    close_line_start = _token_position_to_byte_offset(source, (close_token.start[0], 0))
    interior_prefix = encoded[open_end:close_line_start].decode("utf-8")
    nonempty_lines = [line for line in interior_prefix.splitlines() if line.strip()]
    if nonempty_lines:
        argument_indent = re.match(r"\s*", nonempty_lines[-1]).group(0)
    else:
        close_line = source.splitlines()[close_token.start[0] - 1]
        argument_indent = re.match(r"\s*", close_line).group(0) + "    "
    insertion = f"{argument_indent}{parameter_source},{newline}"
    return close_line_start, insertion.encode("utf-8")


def _validate_relative_path(
    relative: str, allowed_edit_paths: set[str] | None = None
) -> PurePosixPath:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"unsafe_edit_path:{relative}")
    if allowed_edit_paths is None:
        path_allowed = relative == "SKILL.md" or relative.startswith("scripts/")
    else:
        path_allowed = relative in allowed_edit_paths
    if not path_allowed:
        raise ValueError(f"edit_path_outside_scope:{relative}")
    return pure


def apply_structured_edits(
    edits: list[dict[str, Any]],
    source_package: Path,
    candidate_package: Path,
    *,
    node_registry: dict[str, dict[str, Any]],
    allowed_edit_paths: set[str] | None = None,
) -> dict[str, Any]:
    copy_tree_clean(source_package, candidate_package)
    by_path: dict[str, list[dict[str, Any]]] = {}
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise TypeError(f"edit_{index}_must_be_object")
        relative = str(edit.get("path") or "")
        pure = _validate_relative_path(relative, allowed_edit_paths)
        source_path = source_package / pure
        if not source_path.is_file():
            raise FileNotFoundError(f"edit_target_missing:{relative}")
        if edit.get("expected_file_sha256") != sha256_file(source_path):
            raise ValueError(f"edit_file_sha256_mismatch:{relative}")
        by_path.setdefault(relative, []).append({**edit, "index": index})
    receipts: list[dict[str, Any]] = []
    changed_paths: list[str] = []
    ignored_no_op_count = 0
    for relative, file_edits in sorted(by_path.items()):
        source_path = source_package / PurePosixPath(relative)
        target_path = candidate_package / PurePosixPath(relative)
        source = source_path.read_text(encoding="utf-8")
        encoded = source.encode("utf-8")
        positioned: list[tuple[int, int, bytes, dict[str, Any]]] = []
        append_chunks: list[tuple[bytes, dict[str, Any]]] = []
        for edit in file_edits:
            operation = edit.get("operation")
            replacement = edit.get("replacement")
            if not isinstance(replacement, str):
                raise TypeError(f"edit_{edit['index']}_replacement_must_be_string")
            if operation == "replace_node":
                node_id = str(edit.get("target_node_id") or "")
                node = node_registry.get(node_id)
                if node is None:
                    raise ValueError(f"edit_{edit['index']}_unknown_node:{node_id}")
                if node["path"] != relative:
                    raise ValueError(f"edit_{edit['index']}_node_path_mismatch")
                if edit.get("expected_node_sha256") != node["node_source_sha256"]:
                    raise ValueError(f"edit_{edit['index']}_node_sha256_mismatch")
                start, end = _absolute_offsets(source, node["span"])
                if sha256_bytes(encoded[start:end]) != node["node_source_sha256"]:
                    raise ValueError(f"edit_{edit['index']}_node_source_drift")
                replacement_bytes = replacement.encode("utf-8")
                if encoded[start:end] == replacement_bytes:
                    ignored_no_op_count += 1
                    continue
                positioned.append((start, end, replacement_bytes, edit))
            elif operation == "insert_parameter":
                node_id = str(edit.get("target_node_id") or "")
                node = node_registry.get(node_id)
                if node is None:
                    raise ValueError(f"edit_{edit['index']}_unknown_node:{node_id}")
                if node["path"] != relative:
                    raise ValueError(f"edit_{edit['index']}_node_path_mismatch")
                if edit.get("expected_node_sha256") != node["node_source_sha256"]:
                    raise ValueError(f"edit_{edit['index']}_node_sha256_mismatch")
                node_start, node_end = _absolute_offsets(source, node["span"])
                if sha256_bytes(encoded[node_start:node_end]) != node["node_source_sha256"]:
                    raise ValueError(f"edit_{edit['index']}_node_source_drift")
                symbol = str(edit.get("symbol") or node.get("symbol") or "")
                start, rendered = _parameter_insertion(source, symbol, replacement)
                positioned.append((start, start, rendered, edit))
            elif operation == "replace_lines":
                start_line = int(edit.get("start_line") or 0)
                end_line = int(edit.get("end_line") or 0)
                lines = source.splitlines(keepends=True)
                if not (1 <= start_line <= end_line <= len(lines)):
                    raise ValueError(f"edit_{edit['index']}_line_range_invalid")
                start = len("".join(lines[: start_line - 1]).encode("utf-8"))
                end = len("".join(lines[:end_line]).encode("utf-8"))
                newline = "\r\n" if "\r\n" in source else "\n"
                rendered = replacement.replace("\n", newline)
                if lines[end_line - 1].endswith(("\n", "\r\n")) and not rendered.endswith(newline):
                    rendered += newline
                replacement_bytes = rendered.encode("utf-8")
                if encoded[start:end] == replacement_bytes:
                    ignored_no_op_count += 1
                    continue
                positioned.append((start, end, replacement_bytes, edit))
            elif operation == "append_markdown":
                if relative != "SKILL.md":
                    raise ValueError(f"edit_{edit['index']}_append_markdown_wrong_path")
                newline = "\r\n" if "\r\n" in source else "\n"
                rendered = replacement.replace("\n", newline).strip()
                if rendered and rendered in source:
                    ignored_no_op_count += 1
                    continue
                append_chunks.append(
                    ((newline * 2 + rendered + newline).encode("utf-8"), edit)
                )
            else:
                raise ValueError(f"edit_{edit['index']}_operation_invalid:{operation}")
        positioned.sort(key=lambda row: (row[0], row[1]))
        for previous, current in zip(positioned, positioned[1:]):
            if previous[1] > current[0]:
                raise ValueError(f"overlapping_edits:{relative}")
        rendered = encoded
        for start, end, replacement, edit in reversed(positioned):
            old = encoded[start:end]
            rendered = rendered[:start] + replacement + rendered[end:]
            receipts.append(
                {
                    "index": edit["index"],
                    "path": relative,
                    "operation": edit["operation"],
                    "start_byte": start,
                    "end_byte": end,
                    "old_sha256": sha256_bytes(old),
                    "new_sha256": sha256_bytes(replacement),
                    "outside_source_preserved_by_construction": True,
                    "target_node_id": edit.get("target_node_id") or "",
                }
            )
        for chunk, edit in append_chunks:
            start = len(rendered)
            rendered += chunk
            receipts.append(
                {
                    "index": edit["index"],
                    "path": relative,
                    "operation": edit["operation"],
                    "start_byte": start,
                    "end_byte": start,
                    "old_sha256": sha256_bytes(b""),
                    "new_sha256": sha256_bytes(chunk),
                    "outside_source_preserved_by_construction": True,
                    "target_node_id": "",
                }
            )
        if rendered != encoded:
            target_path.write_bytes(rendered)
            changed_paths.append(relative)
    for relative in sorted(changed_paths):
        path = candidate_package / relative
        if path.suffix == ".py":
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        "status": "applied",
        "edit_count": len(receipts),
        "ignored_no_op_count": ignored_no_op_count,
        "changed_paths": changed_paths,
        "receipts": sorted(receipts, key=lambda row: row["index"]),
    }


def generic_structural_gate(
    baseline_package: Path,
    candidate_package: Path,
    request: str,
    changed_paths: Iterable[str],
) -> dict[str, Any]:
    contract = parse_request_contract(request)
    items: list[dict[str, str]] = []
    syntax_ok = True
    for relative in sorted(set(changed_paths)):
        path = candidate_package / relative
        if path.suffix != ".py":
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            syntax_ok = False
            items.append({"name": "python_parse", "status": "fail", "detail": str(exc)})
    if syntax_ok:
        items.append({"name": "python_parse", "status": "pass", "detail": "all_python_files_parse"})
    signature_ok = False
    default_ok = False
    if contract.function and contract.parameter:
        for path in sorted(candidate_package.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:
                continue
            function = next(
                (
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == contract.function
                ),
                None,
            )
            if function is None:
                continue
            positional = [*function.args.posonlyargs, *function.args.args]
            names = [argument.arg for argument in positional] + [
                argument.arg for argument in function.args.kwonlyargs
            ]
            signature_ok = contract.parameter in names
            defaults: dict[str, Any] = {}
            for argument, default_node in zip(positional[-len(function.args.defaults) :], function.args.defaults):
                try:
                    defaults[argument.arg] = ast.literal_eval(default_node)
                except (ValueError, TypeError):
                    pass
            for argument, default_node in zip(function.args.kwonlyargs, function.args.kw_defaults):
                if default_node is None:
                    continue
                try:
                    defaults[argument.arg] = ast.literal_eval(default_node)
                except (ValueError, TypeError):
                    pass
            default_ok = contract.has_default and defaults.get(contract.parameter) == contract.default
            break
    items.append(
        {
            "name": "optional_parameter_present",
            "status": "pass" if signature_ok else "fail",
            "detail": str(contract.parameter),
        }
    )
    items.append(
        {
            "name": "compatibility_default",
            "status": "pass" if default_ok else "fail",
            "detail": repr(contract.default),
        }
    )
    skill_text = (candidate_package / "SKILL.md").read_text(encoding="utf-8")
    doc_ok = bool(contract.function and contract.parameter and contract.function in skill_text and contract.parameter in skill_text)
    items.append(
        {
            "name": "document_sync",
            "status": "pass" if doc_ok else "fail",
            "detail": "SKILL.md mentions symbol and parameter",
        }
    )
    scope_ok = all(path == "SKILL.md" or path.startswith("scripts/") for path in changed_paths)
    items.append(
        {
            "name": "scope",
            "status": "pass" if scope_ok else "fail",
            "detail": ",".join(sorted(changed_paths)),
        }
    )
    failed = [item for item in items if item["status"] == "fail"]
    if failed:
        decision = "REJECT_STRUCTURALLY"
    elif not contract.function or not contract.parameter or not contract.has_default:
        decision = "ABSTAIN"
    else:
        decision = "ACCEPT_STRUCTURALLY"
    return {
        "decision": decision,
        "semantic_correctness": "unknown",
        "items": items,
        "baseline_skill_sha256": sha256_file(baseline_package / "SKILL.md"),
        "candidate_skill_sha256": sha256_file(candidate_package / "SKILL.md"),
    }


def _parameter_name_role(
    node: ast.Name,
    parents: dict[ast.AST, ast.AST],
    family: str,
) -> str | None:
    parent = parents.get(node)
    if isinstance(parent, ast.Compare) and node in [parent.left, *parent.comparators]:
        return "cardinality_limit" if _compare_uses_len(parent) else "comparison_boundary"
    if isinstance(parent, ast.Subscript) and parent.slice is node:
        return "schema_key" if family == "fixed_schema" else "selection_index"
    if isinstance(parent, ast.Slice) and parent.upper is node:
        return "cardinality_limit"
    if isinstance(parent, ast.Dict) and node in parent.keys:
        return "schema_key"
    if isinstance(parent, ast.Call):
        terminal = _call_name(parent).rsplit(".", 1)[-1]
        if terminal == "get" and parent.args and parent.args[0] is node:
            return "schema_key"
        if terminal in {"join", "split", "rsplit"}:
            return "delimiter"
        if family == "fixed_format_path":
            return "format_literal"
    if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Add):
        return "marker" if family == "fixed_format_path" else None
    return None


def structural_generalization_evidence(
    baseline_package: Path,
    candidate_package: Path,
    label: dict[str, Any],
) -> dict[str, Any]:
    target = label["target"]
    family = (label.get("candidate") or {}).get("family")
    relative = str(target["source_path"])
    baseline_source = (baseline_package / relative).read_text(encoding="utf-8")
    candidate_source = (candidate_package / relative).read_text(encoding="utf-8")
    baseline_sites = extract_python_role_sites(relative, baseline_source)
    gold = select_gold_site(baseline_sites, label)
    if gold is None:
        return {
            "status": "ABSTAIN",
            "reason": "gold_site_not_recovered_from_frozen_baseline",
        }
    try:
        candidate_tree = ast.parse(candidate_source, filename=relative)
    except SyntaxError as exc:
        return {"status": "fail", "reason": f"candidate_parse_error:{exc}"}
    function = next(
        (
            node
            for node in ast.walk(candidate_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == target["function_name"]
        ),
        None,
    )
    if function is None:
        return {"status": "fail", "reason": "public_function_missing"}
    positional = [*function.args.posonlyargs, *function.args.args]
    parameter_names = [argument.arg for argument in positional] + [
        argument.arg for argument in function.args.kwonlyargs
    ]
    parameter = str(target["parameter"])
    signature_present = parameter in parameter_names
    defaults: dict[str, Any] = {}
    for argument, default_node in zip(
        positional[-len(function.args.defaults) :], function.args.defaults
    ):
        try:
            defaults[argument.arg] = ast.literal_eval(default_node)
        except (TypeError, ValueError):
            pass
    for argument, default_node in zip(function.args.kwonlyargs, function.args.kw_defaults):
        if default_node is None:
            continue
        try:
            defaults[argument.arg] = ast.literal_eval(default_node)
        except (TypeError, ValueError):
            pass
    default_preserved = defaults.get(parameter) == target.get("default")
    parents = _parent_map(candidate_tree)
    observed_roles = sorted(
        {
            role
            for node in ast.walk(function)
            if isinstance(node, ast.Name) and node.id == parameter
            for role in [_parameter_name_role(node, parents, str(family))]
            if role is not None
        }
    )
    target_role_used = gold["role"] in observed_roles

    anti_sites = [
        site
        for site in baseline_sites
        if site["role"] != "function_scope"
        and site["path"] == relative
        and site["symbol"] == target["function_name"]
        and not exact_target_match(site, label)
    ]
    def anti_key(site: dict[str, Any]) -> str:
        return canonical_json_hash(
            {
                "role": site.get("role"),
                "family": site.get("family"),
                "literal_type": (site.get("facts") or {}).get("literal_type"),
                "literal_value": site.get("default"),
            }
        )

    expected_anti = Counter(anti_key(site) for site in anti_sites)
    candidate_sites = [
        site
        for site in extract_python_role_sites(relative, candidate_source)
        if site["role"] != "function_scope"
        and site["symbol"] == target["function_name"]
    ]
    candidate_nodes = Counter(anti_key(site) for site in candidate_sites)
    anti_total = sum(expected_anti.values())
    anti_preserved = sum(
        min(count, candidate_nodes.get(node_dump, 0))
        for node_dump, count in expected_anti.items()
    )
    anti_target_preserved = anti_preserved == anti_total
    skill_text = (candidate_package / "SKILL.md").read_text(encoding="utf-8")
    document_sync = target["function_name"] in skill_text and parameter in skill_text
    status = (
        "pass"
        if signature_present
        and default_preserved
        and target_role_used
        and anti_target_preserved
        and document_sync
        else "fail"
    )
    return {
        "status": status,
        "signature_present": signature_present,
        "default_preserved": default_preserved,
        "expected_role": gold["role"],
        "observed_parameter_roles": observed_roles,
        "target_role_used": target_role_used,
        "anti_target_total": anti_total,
        "anti_target_preserved_count": anti_preserved,
        "anti_target_preserved": anti_target_preserved,
        "document_sync": document_sync,
        "semantic_correctness": "unknown",
    }
