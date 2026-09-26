from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

from .io_utils import (
    canonical_json_hash,
    copy_tree_clean,
    iter_benchmark_files,
    read_json,
    sha256_file,
    write_json,
)
from .multiruntime_contract_v11 import verified_node_runtime


FORMAL_REDISTRIBUTION_STATUSES = {
    "inherits_redistributable_ancestor_license",
    "inherits_redistributable_repository_license",
    "redistributable_package_declaration",
    "redistributable_package_license_file",
    "redistributable_package_metadata_license",
}
TEST_CODE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".sh", ".bats"}
TEST_DIRECTORY_NAMES = {"test", "tests", "__tests__", "spec"}


def _looks_test_named(path: Path) -> bool:
    name = path.name.lower()
    parts = {part.lower() for part in path.parts}
    if path.suffix.lower() not in TEST_CODE_SUFFIXES:
        return False
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
        or bool(parts & TEST_DIRECTORY_NAMES)
    )


def _python_test_profile(path: Path) -> dict[str, bool]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return {
            "has_tests": False,
            "unittest_style": False,
            "pytest_style": False,
            "direct_script_style": False,
        }
    module_tests = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]
    test_classes = []
    unittest_classes = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        methods = [
            child
            for child in node.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and child.name.startswith("test_")
        ]
        if not methods:
            continue
        test_classes.append(node)
        base_names = {
            base.id
            if isinstance(base, ast.Name)
            else base.attr
            if isinstance(base, ast.Attribute)
            else ""
            for base in node.bases
        }
        if "TestCase" in base_names:
            unittest_classes.append(node)
    pytest_referenced = any(
        isinstance(node, ast.Name) and node.id == "pytest"
        or isinstance(node, ast.Import)
        and any(alias.name == "pytest" for alias in node.names)
        or isinstance(node, ast.ImportFrom)
        and node.module == "pytest"
        for node in ast.walk(tree)
    )
    has_main_guard = any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        and any(
            isinstance(comparator, ast.Constant)
            and comparator.value == "__main__"
            for comparator in node.test.comparators
        )
        for node in tree.body
    )
    has_tests = bool(module_tests or test_classes)
    direct_script_style = bool(
        has_tests and has_main_guard and not unittest_classes and not pytest_referenced
    )
    return {
        "has_tests": has_tests,
        "unittest_style": bool(unittest_classes),
        "pytest_style": bool(
            not direct_script_style
            and (module_tests or (test_classes and not unittest_classes))
        ),
        "direct_script_style": direct_script_style,
    }


def _is_test_entry(path: Path) -> bool:
    if not _looks_test_named(path):
        return False
    suffix = path.suffix.lower()
    if suffix == ".py":
        return _python_test_profile(path)["has_tests"]
    if suffix in {".js", ".mjs", ".cjs", ".ts", ".tsx"}:
        content = path.read_text(encoding="utf-8", errors="replace")
        return "node:test" in content or bool(
            re.search(r"(?m)^\s*(?:test|it|describe)\s*\(", content)
        )
    return suffix in {".sh", ".bats"}


def _shell_test_is_self_contained(path: Path) -> tuple[bool, str | None]:
    content = path.read_text(encoding="utf-8", errors="replace")
    positional_input = re.search(
        r"(?<!\\)\$(?:[1-9]|\{[1-9][^}]*\}|[#@*])",
        content,
    )
    if positional_input:
        return False, "shell_test_consumes_positional_arguments"
    if re.search(r"(?m)^\s*(?:getopts|shift)(?:\s|$)", content):
        return False, "shell_test_has_argument_parser"
    return True, None


def _hidden_test_bundle(package_root: Path, test_files: list[Path]) -> list[Path]:
    test_directories = {
        package_root / Path(*relative.parts[: index + 1])
        for relative in (path.relative_to(package_root) for path in test_files)
        for index, part in enumerate(relative.parts)
        if part.lower() in TEST_DIRECTORY_NAMES
    }
    rows: set[Path] = set(test_files)
    for directory in test_directories:
        rows.update(path for path in iter_benchmark_files(directory))
    for test_file in test_files:
        for name in ("conftest.py", "requirements.txt"):
            support = test_file.parent / name
            if support.is_file():
                rows.add(support)
        fixtures = test_file.parent / "fixtures"
        if fixtures.is_dir():
            rows.update(path for path in iter_benchmark_files(fixtures))
    return sorted(rows)


