from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from .io_utils import hash_tree, read_json, stable_hash
from .multihop_defuse import (
    _canonical_dump,
    _index_package,
    analyze_multihop_parameter,
    extract_visible_multihop_contract,
)


SCHEMA_VERSION = "bvi.visible_invariant_gate.v1"
ACCEPT_VISIBLE_INVARIANT = "ACCEPT_VISIBLE_INVARIANT"
ABSTAIN_VISIBLE_INVARIANT = "ABSTAIN_VISIBLE_INVARIANT"
REJECT_VISIBLE_INVARIANT = "REJECT_VISIBLE_INVARIANT"


def _function_signatures(package: Path) -> dict[str, str]:
    signatures: dict[str, str] = {}
    for path in sorted((package / "scripts").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                signatures[f"{relative}:{node.name}"] = _canonical_dump(node.args)
    return signatures


def _target_function(tree: ast.Module, symbol: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    matches = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol
    ]
    return matches[0] if len(matches) == 1 else None


def _function_cone_only(
    parent_tree: ast.Module,
    candidate_tree: ast.Module,
    *,
    symbol: str,
) -> bool:
    parent_target = _target_function(parent_tree, symbol)
    candidate_target = _target_function(candidate_tree, symbol)
    if parent_target is None or candidate_target is None:
        return False
    if _canonical_dump(parent_target.args) != _canonical_dump(candidate_target.args):
        return False

    def without_target(tree: ast.Module) -> str:
        reduced = ast.Module(
            body=[
                node
                for node in tree.body
                if not (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == symbol
                )
            ],
            type_ignores=[],
        )
        return _canonical_dump(reduced)

    return without_target(parent_tree) == without_target(candidate_tree)


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return ast.unparse(node.func)
    return ast.unparse(node.func)


def _function_inventory(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, Any]:
    ignored = {
        "Load",
        "Store",
        "Del",
        "arguments",
        "arg",
        "FunctionDef",
        "AsyncFunctionDef",
    }
    node_kinds = Counter(
        type(node).__name__
        for node in ast.walk(function)
        if type(node).__name__ not in ignored
    )
    literals = Counter(
        repr(node.value)
        for node in ast.walk(function)
        if isinstance(node, ast.Constant)
    )
    calls = Counter(
        _call_name(node)
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
    )
    return {
        "node_kinds": node_kinds,
        "literals": literals,
        "calls": calls,
    }


def _counter_subset(candidate: Counter[str], parent: Counter[str]) -> bool:
    return all(count <= parent.get(key, 0) for key, count in candidate.items())


def _introduces_no_new_ast_behavior(
    parent_tree: ast.Module,
    candidate_tree: ast.Module,
    *,
    symbol: str,
) -> bool:
    parent_target = _target_function(parent_tree, symbol)
    candidate_target = _target_function(candidate_tree, symbol)
    if parent_target is None or candidate_target is None:
        return False
    parent = _function_inventory(parent_target)
    candidate = _function_inventory(candidate_target)
    return all(
        _counter_subset(candidate[key], parent[key])
        for key in ("node_kinds", "literals", "calls")
    )


def _visible_invocation(case: Path) -> dict[str, Any]:
    trace_path = case / "evolution" / "UNLABELED_TRACE.json"
    if not trace_path.is_file():
        return {"status": "ABSTAIN", "reason": "unlabeled_trace_missing"}
    trace = read_json(trace_path)
    if (
        trace.get("correctness_label_available") is not False
        or trace.get("expected_output_available") is not False
        or trace.get("task_verifier_used") is not False
    ):
        return {"status": "ABSTAIN", "reason": "trace_not_answer_free"}
    command = trace.get("command_shape")
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        return {"status": "ABSTAIN", "reason": "command_shape_invalid"}
    option_pairs = [
        (command[index], command[index + 1])
        for index in range(len(command) - 1)
        if command[index].startswith("--")
        and command[index] not in {"--input", "--output"}
    ]
    inputs = sorted(
        path
        for path in (case / "task" / "environment" / "data").glob("*")
        if path.is_file()
    )
    if len(option_pairs) != 1 or len(inputs) != 1:
        return {
            "status": "ABSTAIN",
            "reason": "visible_invocation_not_unique",
            "option_count": len(option_pairs),
            "input_count": len(inputs),
        }
    task_text = (case / "task" / "task.md").read_text(encoding="utf-8")
    output_matches = re.findall(r"/workspace/output/([^\s`]+)", task_text)
    output_name = output_matches[-1].rstrip(".,") if output_matches else "artifact.out"
    return {
        "status": "READY",
        "input_path": str(inputs[0]),
        "option_flag": option_pairs[0][0],
        "option_value": option_pairs[0][1],
        "output_name": output_name,
        "trace_sha256": stable_hash(trace),
        "answer_free": True,
    }


def _run_visible(
    package: Path,
    invocation: dict[str, Any],
    *,
    explicit: bool,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="visible-invariant-") as temporary:
        output = Path(temporary) / invocation["output_name"]
        command = [
            sys.executable,
            str(package / "scripts" / "cli.py"),
            "--input",
            invocation["input_path"],
            "--output",
            str(output),
        ]
        if explicit:
            command.extend([invocation["option_flag"], invocation["option_value"]])
        completed = subprocess.run(
            command,
            cwd=package / "scripts",
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        output_bytes = output.read_bytes() if output.is_file() else None
    return {
        "success": completed.returncode == 0 and output_bytes is not None,
        "returncode": completed.returncode,
        "output_exists": output_bytes is not None,
        "output_sha256": hashlib.sha256(output_bytes).hexdigest() if output_bytes is not None else None,
        "stdout_sha256": stable_hash(completed.stdout),
        "stderr_sha256": stable_hash(completed.stderr),
        "command_shape": [
            "python",
            "scripts/cli.py",
            "--input",
            "<visible-input>",
            "--output",
            "<temporary-output>",
        ]
        + (
            [invocation["option_flag"], invocation["option_value"]]
            if explicit
            else []
        ),
    }


def evaluate_visible_invariant_candidate(
    case_root: str | Path,
    candidate_root: str | Path | None,
) -> dict[str, Any]:
    case = Path(case_root).resolve()
    contract = extract_visible_multihop_contract(case)
    base = {
        "schema_version": SCHEMA_VERSION,
        "contract": contract,
        "answer_free": True,
        "task_verifier_used": False,
        "expected_output_used": False,
        "hidden_artifacts_used": False,
    }
    if candidate_root is None:
        report = {
            **base,
            "decision": REJECT_VISIBLE_INVARIANT,
            "decision_reason": "candidate_unavailable",
        }
        report["report_hash"] = stable_hash(report)
        return report
    if contract.get("status") != "READY":
        report = {
            **base,
            "decision": ABSTAIN_VISIBLE_INVARIANT,
            "decision_reason": contract.get("reason"),
        }
        report["report_hash"] = stable_hash(report)
        return report
    invocation = _visible_invocation(case)
    if invocation.get("status") != "READY":
        report = {
            **base,
            "decision": ABSTAIN_VISIBLE_INVARIANT,
            "decision_reason": invocation.get("reason"),
            "visible_invocation": invocation,
        }
        report["report_hash"] = stable_hash(report)
        return report

    candidate = Path(candidate_root).resolve()
    parent = Path(contract["parent_skill_root"])
    parent_functions, parent_trees, parent_failures = _index_package(parent)
    candidate_functions, candidate_trees, candidate_failures = _index_package(candidate)
    target_path = contract["site"]["path"]
    target_symbol = contract["site"]["symbol"]
    parent_hashes = hash_tree(parent)
    candidate_hashes = hash_tree(candidate)
    changed_paths = sorted(
        path
        for path in set(parent_hashes) | set(candidate_hashes)
        if parent_hashes.get(path) != candidate_hashes.get(path)
    )
    structure_ready = bool(
        not parent_failures
        and not candidate_failures
        and set(parent_trees) == set(candidate_trees)
        and target_path in parent_trees
        and target_path in candidate_trees
    )
    signatures_preserved = False
    function_cone_only = False
    no_new_ast_behavior = False
    if structure_ready:
        signatures_preserved = _function_signatures(parent) == _function_signatures(candidate)
        function_cone_only = _function_cone_only(
            parent_trees[target_path],
            candidate_trees[target_path],
            symbol=target_symbol,
        )
        no_new_ast_behavior = _introduces_no_new_ast_behavior(
            parent_trees[target_path],
            candidate_trees[target_path],
            symbol=target_symbol,
        )
    docs_preserved = bool(
        (parent / "SKILL.md").is_file()
        and (candidate / "SKILL.md").is_file()
        and (parent / "SKILL.md").read_bytes() == (candidate / "SKILL.md").read_bytes()
    )
    changed_path_bounded = changed_paths == [target_path]
    flow = analyze_multihop_parameter(
        candidate,
        entry_symbol=contract["entry"]["symbol"],
        parameter=contract["parameter"],
        expected_source_parameter=contract["parameter"],
    )
    flow_closed = flow.get("status") == "READY" and bool(flow.get("flow_closed"))
    origin_pure = bool(flow.get("origin_pure"))

    parent_default = _run_visible(parent, invocation, explicit=False)
    parent_explicit = _run_visible(parent, invocation, explicit=True)
    candidate_default = _run_visible(candidate, invocation, explicit=False)
    candidate_explicit = _run_visible(candidate, invocation, explicit=True)
    executions_succeeded = all(
        row["success"]
        for row in (parent_default, parent_explicit, candidate_default, candidate_explicit)
    )
    parent_visible_invariance = bool(
        executions_succeeded
        and parent_default["output_sha256"] == parent_explicit["output_sha256"]
    )
    default_behavior_preserved = bool(
        executions_succeeded
        and parent_default["output_sha256"] == candidate_default["output_sha256"]
    )
    candidate_explicit_sensitivity = bool(
        executions_succeeded
        and candidate_default["output_sha256"] != candidate_explicit["output_sha256"]
    )
    candidate_changes_faulty_observation = bool(
        executions_succeeded
        and parent_explicit["output_sha256"] != candidate_explicit["output_sha256"]
    )
    checks = {
        "structure_ready": structure_ready,
        "signatures_preserved": signatures_preserved,
        "function_cone_only": function_cone_only,
        "no_new_ast_behavior": no_new_ast_behavior,
        "docs_preserved": docs_preserved,
        "changed_path_bounded": changed_path_bounded,
        "flow_closed": flow_closed,
        "origin_pure": origin_pure,
        "executions_succeeded": executions_succeeded,
        "parent_visible_invariance": parent_visible_invariance,
        "default_behavior_preserved": default_behavior_preserved,
        "candidate_explicit_sensitivity": candidate_explicit_sensitivity,
        "candidate_changes_faulty_observation": candidate_changes_faulty_observation,
    }
    decision = (
        ACCEPT_VISIBLE_INVARIANT
        if all(checks.values())
        else ABSTAIN_VISIBLE_INVARIANT
    )
    report = {
        **base,
        "decision": decision,
        "decision_reason": (
            "visible_structure_and_metamorphic_invariants_satisfied"
            if decision == ACCEPT_VISIBLE_INVARIANT
            else "visible_invariant_evidence_incomplete"
        ),
        "checks": checks,
        "changed_paths": changed_paths,
        "visible_invocation": invocation,
        "visible_executions": {
            "parent_default": parent_default,
            "parent_explicit": parent_explicit,
            "candidate_default": candidate_default,
            "candidate_explicit": candidate_explicit,
        },
        "flow_status": flow.get("status"),
        "observed_origins": (flow.get("sink") or {}).get("origins", []),
        "claim_boundary": (
            "Acceptance establishes a bounded function-level change, API and documentation preservation, "
            "public-source provenance, default differential compatibility, and visible parameter sensitivity. "
            "It does not establish expected task output or hidden semantic correctness."
        ),
    }
    report["report_hash"] = stable_hash(report)
    return report
