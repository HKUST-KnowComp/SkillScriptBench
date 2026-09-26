from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

from .execution_isolation import isolated_command
from .io_utils import canonical_json_hash, read_json, sha256_bytes, sha256_file, write_json


SHELL_DEFAULT_ASSIGNMENT_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<export>export[ \t]+)?"
    r"(?P<target>[A-Za-z_][A-Za-z0-9_]*)="
    r"(?P<quote>[\"']?)\$\{(?P<source>[A-Za-z_][A-Za-z0-9_]*|[1-9][0-9]*):-"
    r"(?P<default>[^}\r\n]+)\}(?P=quote)(?P<suffix>[ \t]*(?:#.*)?)$",
    flags=re.MULTILINE,
)
SAFE_DEFAULT_RE = re.compile(r"^[A-Za-z0-9_./:@+ -]+$")
SENSITIVE_TOKENS = {
    "api",
    "auth",
    "credential",
    "credentials",
    "key",
    "password",
    "secret",
    "sig",
    "token",
}
SHELL_SUFFIXES = {".sh", ".bash"}
SHELL_OPERATOR_FAMILIES = (
    "shell_environment_fallback",
    "shell_positional_fallback",
)


def _identifier_tokens(value: str) -> set[str]:
    return {
        token
        for token in value.lower().replace("-", "_").lstrip("_").split("_")
        if token
    }


def _default_kind(value: str) -> str:
    stripped = value.strip()
    if re.fullmatch(r"[-+]?[0-9]+(?:\.[0-9]+)?", stripped):
        return "number"
    if stripped.lower() in {"true", "false", "yes", "no", "on", "off"}:
        return "boolean"
    if "/" in stripped or stripped.startswith("."):
        return "path"
    if " " in stripped:
        return "phrase"
    return "word"