def _command_for_tests(package_root: Path, test_files: list[Path]) -> dict[str, Any]:
    python_files = [path for path in test_files if path.suffix.lower() == ".py"]
    javascript_files = [
        path for path in test_files if path.suffix.lower() in {".js", ".mjs", ".cjs"}
    ]
    typescript_files = [
        path for path in test_files if path.suffix.lower() in {".ts", ".tsx"}
    ]
    shell_files = [
        path for path in test_files if path.suffix.lower() in {".sh", ".bats"}
    ]

    if python_files:
        contents = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in python_files)
        relative = [path.relative_to(package_root).as_posix() for path in python_files]
        uses_pytest = "import pytest" in contents or "from pytest" in contents or any(
            path.name == "conftest.py" for path in package_root.rglob("conftest.py")
        )
        profiles = [_python_test_profile(path) for path in python_files]
        uses_unittest = any(profile["unittest_style"] for profile in profiles)
        pytest_style = any(profile["pytest_style"] for profile in profiles)
        direct_script_style = (
            len(python_files) == 1 and profiles[0]["direct_script_style"]
        )
        pythonpath = sorted(
            {
                parent.relative_to(package_root).as_posix()
                for path in python_files
                for parent in (
                    path.parent,
                    path.parent.parent
                    if path.parent.name.lower() in TEST_DIRECTORY_NAMES
                    else path.parent,
                )
                if parent != package_root and package_root in parent.parents
            }
        )
        if direct_script_style:
            return {
                "status": "auto_command_ready",
                "runner": "python_direct_test",
                "command": ["python3", "-B", relative[0]],
                "runtime_dependency": "script_declared_or_stdlib",
                "pythonpath": pythonpath,
            }
        if uses_unittest and not pytest_style and not (
            "import pytest" in contents or "from pytest" in contents
        ):
            return {
                "status": "auto_command_ready",
                "runner": "python_unittest",
                "command": ["python3", "-B", "-m", "unittest", *relative, "-q"],
                "runtime_dependency": "stdlib",
                "pythonpath": pythonpath,
            }
        if uses_pytest or pytest_style:
            return {
                "status": "auto_command_ready",
                "runner": "python_pytest",
                "command": ["python3", "-B", "-m", "pytest", *relative, "-q"],
                "runtime_dependency": "pytest",
                "pythonpath": pythonpath,
            }
        return {
            "status": "manual_command_required",
            "runner": "python_unknown",
            "reason": "python_test_framework_not_identified",
        }
    if javascript_files:
        contents = "\n".join(
            path.read_text(encoding="utf-8", errors="replace") for path in javascript_files
        )
        if "node:test" in contents:
            return {
                "status": "auto_command_ready",
                "runner": "node_builtin_test",
                "command": [
                    "node",
                    "--test",
                    *[path.relative_to(package_root).as_posix() for path in javascript_files],
                ],
                "runtime_dependency": "node_builtin",
            }
        return {
            "status": "manual_command_required",
            "runner": "javascript_unknown",
            "reason": "javascript_test_framework_not_identified",
        }
    if typescript_files:
        return {
            "status": "manual_command_required",
            "runner": "typescript_external_runner",
            "reason": "typescript_runner_or_build_step_required",
        }
    if shell_files:
        if len(shell_files) == 1 and shell_files[0].suffix.lower() == ".sh":
            self_contained, reason = _shell_test_is_self_contained(shell_files[0])
            if not self_contained:
                return {
                    "status": "manual_command_required",
                    "runner": "shell_parameterized",
                    "reason": reason,
                }
            return {
                "status": "auto_command_ready",
                "runner": "shell_direct",
                "command": ["bash", shell_files[0].relative_to(package_root).as_posix()],
                "runtime_dependency": "bash",
            }
        return {
            "status": "manual_command_required",
            "runner": "shell_external_runner",
            "reason": "multiple_shell_tests_or_bats_required",
        }
    return {"status": "no_test_code", "runner": None, "reason": "no_supported_test_code"}


