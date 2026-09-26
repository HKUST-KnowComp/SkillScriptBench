from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from skillscriptbench.io_utils import canonical_json_hash, sha256_bytes
from skillscriptbench.mutation_operators_v13 import enumerate_shell_behavior_operators_v13
from skillscriptbench.package_matrix_conditions_v64 import (
    _anomaly_score,
    _shell_sites,
)
from skillscriptbench.shell_discrimination import enumerate_shell_default_operators
from skillscriptbench.structural_evolution_v65 import (
    extract_python_role_sites,
)


SCHEMA_VERSION = "0.66-multilang-byte-node-v1"
SCRIPT_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".sh", ".bash"}
JS_SUFFIXES = {".js", ".mjs", ".cjs", ".ts"}
SHELL_SUFFIXES = {".sh", ".bash"}
JS_NODE_HELPER = (
    Path(__file__).resolve().parent / "js_parser" / "extract_structural_nodes_v66.mjs"
)


def _line_column_from_byte(source: str, byte_offset: int) -> tuple[int, int]:
    encoded = source.encode("utf-8")
    if byte_offset < 0 or byte_offset > len(encoded):
        raise ValueError("byte_offset_outside_source")
    prefix = encoded[:byte_offset].decode("utf-8")
    line = prefix.count("\n") + 1
    column = len(prefix.rsplit("\n", 1)[-1])
    return line, column


def _byte_offset_from_line_column(source: str, line: int, column: int) -> int:
    lines = source.splitlines(keepends=True)
    if not 1 <= line <= max(1, len(lines)):
        raise ValueError("line_outside_source")
    prefix = "".join(lines[: line - 1]) + lines[line - 1][:column]
    return len(prefix.encode("utf-8"))


def _line_window(source: str, line: int, radius: int = 5) -> str:
    lines = source.splitlines()
    start = max(0, line - radius - 1)
    end = min(len(lines), line + radius)
    return "\n".join(f"{index + 1}: {lines[index]}" for index in range(start, end))


