from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

from .execution_isolation import isolated_command
from .io_utils import canonical_json_hash, read_json, sha256_bytes, sha256_file, write_json


JS_TS_SUFFIXES = {".js", ".mjs", ".cjs", ".ts"}
JS_TS_OPERATOR_FAMILIES = (
    "js_ts_default_parameter",
    "js_ts_environment_fallback",
)
PARSER_ROOT = Path(__file__).resolve().parent / "js_parser"
PARSER_HELPER = PARSER_ROOT / "extract_opportunities.mjs"


def _node_executable() -> str:
    executable = shutil.which("node")
    if executable is None:
        raise RuntimeError("node_not_available")
    return executable


def _babel_opportunities(source: str, filename: str) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [_node_executable(), str(PARSER_HELPER)],
        input=json.dumps({"source": source, "filename": filename}),
        text=True,
        capture_output=True,
        cwd=PARSER_ROOT,
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(f"babel_parse_failed:{completed.stderr[-800:]}")
    return json.loads(completed.stdout)["opportunities"]


def _node_check(source: str, suffix: str) -> tuple[bool, str]:
    if suffix == ".ts":
        _babel_opportunities(source, f"candidate{suffix}")
        return True, "babel_typescript_parse_only"
    with tempfile.TemporaryDirectory(prefix="ssb-node-check-") as directory:
        path = Path(directory) / f"candidate{suffix}"
        path.write_text(source, encoding="utf-8")
        completed = subprocess.run(
            [_node_executable(), "--check", str(path)],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    return completed.returncode == 0, completed.stderr[-800:]


def enumerate_js_ts_operators(
    source: str,
    *,
    source_hash: str,
    path: str,
    package_id: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for opportunity in _babel_opportunities(source, path):
        start = int(opportunity["start"])
        end = int(opportunity["end"])
        replacement_start = int(opportunity["replacementStart"])
        replacement_end = int(opportunity["replacementEnd"])
        original_fragment = source[start:end]
        replacement_fragment = source[replacement_start:replacement_end]
        if not original_fragment or not replacement_fragment or original_fragment == replacement_fragment:
            continue
        if opportunity["kind"] == "default_parameter":
            family = "js_ts_default_parameter"
            dimension = "parameter_or_domain"
            target_name = opportunity["parameter"]
            template = {
                "family": family,
                "function_type": opportunity["functionType"],
                "literal_type": opportunity["defaultLiteralType"],
                "typescript": Path(path).suffix.lower() == ".ts",
                "async": opportunity["async"],
                "generator": opportunity["generator"],
            }
        elif opportunity["kind"] == "environment_fallback":
            family = "js_ts_environment_fallback"
            dimension = "dependency_or_environment"
            target_name = opportunity["environmentVariable"]
            template = {
                "family": family,
                "logical_operator": opportunity["logicalOperator"],
                "literal_type": opportunity["defaultLiteralType"],
                "typescript": Path(path).suffix.lower() == ".ts",
            }
        else:
            continue
        identity = {
            "package_id": package_id,
            "path": path,
            "start": start,
            "end": end,
            "family": family,
            "target": target_name,
        }
        rows.append(
            {
                "operator_candidate_id": canonical_json_hash(identity)[:24],
                "operator": "remove_js_ts_default",
                "operator_subfamily": family,
                "dimension": dimension,
                "language": "typescript" if Path(path).suffix.lower() == ".ts" else "javascript",
                "path": path,
                "source_hash": source_hash,
                "line": source.count("\n", 0, start) + 1,
                "start": start,
                "end": end,
                "original_fragment": original_fragment,
                "replacement_fragment": replacement_fragment,
                "original_fragment_hash": sha256_bytes(original_fragment.encode("utf-8")),
                "operator_template_fingerprint": canonical_json_hash(template),
                "target_name": target_name,
                "default_value": opportunity["defaultValue"],
                "default_literal_type": opportunity["defaultLiteralType"],
                "parameter_index": opportunity.get("parameterIndex"),
                "parameter_count": opportunity.get("parameterCount"),
                "logical_operator": opportunity.get("logicalOperator"),
                "function_name": opportunity.get("functionName"),
                "function_type": opportunity.get("functionType"),
                "construction_claim": (
                    "The transformed JavaScript/TypeScript node removes one literal default while "
                    "retaining explicitly supplied values."
                ),
            }
        )
    return rows


def apply_js_ts_operator(source: str, operator: dict[str, Any]) -> str:
    if sha256_bytes(source.encode("utf-8")) != operator["source_hash"]:
        raise ValueError("source_hash_mismatch")
    start = int(operator["start"])
    end = int(operator["end"])
    fragment = source[start:end]
    if fragment != operator["original_fragment"]:
        raise ValueError("original_fragment_mismatch")
    if sha256_bytes(fragment.encode("utf-8")) != operator["original_fragment_hash"]:
        raise ValueError("original_fragment_hash_mismatch")
    transformed = source[:start] + operator["replacement_fragment"] + source[end:]
    if transformed == source:
        raise ValueError("operator_did_not_change_source")
    return transformed


def build_js_ts_operator_audit(
    expansion_audit: str | Path | dict[str, Any],
    output: str | Path | None = None,
) -> dict[str, Any]:
    payload = (
        read_json(expansion_audit)
        if isinstance(expansion_audit, (str, Path))
        else expansion_audit
    )
    packages: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for package in payload.get("packages", []):
        if package.get("license_tier") != "formal_redistributable":
            continue
        root = Path(package["source_local_root"]) / package["relative_root"]
        operators: list[dict[str, Any]] = []
        for relative in package.get("script_files", []):
            path = root / relative
            suffix = path.suffix.lower()
            if suffix not in JS_TS_SUFFIXES:
                continue
            try:
                source = path.read_text(encoding="utf-8")
                source_hash = sha256_file(path)
                source_ok, source_error = _node_check(source, suffix)
                if not source_ok:
                    raise ValueError(f"source_node_check_failed:{source_error}")
                for operator in enumerate_js_ts_operators(
                    source,
                    source_hash=source_hash,
                    path=relative,
                    package_id=package["package_id"],
                ):
                    transformed = apply_js_ts_operator(source, operator)
                    _babel_opportunities(transformed, relative)
                    transformed_ok, transformed_error = _node_check(transformed, suffix)
                    if not transformed_ok:
                        raise ValueError(f"mutant_node_check_failed:{transformed_error}")
                    operators.append(operator)
            except Exception as exc:
                failures.append(
                    {
                        "package_id": package["package_id"],
                        "source_id": package["source_id"],
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
        "schema_version": "0.7-js-ts-default-operator-audit-1",
        "benchmark": "SkillScriptBench",
        "status": "zero_model_babel_ast_operator_audit",
        "claim_boundary": (
            "Operators are exact Babel AST-node mutations validated by Babel parse for JS/TS and "
            "an additional Node syntax check for JavaScript. They are not full-script behavioral "
            "cases."
        ),
        "expansion_audit_hash": payload.get("audit_hash") or canonical_json_hash(payload),
        "parser_runtime": {
            "package": "@babel/parser",
            "version": "8.0.4",
            "lockfile": "skillscriptbench/js_parser/package-lock.json",
            "typescript_validation": "babel_parse",
            "javascript_validation": "babel_parse_plus_node_check",
            "node_version": subprocess.run(
                [_node_executable(), "--version"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip(),
        },
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
            "language_counts": dict(
                sorted(Counter(row["language"] for row in all_operators).items())
            ),
            "operator_template_count": len(
                {row["operator_template_fingerprint"] for row in all_operators}
            ),
            "failure_count": len(failures),
        },
        "packages": packages,
        "failures": failures,
    }
    result["operator_audit_hash"] = canonical_json_hash(result)
    if output is not None:
        write_json(output, result)
    return result


def _round_robin_language_source(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
    for row in sorted(
        rows,
        key=lambda item: (
            item["operator"]["language"],
            item["source_id"],
            item["package_id"],
            item["operator"]["path"],
            item["operator"]["line"],
        ),
    ):
        grouped[(row["operator"]["language"], row["source_id"])].append(row)
    ordered: list[dict[str, Any]] = []
    while grouped:
        for key in sorted(list(grouped), reverse=True):
            ordered.append(grouped[key].popleft())
            if not grouped[key]:
                del grouped[key]
    return ordered


def _normalize_js_expression() -> str:
    return (
        "function normalize(value) {\n"
        "  if (value === undefined) return { kind: 'undefined' };\n"
        "  return { kind: typeof value, value };\n"
        "}\n"
    )


def _parameter_harness(fragment: str, parameter: str) -> str:
    return (
        _normalize_js_expression()
        + f"const target = ({fragment}) => {parameter};\n"
        + "const mode = process.env.SSB_MODE;\n"
        + "let value;\n"
        + "if (mode === 'omitted') value = target();\n"
        + "else if (mode === 'undefined') value = target(undefined);\n"
        + "else value = target(JSON.parse(process.env.SSB_VALUE));\n"
        + "console.log(JSON.stringify(normalize(value)));\n"
    )


def _environment_harness(fragment: str) -> str:
    return (
        _normalize_js_expression()
        + f"const value = ({fragment});\n"
        + "console.log(JSON.stringify(normalize(value)));\n"
    )


def _explicit_values(operator: dict[str, Any]) -> list[Any]:
    value = operator["default_value"]
    if isinstance(value, bool):
        return [not value, value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [value + 1, value + 2]
    return ["ssb-explicit-a", "ssb-explicit-b"]


def build_js_ts_discrimination_plan(
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
    for family in JS_TS_OPERATOR_FAMILIES:
        source_counts: Counter[str] = Counter()
        seen_components: set[str] = set()
        seen_templates: set[str] = set()
        selected: list[dict[str, Any]] = []
        for row in _round_robin_language_source(
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
            if family == "js_ts_default_parameter":
                original_harness = _parameter_harness(
                    operator["original_fragment"], operator["target_name"]
                )
                visible_harness = _parameter_harness(
                    operator["replacement_fragment"], operator["target_name"]
                )
                default_calls = [
                    {"env": {"SSB_MODE": "explicit", "SSB_VALUE": json.dumps(value)}}
                    for value in _explicit_values(operator)
                ]
                target_calls = [
                    {"env": {"SSB_MODE": "omitted"}},
                    {"env": {"SSB_MODE": "undefined"}},
                ]
                minimum_target_different = 2
            else:
                original_harness = _environment_harness(operator["original_fragment"])
                visible_harness = _environment_harness(operator["replacement_fragment"])
                variable = operator["target_name"]
                default_calls = [
                    {"env": {variable: "ssb-explicit-a"}},
                    {"env": {variable: "ssb-explicit-b"}},
                ]
                target_calls = [{"env": {}}]
                minimum_target_different = 1
            selected.append(
                {
                    "case_id": f"{family}-{operator['operator_candidate_id'][:16]}",
                    "dimension": operator["dimension"],
                    "case_type": family,
                    "language": operator["language"],
                    "suffix": Path(operator["path"]).suffix.lower(),
                    "package_id": row["package_id"],
                    "source_id": row["source_id"],
                    "source_commit": row["source_commit"],
                    "split_group": row.get("split_group"),
                    "operator": operator,
                    "original_harness": original_harness,
                    "visible_harness": visible_harness,
                    "default_calls": default_calls,
                    "target_calls": target_calls,
                    "minimum_default_equal": 2,
                    "minimum_target_different": minimum_target_different,
                    "fixture_derivation": (
                        "Babel AST literal-default node with explicit-value compatibility and "
                        "omitted-or-unset target probes"
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
        "schema_version": "0.7-js-ts-default-discrimination-plan-1",
        "benchmark": "SkillScriptBench",
        "status": "ready" if not shortages else "fail",
        "claim_boundary": (
            "This plan tests isolated JavaScript/TypeScript default-node semantics, not full-script "
            "utility. Harnesses and results remain private construction artifacts."
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
            "language_source_round_robin": True,
            "unique_content_component_per_family": True,
            "global_content_component_uniqueness": global_component_uniqueness,
            "unique_operator_template_per_family": True,
            "uses_model_results": False,
            "shortages": shortages,
        },
        "construction_runtime": {
            "platform": platform.platform(),
            "node_version": subprocess.run(
                [_node_executable(), "--version"], capture_output=True, text=True, check=True
            ).stdout.strip(),
        },
        "case_count": len(cases),
        "family_counts": dict(sorted(Counter(case["case_type"] for case in cases).items())),
        "language_counts": dict(sorted(Counter(case["language"] for case in cases).items())),
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


def build_js_ts_identity_control(
    plan: str | Path | dict[str, Any],
    output: str | Path | None = None,
) -> dict[str, Any]:
    return build_js_ts_control(plan, output, control_type="identity")


def build_js_ts_control(
    plan: str | Path | dict[str, Any],
    output: str | Path | None = None,
    *,
    control_type: str = "identity",
) -> dict[str, Any]:
    if control_type not in {"identity", "equivalent"}:
        raise ValueError(f"unsupported_js_ts_control_type:{control_type}")
    payload = read_json(plan) if isinstance(plan, (str, Path)) else plan
    result = json.loads(json.dumps(payload))
    result.pop("plan_hash", None)
    result["schema_version"] = "0.8-js-ts-default-control-plan-1"
    result["status"] = f"ready_{control_type}_negative_control"
    result["parent_plan_hash"] = payload.get("plan_hash") or canonical_json_hash(payload)
    result["control"] = {
        "type": f"{control_type}_visible_harness",
        "expected_result": "zero discrimination passes",
        "changes_source_text": control_type == "equivalent",
    }
    for case in result.get("cases", []):
        case["visible_harness"] = case["original_harness"] + (
            "\nvoid 0; // skillscriptbench-equivalent-control\n"
            if control_type == "equivalent"
            else ""
        )
        case["control_type"] = f"{control_type}_visible_harness"
    result["plan_hash"] = canonical_json_hash(result)
    if output is not None:
        write_json(output, result)
    return result


def _observe_node(
    harness: str,
    suffix: str,
    call: dict[str, Any],
    *,
    timeout: int,
    sandbox_mode: str = "none",
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ssb-node-") as directory:
        root = Path(directory)
        path = root / f"harness{suffix}"
        path.write_text(harness, encoding="utf-8")
        env = {
            "HOME": str(root),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            **{key: str(value) for key, value in call.get("env", {}).items()},
        }
        completed = subprocess.run(
            isolated_command(
                [_node_executable(), "--no-warnings", str(path)],
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


def execute_js_ts_discrimination(
    plan: str | Path | dict[str, Any],
    output: str | Path | None = None,
    *,
    timeout: int = 15,
    sandbox_mode: str = "none",
) -> dict[str, Any]:
    payload = read_json(plan) if isinstance(plan, (str, Path)) else plan
    records: list[dict[str, Any]] = []
    for case in payload.get("cases", []):
        try:
            observe_original = lambda call: _observe_node(  # noqa: E731
                case["original_harness"],
                case["suffix"],
                call,
                timeout=timeout,
                sandbox_mode=sandbox_mode,
            )
            observe_visible = lambda call: _observe_node(  # noqa: E731
                case["visible_harness"],
                case["suffix"],
                call,
                timeout=timeout,
                sandbox_mode=sandbox_mode,
            )
            original_default = [observe_original(call) for call in case["default_calls"]]
            visible_default = [observe_visible(call) for call in case["default_calls"]]
            original_target = [observe_original(call) for call in case["target_calls"]]
            visible_target = [observe_visible(call) for call in case["target_calls"]]
            original_repeat = [observe_original(call) for call in case["target_calls"]]
            visible_repeat = [observe_visible(call) for call in case["target_calls"]]
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
                    "language": case["language"],
                    "source_id": case["source_id"],
                    "status": "pass" if passed else "fail",
                    "default_equal_count": default_equal,
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
                    "language": case["language"],
                    "source_id": case["source_id"],
                    "status": "fail",
                    "failure": f"{type(exc).__name__}:{exc}",
                }
            )
    passed = [record for record in records if record["status"] == "pass"]
    result = {
        "schema_version": "0.7-js-ts-default-discrimination-result-1",
        "benchmark": "SkillScriptBench",
        "status": "pass" if records and len(passed) == len(records) else "fail",
        "claim_boundary": (
            "A pass proves deterministic isolated Babel-node separation and explicit-value "
            "compatibility. It does not prove full-script utility or model repair."
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
        "language_summary": {
            language: {
                "case_count": sum(record["language"] == language for record in records),
                "pass_count": sum(
                    record["language"] == language and record["status"] == "pass"
                    for record in records
                ),
            }
            for language in sorted({record["language"] for record in records})
        },
        "behavior_executed": True,
        "model_calls": 0,
        "records": records,
    }
    result["result_hash"] = canonical_json_hash(result)
    if output is not None:
        write_json(output, result)
    return result