def audit_source_tests_v09(
    inventory: str | Path | dict[str, Any],
    output: str | Path | None = None,
) -> dict[str, Any]:
    payload = read_json(inventory) if isinstance(inventory, (str, Path)) else inventory
    packages: list[dict[str, Any]] = []
    for source_scan in payload.get("sources", []):
        source = source_scan["source"]
        source_root = Path(source["local_root"])
        for package in source_scan.get("packages", []):
            if not package.get("strict_script_bearing"):
                continue
            package_root = source_root / package["relative_root"]
            test_files = [
                path for path in iter_benchmark_files(package_root) if _is_test_entry(path)
            ]
            if not test_files:
                continue
            hidden_bundle = _hidden_test_bundle(package_root, test_files)
            hidden_relatives = {
                path.relative_to(package_root).as_posix() for path in hidden_bundle
            }
            target_files = [
                relative
                for relative in package.get("script_files", [])
                if relative not in hidden_relatives
                and not _is_test_entry(package_root / relative)
            ]
            command = _command_for_tests(package_root, test_files)
            if not target_files and command["status"] == "auto_command_ready":
                command = {
                    **command,
                    "status": "no_non_test_script_target",
                    "reason": "all_script_files_are_tests_or_support_files",
                }
            packages.append(
                {
                    "source_id": source["source_id"],
                    "source_repo_url": source["repo_url"],
                    "source_commit": source["commit"],
                    "source_local_root": str(source_root),
                    "package_id": package["package_id"],
                    "relative_root": package["relative_root"],
                    "content_component_id": package.get("script_content_component_id"),
                    "license": package.get("effective_license_label"),
                    "redistribution_status": package.get("redistribution_status"),
                    "formal_redistributable": package.get("redistribution_status")
                    in FORMAL_REDISTRIBUTION_STATUSES,
                    "test_files": [
                        path.relative_to(package_root).as_posix() for path in test_files
                    ],
                    "hidden_test_bundle": sorted(hidden_relatives),
                    "hidden_test_hashes": {
                        path.relative_to(package_root).as_posix(): sha256_file(path)
                        for path in hidden_bundle
                    },
                    "target_files": target_files,
                    "target_file_count": len(target_files),
                    "command_profile": command,
                }
            )
    result = {
        "schema_version": "0.9-source-test-readiness-audit-v1",
        "benchmark": "SkillScriptBench",
        "status": "static_audit_complete",
        "claim_boundary": (
            "This audit identifies package-local test code and infers conservative commands. It does "
            "not execute tests, establish mutation discrimination, or cover repository-external tests."
        ),
        "inventory_hash": payload.get("inventory_hash") or canonical_json_hash(payload),
        "scope": "package_local_tests_only",
        "model_calls": 0,
        "behavior_executions": 0,
        "summary": {
            "package_count": len(packages),
            "source_count": len({row["source_id"] for row in packages}),
            "content_component_count": len(
                {row["content_component_id"] for row in packages if row["content_component_id"]}
            ),
            "auto_command_ready_count": sum(
                row["command_profile"]["status"] == "auto_command_ready" for row in packages
            ),
            "formal_auto_command_ready_count": sum(
                row["formal_redistributable"]
                and row["command_profile"]["status"] == "auto_command_ready"
                for row in packages
            ),
            "runner_counts": dict(
                sorted(Counter(row["command_profile"]["runner"] for row in packages).items())
            ),
        },
        "packages": packages,
    }
    result["audit_hash"] = canonical_json_hash(result)
    if output is not None:
        write_json(output, result)
    return result


