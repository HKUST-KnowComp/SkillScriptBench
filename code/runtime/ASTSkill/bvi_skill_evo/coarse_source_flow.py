from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

from bvi_skill_evo.visible_invariant_gate import (
    ABSTAIN_VISIBLE_INVARIANT,
    ACCEPT_VISIBLE_INVARIANT,
    REJECT_VISIBLE_INVARIANT,
)
from skillscriptbench.io_utils import canonical_json_hash, hash_tree, sha256_file


SCHEMA_VERSION = "bvi.coarse_source_flow.v1"


def _canonical(node: ast.AST) -> str:
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def _node_hash(node: ast.AST) -> str:
    return hashlib.sha256(_canonical(node).encode("utf-8")).hexdigest()


def _annotation(node: ast.expr | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):
        return ""


def _normalize_type(value: str) -> str:
    value = value.replace("typing.", "").replace(" ", "")
    if value.startswith("Optional[") and value.endswith("]"):
        value = value[len("Optional[") : -1]
    parts = [part for part in value.split("|") if part not in {"None", "NoneType"}]
    return "|".join(sorted(parts))


def _compatible(left: str, right: str) -> bool:
    left = _normalize_type(left)
    right = _normalize_type(right)
    if not left or not right:
        return not left and not right
    caller_types = set(left.split("|"))
    callee_types = set(right.split("|"))
    return caller_types.issubset(callee_types)


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[dict[str, str]]:
    positional = list(node.args.posonlyargs) + list(node.args.args)
    positional_defaults: list[ast.expr | None] = [None] * (
        len(positional) - len(node.args.defaults)
    ) + list(node.args.defaults)
    keyword = list(node.args.kwonlyargs)
    keyword_defaults = list(node.args.kw_defaults)
    rows: list[dict[str, str]] = []
    for argument, default in zip(
        positional + keyword, positional_defaults + keyword_defaults, strict=True
    ):
        if argument.arg in {"self", "cls"}:
            continue
        rows.append(
            {
                "name": argument.arg,
                "annotation": _annotation(argument.annotation),
                "default": _annotation(default),
            }
        )
    return rows


def _module_names(relative: Path) -> set[str]:
    parts = list(relative.with_suffix("").parts)
    names = {".".join(parts), parts[-1]}
    if parts[0] == "scripts" and len(parts) > 1:
        names.add(".".join(parts[1:]))
    if parts[-1] == "__init__" and len(parts) > 1:
        names.add(".".join(parts[:-1]))
        if parts[0] == "scripts" and len(parts) > 2:
            names.add(".".join(parts[1:-1]))
    return {name for name in names if name}