def _node(
    *,
    path: str,
    language: str,
    backend: str,
    node_type: str,
    role: str,
    symbol: str,
    source: str,
    start_byte: int,
    end_byte: int,
    facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    encoded = source.encode("utf-8")
    if not 0 <= start_byte < end_byte <= len(encoded):
        raise ValueError(f"invalid_node_byte_span:{path}:{start_byte}:{end_byte}")
    observed = encoded[start_byte:end_byte].decode("utf-8")
    start_line, start_column = _line_column_from_byte(source, start_byte)
    end_line, end_column = _line_column_from_byte(source, end_byte)
    node_hash = sha256_bytes(encoded[start_byte:end_byte])
    identity = {
        "path": path,
        "language": language,
        "backend": backend,
        "node_type": node_type,
        "role": role,
        "symbol": symbol,
        "start_byte": start_byte,
        "end_byte": end_byte,
        "node_source_sha256": node_hash,
    }
    site_id = f"node-{canonical_json_hash(identity)[:16]}"
    return {
        "site_id": site_id,
        "node_id": site_id,
        "path": path,
        "language": language,
        "backend": backend,
        "node_type": node_type,
        "role": role,
        "symbol": symbol,
        "span": {
            "start_line": start_line,
            "start_column": start_column,
            "end_line": end_line,
            "end_column": end_column,
        },
        "byte_span": {"start": start_byte, "end": end_byte},
        "line": start_line,
        "column": start_column,
        "end_line": end_line,
        "end_column": end_column,
        "start": start_byte,
        "end": end_byte,
        "observed_source": observed,
        "target_source": observed,
        "window": _line_window(source, start_line),
        "facts": facts or {},
        "node_source_sha256": node_hash,
    }


def extract_javascript_nodes(path: str, source: str) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [shutil.which("node") or "node", str(JS_NODE_HELPER)],
        input=json.dumps({"source": source, "filename": path}),
        text=True,
        capture_output=True,
        cwd=JS_NODE_HELPER.parent,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise SyntaxError(f"babel_v66_parse_failed:{path}:{completed.stderr[-600:]}")
    payload = json.loads(completed.stdout)
    language = "typescript" if Path(path).suffix.lower() == ".ts" else "javascript"
    rows = []
    for value in payload.get("nodes", []):
        function = value.get("enclosingFunction") or {}
        rows.append(
            _node(
                path=path,
                language=language,
                backend="babel_ast_v66",
                node_type=str(value.get("nodeType") or "Node"),
                role=str(value.get("role") or "syntax_node"),
                symbol=str(function.get("name") or "<module>"),
                source=source,
                start_byte=int(value["startByte"]),
                end_byte=int(value["endByte"]),
                facts=value.get("facts") or {},
            )
        )
    return _dedupe_nodes(rows)


def extract_shell_nodes(path: str, source: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        parsed_sites = _shell_sites(path, source)
    except (ImportError, ModuleNotFoundError):
        parsed_sites = []
    for value in parsed_sites:
        start_byte = len(source[: int(value["start"])].encode("utf-8"))
        end_byte = len(source[: int(value["end"])].encode("utf-8"))
        rows.append(
            _node(
                path=path,
                language="shell",
                backend="tree_sitter_bash_v66",
                node_type=str(value["node_type"]),
                role=str(value["role"]),
                symbol=str(value["symbol"]),
                source=source,
                start_byte=start_byte,
                end_byte=end_byte,
                facts=value.get("facts") or {},
            )
        )

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
    for operator in operators:
        start_char = int(operator["start"])
        end_char = int(operator["end"])
        role = str(
            (operator.get("structural_roles") or [operator.get("operator_subfamily")])[0]
        )
        rows.append(
            _node(
                path=path,
                language="shell",
                backend="shell_operator_ast_v66",
                node_type="shell_token",
                role=role,
                symbol="<module>",
                source=source,
                start_byte=len(source[:start_char].encode("utf-8")),
                end_byte=len(source[:end_char].encode("utf-8")),
                facts={
                    "operatorFamily": operator.get("operator_subfamily"),
                    "dimension": operator.get("dimension"),
                },
            )
        )

    # These coarse syntax nodes keep the editor usable when Tree-sitter is not
    # installed and expose assignments whose missing fallback is the defect.
    offset = 0
    assignment_re = re.compile(
        r"^(?P<indent>[ \t]*)(?:export[ \t]+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*?)(?P<newline>\r?\n)?$"
    )
    redirect_re = re.compile(r"(?<!\S)(?:[012])?>&[012](?!\S)")
    expansion_re = re.compile(r"\$\{[^}\r\n]+\}")
    test_re = re.compile(r"\[\[.*?\]\]|(?<!\[)\[[^\[\]]*?\]")
    status_re = re.compile(r"\b(?:exit|return)[ \t]+(?P<status>[0-9]+)\b")
    for line in source.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        if not content.strip() or content.lstrip().startswith("#"):
            offset += len(line)
            continue
        assignment = assignment_re.match(line)
        if assignment:
            start_char = offset + len(assignment.group("indent"))
            end_char = offset + len(content)
            rows.append(
                _node(
                    path=path,
                    language="shell",
                    backend="shell_lexical_ast_v66",
                    node_type="variable_assignment",
                    role="binding_assignment",
                    symbol="<module>",
                    source=source,
                    start_byte=len(source[:start_char].encode("utf-8")),
                    end_byte=len(source[:end_char].encode("utf-8")),
                    facts={"bindingName": assignment.group("name")},
                )
            )
        for match, node_type, role in [
            *((match, "file_redirect", "output_stream_redirection") for match in redirect_re.finditer(content)),
            *((match, "expansion", "parameter_expansion") for match in expansion_re.finditer(content)),
            *((match, "test_command", "guard_expression") for match in test_re.finditer(content)),
        ]:
            start_char = offset + match.start()
            end_char = offset + match.end()
            rows.append(
                _node(
                    path=path,
                    language="shell",
                    backend="shell_lexical_ast_v66",
                    node_type=node_type,
                    role=role,
                    symbol="<module>",
                    source=source,
                    start_byte=len(source[:start_char].encode("utf-8")),
                    end_byte=len(source[:end_char].encode("utf-8")),
                )
            )
        status = status_re.search(content)
        if status:
            start_char = offset + status.start("status")
            end_char = offset + status.end("status")
            rows.append(
                _node(
                    path=path,
                    language="shell",
                    backend="shell_lexical_ast_v66",
                    node_type="word",
                    role="exit_status",
                    symbol="<module>",
                    source=source,
                    start_byte=len(source[:start_char].encode("utf-8")),
                    end_byte=len(source[:end_char].encode("utf-8")),
                )
            )
        offset += len(line)
    return _dedupe_nodes(rows)


def extract_python_nodes(path: str, source: str) -> list[dict[str, Any]]:
    rows = []
    for value in extract_python_role_sites(path, source):
        start_byte = _byte_offset_from_line_column(
            source, int(value["span"]["start_line"]), int(value["span"]["start_column"])
        )
        end_byte = _byte_offset_from_line_column(
            source, int(value["span"]["end_line"]), int(value["span"]["end_column"])
        )
        rows.append(
            _node(
                path=path,
                language="python",
                backend="python_ast_v66",
                node_type=str(value["node_type"]),
                role=str(value["role"]),
                symbol=str(value["symbol"]),
                source=source,
                start_byte=start_byte,
                end_byte=end_byte,
                facts=value.get("facts") or {},
            )
        )
    return _dedupe_nodes(rows)


def extract_markdown_nodes(path: str, source: str) -> list[dict[str, Any]]:
    encoded = source.encode("utf-8")
    rows = []
    offset = 0
    fenced = False
    for raw in source.splitlines(keepends=True):
        content = raw.rstrip("\r\n")
        stripped = content.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = not fenced
            offset += len(raw.encode("utf-8"))
            continue
        if not stripped:
            offset += len(raw.encode("utf-8"))
            continue
        leading = len(content) - len(content.lstrip())
        start = offset + len(content[:leading].encode("utf-8"))
        end = offset + len(content.encode("utf-8"))
        role = (
            "code_line"
            if fenced
            else "heading"
            if re.match(r"^#{1,6}\s", stripped)
            else "table_row"
            if stripped.startswith("|") and stripped.endswith("|")
            else "list_item"
            if re.match(r"^(?:[-*+]\s|\d+[.)]\s)", stripped)
            else "paragraph_line"
        )
        rows.append(
            _node(
                path=path,
                language="markdown",
                backend="markdown_line_v66",
                node_type="markdown_line",
                role=role,
                symbol="<document>",
                source=source,
                start_byte=start,
                end_byte=end,
            )
        )
        offset += len(raw.encode("utf-8"))
    if offset < len(encoded):
        raise ValueError("markdown_offset_accounting_failed")
    return rows


def _dedupe_nodes(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped = {
        (row["path"], row["byte_span"]["start"], row["byte_span"]["end"], row["role"]): row
        for row in rows
    }
    return sorted(
        deduped.values(),
        key=lambda row: (row["path"], row["byte_span"]["start"], row["role"]),
    )


def enumerate_package_nodes(package: str | Path, *, include_markdown: bool = True) -> list[dict[str, Any]]:
    root = Path(package)
    rows: list[dict[str, Any]] = []
    scripts = root / "scripts"
    for file in sorted(scripts.rglob("*")) if scripts.is_dir() else []:
        if not file.is_file() or file.suffix.lower() not in SCRIPT_SUFFIXES:
            continue
        relative = file.relative_to(root).as_posix()
        try:
            source = file.read_text(encoding="utf-8")
            suffix = file.suffix.lower()
            if suffix == ".py":
                rows.extend(extract_python_nodes(relative, source))
            elif suffix in JS_SUFFIXES:
                rows.extend(extract_javascript_nodes(relative, source))
            elif suffix in SHELL_SUFFIXES:
                rows.extend(extract_shell_nodes(relative, source))
        except (UnicodeDecodeError, SyntaxError, ValueError, subprocess.SubprocessError):
            continue
    if include_markdown and (root / "SKILL.md").is_file():
        rows.extend(
            extract_markdown_nodes(
                "SKILL.md", (root / "SKILL.md").read_text(encoding="utf-8")
            )
        )
    return _dedupe_nodes(rows)


def _focus_request(request: str) -> str:
    marker = "## Required Use Case"
    return request.split(marker, 1)[1].strip() if marker in request else request.strip()


def _tokens(value: str) -> set[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    rows: set[str] = set()
    for token in re.findall(r"[A-Za-z0-9_.-]+", expanded.lower()):
        normalized = token.strip("-_.")
        if len(normalized) <= 1:
            continue
        rows.add(normalized)
        rows.update(part for part in re.split(r"[-_.]+", normalized) if len(part) > 1)
    return rows


def _request_anchors(value: str) -> list[str]:
    anchors: set[str] = set()
    for quoted in re.findall(r"`([^`\n]+)`", value):
        stripped = quoted.strip()
        if 1 < len(stripped) <= 180:
            anchors.add(stripped)
        anchors.update(re.findall(r"--[A-Za-z0-9-]+", stripped))
        anchors.update(re.findall(r"(?<![A-Za-z0-9])[.][A-Za-z0-9]{2,8}\b", stripped))
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,30}", stripped):
            if token.lower() not in {"node", "scripts", "default", "seconds", "output"}:
                anchors.add(token)
    anchors.update(re.findall(r"--[A-Za-z0-9-]+", value))
    anchors.update(re.findall(r"(?<![A-Za-z0-9])[.][A-Za-z0-9]{2,8}\b", value))
    anchors.update(re.findall(r"\b([1-5][0-9]{2})(?=s?\b)", value))
    return sorted(anchors, key=lambda item: (-len(item), item))


def _explicit_script_paths(focus: str, available_paths: Iterable[str]) -> set[str]:
    available = set(available_paths)
    mentions = {
        match.rstrip(".,;:)")
        for match in re.findall(
            r"(?:[A-Za-z0-9_.~/-]+/)*[A-Za-z0-9_.~-]+\.(?:py|js|mjs|cjs|ts|sh|bash)",
            focus,
            flags=re.IGNORECASE,
        )
    }
    resolved = set()
    for path in available:
        for mention in mentions:
            if path == mention or path.endswith(mention) or mention.endswith(path):
                resolved.add(path)
            elif Path(path).name == Path(mention).name:
                resolved.add(path)
    return resolved


def _document_linked_paths(
    package: Path, focus: str, available_paths: Iterable[str]
) -> dict[str, float]:
    skill = package / "SKILL.md"
    if not skill.is_file():
        return {}
    paths = set(available_paths)
    lines = skill.read_text(encoding="utf-8").splitlines()
    focus_lines = [line.strip() for line in focus.splitlines() if len(line.strip()) >= 6]
    scored: list[tuple[float, int]] = []
    for index, line in enumerate(lines):
        line_tokens = _tokens(line)
        for required in focus_lines:
            ratio = SequenceMatcher(None, required.lower(), line.strip().lower()).ratio()
            overlap = len(_tokens(required) & line_tokens)
            scored.append((ratio + min(0.6, overlap * 0.06), index))
    linked: dict[str, float] = {}
    for score, index in sorted(scored, reverse=True)[:5]:
        if score < 0.45:
            continue
        for line_index in range(max(0, index - 8), min(len(lines), index + 9)):
            neighborhood = lines[line_index]
            distance_weight = max(0.0, 1.0 - abs(line_index - index) / 9.0)
            path_score = 250.0 * score * distance_weight
            for path in _explicit_script_paths(neighborhood, paths):
                linked[path] = max(linked.get(path, 0.0), path_score)
            for path in paths:
                if Path(path).name in neighborhood:
                    linked[path] = max(linked.get(path, 0.0), path_score)
    return linked


def _role_bonus(node: dict[str, Any], focus: str) -> float:
    role = str(node["role"]).lower()
    node_type = str(node["node_type"]).lower()
    lower = focus.lower()
    bonus = 0.0
    if re.search(r"stdout|stderr|json only|piping|parsing", lower) and (
        "output_stream" in role or "redirect" in node_type
    ):
        bonus += 100.0
    if re.search(r"exit|status|non-zero|failure|fails", lower) and "exit_status" in role:
        bonus += 100.0
    if re.search(r"file|directory|folder|path", lower) and (
        "path_kind" in role or "guard_expression" in role
    ):
        bonus += 80.0
    if re.search(r"default|fallback|silent|omitted", lower) and role in {
        "binding_initializer",
        "binding_assignment",
        "fallback_expression",
        "parameter_expansion",
        "shell_environment_fallback",
        "shell_positional_fallback",
    }:
        bonus += 80.0
    if re.search(r"newest|oldest|relevant|ascending|descending|sorted", lower) and "sort" in role:
        bonus += 100.0
    if re.search(r"\.[a-z0-9]{2,8}\b", lower) and (
        role == "path_suffix"
        or (role == "literal" and re.search(r"\.[a-z0-9]{2,8}", str(node["observed_source"]).lower()))
    ):
        bonus += 55.0
    if re.search(r"\b[1-5][0-9]{2}(?:s)?\b|boundary|threshold", lower) and "comparison" in role:
        bonus += 90.0
    if re.search(r"--[a-z0-9-]+|\bget\b|\brecord\b|\bbrowser\b", lower) and role in {
        "literal",
        "comparison_expression",
        "comparison_operator",
    }:
        bonus += 35.0
    return bonus


def rank_script_nodes(
    nodes: Iterable[dict[str, Any]], request: str, package: str | Path
) -> list[dict[str, Any]]:
    root = Path(package)
    script_nodes = [
        row
        for row in nodes
        if row["language"] != "markdown" and row["role"] != "function_scope"
    ]
    focus = _focus_request(request)
    focus_tokens = _tokens(focus)
    parameter_names = {
        match.lower() for match in re.findall(r"<([A-Za-z_][A-Za-z0-9_]*)>", focus)
    }
    option_value_contracts = []
    for match in re.finditer(
        r"--(?P<option>[A-Za-z0-9-]+)\s+(?P<values>[A-Za-z0-9_-]+(?:\|[A-Za-z0-9_-]+)+)",
        focus,
    ):
        option_value_contracts.append(
            (
                match.group("option").lower(),
                {value.lower() for value in match.group("values").split("|")},
            )
        )
    anchors = _request_anchors(focus)
    paths = {row["path"] for row in script_nodes}
    explicit_paths = _explicit_script_paths(focus, paths)
    linked_paths = _document_linked_paths(root, focus, paths)
    file_tokens: dict[str, set[str]] = {}
    anchor_lines: dict[str, list[int]] = {}
    for path in paths:
        source = (root / path).read_text(encoding="utf-8")
        file_tokens[path] = _tokens(source)
        anchor_lines[path] = [
            index
            for index, line in enumerate(source.splitlines(), start=1)
            if any(anchor in line for anchor in anchors)
        ]
    document_frequency = {
        token: sum(token in terms for terms in file_tokens.values()) for token in focus_tokens
    }
    observed_counts: dict[tuple[str, str], int] = {}
    comparison_counts: dict[tuple[str, str, str], dict[str, int]] = {}
    for row in script_nodes:
        key = (row["path"], row["observed_source"].strip())
        observed_counts[key] = observed_counts.get(key, 0) + 1
        facts = row.get("facts") or {}
        operator = str(facts.get("operator") or "")
        right = str(facts.get("rightSource") or "").strip()
        left = str(facts.get("leftSource") or "").strip()
        direction = "greater" if operator in {">", ">="} else "less" if operator in {"<", "<="} else ""
        if row["role"] == "comparison_operator" and direction and right.isdigit() and "status" in left.lower():
            group = comparison_counts.setdefault((row["path"], right, direction), {})
            group[operator] = group.get(operator, 0) + 1
    result = []
    for row in script_nodes:
        item = dict(row)
        searchable = " ".join(
            [
                item["observed_source"],
                item["window"],
                item["symbol"],
                json.dumps(item.get("facts") or {}, sort_keys=True),
            ]
        )
        overlap = focus_tokens & _tokens(searchable)
        direct_anchor_hits = sum(anchor in item["observed_source"] for anchor in anchors)
        window_anchor_hits = sum(anchor in item["window"] for anchor in anchors)
        distances = [abs(int(item["line"]) - line) for line in anchor_lines[item["path"]]]
        proximity = max(0.0, 60.0 - 5.0 * min(distances)) if distances else 0.0
        binding = str((item.get("facts") or {}).get("bindingName") or "")
        enclosing_binding = str((item.get("facts") or {}).get("enclosingBinding") or "")
        binding_overlap = len(
            focus_tokens & (_tokens(binding) | _tokens(enclosing_binding))
        )
        exact_binding_match = bool(binding) and binding.lower() in focus_tokens
        exact_enclosing_binding_match = (
            bool(enclosing_binding) and enclosing_binding.lower() in focus_tokens
        )
        parameter_binding_match = bool(binding) and binding.lower() in parameter_names
        transport_binding = bool(binding) and bool(
            re.search(r"(?:arg|raw|input|value)$", binding, flags=re.IGNORECASE)
        )
        role_bonus = _role_bonus(item, focus)
        focus_lower = focus.lower()
        if "default" in focus_lower and item["role"] in {"binding_initializer", "fallback_expression"}:
            role_bonus += 14.0
            if exact_binding_match:
                role_bonus += 180.0
            if parameter_binding_match:
                role_bonus += 220.0
            if exact_enclosing_binding_match:
                role_bonus += 120.0
            if transport_binding:
                role_bonus -= 20.0
        if item["role"] == "sort_comparator" and re.search(
            r"newest|oldest|relevant|ascending|descending|sorted", focus_lower
        ):
            role_bonus += 260.0
        if (
            item["language"] == "shell"
            and item["role"] == "binding_assignment"
            and re.search(r"default|fallback|omitted", focus_lower)
            and re.search(r"=\"?\$\{[^}:]+\}\"?$", item["observed_source"].strip())
        ):
            role_bonus += 120.0
        if (
            item["role"] == "literal"
            and exact_enclosing_binding_match
            and item["observed_source"].strip().strip("'\"").lower() not in focus_tokens
        ):
            role_bonus += 80.0
        comparison_facts = item.get("facts") or {}
        comparison_operands = " ".join(
            str(comparison_facts.get(key) or "") for key in ("leftSource", "rightSource")
        )
        comparison_operand_bonus = 0.0
        structural_outlier_bonus = 0.0
        if item["role"] in {"comparison_operator", "comparison_expression"}:
            comparison_operand_bonus = 90.0 * min(
                2,
                sum(anchor in comparison_operands for anchor in anchors if anchor.isdigit()),
            )
            role_bonus += comparison_operand_bonus
            operator = str(comparison_facts.get("operator") or "")
            right = str(comparison_facts.get("rightSource") or "").strip()
            left = str(comparison_facts.get("leftSource") or "").strip()
            direction = (
                "greater"
                if operator in {">", ">="}
                else "less"
                if operator in {"<", "<="}
                else ""
            )
            group = comparison_counts.get((item["path"], right, direction), {})
            if group and "status" in left.lower():
                dominant_operator, dominant_count = max(
                    group.items(), key=lambda pair: (pair[1], pair[0])
                )
                if dominant_count >= 2 and operator != dominant_operator:
                    structural_outlier_bonus = 180.0
                    role_bonus += structural_outlier_bonus
        literal_value = item["observed_source"].strip().strip("'\"")
        contract_mismatch_bonus = 0.0
        literal_context = " ".join(
            [
                item["window"],
                *[
                    str((item.get("facts") or {}).get(key) or "")
                    for key in ("parentSource", "grandparentSource")
                ],
            ]
        ).lower()
        if (
            item["role"] == "literal"
            and 1 < len(literal_value) <= 40
            and str((item.get("facts") or {}).get("parentType") or "")
            in {"BinaryExpression", "LogicalExpression"}
        ):
            for option, allowed_values in option_value_contracts:
                if (
                    option in literal_context
                    and literal_value.lower() not in allowed_values
                    and any(value in literal_context for value in allowed_values)
                ):
                    contract_mismatch_bonus = max(contract_mismatch_bonus, 240.0)
        if (
            item["role"] == "literal"
            and 1 < len(literal_value) <= 40
            and not literal_value.startswith("--")
            and literal_value.lower() not in focus_tokens
            and len(
                focus_tokens
                & _tokens(
                    " ".join(
                        str((item.get("facts") or {}).get(key) or "")
                        for key in ("parentSource", "grandparentSource")
                    )
                )
            )
            >= 1
            and str((item.get("facts") or {}).get("parentType") or "")
            in {"BinaryExpression", "LogicalExpression", "SwitchCase"}
        ):
            contract_mismatch_bonus = max(contract_mismatch_bonus, 140.0)
        role_bonus += contract_mismatch_bonus
        file_overlap = focus_tokens & file_tokens[item["path"]]
        file_idf = sum(
            1.0 + math.log((len(paths) + 1) / (document_frequency[token] + 1))
            for token in file_overlap
        )
        file_score = min(120.0, 5.0 * file_idf)
        if item["path"] in explicit_paths:
            file_score += 1000.0
        file_score += linked_paths.get(item["path"], 0.0)
        if Path(item["path"]).name.lower() in focus_lower:
            file_score += 700.0
        duplicate_count = observed_counts[(item["path"], item["observed_source"].strip())]
        duplicate_bonus = (
            420.0
            if duplicate_count > 1
            and item["role"] == "literal"
            and literal_value.startswith("--")
            and re.search(r"--[a-z0-9-]+", focus_lower)
            else 0.0
        )
        node_size = item["byte_span"]["end"] - item["byte_span"]["start"]
        precision_bonus = max(0.0, 12.0 - min(12.0, node_size / 20.0))
        item["localization_score"] = round(
            file_score
            + 4.0 * len(overlap)
            + 30.0 * min(3, direct_anchor_hits)
            + 10.0 * min(4, window_anchor_hits)
            + proximity
            + 12.0 * binding_overlap
            + role_bonus
            + duplicate_bonus
            + precision_bonus
            + 12.0 * _anomaly_score(item, focus),
            6,
        )
        item["v66_overlap_terms"] = sorted(overlap)
        item["v66_anchor_proximity"] = proximity
        item["v66_file_score"] = round(file_score, 6)
        item["v66_explicit_path"] = item["path"] in explicit_paths
        item["v66_document_linked_path"] = item["path"] in linked_paths
        item["v66_document_link_score"] = round(linked_paths.get(item["path"], 0.0), 6)
        item["v66_role_bonus"] = role_bonus
        item["v66_duplicate_bonus"] = duplicate_bonus
        item["v66_contract_mismatch_bonus"] = contract_mismatch_bonus
        item["v66_comparison_operand_bonus"] = comparison_operand_bonus
        item["v66_structural_outlier_bonus"] = structural_outlier_bonus
        item["v66_parameter_binding_match"] = parameter_binding_match
        item["v66_exact_binding_match"] = exact_binding_match
        item["v66_generic_anomaly_score"] = _anomaly_score(item, focus)
        result.append(item)
    return sorted(
        result,
        key=lambda row: (-float(row["localization_score"]), row["path"], row["line"], row["site_id"]),
    )


def select_editable_nodes(
    ranked: Iterable[dict[str, Any]], *, max_nodes: int = 16
) -> list[dict[str, Any]]:
    ordered = list(ranked)
    if max_nodes <= 0:
        return []

    # Keep one representative per exact byte span. Different nested spans are
    # retained because an operator token and its enclosing expression are
    # materially different edit scopes.
    representatives: list[dict[str, Any]] = []
    seen_spans: set[tuple[str, int, int]] = set()
    for node in ordered:
        key = (
            node["path"],
            int(node["byte_span"]["start"]),
            int(node["byte_span"]["end"]),
        )
        if key in seen_spans:
            continue
        seen_spans.add(key)
        representatives.append(node)

    comparison_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for node in representatives:
        if node.get("v66_comparison_operand_bonus", 0) <= 0:
            continue
        facts = node.get("facts") or {}
        key = (
            node["path"],
            int(node["line"]),
            facts.get("leftSource"),
            facts.get("rightSource"),
        )
        comparison_groups.setdefault(key, []).append(node)
    comparison_nodes = []
    for group in comparison_groups.values():
        comparison_nodes.append(
            min(
                group,
                key=lambda node: (
                    node["role"] != "comparison_operator",
                    -float(node["localization_score"]),
                    node["site_id"],
                ),
            )
        )
    comparison_nodes.sort(
        key=lambda node: (-float(node["localization_score"]), node["site_id"])
    )
    typed_nodes = [
        node
        for node in representatives
        if node["role"]
        in {
            "sort_comparator",
            "output_stream_redirection",
            "exit_status",
            "path_kind_guard",
            "shell_environment_fallback",
            "shell_positional_fallback",
        }
    ]
    typed_nodes.sort(
        key=lambda node: (
            -int(
                node["role"] == "exit_status"
                and node["observed_source"].strip() == "0"
                and float(node.get("v66_generic_anomaly_score", 0)) >= 4.0
            ),
            -float(node["localization_score"]),
            node["site_id"],
        )
    )
    channels = [
        ("rank", representatives[:4]),
        (
            "visible_contract_mismatch",
            [node for node in representatives if node.get("v66_contract_mismatch_bonus", 0) > 0][
                :6
            ],
        ),
        (
            "comparison_operand",
            comparison_nodes[:8],
        ),
        (
            "documented_parameter",
            [
                node
                for node in representatives
                if node.get("v66_parameter_binding_match")
                or node.get("v66_exact_binding_match")
            ][:6],
        ),
        (
            "duplicate_cli_branch",
            [node for node in representatives if node.get("v66_duplicate_bonus", 0) > 0][
                :8
            ],
        ),
        (
            "typed_role",
            typed_nodes[:12],
        ),
        (
            "generic_anomaly",
            [
                node
                for node in representatives
                if float(node.get("v66_generic_anomaly_score", 0)) >= 4.0
            ][:8],
        ),
    ]
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    cursor = 0
    while len(selected) < max_nodes:
        progressed = False
        for reason, channel in channels:
            if cursor >= len(channel):
                continue
            node = channel[cursor]
            progressed = True
            if node["site_id"] in selected_ids:
                continue
            item = dict(node)
            item["selection_channel"] = reason
            selected.append(item)
            selected_ids.add(node["site_id"])
            if len(selected) >= max_nodes:
                break
        if not progressed:
            break
        cursor += 1
    return selected


def opportunity_decision(
    ranked: list[dict[str, Any]], *, minimum_score: float = 35.0, minimum_margin: float = 2.0
) -> dict[str, Any]:
    if not ranked:
        return {"decision": "ABSTAIN", "reason": "no_script_nodes", "confidence": 0.0, "margin": 0.0}
    top1 = float(ranked[0]["localization_score"])
    top2 = float(ranked[1]["localization_score"]) if len(ranked) > 1 else 0.0
    margin = top1 - top2
    decision = "PROPOSE" if top1 >= minimum_score and margin >= minimum_margin else "ABSTAIN"
    confidence = 1.0 / (1.0 + math.exp(-((top1 - minimum_score) / 10.0)))
    return {
        "decision": decision,
        "reason": "high_confidence_unique_top1" if decision == "PROPOSE" else "low_score_or_margin",
        "top1_score": round(top1, 6),
        "top2_score": round(top2, 6),
        "margin": round(margin, 6),
        "confidence": round(confidence, 6),
    }


def select_visible_document_target(
    markdown_nodes: Iterable[dict[str, Any]], request: str
) -> dict[str, Any]:
    focus_lines = [
        line.strip()
        for line in _focus_request(request).splitlines()
        if len(line.strip()) >= 6
    ]
    rows = list(markdown_nodes)
    if any(
        request_line == node["observed_source"].strip()
        for request_line in focus_lines
        for node in rows
    ):
        return {"status": "ALIGNED", "node": None, "score": 1.0}
    candidates = []
    for node in rows:
        observed = node["observed_source"].strip()
        for request_line in focus_lines:
            ratio = SequenceMatcher(None, request_line.lower(), observed.lower()).ratio()
            token_overlap = len(_tokens(request_line) & _tokens(observed))
            score = ratio + min(0.35, token_overlap * 0.05)
            candidates.append((score, node, request_line))
    if not candidates:
        return {"status": "ABSTAIN", "node": None, "score": 0.0}
    score, node, required_line = max(candidates, key=lambda row: (row[0], row[1]["site_id"]))
    if score < 0.55:
        return {"status": "ABSTAIN", "node": None, "score": round(score, 6)}
    return {
        "status": "MISMATCH",
        "node": node,
        "score": round(score, 6),
        "required_visible_line": required_line,
    }


def select_gold_repair_node(
    nodes: Iterable[dict[str, Any]], label: dict[str, Any]
) -> dict[str, Any] | None:
    target_path = str(label["target_path"])
    target_line = int((label.get("operator") or {}).get("line") or 0)
    fault = str((label.get("operator") or {}).get("replacement_fragment") or "")
    candidates = []
    for node in nodes:
        if node["language"] == "markdown" or node["role"] == "function_scope" or node["path"] != target_path:
            continue
        line_distance = min(
            abs(target_line - int(node["span"]["start_line"])),
            abs(target_line - int(node["span"]["end_line"])),
        )
        contains_line = int(node["span"]["start_line"]) <= target_line <= int(node["span"]["end_line"])
        observed = node["observed_source"]
        fragment_exact = bool(fault) and observed.strip() == fault.strip()
        fragment_contains = bool(fault) and fault.strip() in observed
        score = 200 * fragment_exact + 100 * fragment_contains + 40 * contains_line - 4 * line_distance
        if contains_line or fragment_contains or line_distance <= 2:
            candidates.append((score, -(node["byte_span"]["end"] - node["byte_span"]["start"]), node))
    if not candidates:
        return None
    return max(candidates, key=lambda row: (row[0], row[1], row[2]["site_id"]))[2]


def select_matched_sham_node(
    ranked: Iterable[dict[str, Any]], gold: dict[str, Any]
) -> dict[str, Any] | None:
    candidates = [
        row
        for row in ranked
        if row["site_id"] != gold["site_id"]
        and row["language"] == gold["language"]
        and not (
            row["path"] == gold["path"]
            and row["byte_span"]["start"] <= gold["byte_span"]["start"]
            and row["byte_span"]["end"] >= gold["byte_span"]["end"]
        )
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda row: (
            row["path"] != gold["path"],
            row["role"] != gold["role"],
            abs((row["byte_span"]["end"] - row["byte_span"]["start"]) - (gold["byte_span"]["end"] - gold["byte_span"]["start"])),
            -float(row.get("localization_score", 0.0)),
            row["site_id"],
        )
    )
    return candidates[0]


def node_public_view(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_id": node["site_id"],
        "path": node["path"],
        "language": node["language"],
        "parser_backend": node["backend"],
        "node_type": node["node_type"],
        "role": node["role"],
        "symbol": node["symbol"],
        "span": node["span"],
        "source_sha256": node["node_source_sha256"],
        "observed_source": node["observed_source"],
        "context_source": node["window"],
        "facts": node.get("facts") or {},
    }


def build_repair_spec(
    selected: dict[str, Any] | None,
    decision: dict[str, Any],
    doc_target: dict[str, Any],
    *,
    force_propose: bool = False,
) -> dict[str, Any]:
    resolved = dict(decision)
    if force_propose:
        resolved.update({"decision": "PROPOSE", "reason": "control_target_forced"})
    target = node_public_view(selected) if selected is not None and resolved["decision"] == "PROPOSE" else None
    editable = [target] if target else []
    if doc_target.get("status") == "MISMATCH" and doc_target.get("node"):
        editable.append(node_public_view(doc_target["node"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "decision": resolved["decision"],
        "decision_detail": resolved,
        "target": target,
        "function": target,
        "document_alignment": {
            key: value
            for key, value in doc_target.items()
            if key != "node"
        },
        "editable_nodes": editable,
        "constraints": [
            "replace only listed node ids",
            "preserve all bytes outside selected spans",
            "do not infer hidden labels or evaluator results",
        ],
    }


def localization_audit(
    case_id: str,
    ranked: list[dict[str, Any]],
    gold: dict[str, Any],
    decision: dict[str, Any],
) -> dict[str, Any]:
    rank = next((index for index, row in enumerate(ranked, start=1) if row["site_id"] == gold["site_id"]), None)
    return {
        "case_id": case_id,
        "language": gold["language"],
        "gold_node_id": gold["site_id"],
        "predicted_node_id": ranked[0]["site_id"] if ranked else None,
        "exact_rank": rank,
        "recall_at_1": rank == 1,
        "recall_at_3": rank is not None and rank <= 3,
        "mrr": 1.0 / rank if rank else 0.0,
        "decision": decision,
    }


def validate_node_registry(package: str | Path, registry: dict[str, dict[str, Any]]) -> dict[str, Any]:
    root = Path(package)
    failures = []
    for node_id, node in registry.items():
        path = root / node["path"]
        if not path.is_file():
            failures.append({"node_id": node_id, "reason": "file_missing"})
            continue
        encoded = path.read_bytes()
        start = int(node["byte_span"]["start"])
        end = int(node["byte_span"]["end"])
        if sha256_bytes(encoded[start:end]) != node["node_source_sha256"]:
            failures.append({"node_id": node_id, "reason": "node_hash_mismatch"})
    return {
        "status": "pass" if not failures else "fail",
        "node_count": len(registry),
        "failures": failures,
    }
