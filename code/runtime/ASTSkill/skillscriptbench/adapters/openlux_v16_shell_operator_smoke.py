from __future__ import annotations

import argparse
import getpass
import importlib.metadata
import json
import math
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable

from skillscriptbench.adapters.openlux_v04_smoke import (
    _call_chat_completion,
    _call_responses,
    _model_content,
    _normalized_usage,
    _parse_model_json,
)
from skillscriptbench.adapters.bounded_exact_edits_v16 import (
    exact_replacement_instruction,
    parse_and_apply_exact_replacements,
)
from skillscriptbench.adapters.openlux_v16_operator_smoke import (
    _credential_absent,
    _leaf_diffs,
    _load_attempts,
    _path_value,
    _prompt_leakage_report,
    _read_utf8_exact,
    _sanitize_skill_provenance,
    _skill_markdown,
    _utc_now,
    consolidate_transport_recovery_runs,
)
from skillscriptbench.io_utils import (
    canonical_json_hash,
    hash_tree,
    read_json,
    sha256_bytes,
    sha256_file,
    write_json,
)
from skillscriptbench.mutation_operators_v13 import (
    SHELL_V13_OPERATORS,
    apply_shell_behavior_operator_v13,
)


CONDITIONS = ("raw-script", "skill-script")
DEFAULT_OPERATOR_PRIORITY = (
    "swap_shell_output_stream_v13",
    "toggle_shell_exit_status_v13",
    "toggle_shell_unary_predicate_v13",
    "toggle_shell_numeric_predicate_v13",
    "toggle_shell_string_predicate_v13",
    "remove_shell_failure_mask_v13",
)
_IGNORED_TREE_SITTER_TYPES = {"comment"}
_OPERATOR_SCOPE_TYPES = {
    "swap_shell_output_stream_v13": (
        "redirected_statement",
        "file_redirect",
        "command",
    ),
    "toggle_shell_exit_status_v13": ("command",),
    "toggle_shell_unary_predicate_v13": (
        "unary_expression",
        "test_command",
    ),
    "toggle_shell_numeric_predicate_v13": (
        "binary_expression",
        "test_command",
    ),
    "toggle_shell_string_predicate_v13": (
        "binary_expression",
        "test_command",
    ),
    "remove_shell_failure_mask_v13": ("list", "pipeline", "command"),
}


def _tree_sitter_runtime() -> dict[str, str]:
    try:
        tree_sitter_version = importlib.metadata.version("tree-sitter")
        bash_version = importlib.metadata.version("tree-sitter-bash")
        from tree_sitter import Language, Parser  # noqa: F401
        import tree_sitter_bash  # noqa: F401
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        raise RuntimeError(
            "Shell AST evaluation requires tree-sitter and tree-sitter-bash"
        ) from exc
    return {
        "tree_sitter": tree_sitter_version,
        "tree_sitter_bash": bash_version,
    }


def _new_parser():
    from tree_sitter import Language, Parser
    import tree_sitter_bash

    return Parser(Language(tree_sitter_bash.language()))