def _resolve_imports(
    tree: ast.Module,
    modules: dict[str, Path],
) -> tuple[dict[str, tuple[Path, str]], dict[str, Path]]:
    symbols: dict[str, tuple[Path, str]] = {}
    aliases: dict[str, Path] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            target = next(
                (
                    modules[name]
                    for name in (node.module, node.module.split(".")[-1])
                    if name in modules
                ),
                None,
            )
            if target is not None:
                for alias in node.names:
                    symbols[alias.asname or alias.name] = (target, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                target = next(
                    (
                        modules[name]
                        for name in (alias.name, alias.name.split(".")[-1])
                        if name in modules
                    ),
                    None,
                )
                if target is not None:
                    aliases[alias.asname or alias.name.split(".")[0]] = target
    return symbols, aliases


def _call_target(
    call: ast.Call,
    symbols: dict[str, tuple[Path, str]],
    aliases: dict[str, Path],
) -> tuple[Path, str] | None:
    if isinstance(call.func, ast.Name):
        return symbols.get(call.func.id)
    if (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id in aliases
    ):
        return aliases[call.func.value.id], call.func.attr
    return None


def _function(
    tree: ast.Module, symbol: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol
    ]
    if len(matches) != 1:
        raise ValueError(f"function_resolution_failed:{symbol}:{len(matches)}")
    return matches[0]


def _excerpt(source: str, start: int, end: int, radius: int = 4) -> dict[str, Any]:
    lines = source.splitlines()
    first = max(1, start - radius)
    last = min(len(lines), end + radius)
    return {
        "start_line": first,
        "end_line": last,
        "numbered_content": "\n".join(
            f"{index}: {lines[index - 1]}" for index in range(first, last + 1)
        ),
    }


def build_coarse_source_flow_facts(
    package_root: str | Path,
    activation: dict[str, Any],
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    trees: dict[Path, ast.Module] = {}
    sources: dict[Path, str] = {}
    definitions: dict[
        Path, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
    ] = {}
    modules: dict[str, Path] = {}
    parse_errors: list[dict[str, str]] = []
    for path in sorted((package / "scripts").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package)
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source, filename=relative.as_posix())
        except (SyntaxError, UnicodeError, ValueError) as exc:
            parse_errors.append(
                {
                    "path": relative.as_posix(),
                    "error": f"{type(exc).__name__}:{exc}",
                }
            )
            continue
        trees[relative] = tree
        sources[relative] = source
        definitions[relative] = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name in _module_names(relative):
            modules[name] = relative

    caller_path = Path(str(activation["caller_path"]))
    if caller_path not in trees:
        raise ValueError(f"activated_caller_unavailable:{caller_path}")
    caller = _function(trees[caller_path], str(activation["caller_symbol"]))
    caller_parameters = {row["name"]: row for row in _parameters(caller)}
    imported_symbols, module_aliases = _resolve_imports(trees[caller_path], modules)
    findings: list[dict[str, Any]] = []
    for call in (node for node in ast.walk(caller) if isinstance(node, ast.Call)):
        target = _call_target(call, imported_symbols, module_aliases)
        if target is None:
            continue
        callee_path, callee_symbol = target
        if callee_symbol not in definitions.get(callee_path, {}):
            continue
        callee = definitions[callee_path][callee_symbol]
        callee_parameters = _parameters(callee)
        callee_by_name = {row["name"]: row for row in callee_parameters}
        slots: list[tuple[str, str | int, str | None, ast.expr]] = []
        for index, argument in enumerate(call.args):
            formal = callee_parameters[index]["name"] if index < len(callee_parameters) else None
            slots.append(("positional", index, formal, argument))
        for keyword in call.keywords:
            if keyword.arg is not None:
                slots.append(("keyword", keyword.arg, keyword.arg, keyword.value))
        for call_kind, call_slot, formal, argument in slots:
            if (
                formal is None
                or formal not in caller_parameters
                or formal not in callee_by_name
                or not isinstance(argument, ast.Name)
                or argument.id not in caller_parameters
                or argument.id == formal
                or not _compatible(
                    caller_parameters[formal]["annotation"],
                    callee_by_name[formal]["annotation"],
                )
            ):
                continue
            finding: dict[str, Any] = {
                "kind": "formal_origin_mismatch",
                "confidence": 0.98,
                "caller_path": caller_path.as_posix(),
                "caller_symbol": caller.name,
                "callee_path": callee_path.as_posix(),
                "callee_symbol": callee_symbol,
                "call_line": call.lineno,
                "call_kind": call_kind,
                "call_slot": call_slot,
                "callee_formal_parameter": formal,
                "observed_origin": argument.id,
                "same_named_origin_available": True,
                "node_id": f"{caller_path.as_posix()}:{call.lineno}:{call.col_offset}:Call",
                "node_sha256": _node_hash(call),
                "call_source": ast.get_source_segment(sources[caller_path], call),
            }
            finding["finding_hash"] = canonical_json_hash(finding)
            findings.append(finding)
    findings.sort(
        key=lambda row: (
            -float(row["confidence"]),
            int(row["call_line"]),
            str(row["callee_symbol"]),
            str(row["callee_formal_parameter"]),
        )
    )
    for rank, finding in enumerate(findings, start=1):
        finding["rank"] = rank

    selected_paths = [caller_path] + sorted(
        {Path(row["callee_path"]) for row in findings if Path(row["callee_path"]) != caller_path}
    )
    selected_files: list[dict[str, Any]] = []
    for relative in selected_paths:
        relevant = [row for row in findings if Path(row["callee_path"]) == relative]
        if relative == caller_path:
            starts = [int(row["call_line"]) for row in findings] or [int(caller.lineno)]
            ends = starts
            role = "activated_caller_and_candidate_sites"
        else:
            functions = [definitions[relative][row["callee_symbol"]] for row in relevant]
            starts = [int(node.lineno) for node in functions]
            ends = [int(node.end_lineno or node.lineno) for node in functions]
            role = "local_callee_signatures"
        selected_files.append(
            {
                "path": relative.as_posix(),
                "sha256": sha256_file(package / relative),
                "role": role,
                **_excerpt(sources[relative], min(starts), max(ends)),
            }
        )
    status = "ranked_formal_origin_mismatches" if findings else "abstain_no_mismatch"
    facts: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "activation": {
            "provenance": activation.get("provenance"),
            "caller_path": caller_path.as_posix(),
            "caller_symbol": caller.name,
            "callee_or_line_exposed_by_activation": False,
        },
        "finding_count": len(findings),
        "findings": findings,
        "generic_invariant": (
            "Within an activated caller, flag a direct local call when a callee formal parameter has a "
            "type-compatible same-named caller parameter available but receives a different caller parameter. "
            "Findings are structural risks, not semantic verdicts; inspect visible code and abstain when an "
            "intentional transformation is plausible."
        ),
        "selected_files": selected_files,
        "parse_errors": parse_errors,
        "derivation": {
            "visible_inputs_only": True,
            "uses_task_verdict": False,
            "uses_expected_output": False,
            "uses_gold_package": False,
            "uses_hidden_oracle": False,
            "uses_reward": False,
            "uses_mutation_label": False,
            "inputs": [
                "visible_package_python_ast",
                "visible_local_function_signatures",
                "answer_free_caller_activation",
            ],
        },
    }
    facts["facts_hash"] = canonical_json_hash(facts)
    return facts