def freeze_source_test_baselines_v09(
    audit: str | Path | dict[str, Any],
    output: str | Path | None = None,
    *,
    target_count: int = 30,
    source_cap: int = 3,
    exclude_sources: tuple[str, ...] | list[str] = (),
    excluded_freezes: tuple[str | Path | dict[str, Any], ...]
    | list[str | Path | dict[str, Any]] = (),
) -> dict[str, Any]:
    payload = read_json(audit) if isinstance(audit, (str, Path)) else audit
    excluded = set(exclude_sources)
    excluded_packages: set[str] = set()
    excluded_components: set[str] = set()
    excluded_freeze_hashes: list[str] = []
    for prior in excluded_freezes:
        frozen = read_json(prior) if isinstance(prior, (str, Path)) else prior
        excluded_freeze_hashes.append(
            frozen.get("freeze_hash") or canonical_json_hash(frozen)
        )
        for row in frozen.get("cases", []):
            excluded_packages.add(row["package_id"])
            excluded_components.add(row.get("content_component_id") or row["package_id"])
    candidates = [
        row
        for row in payload.get("packages", [])
        if row["formal_redistributable"]
        and row["command_profile"]["status"] == "auto_command_ready"
        and row["source_id"] not in excluded
        and row["package_id"] not in excluded_packages
        and (row.get("content_component_id") or row["package_id"])
        not in excluded_components
    ]
    runner_priority = {
        "python_unittest": 0,
        "node_builtin_test": 1,
        "shell_direct": 2,
        "python_pytest": 3,
    }
    grouped: dict[str, deque[dict[str, Any]]] = {}
    for source_id in sorted({row["source_id"] for row in candidates}):
        grouped[source_id] = deque(
            sorted(
                (row for row in candidates if row["source_id"] == source_id),
                key=lambda row: (
                    runner_priority.get(row["command_profile"]["runner"], 99),
                    row["package_id"],
                ),
            )
        )
    selected: list[dict[str, Any]] = []
    source_counts: Counter[str] = Counter()
    components: set[str] = set()
    while grouped and len(selected) < target_count:
        progressed = False
        for source_id in sorted(list(grouped)):
            queue = grouped[source_id]
            while queue:
                row = queue.popleft()
                component = row.get("content_component_id") or row["package_id"]
                if component in components or source_counts[source_id] >= source_cap:
                    continue
                selected.append(row)
                components.add(component)
                source_counts[source_id] += 1
                progressed = True
                break
            if not queue:
                del grouped[source_id]
            if len(selected) == target_count:
                break
        if not progressed:
            break
    frozen_cases = [
        {
            "baseline_id": f"baseline-{canonical_json_hash(row['package_id'])[:20]}",
            **row,
        }
        for row in selected
    ]
    result = {
        "schema_version": "0.9-source-test-baseline-freeze-v1",
        "benchmark": "SkillScriptBench",
        "status": "frozen_before_baseline_execution",
        "claim_boundary": (
            "Packages and commands are frozen before any baseline test execution. Every selected row "
            "must remain in the result denominator as PASS, FAIL, or ABSTAIN."
        ),
        "audit_hash": payload.get("audit_hash") or canonical_json_hash(payload),
        "selection_policy": {
            "target_count": target_count,
            "source_cap": source_cap,
            "excluded_sources": sorted(excluded),
            "excluded_freeze_hashes": sorted(excluded_freeze_hashes),
            "excluded_package_count": len(excluded_packages),
            "excluded_content_component_count": len(excluded_components),
            "formal_redistributable_only": True,
            "unique_content_component": True,
            "behavior_based_selection": False,
        },
        "behavior_executions_before_freeze": 0,
        "model_calls": 0,
        "summary": {
            "case_count": len(frozen_cases),
            "source_count": len({row["source_id"] for row in frozen_cases}),
            "content_component_count": len(components),
            "runner_counts": dict(
                sorted(Counter(row["command_profile"]["runner"] for row in frozen_cases).items())
            ),
            "selection_shortfall": max(0, target_count - len(frozen_cases)),
        },
        "cases": frozen_cases,
    }
    result["freeze_hash"] = canonical_json_hash(result)
    if output is not None:
        write_json(output, result)
    return result


def _sandbox_prefix(mode: str, root: Path) -> list[str]:
    if mode == "none":
        return []
    if mode != "sandbox-exec":
        raise ValueError(f"unsupported_sandbox_mode:{mode}")
    executable = shutil.which("sandbox-exec")
    if executable is None:
        raise RuntimeError("sandbox-exec_not_available")
    profile = (
        '(version 1) (allow default) (deny network*) (deny file-write*) '
        '(allow file-write* (literal "/dev/null")) '
        f'(allow file-write* (subpath "{root}"))'
    )
    return [executable, "-p", profile]


def _resolve_command(
    command: list[str],
    *,
    python_executable: str | Path | None = None,
    node_executable: str | Path | None = None,
) -> list[str]:
    if not command or command[0] not in {"python3", "node", "bash"}:
        raise ValueError("unsupported_source_test_command")
    executable = (
        str(Path(python_executable).absolute())
        if command[0] == "python3" and python_executable is not None
        else str(Path(node_executable).absolute())
        if command[0] == "node" and node_executable is not None
        else sys.executable
        if command[0] == "python3"
        else shutil.which(command[0])
    )
    if executable is None:
        raise RuntimeError(f"test_runtime_not_available:{command[0]}")
    for argument in command[1:]:
        path = Path(argument)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("source_test_command_must_be_package_relative")
    return [str(executable), *command[1:]]