def enumerate_shell_default_operators(
    source: str,
    *,
    source_hash: str,
    path: str,
    package_id: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for match in SHELL_DEFAULT_ASSIGNMENT_RE.finditer(source):
        target = match.group("target")
        fallback_source = match.group("source")
        default = match.group("default").strip()
        if not default or not SAFE_DEFAULT_RE.fullmatch(default):
            continue
        if "$" in default or "`" in default or "$(" in default:
            continue
        if (
            _identifier_tokens(target) | _identifier_tokens(fallback_source)
        ) & SENSITIVE_TOKENS:
            continue
        positional = fallback_source.isdigit()
        family = (
            "shell_positional_fallback" if positional else "shell_environment_fallback"
        )
        replacement_expression = f"${{{fallback_source}}}"
        original_expression = f"${{{fallback_source}:-{match.group('default')}}}"
        original_fragment = match.group(0)
        replacement_fragment = original_fragment.replace(
            original_expression,
            replacement_expression,
            1,
        )
        if replacement_fragment == original_fragment:
            continue
        template = {
            "family": family,
            "exported": bool(match.group("export")),
            "quoted": bool(match.group("quote")),
            "target_is_source": target == fallback_source,
            "default_kind": _default_kind(default),
        }
        identity = {
            "package_id": package_id,
            "path": path,
            "start": match.start(),
            "end": match.end(),
            "target": target,
            "fallback_source": fallback_source,
        }
        rows.append(
            {
                "operator_candidate_id": canonical_json_hash(identity)[:24],
                "operator": "remove_shell_default_fallback",
                "operator_subfamily": family,
                "dimension": (
                    "parameter_or_domain" if positional else "dependency_or_environment"
                ),
                "path": path,
                "source_hash": source_hash,
                "line": source.count("\n", 0, match.start()) + 1,
                "start": match.start(),
                "end": match.end(),
                "target": target,
                "fallback_source": fallback_source,
                "default_value": default,
                "default_kind": template["default_kind"],
                "original_fragment": original_fragment,
                "replacement_fragment": replacement_fragment,
                "original_fragment_hash": sha256_bytes(original_fragment.encode("utf-8")),
                "operator_template_fingerprint": canonical_json_hash(template),
                "construction_claim": (
                    "The transformed shell assignment removes one safe literal fallback while "
                    "preserving explicitly supplied environment or positional values."
                ),
            }
        )
    return rows


def apply_shell_operator(source: str, operator: dict[str, Any]) -> str:
    if sha256_bytes(source.encode("utf-8")) != operator["source_hash"]:
        raise ValueError("source_hash_mismatch")
    start = int(operator["start"])
    end = int(operator["end"])
    fragment = source[start:end]
    if sha256_bytes(fragment.encode("utf-8")) != operator["original_fragment_hash"]:
        raise ValueError("original_fragment_hash_mismatch")
    if fragment != operator["original_fragment"]:
        raise ValueError("original_fragment_mismatch")
    transformed = source[:start] + operator["replacement_fragment"] + source[end:]
    if transformed == source:
        raise ValueError("operator_did_not_change_source")
    return transformed


def _bash_parse(source: str) -> tuple[bool, str]:
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
    return completed.returncode == 0, completed.stderr[-500:]


def build_shell_operator_audit(
    expansion_audit: str | Path | dict[str, Any],
    output: str | Path | None = None,
) -> dict[str, Any]:
    # Import lazily because the v13 repository enumerator reuses the legacy
    # shell fallback helpers from this module.
    from .mutation_operators_v13 import enumerate_shell_behavior_operators_v13

    payload = (
        read_json(expansion_audit)
        if isinstance(expansion_audit, (str, Path))
        else expansion_audit
    )
    packages: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    parse_failures = 0
    for package in payload.get("packages", []):
        if package.get("license_tier") != "formal_redistributable":
            continue
        root = Path(package["source_local_root"]) / package["relative_root"]
        operators: list[dict[str, Any]] = []
        for relative in package.get("script_files", []):
            path = root / relative
            if path.suffix.lower() not in SHELL_SUFFIXES:
                continue
            try:
                source = path.read_bytes().decode("utf-8")
                source_hash = sha256_file(path)
                source_ok, source_error = _bash_parse(source)
                if not source_ok:
                    parse_failures += 1
                    failures.append(
                        {
                            "package_id": package["package_id"],
                            "path": relative,
                            "reason": f"source_bash_parse_failed:{source_error}",
                        }
                    )
                    continue
                candidates = [
                    *enumerate_shell_default_operators(
                        source,
                        source_hash=source_hash,
                        path=relative,
                        package_id=package["package_id"],
                    ),
                    *enumerate_shell_behavior_operators_v13(
                        source,
                        source_hash=source_hash,
                        path=relative,
                        package_id=package["package_id"],
                    ),
                ]
                deduped = {
                    operator["operator_candidate_id"]: operator
                    for operator in candidates
                }
                for operator in deduped.values():
                    transformed = apply_shell_operator(source, operator)
                    transformed_ok, transformed_error = _bash_parse(transformed)
                    if not transformed_ok:
                        failures.append(
                            {
                                "package_id": package["package_id"],
                                "path": relative,
                                "operator_candidate_id": operator["operator_candidate_id"],
                                "reason": f"mutant_bash_parse_failed:{transformed_error}",
                            }
                        )
                        continue
                    operators.append(operator)
            except Exception as exc:
                failures.append(
                    {
                        "package_id": package["package_id"],
                        "path": relative,
                        "reason": f"{type(exc).__name__}:{exc}",
                    }
                )
        if operators:
            packages.append(
                {
                    "package_id": package["package_id"],
                    "source_id": package["source_id"],
                    "source_commit": package["source_commit"],
                    "source_repo_url": package["source_repo_url"],
                    "source_local_root": package["source_local_root"],
                    "relative_root": package["relative_root"],
                    "split_group": package.get("split_group"),
                    "license_tier": package.get("license_tier"),
                    "operators": operators,
                }
            )
    all_operators = [operator for package in packages for operator in package["operators"]]
    result = {
        "schema_version": "0.16-shell-multidimensional-operator-audit-v2",
        "benchmark": "SkillScriptBench",
        "status": "zero_model_static_shell_operator_audit",
        "claim_boundary": (
            "Operators are exact-span, source-hash replayable, bash-parseable transformations of "
            "safe literal fallbacks and typed shell control or interface tokens. They are not "
            "full-script behavioral cases."
        ),
        "expansion_audit_hash": payload.get("audit_hash") or canonical_json_hash(payload),
        "model_calls": 0,
        "behavior_executions": 0,
        "summary": {
            "package_count": len(packages),
            "source_count": len({package["source_id"] for package in packages}),
            "content_component_count": len(
                {package["split_group"] for package in packages if package.get("split_group")}
            ),
            "operator_count": len(all_operators),
            "operator_subfamily_counts": dict(
                sorted(Counter(row["operator_subfamily"] for row in all_operators).items())
            ),
            "operator_counts": dict(
                sorted(Counter(row["operator"] for row in all_operators).items())
            ),
            "dimension_counts": dict(
                sorted(Counter(row["dimension"] for row in all_operators).items())
            ),
            "default_kind_counts": dict(
                sorted(
                    Counter(
                        row["default_kind"]
                        for row in all_operators
                        if row.get("default_kind")
                    ).items()
                )
            ),
            "operator_template_count": len(
                {row["operator_template_fingerprint"] for row in all_operators}
            ),
            "bash_source_parse_failure_count": parse_failures,
            "failure_count": len(failures),
        },
        "packages": packages,
        "failures": failures,
    }
    result["operator_audit_hash"] = canonical_json_hash(result)
    if output is not None:
        write_json(output, result)
    return result


def _round_robin_sources(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for row in sorted(
        rows,
        key=lambda item: (
            item["source_id"],
            item["package_id"],
            item["operator"]["path"],
            item["operator"]["line"],
        ),
    ):
        grouped[row["source_id"]].append(row)
    ordered: list[dict[str, Any]] = []
    while grouped:
        for source_id in sorted(list(grouped)):
            ordered.append(grouped[source_id].popleft())
            if not grouped[source_id]:
                del grouped[source_id]
    return ordered


def _harness(fragment: str, target: str) -> str:
    return (
        "#!/usr/bin/env bash\n"
        f"{fragment}\n"
        f"printf '%s\\n' \"${{{target}}}\"\n"
    )


def build_shell_discrimination_plan(
    operator_audit: str | Path | dict[str, Any],
    output: str | Path | None = None,
    *,
    per_family: int = 6,
    source_cap_per_family: int = 2,
    excluded_selections: tuple[str | Path | dict[str, Any], ...]
    | list[str | Path | dict[str, Any]] = (),
    global_component_uniqueness: bool = False,
) -> dict[str, Any]:
    payload = (
        read_json(operator_audit)
        if isinstance(operator_audit, (str, Path))
        else operator_audit
    )
    excluded_components: set[str] = set()
    excluded_package_ids: set[str] = set()
    excluded_operator_ids: set[str] = set()
    excluded_selection_hashes: list[str] = []
    for selection in excluded_selections:
        frozen = read_json(selection) if isinstance(selection, (str, Path)) else selection
        excluded_selection_hashes.append(
            frozen.get("plan_hash") or frozen.get("curation_hash") or canonical_json_hash(frozen)
        )
        for case in frozen.get("cases", []):
            excluded_package_ids.add(str(case["package_id"]))
            excluded_components.add(str(case.get("split_group") or case["package_id"]))
            operator_id = (case.get("operator") or {}).get("operator_candidate_id")
            if operator_id:
                excluded_operator_ids.add(str(operator_id))
    rows = [
        {**package, "operator": operator}
        for package in payload.get("packages", [])
        for operator in package.get("operators", [])
        if package["package_id"] not in excluded_package_ids
        and str(package.get("split_group") or package["package_id"])
        not in excluded_components
        and operator.get("operator_candidate_id") not in excluded_operator_ids
    ]
    cases: list[dict[str, Any]] = []
    shortages: dict[str, int] = {}
    globally_seen_components: set[str] = set()
    for family in SHELL_OPERATOR_FAMILIES:
        source_counts: Counter[str] = Counter()
        seen_components: set[str] = set()
        seen_templates: set[str] = set()
        selected: list[dict[str, Any]] = []
        for row in _round_robin_sources(
            [item for item in rows if item["operator"]["operator_subfamily"] == family]
        ):
            if len(selected) == per_family:
                break
            component = row.get("split_group") or row["package_id"]
            template = row["operator"]["operator_template_fingerprint"]
            if component in seen_components or template in seen_templates:
                continue
            if global_component_uniqueness and component in globally_seen_components:
                continue
            if source_counts[row["source_id"]] >= source_cap_per_family:
                continue
            operator = row["operator"]
            positional = operator["fallback_source"].isdigit()
            position = int(operator["fallback_source"]) if positional else 0

            def positional_args(value: str) -> list[str]:
                values = [f"ssb-prefix-{index}" for index in range(1, position + 1)]
                values[-1] = value
                return values

            default_calls = (
                [
                    {"args": positional_args("ssb-explicit-a"), "env": {}},
                    {"args": positional_args("ssb-explicit-b"), "env": {}},
                ]
                if positional
                else [
                    {"args": [], "env": {operator["fallback_source"]: "ssb-explicit-a"}},
                    {"args": [], "env": {operator["fallback_source"]: "ssb-explicit-b"}},
                ]
            )
            target_calls = (
                [
                    {"args": [], "env": {}},
                    {"args": positional_args(""), "env": {}},
                ]
                if positional
                else [
                    {"args": [], "env": {}},
                    {"args": [], "env": {operator["fallback_source"]: ""}},
                ]
            )
            selected.append(
                {
                    "case_id": f"shell-{family}-{operator['operator_candidate_id'][:16]}",
                    "dimension": operator["dimension"],
                    "case_type": family,
                    "package_id": row["package_id"],
                    "source_id": row["source_id"],
                    "source_commit": row["source_commit"],
                    "split_group": row.get("split_group"),
                    "operator": operator,
                    "original_harness": _harness(
                        operator["original_fragment"], operator["target"]
                    ),
                    "visible_harness": _harness(
                        operator["replacement_fragment"], operator["target"]
                    ),
                    "default_calls": default_calls,
                    "target_calls": target_calls,
                    "minimum_default_equal": 2,
                    "minimum_target_different": 2,
                    "fixture_derivation": (
                        "literal fallback expression with paired explicit-value compatibility and "
                        "missing-or-empty target probes"
                    ),
                }
            )
            source_counts[row["source_id"]] += 1
            seen_components.add(component)
            globally_seen_components.add(component)
            seen_templates.add(template)
        cases.extend(selected)
        if len(selected) < per_family:
            shortages[family] = per_family - len(selected)
    result = {
        "schema_version": "0.7-shell-default-discrimination-plan-1",
        "benchmark": "SkillScriptBench",
        "status": "ready" if not shortages else "fail",
        "claim_boundary": (
            "This plan tests isolated shell fallback assignment semantics, not full-script utility. "
            "Harnesses and results remain private benchmark-construction artifacts."
        ),
        "operator_audit_hash": payload.get("operator_audit_hash")
        or canonical_json_hash(payload),
        "selection_policy": {
            "per_family": per_family,
            "source_cap_per_family": source_cap_per_family,
            "excluded_selection_hashes": sorted(excluded_selection_hashes),
            "excluded_content_component_count": len(excluded_components),
            "excluded_package_count": len(excluded_package_ids),
            "excluded_operator_candidate_count": len(excluded_operator_ids),
            "unique_content_component_per_family": True,
            "global_content_component_uniqueness": global_component_uniqueness,
            "unique_operator_template_per_family": True,
            "uses_model_results": False,
            "shortages": shortages,
        },
        "construction_runtime": {
            "platform": platform.platform(),
            "bash": shutil.which("bash"),
        },
        "case_count": len(cases),
        "family_counts": dict(
            sorted(Counter(case["case_type"] for case in cases).items())
        ),
        "source_count": len({case["source_id"] for case in cases}),
        "content_component_count": len(
            {case["split_group"] for case in cases if case.get("split_group")}
        ),
        "behavior_executed": False,
        "model_calls": 0,
        "cases": cases,
        "failures": [
            {"family": family, "reason": f"insufficient_cases:{count}"}
            for family, count in sorted(shortages.items())
        ],
    }
    result["plan_hash"] = canonical_json_hash(result)
    if output is not None:
        write_json(output, result)
    return result


def build_shell_identity_control(
    plan: str | Path | dict[str, Any],
    output: str | Path | None = None,
) -> dict[str, Any]:
    return build_shell_control(plan, output, control_type="identity")


def build_shell_control(
    plan: str | Path | dict[str, Any],
    output: str | Path | None = None,
    *,
    control_type: str = "identity",
) -> dict[str, Any]:
    if control_type not in {"identity", "equivalent"}:
        raise ValueError(f"unsupported_shell_control_type:{control_type}")
    payload = read_json(plan) if isinstance(plan, (str, Path)) else plan
    result = json.loads(json.dumps(payload))
    result.pop("plan_hash", None)
    result["schema_version"] = "0.8-shell-default-control-plan-1"
    result["status"] = f"ready_{control_type}_negative_control"
    result["parent_plan_hash"] = payload.get("plan_hash") or canonical_json_hash(payload)
    result["control"] = {
        "type": f"{control_type}_visible_harness",
        "expected_result": "zero discrimination passes",
        "changes_source_text": control_type == "equivalent",
    }
    for case in result.get("cases", []):
        case["visible_harness"] = case["original_harness"] + (
            "\n: # skillscriptbench-equivalent-control\n"
            if control_type == "equivalent"
            else ""
        )
        case["control_type"] = f"{control_type}_visible_harness"
    result["plan_hash"] = canonical_json_hash(result)
    if output is not None:
        write_json(output, result)
    return result


def _observe_shell(
    harness: str,
    call: dict[str, Any],
    *,
    timeout: int,
    sandbox_mode: str = "none",
) -> dict[str, Any]:
    executable = shutil.which("bash")
    if executable is None:
        raise RuntimeError("bash_not_available")
    with tempfile.TemporaryDirectory(prefix="ssb-shell-") as directory:
        root = Path(directory)
        env = {
            "HOME": str(root),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            **{key: str(value) for key, value in call.get("env", {}).items()},
        }
        completed = subprocess.run(
            isolated_command(
                [
                    executable,
                    "--noprofile",
                    "--norc",
                    "-c",
                    harness,
                    "ssb-harness",
                    *call.get("args", []),
                ],
                mode=sandbox_mode,
                temporary_root=root,
            ),
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout.replace(str(root), "<CASE_ROOT>"),
        "stderr": completed.stderr.replace(str(root), "<CASE_ROOT>"),
    }


def execute_shell_discrimination(
    plan: str | Path | dict[str, Any],
    output: str | Path | None = None,
    *,
    timeout: int = 10,
    sandbox_mode: str = "none",
) -> dict[str, Any]:
    payload = read_json(plan) if isinstance(plan, (str, Path)) else plan
    records: list[dict[str, Any]] = []
    for case in payload.get("cases", []):
        try:
            original_default = [
                _observe_shell(
                    case["original_harness"],
                    call,
                    timeout=timeout,
                    sandbox_mode=sandbox_mode,
                )
                for call in case["default_calls"]
            ]
            visible_default = [
                _observe_shell(
                    case["visible_harness"],
                    call,
                    timeout=timeout,
                    sandbox_mode=sandbox_mode,
                )
                for call in case["default_calls"]
            ]
            original_target = [
                _observe_shell(
                    case["original_harness"],
                    call,
                    timeout=timeout,
                    sandbox_mode=sandbox_mode,
                )
                for call in case["target_calls"]
            ]
            visible_target = [
                _observe_shell(
                    case["visible_harness"],
                    call,
                    timeout=timeout,
                    sandbox_mode=sandbox_mode,
                )
                for call in case["target_calls"]
            ]
            original_repeat = [
                _observe_shell(
                    case["original_harness"],
                    call,
                    timeout=timeout,
                    sandbox_mode=sandbox_mode,
                )
                for call in case["target_calls"]
            ]
            visible_repeat = [
                _observe_shell(
                    case["visible_harness"],
                    call,
                    timeout=timeout,
                    sandbox_mode=sandbox_mode,
                )
                for call in case["target_calls"]
            ]
            default_equal = sum(
                left == right for left, right in zip(original_default, visible_default)
            )
            target_different = sum(
                left != right for left, right in zip(original_target, visible_target)
            )
            original_target_valid = sum(row["returncode"] == 0 for row in original_target)
            original_deterministic = original_target == original_repeat
            visible_deterministic = visible_target == visible_repeat
            passed = (
                default_equal >= case["minimum_default_equal"]
                and target_different >= case["minimum_target_different"]
                and original_target_valid == len(original_target)
                and original_deterministic
                and visible_deterministic
            )
            records.append(
                {
                    "case_id": case["case_id"],
                    "dimension": case["dimension"],
                    "case_type": case["case_type"],
                    "source_id": case["source_id"],
                    "status": "pass" if passed else "fail",
                    "default_call_count": len(original_default),
                    "default_equal_count": default_equal,
                    "target_call_count": len(original_target),
                    "target_different_count": target_different,
                    "original_target_valid_count": original_target_valid,
                    "original_deterministic": original_deterministic,
                    "visible_deterministic": visible_deterministic,
                    "original_default": original_default,
                    "visible_default": visible_default,
                    "original_target": original_target,
                    "visible_target": visible_target,
                    "failure": None,
                }
            )
        except Exception as exc:
            records.append(
                {
                    "case_id": case["case_id"],
                    "dimension": case["dimension"],
                    "case_type": case["case_type"],
                    "source_id": case["source_id"],
                    "status": "fail",
                    "failure": f"{type(exc).__name__}:{exc}",
                }
            )
    passed = [record for record in records if record["status"] == "pass"]
    result = {
        "schema_version": "0.7-shell-default-discrimination-result-1",
        "benchmark": "SkillScriptBench",
        "status": "pass" if records and len(passed) == len(records) else "fail",
        "claim_boundary": (
            "A pass proves deterministic isolated fallback-expression separation and explicit-value "
            "compatibility. It does not prove full-script utility, semantic importance, or model repair."
        ),
        "plan_hash": payload.get("plan_hash") or canonical_json_hash(payload),
        "sandbox_mode": sandbox_mode,
        "isolation_status": (
            "inner_sandbox" if sandbox_mode == "sandbox-exec" else "not_requested"
        ),
        "case_count": len(records),
        "pass_count": len(passed),
        "fail_count": len(records) - len(passed),
        "family_summary": {
            family: {
                "case_count": sum(record["case_type"] == family for record in records),
                "pass_count": sum(
                    record["case_type"] == family and record["status"] == "pass"
                    for record in records
                ),
            }
            for family in sorted({record["case_type"] for record in records})
        },
        "behavior_executed": True,
        "model_calls": 0,
        "records": records,
    }
    result["result_hash"] = canonical_json_hash(result)
    if output is not None:
        write_json(output, result)
    return result