def _syntax(package: Path) -> tuple[bool, list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    for path in sorted((package / "scripts").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeError, ValueError) as exc:
            errors.append(
                {
                    "path": path.relative_to(package).as_posix(),
                    "error": f"{type(exc).__name__}:{exc}",
                }
            )
    return not errors, errors


def _signature_inventory(package: Path) -> list[tuple[str, str, str]]:
    inventory: list[tuple[str, str, str]] = []
    for path in sorted((package / "scripts").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inventory.append((relative, node.name, _node_hash(node.args)))
    return sorted(inventory)


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _replace_finding_origin(tree: ast.Module, finding: dict[str, Any]) -> None:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and node.lineno == int(finding["call_line"])
        and _call_name(node) == str(finding["callee_symbol"])
    ]
    if len(matches) != 1:
        raise ValueError(
            f"repair_call_resolution_failed:{finding['callee_symbol']}:{finding['call_line']}:{len(matches)}"
        )
    call = matches[0]
    if finding["call_kind"] == "keyword":
        arguments = [
            keyword.value
            for keyword in call.keywords
            if keyword.arg == str(finding["call_slot"])
        ]
    elif finding["call_kind"] == "positional":
        index = int(finding["call_slot"])
        arguments = [call.args[index]] if index < len(call.args) else []
    else:
        raise ValueError(f"unknown_call_kind:{finding['call_kind']}")
    if (
        len(arguments) != 1
        or not isinstance(arguments[0], ast.Name)
        or arguments[0].id != finding["observed_origin"]
    ):
        raise ValueError(
            f"repair_argument_resolution_failed:{finding['callee_symbol']}:{finding['call_slot']}"
        )
    arguments[0].id = str(finding["callee_formal_parameter"])


def _ast_inventory(package: Path) -> dict[str, str]:
    inventory: dict[str, str] = {}
    for path in sorted((package / "scripts").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        inventory[relative] = _canonical(tree)
    return inventory


def _exact_visible_rewrite(
    parent: Path,
    candidate: Path,
    parent_facts: dict[str, Any],
    removed_keys: set[tuple[Any, ...]],
) -> bool:
    if not removed_keys:
        return False
    trees: dict[str, ast.Module] = {}
    for path in sorted((parent / "scripts").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(parent).as_posix()
        trees[relative] = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    rows_by_key = {
        (
            row["caller_path"],
            row["callee_path"],
            row["callee_symbol"],
            row["call_line"],
            row["callee_formal_parameter"],
            row["observed_origin"],
        ): row
        for row in parent_facts.get("findings", [])
    }
    for key in removed_keys:
        finding = rows_by_key.get(key)
        if finding is None or finding["caller_path"] not in trees:
            return False
        _replace_finding_origin(trees[finding["caller_path"]], finding)
    expected = {path: _canonical(tree) for path, tree in trees.items()}
    return expected == _ast_inventory(candidate)


def _finding_keys(facts: dict[str, Any]) -> set[tuple[Any, ...]]:
    return {
        (
            row["caller_path"],
            row["callee_path"],
            row["callee_symbol"],
            row["call_line"],
            row["callee_formal_parameter"],
            row["observed_origin"],
        )
        for row in facts.get("findings", [])
    }


def evaluate_coarse_source_flow_candidate(
    parent_root: str | Path,
    candidate_root: str | Path | None,
    activation: dict[str, Any],
) -> dict[str, Any]:
    parent = Path(parent_root).resolve()
    if candidate_root is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "decision": REJECT_VISIBLE_INVARIANT,
            "reason": "candidate_unavailable",
            "checks": {},
        }
    candidate = Path(candidate_root).resolve()
    syntax_ok, syntax_errors = _syntax(candidate)
    parent_hashes = hash_tree(parent)
    candidate_hashes = hash_tree(candidate)
    changed_paths = sorted(
        path
        for path in set(parent_hashes) | set(candidate_hashes)
        if parent_hashes.get(path) != candidate_hashes.get(path)
    )
    code_changes = [path for path in changed_paths if path.startswith("scripts/")]
    scope_ok = (
        bool(code_changes)
        and len(code_changes) == 1
        and set(changed_paths).issubset({"SKILL.md", code_changes[0]})
    )
    try:
        signatures_preserved = syntax_ok and _signature_inventory(parent) == _signature_inventory(
            candidate
        )
    except (FileNotFoundError, SyntaxError, ValueError):
        signatures_preserved = False
    try:
        parent_facts = build_coarse_source_flow_facts(parent, activation)
        candidate_facts = build_coarse_source_flow_facts(candidate, activation) if syntax_ok else None
    except (FileNotFoundError, SyntaxError, ValueError) as exc:
        parent_facts = None
        candidate_facts = None
        facts_error = f"{type(exc).__name__}:{exc}"
    else:
        facts_error = None
    parent_keys = _finding_keys(parent_facts or {})
    candidate_keys = _finding_keys(candidate_facts or {})
    finding_reduced = len(candidate_keys) < len(parent_keys)
    no_new_findings = candidate_keys.issubset(parent_keys)
    try:
        exact_visible_rewrite = (
            syntax_ok
            and parent_facts is not None
            and _exact_visible_rewrite(
                parent,
                candidate,
                parent_facts,
                parent_keys - candidate_keys,
            )
        )
    except (FileNotFoundError, IndexError, KeyError, SyntaxError, ValueError):
        exact_visible_rewrite = False
    checks = {
        "syntax_ok": syntax_ok,
        "changed_scope_ok": scope_ok,
        "all_function_signatures_preserved": signatures_preserved,
        "visible_finding_count_reduced": finding_reduced,
        "no_new_visible_findings": no_new_findings,
        "only_ranked_name_nodes_changed": exact_visible_rewrite,
    }
    if all(checks.values()):
        decision = ACCEPT_VISIBLE_INVARIANT
        reason = "answer_free_coarse_finding_set_reduced"
    elif syntax_ok and signatures_preserved and not parent_keys and not changed_paths:
        decision = ABSTAIN_VISIBLE_INVARIANT
        reason = "no_visible_structural_finding_to_validate"
    else:
        decision = REJECT_VISIBLE_INVARIANT
        reason = "coarse_source_flow_checks_failed"
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "decision": decision,
        "reason": reason,
        "checks": checks,
        "changed_paths": changed_paths,
        "syntax_errors": syntax_errors,
        "facts_error": facts_error,
        "parent_finding_count": len(parent_keys),
        "candidate_finding_count": len(candidate_keys),
        "removed_findings": sorted(parent_keys - candidate_keys),
        "new_findings": sorted(candidate_keys - parent_keys),
        "uses_hidden_feedback": False,
    }
    result["gate_hash"] = canonical_json_hash(result)
    return result
