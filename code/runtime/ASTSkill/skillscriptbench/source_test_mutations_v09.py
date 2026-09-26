from __future__ import annotations

import ast
import shutil
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .expansion_operators import apply_operator, enumerate_python_operators
from .io_utils import canonical_json_hash, copy_tree_clean, read_json, sha256_file, write_json
from .js_mutations_v08 import apply_js_mutation_v08, enumerate_js_mutations_v08
from .js_ts_discrimination import _node_check
from .multiruntime_contract_v11 import verified_node_runtime
from .shell_discrimination import apply_shell_operator, enumerate_shell_default_operators
from .source_test_inventory_v09 import _classify_baseline, _run_baseline_command
from .v06_operators import enumerate_multidimensional_operators


def _enumerate_package_operators(
    package: dict[str, Any],
    *,
    node_executable: str | None = None,
    preserve_typescript_language: bool = False,
) -> list[dict[str, Any]]:
    root = Path(package["source_local_root"]) / package["relative_root"]
    target_files = package["target_files"]
    rows: list[dict[str, Any]] = []
    python_files = [relative for relative in target_files if Path(relative).suffix.lower() == ".py"]
    python_operators = [
        *enumerate_python_operators(root, python_files),
        *enumerate_multidimensional_operators(root, python_files),
    ]
    seen_python_ids: set[str] = set()
    for operator in python_operators:
        if operator["operator_candidate_id"] in seen_python_ids:
            continue
        seen_python_ids.add(operator["operator_candidate_id"])
        rows.append(
            {
                "operator_id": operator["operator_candidate_id"],
                "language": "python",
                "family": operator["operator_subfamily"],
                "dimension": operator["dimension"],
                "operator": operator,
            }
        )
    for relative in target_files:
        path = root / relative
        suffix = path.suffix.lower()
        if suffix not in {".js", ".mjs", ".cjs", ".ts", ".sh", ".bash"}:
            continue
        source = path.read_text(encoding="utf-8")
        source_hash = sha256_file(path)
        if suffix in {".js", ".mjs", ".cjs", ".ts"}:
            for operator in enumerate_js_mutations_v08(
                source,
                source_hash=source_hash,
                path=relative,
                package_id=package["package_id"],
                node_executable=node_executable,
            ):
                rows.append(
                    {
                        "operator_id": operator["mutation_id"],
                        "language": (
                            "typescript"
                            if preserve_typescript_language and suffix == ".ts"
                            else "javascript"
                        ),
                        "family": operator["family"],
                        "dimension": operator["dimension"],
                        "operator": operator,
                    }
                )
        else:
            for operator in enumerate_shell_default_operators(
                source,
                source_hash=source_hash,
                path=relative,
                package_id=package["package_id"],
            ):
                rows.append(
                    {
                        "operator_id": operator["operator_candidate_id"],
                        "language": "shell",
                        "family": operator["operator_subfamily"],
                        "dimension": operator["dimension"],
                        "operator": operator,
                    }
                )
    return rows