def _run_baseline_command(
    command_profile: dict[str, Any],
    root: Path,
    *,
    sandbox_mode: str,
    timeout: int,
    python_executable: str | Path | None = None,
    node_executable: str | Path | None = None,
) -> dict[str, Any]:
    (root / ".skillscriptbench-home").mkdir(exist_ok=True)
    (root / ".skillscriptbench-tmp").mkdir(exist_ok=True)
    started = time.monotonic()
    try:
        pythonpath = [
            str(root / relative) for relative in command_profile.get("pythonpath", [])
        ]
        completed = subprocess.run(
            [
                *_sandbox_prefix(sandbox_mode, root),
                *_resolve_command(
                    command_profile["command"],
                    python_executable=python_executable,
                    node_executable=node_executable,
                ),
            ],
            cwd=root,
            env={
                **os.environ,
                "HOME": str(root / ".skillscriptbench-home"),
                "TMPDIR": str(root / ".skillscriptbench-tmp"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": os.pathsep.join(pythonpath),
                "PYTHONNOUSERSITE": "1",
                "NO_COLOR": "1",
            },
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "exit_code": completed.returncode,
            "timed_out": False,
            "duration_seconds": round(time.monotonic() - started, 6),
            "stdout_tail": completed.stdout[-3000:],
            "stderr_tail": completed.stderr[-3000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": None,
            "timed_out": True,
            "duration_seconds": round(time.monotonic() - started, 6),
            "stdout_tail": (exc.stdout or "")[-3000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-3000:] if isinstance(exc.stderr, str) else "",
        }


def _classify_baseline(outcome: dict[str, Any]) -> tuple[str, str]:
    if outcome["timed_out"]:
        return "abstain", "baseline_timeout"
    if outcome["exit_code"] == 0:
        return "pass", "source_test_baseline_passed"
    text = f"{outcome.get('stdout_tail', '')}\n{outcome.get('stderr_tail', '')}".lower()
    environment_markers = (
        "no module named",
        "modulenotfounderror",
        "is required. install",
        "is required for this script",
        "operation not permitted",
        "command not found",
        "cannot find module",
        "not found. install",
        "not on path",
        "install with:",
        "install it with:",
    )
    if any(marker in text for marker in environment_markers):
        return "abstain", "baseline_environment_or_dependency_unavailable"
    if outcome["exit_code"] == 5 and re.search(r"\b\d+\s+skipped\b", text):
        return "abstain", "baseline_all_tests_skipped"
    if "no tests ran" in text or "collected 0 items" in text:
        return "fail", "baseline_command_collected_zero_tests"
    return "fail", "source_test_baseline_assertion_or_runtime_failure"


def run_source_test_baselines_v09(
    freeze: str | Path | dict[str, Any],
    output: str | Path | None = None,
    *,
    sandbox_mode: str = "sandbox-exec",
    timeout: int = 30,
    python_executable: str | Path | None = None,
    node_runtime: str | Path | dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = read_json(freeze) if isinstance(freeze, (str, Path)) else freeze
    if payload.get("status") != "frozen_before_baseline_execution":
        raise ValueError("source_test_baselines_not_frozen")
    raw_node_runtime = (
        read_json(node_runtime) if isinstance(node_runtime, (str, Path)) else node_runtime
    )
    node_runtime_payload = (
        verified_node_runtime(
            raw_node_runtime,
            base_runtime_hash=raw_node_runtime["base_python_runtime_hash"],
        )
        if raw_node_runtime is not None
        else None
    )
    node_executable = (
        node_runtime_payload["node_executable"] if node_runtime_payload is not None else None
    )
    records: list[dict[str, Any]] = []
    for case in payload.get("cases", []):
        source_root = Path(case["source_local_root"]) / case["relative_root"]
        with tempfile.TemporaryDirectory(prefix="ssb-source-test-baseline-") as directory:
            worktree = Path(directory).resolve() / source_root.name
            copy_tree_clean(source_root, worktree)
            outcome = _run_baseline_command(
                case["command_profile"],
                worktree,
                sandbox_mode=sandbox_mode,
                timeout=timeout,
                python_executable=python_executable,
                node_executable=node_executable,
            )
        status, reason = _classify_baseline(outcome)
        records.append(
            {
                "baseline_id": case["baseline_id"],
                "source_id": case["source_id"],
                "package_id": case["package_id"],
                "content_component_id": case.get("content_component_id"),
                "runner": case["command_profile"]["runner"],
                "status": status,
                "reason": reason,
                "outcome": outcome,
            }
        )
    counts = Counter(row["status"] for row in records)
    result = {
        "schema_version": "0.9-source-test-baseline-result-v1",
        "benchmark": "SkillScriptBench",
        "status": "complete_with_full_frozen_denominator",
        "claim_boundary": (
            "A PASS means the public package-local source tests run in the recorded network-denied "
            "environment. It does not yet mean any generated mutation is discriminated."
        ),
        "freeze_hash": payload["freeze_hash"],
        "sandbox_mode": sandbox_mode,
        "python_executable": (
            str(Path(python_executable).absolute())
            if python_executable is not None
            else sys.executable
        ),
        "model_calls": 0,
        "behavior_results_used_for_case_deletion": False,
        "summary": {
            "case_count": len(records),
            "pass_count": counts["pass"],
            "fail_count": counts["fail"],
            "abstain_count": counts["abstain"],
            "status_counts": dict(sorted(counts.items())),
            "source_count": len({row["source_id"] for row in records}),
            "content_component_count": len(
                {row["content_component_id"] for row in records if row["content_component_id"]}
            ),
            "runner_status_counts": {
                runner: dict(
                    sorted(Counter(row["status"] for row in records if row["runner"] == runner).items())
                )
                for runner in sorted({row["runner"] for row in records})
            },
        },
        "records": records,
    }
    if node_runtime_payload is not None:
        result["supplemental_node_runtime_hash"] = node_runtime_payload["node_runtime_hash"]
    result["result_hash"] = canonical_json_hash(result)
    if output is not None:
        write_json(output, result)
    return result


def freeze_source_test_runtime_v09(
    requirements: str | Path,
    python_executable: str | Path,
    output: str | Path | None = None,
) -> dict[str, Any]:
    requirements_path = Path(requirements).resolve()
    executable = Path(python_executable).absolute()
    if not requirements_path.is_file():
        raise FileNotFoundError(requirements_path)
    if not executable.is_file():
        raise FileNotFoundError(executable)
    direct_requirements = [
        line.strip()
        for line in requirements_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    completed = subprocess.run(
        [str(executable), "-m", "pip", "freeze", "--all"],
        env={**os.environ, "PYTHONPATH": "", "PYTHONNOUSERSITE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"pip_freeze_failed:{completed.stderr[-800:]}")
    installed = sorted(line.strip() for line in completed.stdout.splitlines() if line.strip())
    installed_map = {
        line.split("==", 1)[0].lower().replace("_", "-"): line.split("==", 1)[1]
        for line in installed
        if "==" in line
    }
    missing_or_mismatched = []
    for requirement in direct_requirements:
        if "==" not in requirement:
            missing_or_mismatched.append(f"unpinned:{requirement}")
            continue
        name, version = requirement.split("==", 1)
        observed = installed_map.get(name.lower().replace("_", "-"))
        if observed != version:
            missing_or_mismatched.append(
                f"{name}:expected={version}:observed={observed}"
            )
    version = subprocess.run(
        [str(executable), "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    prefix = subprocess.run(
        [str(executable), "-c", "import sys; print(sys.prefix)"],
        env={**os.environ, "PYTHONPATH": "", "PYTHONNOUSERSITE": "1"},
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    result = {
        "schema_version": "0.9-source-test-runtime-freeze-v1",
        "benchmark": "SkillScriptBench",
        "status": "frozen" if not missing_or_mismatched else "fail",
        "claim_boundary": (
            "This runtime is frozen after development baseline environment diagnosis and before "
            "mutation screening. A runtime match does not establish package test correctness."
        ),
        "python_executable": str(executable),
        "python_executable_sha256": sha256_file(executable),
        "python_version": (version.stdout or version.stderr).strip(),
        "python_prefix": prefix.stdout.strip(),
        "requirements_path": str(requirements_path),
        "requirements_sha256": sha256_file(requirements_path),
        "direct_requirements": direct_requirements,
        "installed_distributions": installed,
        "installed_distributions_hash": canonical_json_hash(installed),
        "missing_or_mismatched": missing_or_mismatched,
        "model_calls": 0,
        "behavior_executions": 0,
    }
    result["runtime_hash"] = canonical_json_hash(result)
    if output is not None:
        write_json(output, result)
    return result