def _bash_syntax(source: str) -> tuple[bool, str]:
    executable = shutil.which("bash")
    if executable is None:
        return False, "bash_not_available"
    completed = subprocess.run(
        [executable, "--noprofile", "--norc", "-n"],
        input=source,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    return completed.returncode == 0, completed.stderr[-1000:]


def _shell_ast(source: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    encoded = source.encode("utf-8")
    tree = _new_parser().parse(encoded)
    if tree.root_node.has_error:
        raise SyntaxError("tree_sitter_bash_parse_error")
    records: list[dict[str, Any]] = []

    def convert(node: Any, path: list[Any]) -> dict[str, Any]:
        # Bash represents several semantics-bearing operators (for example
        # string equality) as anonymous grammar tokens. Preserve every syntax
        # child and drop only comments; whitespace is not represented as a
        # Tree-sitter node.
        children = [
            child for child in node.children if child.type not in _IGNORED_TREE_SITTER_TYPES
        ]
        payload: dict[str, Any] = {"type": node.type}
        records.append(
            {
                "path": list(path),
                "type": node.type,
                "start_byte": node.start_byte,
                "end_byte": node.end_byte,
            }
        )
        if children:
            payload["children"] = [
                convert(child, [*path, "children", index])
                for index, child in enumerate(children)
            ]
        else:
            payload["text"] = encoded[node.start_byte : node.end_byte].decode("utf-8")
        return payload

    return convert(tree.root_node, []), records


def _shell_ast_hash(source: str) -> str:
    data, _ = _shell_ast(source)
    return canonical_json_hash(data)


def _char_to_byte(source: str, offset: int) -> int:
    if offset < 0 or offset > len(source):
        raise ValueError("character offset outside source")
    return len(source[:offset].encode("utf-8"))


def _target_scope(
    source: str,
    operator: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    start_byte = _char_to_byte(source, int(operator["start"]))
    end_byte = _char_to_byte(source, int(operator["end"]))
    containing = [
        row
        for row in records
        if int(row["start_byte"]) <= start_byte
        and int(row["end_byte"]) >= end_byte
    ]
    if not containing:
        raise ValueError("mutation span is not contained by a Bash AST node")
    preferred = _OPERATOR_SCOPE_TYPES.get(str(operator.get("operator")), ())
    for node_type in preferred:
        matches = [row for row in containing if row["type"] == node_type]
        if matches:
            selected = max(matches, key=lambda row: len(row["path"]))
            return {
                **selected,
                "mutation_start_byte": start_byte,
                "mutation_end_byte": end_byte,
            }
    selected = max(containing, key=lambda row: len(row["path"]))
    return {
        **selected,
        "mutation_start_byte": start_byte,
        "mutation_end_byte": end_byte,
    }


def _shell_analysis(source: str) -> dict[str, Any]:
    bash_ok, bash_error = _bash_syntax(source)
    try:
        data, records = _shell_ast(source)
        tree_sitter_ok = True
        tree_sitter_error = None
        ast_hash = canonical_json_hash(data)
    except (UnicodeError, SyntaxError, ValueError) as exc:
        data = None
        records = []
        tree_sitter_ok = False
        tree_sitter_error = f"{type(exc).__name__}:{exc}"
        ast_hash = None
    return {
        "valid": bash_ok and tree_sitter_ok,
        "bash_n_ok": bash_ok,
        "bash_n_error": bash_error,
        "tree_sitter_ok": tree_sitter_ok,
        "tree_sitter_error": tree_sitter_error,
        "ast_hash": ast_hash,
        "ast": data,
        "records": records,
    }


def _case_id(attempt: dict[str, Any]) -> str:
    identity = {
        "transformation_id": attempt.get("transformation_id"),
        "operator_candidate_id": attempt["operator"].get("operator_candidate_id"),
        "source_commit": attempt.get("source_commit"),
    }
    return f"v16-shell-api-{canonical_json_hash(identity)[:16]}"


def select_shell_v13_cases(
    attempts: list[dict[str, Any]],
    *,
    case_count: int,
    max_script_bytes: int,
    max_skill_bytes: int,
    transformation_ids: tuple[str, ...] = (),
    operator_priority: tuple[str, ...] = DEFAULT_OPERATOR_PRIORITY,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if case_count < 1:
        raise ValueError("case_count must be positive")
    priority = {name: index for index, name in enumerate(operator_priority)}
    by_id = {str(row.get("transformation_id")): row for row in attempts}
    if transformation_ids:
        missing = [identifier for identifier in transformation_ids if identifier not in by_id]
        if missing:
            raise ValueError(f"unknown transformation ids: {missing}")
        if len(transformation_ids) != case_count:
            raise ValueError("explicit transformation count must equal case_count")
        candidates = [by_id[identifier] for identifier in transformation_ids]
    else:
        candidates = sorted(
            (
                row
                for row in attempts
                if row.get("language") == "shell"
                and row.get("operator", {}).get("operator") in priority
            ),
            key=lambda row: (
                priority[row["operator"]["operator"]],
                row.get("source_id", ""),
                row.get("relative_root", ""),
                row["operator"].get("path", ""),
                row["operator"].get("operator_candidate_id", ""),
            ),
        )
    selected: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    used_sources: set[str] = set()
    used_components: set[str] = set()
    used_operators: set[str] = set()
    enforce_diversity = not bool(transformation_ids)

    def eligible(row: dict[str, Any], *, require_new_operator: bool) -> tuple[bool, str]:
        operator = row.get("operator", {})
        operator_name = str(operator.get("operator", ""))
        source_id = str(row.get("source_id", ""))
        component_id = str(row.get("content_component_id", ""))
        if row.get("language") != "shell" or operator_name not in SHELL_V13_OPERATORS:
            return False, "not_shell_v13"
        if enforce_diversity and source_id in used_sources:
            return False, "source_already_selected"
        if enforce_diversity and component_id and component_id in used_components:
            return False, "component_already_selected"
        if require_new_operator and operator_name in used_operators:
            return False, "operator_already_selected"
        package_root = Path(row["source_local_root"]) / row["relative_root"]
        script_path = package_root / operator["path"]
        skill_path = _skill_markdown(package_root) if package_root.is_dir() else None
        if not script_path.is_file():
            return False, "script_missing"
        if skill_path is None:
            return False, "skill_markdown_missing_or_ambiguous"
        if script_path.stat().st_size > max_script_bytes:
            return False, "script_too_large"
        if skill_path.stat().st_size > max_skill_bytes:
            return False, "skill_markdown_too_large"
        try:
            oracle = _read_utf8_exact(script_path)
            _read_utf8_exact(skill_path)
        except (OSError, UnicodeError):
            return False, "source_not_utf8"
        if sha256_bytes(oracle.encode("utf-8")) != operator.get("source_hash"):
            return False, "source_hash_mismatch"
        try:
            mutant = apply_shell_behavior_operator_v13(oracle, operator)
            oracle_analysis = _shell_analysis(oracle)
            mutant_analysis = _shell_analysis(mutant)
            if not oracle_analysis["valid"]:
                return False, "oracle_shell_parse_failed"
            if not mutant_analysis["valid"]:
                return False, "mutant_shell_parse_failed"
            scope = _target_scope(oracle, operator, oracle_analysis["records"])
            found_oracle, oracle_scope = _path_value(oracle_analysis["ast"], scope["path"])
            found_mutant, mutant_scope = _path_value(mutant_analysis["ast"], scope["path"])
            if not found_oracle or not found_mutant or oracle_scope == mutant_scope:
                return False, "mutation_not_ast_visible_at_target_scope"
        except (KeyError, SyntaxError, ValueError):
            return False, "operator_or_ast_replay_failed"
        return True, "eligible"

    passes = (False,) if transformation_ids else (True, False)
    for require_new_operator in passes:
        for row in candidates:
            if len(selected) >= case_count:
                break
            if row in selected:
                continue
            ok, reason = eligible(row, require_new_operator=require_new_operator)
            if not ok:
                exclusions.append(
                    {
                        "transformation_id": row.get("transformation_id"),
                        "operator_candidate_id": row.get("operator", {}).get(
                            "operator_candidate_id"
                        ),
                        "reason": reason,
                    }
                )
                continue
            selected.append(row)
            used_sources.add(str(row.get("source_id", "")))
            component_id = str(row.get("content_component_id", ""))
            if component_id:
                used_components.add(component_id)
            used_operators.add(str(row["operator"]["operator"]))
    if len(selected) != case_count:
        raise RuntimeError(f"selected {len(selected)} of {case_count} requested cases")
    return selected, exclusions


def prepare_stage(
    attempt_pools: list[Path],
    output_root: Path,
    *,
    case_count: int = 4,
    max_script_bytes: int = 24_000,
    max_skill_bytes: int = 48_000,
    model: str = "gpt-5.5",
    base_url: str = "https://api.openlux.ai/v1",
    timeout: int = 600,
    max_attempts: int = 2,
    api_protocol: str = "chat-completions",
    max_output_tokens: int = 8_192,
    transformation_ids: tuple[str, ...] = (),
    operator_priority: tuple[str, ...] = DEFAULT_OPERATOR_PRIORITY,
) -> dict[str, Any]:
    if api_protocol not in {"chat-completions", "responses"}:
        raise ValueError(f"unsupported api protocol: {api_protocol}")
    runtime = _tree_sitter_runtime()
    root = output_root.resolve()
    if root.exists():
        raise FileExistsError(root)
    root.mkdir(parents=True)
    public_root = root / "public"
    private_root = root / "_private"
    public_root.mkdir()
    private_root.mkdir()
    attempts, pool_receipts = _load_attempts(attempt_pools)
    selected, exclusions = select_shell_v13_cases(
        attempts,
        case_count=case_count,
        max_script_bytes=max_script_bytes,
        max_skill_bytes=max_skill_bytes,
        transformation_ids=transformation_ids,
        operator_priority=operator_priority,
    )
    public_cases: list[dict[str, Any]] = []
    private_cases: list[dict[str, Any]] = []
    for attempt in selected:
        operator = attempt["operator"]
        case_id = _case_id(attempt)
        package_root = Path(attempt["source_local_root"]) / attempt["relative_root"]
        script_path = package_root / operator["path"]
        skill_path = _skill_markdown(package_root)
        if skill_path is None:
            raise RuntimeError(f"selected package lost SKILL.md: {package_root}")
        oracle = _read_utf8_exact(script_path)
        mutant = apply_shell_behavior_operator_v13(oracle, operator)
        skill_markdown = _sanitize_skill_provenance(
            _read_utf8_exact(skill_path),
            source_repo_url=attempt.get("source_repo_url"),
            source_commit=attempt.get("source_commit"),
        )
        oracle_analysis = _shell_analysis(oracle)
        mutant_analysis = _shell_analysis(mutant)
        if not oracle_analysis["valid"] or not mutant_analysis["valid"]:
            raise RuntimeError(f"selected case no longer parses: {case_id}")
        scope = _target_scope(oracle, operator, oracle_analysis["records"])

        public_case_root = public_root / "cases" / case_id
        public_case_root.mkdir(parents=True)
        (public_case_root / "mutant.sh").write_text(mutant, encoding="utf-8")
        (public_case_root / "SKILL.md").write_text(skill_markdown, encoding="utf-8")
        write_json(
            public_case_root / "TASK.json",
            {
                "task_id": case_id,
                "objective": (
                    "Repair one localized behavioral regression in the visible Shell script "
                    "using only the supplied evidence."
                ),
                "constraints": [
                    "Preserve command-line interfaces and unrelated behavior.",
                    "Do not refactor, add features, or rewrite documentation.",
                    "Return only minimal exact source replacements in the required JSON schema.",
                ],
            },
        )

        private_case_root = private_root / "cases" / case_id
        private_case_root.mkdir(parents=True)
        (private_case_root / "oracle.sh").write_text(oracle, encoding="utf-8")
        label = {
            "case_id": case_id,
            "source_id": attempt.get("source_id"),
            "source_commit": attempt.get("source_commit"),
            "source_repo_url": attempt.get("source_repo_url"),
            "relative_root": attempt.get("relative_root"),
            "content_component_id": attempt.get("content_component_id"),
            "target_path": operator["path"],
            "transformation_id": attempt.get("transformation_id"),
            "operator": operator,
            "target_scope": scope,
            "oracle_sha256": sha256_bytes(oracle.encode("utf-8")),
            "oracle_ast_hash": oracle_analysis["ast_hash"],
            "mutant_sha256": sha256_bytes(mutant.encode("utf-8")),
            "mutant_ast_hash": mutant_analysis["ast_hash"],
        }
        write_json(private_case_root / "label.json", label)
        public_cases.append(
            {
                "case_id": case_id,
                "public_case_path": f"cases/{case_id}",
                "target_path": operator["path"],
                "mutant_sha256": label["mutant_sha256"],
                "skill_markdown_sha256": sha256_bytes(skill_markdown.encode("utf-8")),
            }
        )
        private_cases.append(label)

    public_manifest = {
        "schema_version": "0.16-shell-api-smoke-public-v1",
        "created_at": _utc_now(),
        "cases": public_cases,
        "claim_boundary": (
            "This stage tests bounded Shell regression repairability and SKILL.md visibility. "
            "It does not establish whole-package runtime correctness or AST-guided causality."
        ),
    }
    write_json(public_root / "manifest.json", public_manifest)
    write_json(
        private_root / "manifest.json",
        {
            "schema_version": "0.16-shell-api-smoke-private-v1",
            "created_at": _utc_now(),
            "cases": private_cases,
        },
    )
    calls = [
        {
            "trial_id": f"{case['case_id']}--{condition}--r1",
            "case_id": case["case_id"],
            "condition": condition,
            "repeat": 1,
        }
        for case in public_cases
        for condition in CONDITIONS
    ]
    plan = {
        "schema_version": "0.16-shell-api-smoke-plan-v1",
        "status": "frozen_before_model_calls",
        "created_at": _utc_now(),
        "model": model,
        "base_url": base_url,
        "api_protocol": api_protocol,
        "temperature": 0 if api_protocol == "chat-completions" else None,
        "max_output_tokens": max_output_tokens if api_protocol == "responses" else None,
        "response_mode": "exact_replacement_edits_v1",
        "response_limits": {
            "minimum_edit_count": 1,
            "maximum_edit_count": 4,
            "maximum_single_edit_bytes": 8_000,
            "maximum_total_edit_span_bytes": 12_000,
        },
        "input_limits": {
            "maximum_script_bytes": max_script_bytes,
            "maximum_skill_bytes": max_skill_bytes,
        },
        "timeout": timeout,
        "max_attempts": max_attempts,
        "repeats": 1,
        "conditions": list(CONDITIONS),
        "calls": calls,
        "public_tree_hashes": hash_tree(public_root),
        "public_manifest_hash": canonical_json_hash(public_manifest),
        "hidden_evaluation_loaded": False,
    }
    plan["plan_hash"] = canonical_json_hash(plan)
    write_json(root / "FROZEN_PLAN.json", plan)
    record = {
        "schema_version": "0.16-shell-api-smoke-prepare-v1",
        "status": "ready_for_model_smoke",
        "created_at": _utc_now(),
        "attempt_pools": pool_receipts,
        "selected_case_count": len(public_cases),
        "model_call_count": len(calls),
        "selection_mode": (
            "explicit_model_independent_preflight"
            if transformation_ids
            else "deterministic_operator_priority"
        ),
        "selected_transformation_ids": [row["transformation_id"] for row in private_cases],
        "operator_counts": {
            name: sum(1 for row in private_cases if row["operator"]["operator"] == name)
            for name in sorted({row["operator"]["operator"] for row in private_cases})
        },
        "source_count": len({row["source_id"] for row in private_cases}),
        "component_count": len(
            {row["content_component_id"] for row in private_cases if row["content_component_id"]}
        ),
        "selection_exclusions": exclusions,
        "tree_sitter_runtime": runtime,
        "public_tree_hashes": plan["public_tree_hashes"],
        "private_tree_hashes": hash_tree(private_root),
        "plan_hash": plan["plan_hash"],
        "response_mode": plan["response_mode"],
        "input_limits": plan["input_limits"],
        "model_calls": 0,
        "credential_persisted": False,
    }
    write_json(root / "PREPARE_RECORD.json", record)
    return record


def _visible_prompt(public_case_root: Path, *, condition: str) -> str:
    if condition not in CONDITIONS:
        raise ValueError(condition)
    task = read_json(public_case_root / "TASK.json")
    visible: list[dict[str, str]] = [
        {"path": "TASK.json", "content": json.dumps(task, indent=2, sort_keys=True)},
        {"path": "script.sh", "content": _read_utf8_exact(public_case_root / "mutant.sh")},
    ]
    if condition == "skill-script":
        visible.append(
            {"path": "SKILL.md", "content": _read_utf8_exact(public_case_root / "SKILL.md")}
        )
    return (
        "Perform exactly one bounded repair. One localized behavioral regression was introduced "
        "into script.sh. Use only visible evidence, preserve interfaces and unrelated behavior, "
        f"and do not add features or refactor. {exact_replacement_instruction('script.sh')}\n\n"
        f"Visible files:\n{json.dumps(visible, indent=2, ensure_ascii=True)}\n"
    )


def _parse_exact_edit_response(
    content: str,
    public_case_root: Path,
) -> tuple[str, str, dict[str, Any]]:
    def validate_candidate(source: str) -> None:
        analysis = _shell_analysis(source)
        if not analysis["valid"]:
            raise SyntaxError(
                analysis["tree_sitter_error"] or analysis["bash_n_error"] or "invalid shell"
            )

    return parse_and_apply_exact_replacements(
        content,
        _read_utf8_exact(public_case_root / "mutant.sh"),
        parse_json=_parse_model_json,
        validate_candidate=validate_candidate,
    )


CallFunction = Callable[..., tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any]]]


def run_one_trial(
    public_root: Path,
    trial: dict[str, Any],
    output_root: Path,
    *,
    api_key: str,
    model: str,
    base_url: str,
    timeout: int,
    max_attempts: int,
    api_protocol: str = "chat-completions",
    call_function: CallFunction = _call_chat_completion,
) -> dict[str, Any]:
    trial_root = output_root / trial["trial_id"]
    if trial_root.exists():
        raise FileExistsError(trial_root)
    trial_root.mkdir(parents=True)
    prompt = _visible_prompt(
        public_root / "cases" / trial["case_id"], condition=trial["condition"]
    )
    (trial_root / "prompt.txt").write_text(prompt, encoding="utf-8")
    system_prompt = (
        "You repair one visible Agent Skill Shell script. Never request hidden tests, mutation "
        "labels, gold code, verifier output, or oracle behavior. Return strict JSON only."
    )
    response, attempts, request_payload = call_function(
        api_key=api_key,
        base_url=base_url,
        model=model,
        system_prompt=system_prompt,
        user_prompt=prompt,
        timeout=timeout,
        max_attempts=max_attempts,
    )
    if response is not None:
        write_json(trial_root / "provider_response.json", response)
    write_json(trial_root / "request_payload.json", request_payload)
    content_error: str | None = None
    try:
        content = _model_content(response) if response is not None else ""
    except (KeyError, TypeError, ValueError) as exc:
        content = ""
        content_error = f"{type(exc).__name__}:{exc}"
    (trial_root / "raw_response.txt").write_text(content, encoding="utf-8")
    parse_error: str | None = content_error
    candidate_source: str | None = None
    summary: str | None = None
    response_application: dict[str, Any] | None = None
    if content_error is None:
        try:
            candidate_source, summary, response_application = _parse_exact_edit_response(
                content, public_root / "cases" / trial["case_id"]
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, SyntaxError) as exc:
            parse_error = f"{type(exc).__name__}:{exc}"
    if candidate_source is not None:
        candidate_root = trial_root / "candidate"
        candidate_root.mkdir()
        (candidate_root / "script.sh").write_text(candidate_source, encoding="utf-8")
        write_json(trial_root / "RESPONSE_APPLICATION.json", response_application)
    if response is None:
        freeze_status = "provider_unavailable_no_candidate"
    elif candidate_source is None:
        freeze_status = "invalid_response_frozen"
    else:
        freeze_status = "candidate_frozen"
    freeze = {
        "schema_version": "0.16-shell-api-smoke-candidate-freeze-v1",
        "trial_id": trial["trial_id"],
        "status": freeze_status,
        "created_at": _utc_now(),
        "prompt_sha256": sha256_file(trial_root / "prompt.txt"),
        "raw_response_sha256": sha256_file(trial_root / "raw_response.txt"),
        "candidate_sha256": (
            sha256_bytes(candidate_source.encode("utf-8")) if candidate_source is not None else None
        ),
        "candidate_ast_hash": (
            _shell_ast_hash(candidate_source) if candidate_source is not None else None
        ),
        "response_application_sha256": (
            sha256_file(trial_root / "RESPONSE_APPLICATION.json")
            if candidate_source is not None
            else None
        ),
        "hidden_evaluation_loaded": False,
    }
    write_json(trial_root / "CANDIDATE_FREEZE.json", freeze)
    credential_persisted = not _credential_absent(trial_root, api_key)
    record = {
        "schema_version": "0.16-shell-api-smoke-run-v1",
        "trial_id": trial["trial_id"],
        "case_id": trial["case_id"],
        "condition": trial["condition"],
        "repeat": trial["repeat"],
        "status": freeze_status if not credential_persisted else "credential_persistence_failure",
        "created_at": _utc_now(),
        "provider": base_url,
        "model": model,
        "api_protocol": api_protocol,
        "temperature": 0 if api_protocol == "chat-completions" else None,
        "transport_attempt_count": len(attempts),
        "completed_model_response_count": int(response is not None),
        "model_calls": int(response is not None),
        "attempts": attempts,
        "response_id": response.get("id") if response else None,
        "usage": _normalized_usage(response.get("usage")) if response else None,
        "request_hash": canonical_json_hash(request_payload),
        "prompt_hash": canonical_json_hash(prompt),
        "parse_error": parse_error,
        "model_summary": summary,
        "response_mode": (
            response_application.get("response_mode") if response_application else None
        ),
        "candidate_freeze_hash": canonical_json_hash(freeze),
        "hidden_evaluation_loaded": False,
        "credential_persisted": credential_persisted,
    }
    write_json(trial_root / "RUN_RECORD.json", record)
    if credential_persisted:
        raise RuntimeError("API credential appeared in persisted trial artifacts")
    return record


def run_stage(
    public_root: Path,
    plan_path: Path,
    output_root: Path,
    *,
    api_key: str,
    workers: int = 4,
    trial_ids: set[str] | None = None,
    call_function: CallFunction | None = None,
) -> dict[str, Any]:
    _tree_sitter_runtime()
    public = public_root.resolve()
    output = output_root.resolve()
    if output.exists():
        raise FileExistsError(output)
    plan = read_json(plan_path)
    api_protocol = str(plan.get("api_protocol", "chat-completions"))
    if api_protocol not in {"chat-completions", "responses"}:
        raise ValueError(f"unsupported api protocol: {api_protocol}")
    if call_function is None:
        if api_protocol == "responses":
            max_output_tokens = int(plan.get("max_output_tokens") or 8_192)

            def selected_call_function(**kwargs: Any):
                return _call_responses(
                    **kwargs,
                    max_output_tokens=max_output_tokens,
                )

            call_function = selected_call_function
        else:
            call_function = _call_chat_completion
    if plan.get("response_mode") != "exact_replacement_edits_v1":
        raise ValueError("shell_operator_smoke_stage_response_mode_mismatch")
    if plan.get("status") != "frozen_before_model_calls":
        raise ValueError("plan is not frozen")
    expected_hash = canonical_json_hash({key: value for key, value in plan.items() if key != "plan_hash"})
    if expected_hash != plan.get("plan_hash"):
        raise ValueError("plan hash mismatch")
    if hash_tree(public) != plan.get("public_tree_hashes"):
        raise ValueError("public tree changed after plan freeze")
    calls = list(plan["calls"])
    if trial_ids is not None:
        known = {row["trial_id"] for row in calls}
        if trial_ids - known:
            raise ValueError(f"trial ids are not in frozen plan: {sorted(trial_ids - known)}")
        calls = [row for row in calls if row["trial_id"] in trial_ids]
    if not calls:
        raise ValueError("no trials selected")
    output.mkdir(parents=True)
    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(calls)))) as executor:
        futures = {
            executor.submit(
                run_one_trial,
                public,
                trial,
                output,
                api_key=api_key,
                model=plan["model"],
                base_url=plan["base_url"],
                timeout=int(plan["timeout"]),
                max_attempts=int(plan["max_attempts"]),
                api_protocol=api_protocol,
                call_function=call_function,
            ): trial
            for trial in calls
        }
        for future in as_completed(futures):
            records.append(future.result())
    records.sort(key=lambda row: row["trial_id"])
    summary = {
        "schema_version": "0.16-shell-api-smoke-batch-run-v1",
        "status": (
            "all_candidates_frozen"
            if all(row["status"] == "candidate_frozen" for row in records)
            else "completed_with_invalid_trials"
        ),
        "created_at": _utc_now(),
        "plan_hash": plan["plan_hash"],
        "frozen_plan_trial_count": len(plan["calls"]),
        "trial_count": len(records),
        "is_partial_transport_recovery_run": len(records) != len(plan["calls"]),
        "candidate_frozen_count": sum(row["status"] == "candidate_frozen" for row in records),
        "invalid_trial_count": sum(row["status"] != "candidate_frozen" for row in records),
        "transport_attempt_count": sum(int(row["transport_attempt_count"]) for row in records),
        "completed_model_response_count": sum(
            int(row["completed_model_response_count"]) for row in records
        ),
        "model_calls": sum(int(row["completed_model_response_count"]) for row in records),
        "usage": {
            key: sum(int((row.get("usage") or {}).get(key, 0)) for row in records)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "hidden_evaluation_loaded": False,
        "credential_persisted": False,
        "trials": records,
    }
    write_json(output / "BATCH_RUN_SUMMARY.json", summary)
    return summary


def _scope_leaf_texts(scope: Any) -> list[str]:
    if not isinstance(scope, dict):
        return []
    children = scope.get("children")
    if isinstance(children, list):
        values: list[str] = []
        for child in children:
            values.extend(_scope_leaf_texts(child))
        return values
    value = scope.get("text")
    return [value] if isinstance(value, str) else []


def _canonical_operator_scope(scope: Any, operator: str) -> Any:
    if not isinstance(scope, dict):
        return scope
    node_type = scope.get("type")
    children = scope.get("children")

    if operator == "toggle_shell_exit_status_v13" and node_type == "command":
        leaves = _scope_leaf_texts(scope)
        if len(leaves) == 2 and leaves[0] == "exit" and leaves[1].isdigit():
            return {
                "type": "command",
                "command": "exit",
                "status_class": "zero" if int(leaves[1]) == 0 else "nonzero",
            }

    if operator == "swap_shell_output_stream_v13" and node_type == "file_redirect":
        leaves = _scope_leaf_texts(scope)
        if ">&" in leaves:
            index = leaves.index(">&")
            source_fd = leaves[index - 1] if index == 1 else "1" if index == 0 else None
            destination_fd = leaves[index + 1] if index + 1 < len(leaves) else None
            expected_count = 3 if index == 1 else 2
            if (
                len(leaves) == expected_count
                and source_fd in {"0", "1", "2"}
                and destination_fd in {"0", "1", "2"}
            ):
                return {
                    "type": "file_redirect",
                    "source_fd": source_fd,
                    "destination_fd": destination_fd,
                }

    canonical: dict[str, Any] = {"type": node_type}
    if isinstance(children, list):
        canonical["children"] = [
            _canonical_operator_scope(child, operator) for child in children
        ]
    elif isinstance(scope.get("text"), str):
        canonical["text"] = scope["text"]
    return canonical


def _operator_semantic_target_equivalence(
    operator: str, oracle_scope: Any, candidate_scope: Any
) -> dict[str, Any]:
    if operator == "toggle_shell_exit_status_v13":
        oracle_normalized = _canonical_operator_scope(oracle_scope, operator)
        candidate_normalized = _canonical_operator_scope(candidate_scope, operator)
        equivalent = oracle_normalized == candidate_normalized and oracle_normalized != oracle_scope
        return {
            "equivalent": equivalent,
            "reason": (
                "nonzero_exit_status_contract"
                if equivalent
                else "exit_status_contract_or_command_mismatch"
            ),
        }

    if operator == "swap_shell_output_stream_v13":
        oracle_normalized = _canonical_operator_scope(oracle_scope, operator)
        candidate_normalized = _canonical_operator_scope(candidate_scope, operator)
        equivalent = oracle_normalized == candidate_normalized and oracle_normalized != oracle_scope
        return {
            "equivalent": equivalent,
            "reason": (
                "explicit_implicit_fd_redirection_equivalence"
                if equivalent
                else "redirection_target_or_command_mismatch"
            ),
        }

    return {"equivalent": False, "reason": "operator_has_no_semantic_equivalence_backend"}


def evaluate_stage(
    public_root: Path,
    private_root: Path,
    plan_path: Path,
    runs_root: Path,
    output_path: Path,
    *,
    allow_operator_semantic_equivalence: bool = False,
) -> dict[str, Any]:
    runtime = _tree_sitter_runtime()
    public = public_root.resolve()
    private = private_root.resolve()
    runs = runs_root.resolve()
    plan = read_json(plan_path)
    if hash_tree(public) != plan.get("public_tree_hashes"):
        raise ValueError("public tree changed after plan freeze")
    labels = {
        row["case_id"]: row for row in read_json(private / "manifest.json")["cases"]
    }
    rows: list[dict[str, Any]] = []
    for trial in plan["calls"]:
        trial_root = runs / trial["trial_id"]
        freeze = read_json(trial_root / "CANDIDATE_FREEZE.json")
        run_record = read_json(trial_root / "RUN_RECORD.json")
        label = labels[trial["case_id"]]
        oracle = _read_utf8_exact(private / "cases" / trial["case_id"] / "oracle.sh")
        mutant = _read_utf8_exact(public / "cases" / trial["case_id"] / "mutant.sh")
        prompt = _read_utf8_exact(trial_root / "prompt.txt")
        leakage = _prompt_leakage_report(prompt, label)
        candidate_path = trial_root / "candidate" / "script.sh"
        candidate_source: str | None = None
        candidate_analysis: dict[str, Any] = {
            "valid": False,
            "bash_n_ok": False,
            "tree_sitter_ok": False,
            "ast_hash": None,
            "ast": None,
        }
        freeze_integrity = False
        if candidate_path.is_file() and freeze.get("candidate_sha256"):
            candidate_source = _read_utf8_exact(candidate_path)
            freeze_integrity = (
                sha256_bytes(candidate_source.encode("utf-8")) == freeze["candidate_sha256"]
            )
            if freeze_integrity:
                candidate_analysis = _shell_analysis(candidate_source)
                freeze_integrity = (
                    candidate_analysis["ast_hash"] == freeze.get("candidate_ast_hash")
                )
        oracle_analysis = _shell_analysis(oracle)
        mutant_analysis = _shell_analysis(mutant)
        source_mutant_diffs = _leaf_diffs(oracle_analysis["ast"], mutant_analysis["ast"])
        candidate_oracle_diffs = (
            _leaf_diffs(oracle_analysis["ast"], candidate_analysis["ast"])
            if candidate_analysis["valid"]
            else []
        )
        scope_path = label["target_scope"]["path"]
        found_oracle, oracle_scope = _path_value(oracle_analysis["ast"], scope_path)
        found_mutant, mutant_scope = _path_value(mutant_analysis["ast"], scope_path)
        found_candidate, candidate_scope = (
            _path_value(candidate_analysis["ast"], scope_path)
            if candidate_analysis["valid"]
            else (False, None)
        )
        target_restored = (
            found_oracle
            and found_mutant
            and found_candidate
            and oracle_scope != mutant_scope
            and candidate_scope == oracle_scope
        )
        semantic_equivalence = (
            _operator_semantic_target_equivalence(
                label["operator"]["operator"], oracle_scope, candidate_scope
            )
            if allow_operator_semantic_equivalence
            and found_oracle
            and found_candidate
            and not target_restored
            else {"equivalent": False, "reason": "not_enabled_or_not_needed"}
        )
        target_contract_restored = bool(
            target_restored or semantic_equivalence["equivalent"]
        )
        outside_scope_diffs = [
            row
            for row in candidate_oracle_diffs
            if row["path"][: len(scope_path)] != scope_path
        ]
        changed_from_mutant = bool(
            candidate_source is not None
            and sha256_bytes(candidate_source.encode("utf-8")) != label["mutant_sha256"]
        )
        ast_exact = bool(
            candidate_analysis["valid"]
            and candidate_analysis["ast_hash"] == label["oracle_ast_hash"]
        )
        bounded = bool(
            candidate_analysis["valid"]
            and freeze_integrity
            and changed_from_mutant
            and target_restored
            and not outside_scope_diffs
        )
        semantic_bounded = bool(
            candidate_analysis["valid"]
            and freeze_integrity
            and changed_from_mutant
            and target_contract_restored
            and not outside_scope_diffs
        )
        repair_class = (
            "exact_oracle_ast_repair"
            if ast_exact
            else "target_scope_repaired_without_external_ast_edits"
            if bounded
            else "operator_semantic_target_only_repair"
            if semantic_bounded and semantic_equivalence["equivalent"]
            else "target_repaired_with_external_ast_edits"
            if target_restored
            else "operator_semantic_target_repaired_with_external_ast_edits"
            if target_contract_restored and semantic_equivalence["equivalent"]
            else "target_missed_with_other_ast_edits"
            if candidate_oracle_diffs
            else "ast_noop_or_invalid"
        )
        row = {
                "trial_id": trial["trial_id"],
                "case_id": trial["case_id"],
                "condition": trial["condition"],
                "repeat": trial["repeat"],
                "operator": label["operator"]["operator"],
                "source_id": label["source_id"],
                "transformation_id": label["transformation_id"],
                "target_scope_type": label["target_scope"]["type"],
                "candidate_valid_shell": bool(candidate_analysis["valid"]),
                "candidate_bash_n_ok": bool(candidate_analysis["bash_n_ok"]),
                "candidate_tree_sitter_ok": bool(candidate_analysis["tree_sitter_ok"]),
                "candidate_freeze_integrity": freeze_integrity,
                "candidate_changed_from_mutant": changed_from_mutant,
                "exact_oracle_ast_match": ast_exact,
                "mutation_target_scope_restored": target_restored,
                "outside_target_scope_ast_diff_count": len(outside_scope_diffs),
                "outside_target_scope_ast_diff_paths": [
                    row["path"] for row in outside_scope_diffs[:10]
                ],
                "source_mutant_ast_leaf_diff_count": len(source_mutant_diffs),
                "candidate_oracle_ast_leaf_diff_count": len(candidate_oracle_diffs),
                "bounded_structural_repair_pass": bounded,
                "repair_class": repair_class,
                "prompt_private_label_leakage": leakage,
                "model_calls": int(run_record.get("completed_model_response_count", 0)),
                "usage": run_record.get("usage"),
            }
        if allow_operator_semantic_equivalence:
            row.update(
                {
                    "operator_semantic_equivalence": semantic_equivalence,
                    "operator_target_contract_restored": target_contract_restored,
                    "bounded_operator_semantic_repair_pass": semantic_bounded,
                }
            )
        rows.append(row)
    condition_summary: dict[str, Any] = {}
    for condition in CONDITIONS:
        condition_rows = [row for row in rows if row["condition"] == condition]
        condition_summary[condition] = {
            "trial_count": len(condition_rows),
            "bounded_structural_repair_pass_count": sum(
                row["bounded_structural_repair_pass"] for row in condition_rows
            ),
            "exact_oracle_ast_match_count": sum(
                row["exact_oracle_ast_match"] for row in condition_rows
            ),
            "target_scope_restored_count": sum(
                row["mutation_target_scope_restored"] for row in condition_rows
            ),
            "changed_count": sum(row["candidate_changed_from_mutant"] for row in condition_rows),
            "invalid_count": sum(not row["candidate_valid_shell"] for row in condition_rows),
            "mean_candidate_oracle_ast_leaf_diff_count": (
                sum(row["candidate_oracle_ast_leaf_diff_count"] for row in condition_rows)
                / len(condition_rows)
                if condition_rows
                else math.nan
            ),
        }
        if allow_operator_semantic_equivalence:
            condition_summary[condition].update(
                {
                    "operator_target_contract_restored_count": sum(
                        row["operator_target_contract_restored"] for row in condition_rows
                    ),
                    "bounded_operator_semantic_repair_pass_count": sum(
                        row["bounded_operator_semantic_repair_pass"] for row in condition_rows
                    ),
                }
            )
    pairwise: list[dict[str, Any]] = []
    for case_id in sorted(labels):
        case_rows = {row["condition"]: row for row in rows if row["case_id"] == case_id}
        pass_field = (
            "bounded_operator_semantic_repair_pass"
            if allow_operator_semantic_equivalence
            else "bounded_structural_repair_pass"
        )
        raw_pass = bool(case_rows["raw-script"][pass_field])
        skill_pass = bool(case_rows["skill-script"][pass_field])
        outcome = (
            "skill_only"
            if skill_pass and not raw_pass
            else "raw_only"
            if raw_pass and not skill_pass
            else "both"
            if raw_pass and skill_pass
            else "neither"
        )
        pairwise.append(
            {
                "case_id": case_id,
                "raw_script_pass": raw_pass,
                "skill_script_pass": skill_pass,
                "paired_outcome": outcome,
            }
        )
    report = {
        "schema_version": (
            "0.16-shell-api-smoke-hidden-evaluation-v2"
            if allow_operator_semantic_equivalence
            else "0.16-shell-api-smoke-hidden-evaluation-v1"
        ),
        "status": "complete",
        "created_at": _utc_now(),
        "plan_hash": plan["plan_hash"],
        "tree_sitter_runtime": runtime,
        "claim_boundary": (
            "A semantic-v2 pass requires candidate freeze integrity, Tree-sitter Bash and bash -n "
            "validity, zero AST differences outside the target scope, and either exact target "
            "restoration or one of two whitelisted shell equivalences: non-zero exit statuses, or "
            "implicit versus explicit stdout-to-stderr redirection. It is bounded operator-contract "
            "repair, not whole-script behavioral correctness, documentation benefit, AST-guided "
            "causality, or transferable evolution."
            if allow_operator_semantic_equivalence
            else "A pass means the candidate is valid under both Tree-sitter Bash and bash -n, restores "
            "the held-out mutation target AST subtree, and introduces no AST change outside that "
            "subtree. This is bounded structural mutation inversion, not whole-script behavioral "
            "correctness, documentation benefit, AST-guided causality, or transferable evolution."
        ),
        "condition_summary": condition_summary,
        "paired_summary": {
            outcome: sum(row["paired_outcome"] == outcome for row in pairwise)
            for outcome in ("skill_only", "raw_only", "both", "neither")
        },
        "all_freezes_integral": all(row["candidate_freeze_integrity"] for row in rows),
        "all_prompts_private_label_free": all(
            row["prompt_private_label_leakage"]["status"] == "pass" for row in rows
        ),
        "model_calls": sum(int(row["model_calls"]) for row in rows),
        "rows": rows,
        "pairwise": pairwise,
    }
    write_json(output_path, report)
    return report


def _operator_priority(values: Iterable[str] | None) -> tuple[str, ...]:
    return tuple(values) if values else DEFAULT_OPERATOR_PRIORITY


def main() -> int:
    parser = argparse.ArgumentParser(description="Frozen v0.16 Shell operator API smoke")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--attempt-pool", action="append", type=Path, required=True)
    prepare.add_argument("--output-root", type=Path, required=True)
    prepare.add_argument("--case-count", type=int, default=4)
    prepare.add_argument("--max-script-bytes", type=int, default=24_000)
    prepare.add_argument("--max-skill-bytes", type=int, default=48_000)
    prepare.add_argument("--model", default="gpt-5.5")
    prepare.add_argument("--base-url", default="https://api.openlux.ai/v1")
    prepare.add_argument("--timeout", type=int, default=600)
    prepare.add_argument("--max-attempts", type=int, default=2)
    prepare.add_argument(
        "--api-protocol",
        choices=("chat-completions", "responses"),
        default="chat-completions",
    )
    prepare.add_argument("--max-output-tokens", type=int, default=8_192)
    prepare.add_argument("--transformation-id", action="append")
    prepare.add_argument("--operator", action="append")

    run = subparsers.add_parser("run")
    run.add_argument("--public-root", type=Path, required=True)
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument("--workers", type=int, default=4)
    run.add_argument("--trial-id", action="append")

    consolidate = subparsers.add_parser("consolidate")
    consolidate.add_argument("--plan", type=Path, required=True)
    consolidate.add_argument("--primary-runs-root", type=Path, required=True)
    consolidate.add_argument("--recovery-runs-root", action="append", type=Path, required=True)
    consolidate.add_argument("--output-root", type=Path, required=True)

    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--public-root", type=Path, required=True)
    evaluate.add_argument("--private-root", type=Path, required=True)
    evaluate.add_argument("--plan", type=Path, required=True)
    evaluate.add_argument("--runs-root", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    evaluate.add_argument("--allow-operator-semantic-equivalence", action="store_true")

    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare_stage(
            args.attempt_pool,
            args.output_root,
            case_count=args.case_count,
            max_script_bytes=args.max_script_bytes,
            max_skill_bytes=args.max_skill_bytes,
            model=args.model,
            base_url=args.base_url,
            timeout=args.timeout,
            max_attempts=args.max_attempts,
            api_protocol=args.api_protocol,
            max_output_tokens=args.max_output_tokens,
            transformation_ids=tuple(args.transformation_id or ()),
            operator_priority=_operator_priority(args.operator),
        )
    elif args.command == "run":
        result = run_stage(
            args.public_root,
            args.plan,
            args.output_root,
            api_key=getpass.getpass("OpenLux API key: "),
            workers=args.workers,
            trial_ids=set(args.trial_id) if args.trial_id else None,
        )
    elif args.command == "consolidate":
        result = consolidate_transport_recovery_runs(
            args.plan,
            args.primary_runs_root,
            args.recovery_runs_root,
            args.output_root,
        )
    else:
        result = evaluate_stage(
            args.public_root,
            args.private_root,
            args.plan,
            args.runs_root,
            args.output,
            allow_operator_semantic_equivalence=(
                args.allow_operator_semantic_equivalence
            ),
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