def _select_package_operators(
    operators: list[dict[str, Any]],
    *,
    per_package_cap: int,
    per_family_cap: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for operator in sorted(operators, key=lambda row: (row["family"], row["operator_id"])):
        grouped[operator["family"]].append(operator)
    selected: list[dict[str, Any]] = []
    family_counts: Counter[str] = Counter()
    while len(selected) < per_package_cap:
        progressed = False
        for family in sorted(grouped):
            if family_counts[family] >= per_family_cap or not grouped[family]:
                continue
            selected.append(grouped[family].pop(0))
            family_counts[family] += 1
            progressed = True
            if len(selected) == per_package_cap:
                break
        if not progressed:
            break
    return selected


def freeze_source_test_mutations_v09(
    audit: str | Path | dict[str, Any],
    baseline_freeze: str | Path | dict[str, Any],
    baseline_result: str | Path | dict[str, Any],
    runtime: str | Path | dict[str, Any],
    output: str | Path | None = None,
    *,
    per_package_cap: int = 6,
    per_family_cap: int = 2,
    excluded_core_freezes: tuple[str | Path | dict[str, Any], ...]
    | list[str | Path | dict[str, Any]] = (),
    exclude_sources: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    audit_payload = read_json(audit) if isinstance(audit, (str, Path)) else audit
    frozen = (
        read_json(baseline_freeze)
        if isinstance(baseline_freeze, (str, Path))
        else baseline_freeze
    )
    baseline = (
        read_json(baseline_result)
        if isinstance(baseline_result, (str, Path))
        else baseline_result
    )
    runtime_payload = read_json(runtime) if isinstance(runtime, (str, Path)) else runtime
    if baseline.get("freeze_hash") != frozen.get("freeze_hash"):
        raise ValueError("baseline_result_freeze_hash_mismatch")
    if runtime_payload.get("status") != "frozen":
        raise ValueError("source_test_runtime_not_frozen")
    if Path(baseline["python_executable"]).absolute() != Path(
        runtime_payload["python_executable"]
    ).absolute():
        raise ValueError("baseline_runtime_path_mismatch")
    package_by_id = {row["package_id"]: row for row in audit_payload.get("packages", [])}
    pass_ids = {
        row["package_id"] for row in baseline.get("records", []) if row["status"] == "pass"
    }
    frozen_ids = {row["package_id"] for row in frozen.get("cases", [])}
    if not pass_ids.issubset(frozen_ids):
        raise ValueError("baseline_pass_package_not_in_freeze")

    excluded_components: set[str] = set()
    excluded_core_freeze_hashes: list[str] = []
    for prior in excluded_core_freezes:
        core = read_json(prior) if isinstance(prior, (str, Path)) else prior
        excluded_core_freeze_hashes.append(
            core.get("freeze_hash")
            or core.get("catalog_hash")
            or canonical_json_hash(core)
        )
        core_cases = core.get("cases")
        if core_cases is None:
            core_cases = [
                *core.get("primary_cases", []),
                *core.get("diagnostic_variants", []),
            ]
        excluded_components.update(
            row.get("content_component_id") or f"package:{row['package_id']}"
            for row in core_cases
        )
    excluded_source_ids = set(exclude_sources)
    pass_ids = {
        package_id
        for package_id in pass_ids
        if package_by_id[package_id]["source_id"] not in excluded_source_ids
        and (
            package_by_id[package_id].get("content_component_id")
            or f"package:{package_id}"
        )
        not in excluded_components
    }

    packages: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    for package_id in sorted(pass_ids):
        package = package_by_id[package_id]
        operators = _enumerate_package_operators(package)
        selected = _select_package_operators(
            operators,
            per_package_cap=per_package_cap,
            per_family_cap=per_family_cap,
        )
        package_cases = []
        for selected_operator in selected:
            case_id = (
                f"source-test-v09-{canonical_json_hash(package_id)[:10]}-"
                f"{selected_operator['operator_id']}"
            )
            row = {
                "case_id": case_id,
                "package_id": package_id,
                "source_id": package["source_id"],
                "content_component_id": package.get("content_component_id"),
                **selected_operator,
            }
            cases.append(row)
            package_cases.append(case_id)
        packages.append(
            {
                "package_id": package_id,
                "source_id": package["source_id"],
                "source_commit": package["source_commit"],
                "source_repo_url": package["source_repo_url"],
                "source_local_root": package["source_local_root"],
                "relative_root": package["relative_root"],
                "content_component_id": package.get("content_component_id"),
                "command_profile": package["command_profile"],
                "test_files": package["test_files"],
                "hidden_test_bundle": package["hidden_test_bundle"],
                "hidden_test_hashes": package["hidden_test_hashes"],
                "target_files": package["target_files"],
                "enumerated_operator_count": len(operators),
                "selected_case_ids": package_cases,
            }
        )
    result = {
        "schema_version": "0.9-source-test-mutation-freeze-v1",
        "benchmark": "SkillScriptBench",
        "status": "frozen_before_mutation_test_execution",
        "claim_boundary": (
            "Packages are development construction candidates selected only after their public source "
            "tests passed. Mutation sites are frozen before mutation test execution; every selected "
            "case remains in the result denominator. This is not untouched evaluation."
        ),
        "audit_hash": audit_payload.get("audit_hash") or canonical_json_hash(audit_payload),
        "baseline_freeze_hash": frozen["freeze_hash"],
        "baseline_result_hash": baseline["result_hash"],
        "runtime_hash": runtime_payload["runtime_hash"],
        "selection_policy": {
            "per_package_cap": per_package_cap,
            "per_family_cap": per_family_cap,
            "baseline_pass_required": True,
            "behavior_based_case_deletion_after_freeze": False,
            "family_round_robin": True,
            "excluded_core_freeze_hashes": sorted(excluded_core_freeze_hashes),
            "excluded_content_component_count": len(excluded_components),
            "excluded_sources": sorted(excluded_source_ids),
        },
        "mutation_test_executions_before_freeze": 0,
        "model_calls": 0,
        "summary": {
            "package_count": len(packages),
            "source_count": len({row["source_id"] for row in packages}),
            "content_component_count": len(
                {row["content_component_id"] for row in packages if row["content_component_id"]}
            ),
            "case_count": len(cases),
            "language_counts": dict(
                sorted(Counter(row["language"] for row in cases).items())
            ),
            "dimension_counts": dict(
                sorted(Counter(row["dimension"] for row in cases).items())
            ),
            "family_counts": dict(sorted(Counter(row["family"] for row in cases).items())),
            "packages_without_supported_operator_count": sum(
                not row["selected_case_ids"] for row in packages
            ),
        },
        "packages": packages,
        "cases": cases,
    }
    result["freeze_hash"] = canonical_json_hash(result)
    if output is not None:
        write_json(output, result)
    return result


def _apply(language: str, source: str, operator: dict[str, Any]) -> str:
    if language == "python":
        return apply_operator(source, operator)
    if language in {"javascript", "typescript"}:
        return apply_js_mutation_v08(source, operator)
    if language == "shell":
        return apply_shell_operator(source, operator)
    raise ValueError(f"unsupported_mutation_language:{language}")


def _parse_check(language: str, source: str, suffix: str) -> tuple[bool, str]:
    try:
        if language == "python":
            ast.parse(source)
            return True, "python_ast_pass"
        if language in {"javascript", "typescript"}:
            return _node_check(source, suffix)
        if language == "shell":
            bash = shutil.which("bash")
            if bash is None:
                return False, "bash_not_available"
            import subprocess

            completed = subprocess.run(
                [bash, "--noprofile", "--norc", "-n"],
                input=source,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return completed.returncode == 0, completed.stderr[-800:]
    except Exception as exc:
        return False, f"{type(exc).__name__}:{exc}"
    return False, f"unsupported_language:{language}"


def _run_package_mutations(
    package: dict[str, Any],
    cases: list[dict[str, Any]],
    *,
    python_executable: str,
    node_executable: str | None = None,
    sandbox_mode: str,
    timeout: int,
) -> list[dict[str, Any]]:
    source_root = Path(package["source_local_root"]) / package["relative_root"]
    with tempfile.TemporaryDirectory(prefix="ssb-v09-baseline-rerun-") as directory:
        baseline_root = Path(directory).resolve() / source_root.name
        copy_tree_clean(source_root, baseline_root)
        baseline_outcome = _run_baseline_command(
            package["command_profile"],
            baseline_root,
            sandbox_mode=sandbox_mode,
            timeout=timeout,
            python_executable=python_executable,
            node_executable=node_executable,
        )
    baseline_status, baseline_reason = _classify_baseline(baseline_outcome)
    rows: list[dict[str, Any]] = []
    for case in cases:
        if baseline_status != "pass":
            rows.append(
                {
                    "case_id": case["case_id"],
                    "package_id": case["package_id"],
                    "source_id": case["source_id"],
                    "content_component_id": case.get("content_component_id"),
                    "language": case["language"],
                    "family": case["family"],
                    "dimension": case["dimension"],
                    "status": "abstain",
                    "reason": f"baseline_rerun_{baseline_reason}",
                    "baseline_outcome": baseline_outcome,
                    "mutation_outcome": None,
                }
            )
            continue
        with tempfile.TemporaryDirectory(prefix="ssb-v09-mutation-") as directory:
            worktree = Path(directory).resolve() / source_root.name
            copy_tree_clean(source_root, worktree)
            target = worktree / case["operator"]["path"]
            pristine = target.read_text(encoding="utf-8")
            transformed = _apply(case["language"], pristine, case["operator"])
            parse_ok, parse_detail = _parse_check(
                case["language"], transformed, target.suffix.lower()
            )
            if parse_ok:
                target.write_text(transformed, encoding="utf-8")
                mutation_outcome = _run_baseline_command(
                    package["command_profile"],
                    worktree,
                    sandbox_mode=sandbox_mode,
                    timeout=timeout,
                    python_executable=python_executable,
                    node_executable=node_executable,
                )
            else:
                mutation_outcome = None
        if not parse_ok:
            status = "fail"
            reason = "mutation_parse_failed"
        elif mutation_outcome["timed_out"]:
            status = "abstain"
            reason = "mutation_test_timeout"
        elif mutation_outcome["exit_code"] == 0:
            status = "fail"
            reason = "mutation_survived_source_tests"
        else:
            status = "pass"
            reason = "mutation_killed_by_source_tests"
        rows.append(
            {
                "case_id": case["case_id"],
                "package_id": case["package_id"],
                "source_id": case["source_id"],
                "content_component_id": case.get("content_component_id"),
                "language": case["language"],
                "family": case["family"],
                "dimension": case["dimension"],
                "status": status,
                "reason": reason,
                "parse_ok": parse_ok,
                "parse_detail": parse_detail,
                "baseline_outcome": baseline_outcome,
                "mutation_outcome": mutation_outcome,
            }
        )
    return rows


def run_source_test_mutations_v09(
    freeze: str | Path | dict[str, Any],
    runtime: str | Path | dict[str, Any],
    output: str | Path | None = None,
    *,
    sandbox_mode: str = "sandbox-exec",
    timeout: int = 60,
    max_workers: int = 6,
    node_runtime: str | Path | dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = read_json(freeze) if isinstance(freeze, (str, Path)) else freeze
    runtime_payload = read_json(runtime) if isinstance(runtime, (str, Path)) else runtime
    if payload.get("status") != "frozen_before_mutation_test_execution":
        raise ValueError("source_test_mutations_not_frozen")
    if runtime_payload.get("runtime_hash") != payload.get("runtime_hash"):
        raise ValueError("source_test_mutation_runtime_hash_mismatch")
    node_runtime_payload = verified_node_runtime(
        node_runtime,
        base_runtime_hash=runtime_payload["runtime_hash"],
    )
    node_executable = (
        node_runtime_payload["node_executable"] if node_runtime_payload is not None else None
    )
    cases_by_package: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in payload.get("cases", []):
        cases_by_package[case["package_id"]].append(case)
    package_by_id = {row["package_id"]: row for row in payload.get("packages", [])}
    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _run_package_mutations,
                package_by_id[package_id],
                cases,
                python_executable=runtime_payload["python_executable"],
                node_executable=node_executable,
                sandbox_mode=sandbox_mode,
                timeout=timeout,
            ): package_id
            for package_id, cases in cases_by_package.items()
        }
        for future in as_completed(futures):
            package_id = futures[future]
            try:
                records.extend(future.result())
            except Exception as exc:
                for case in cases_by_package[package_id]:
                    records.append(
                        {
                            "case_id": case["case_id"],
                            "package_id": package_id,
                            "source_id": case["source_id"],
                            "content_component_id": case.get("content_component_id"),
                            "language": case["language"],
                            "family": case["family"],
                            "dimension": case["dimension"],
                            "status": "abstain",
                            "reason": f"package_worker_failure:{type(exc).__name__}:{exc}",
                        }
                    )
    records.sort(key=lambda row: row["case_id"])
    counts = Counter(row["status"] for row in records)
    result = {
        "schema_version": "0.9-source-test-mutation-result-v1",
        "benchmark": "SkillScriptBench",
        "status": "complete_with_full_frozen_denominator",
        "claim_boundary": (
            "A PASS means public package source tests distinguish the frozen source from one generated "
            "mutation under the frozen runtime. Cases are construction behavior-filtered and must not "
            "be reported as untouched task evaluation or model repair success."
        ),
        "freeze_hash": payload["freeze_hash"],
        "runtime_hash": runtime_payload["runtime_hash"],
        "sandbox_mode": sandbox_mode,
        "max_workers": max_workers,
        "model_calls": 0,
        "behavior_results_used_for_case_deletion": False,
        "summary": {
            "case_count": len(records),
            "pass_count": counts["pass"],
            "fail_count": counts["fail"],
            "abstain_count": counts["abstain"],
            "status_counts": dict(sorted(counts.items())),
            "package_count": len({row["package_id"] for row in records}),
            "source_count": len({row["source_id"] for row in records}),
            "content_component_count": len(
                {row["content_component_id"] for row in records if row["content_component_id"]}
            ),
            "killed_component_count": len(
                {
                    row["content_component_id"]
                    for row in records
                    if row["status"] == "pass" and row["content_component_id"]
                }
            ),
            "dimension_status_counts": {
                dimension: dict(
                    sorted(
                        Counter(
                            row["status"] for row in records if row["dimension"] == dimension
                        ).items()
                    )
                )
                for dimension in sorted({row["dimension"] for row in records})
            },
            "family_pass_counts": dict(
                sorted(Counter(row["family"] for row in records if row["status"] == "pass").items())
            ),
        },
        "records": records,
    }
    if node_runtime_payload is not None:
        result["supplemental_node_runtime_hash"] = node_runtime_payload["node_runtime_hash"]
    result["result_hash"] = canonical_json_hash(result)
    if output is not None:
        write_json(output, result)
    return result
