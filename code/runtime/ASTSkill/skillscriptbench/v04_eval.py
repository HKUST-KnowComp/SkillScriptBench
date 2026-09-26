from __future__ import annotations

import ast
import inspect
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from .io_utils import canonical_json_hash, copy_tree_clean, hash_tree, read_json, write_json
from .v04_catalog import _run_probe


def _family(manifest: dict[str, Any], family_id: str) -> dict[str, Any]:
    try:
        return next(row for row in manifest["families"] if row["family_id"] == family_id)
    except StopIteration as exc:
        raise KeyError(family_id) from exc


def _function_node(source: str, function_name: str) -> ast.FunctionDef:
    tree = ast.parse(source, filename="<candidate>")
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )


def _signature_schema(source: str, function_name: str) -> list[dict[str, Any]]:
    function = _function_node(source, function_name)
    positional = [*function.args.posonlyargs, *function.args.args]
    default_nodes: dict[str, ast.AST] = {}
    if function.args.defaults:
        default_nodes.update(
            {
                argument.arg: default
                for argument, default in zip(
                    positional[-len(function.args.defaults) :],
                    function.args.defaults,
                )
            }
        )
    default_nodes.update(
        {
            argument.arg: default
            for argument, default in zip(function.args.kwonlyargs, function.args.kw_defaults)
            if default is not None
        }
    )
    rows = []
    for argument in [*positional, *function.args.kwonlyargs]:
        default = default_nodes.get(argument.arg)
        try:
            default_value = ast.literal_eval(default) if default is not None else None
        except Exception:
            default_value = "<nonliteral>"
        rows.append(
            {
                "name": argument.arg,
                "kind": "keyword_only" if argument in function.args.kwonlyargs else "positional",
                "has_default": default is not None,
                "default": default_value,
            }
        )
    if function.args.vararg:
        rows.append({"name": function.args.vararg.arg, "kind": "vararg", "has_default": False})
    if function.args.kwarg:
        rows.append({"name": function.args.kwarg.arg, "kind": "kwarg", "has_default": False})
    return rows


def _compatibility_signature(baseline_source: str, candidate_source: str, function_name: str) -> dict[str, Any]:
    try:
        baseline = _signature_schema(baseline_source, function_name)
        candidate = _signature_schema(candidate_source, function_name)
    except (SyntaxError, StopIteration) as exc:
        return {"status": "fail", "failures": [f"signature_parse:{type(exc).__name__}:{exc}"]}
    failures: list[str] = []
    candidate_by_name = {row["name"]: row for row in candidate}
    baseline_names = [row["name"] for row in baseline]
    candidate_names = [row["name"] for row in candidate]
    for baseline_parameter in baseline:
        current = candidate_by_name.get(baseline_parameter["name"])
        if current is None:
            failures.append(f"removed_parameter:{baseline_parameter['name']}")
            continue
        if current["kind"] != baseline_parameter["kind"]:
            failures.append(f"parameter_kind_changed:{baseline_parameter['name']}")
        if current.get("has_default") != baseline_parameter.get("has_default"):
            failures.append(f"parameter_default_presence_changed:{baseline_parameter['name']}")
        if current.get("default") != baseline_parameter.get("default"):
            failures.append(f"parameter_default_changed:{baseline_parameter['name']}")
    projected = [name for name in candidate_names if name in set(baseline_names)]
    if projected != baseline_names:
        failures.append("existing_parameter_order_changed")
    for row in candidate:
        if row["name"] not in set(baseline_names) and not row.get("has_default"):
            failures.append(f"new_required_parameter:{row['name']}")
    return {
        "status": "pass" if not failures else "fail",
        "failures": failures,
        "baseline": baseline,
        "candidate": candidate,
    }


def _evaluate_rows(source: str, function_name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    inputs = [row["input"] for row in rows]
    try:
        actual = _run_probe(source, function_name, inputs, timeout=12)
    except Exception as exc:
        return {
            "status": "fail",
            "requested": len(rows),
            "matched": 0,
            "failures": [f"candidate_execution:{type(exc).__name__}:{exc}"],
        }
    matched = 0
    failures: list[dict[str, Any]] = []
    for expected, observed in zip(rows, actual):
        observed_ok = observed.get("status") == "ok"
        equal = observed_ok and canonical_json_hash(observed.get("output")) == canonical_json_hash(
            expected["expected_output"]
        )
        if equal:
            matched += 1
        elif len(failures) < 8:
            failures.append(
                {
                    "input_hash": canonical_json_hash(expected["input"]),
                    "observed_status": observed.get("status"),
                    "observed_output_hash": (
                        canonical_json_hash(observed.get("output")) if observed_ok else None
                    ),
                    "expected_output_hash": canonical_json_hash(expected["expected_output"]),
                }
            )
    passed = len(rows) > 0 and matched == len(rows) and len(actual) == len(rows)
    return {
        "status": "pass" if passed else "fail",
        "requested": len(rows),
        "observed": len(actual),
        "matched": matched,
        "match_rate": matched / len(rows) if rows else 0.0,
        "failures": failures,
    }


def freeze_candidate(
    candidate_root: str | Path,
    run_root: str | Path,
    *,
    family_id: str,
    diagnostic_private_oracle_injected: bool = False,
) -> dict[str, Any]:
    candidate = Path(candidate_root).resolve()
    run = Path(run_root).resolve()
    record = {
        "schema_version": "0.4-candidate-freeze-1",
        "status": "candidate_frozen",
        "family_id": family_id,
        "candidate_root": str(candidate),
        "candidate_hashes": hash_tree(candidate),
        "candidate_tree_hash": canonical_json_hash(hash_tree(candidate)),
        "hidden_evaluation_loaded": False,
        "diagnostic_private_oracle_injected": diagnostic_private_oracle_injected,
    }
    write_json(run / "candidate_frozen.json", record)
    return record


def materialize_diagnostic_candidate(
    benchmark_root: str | Path,
    *,
    family_id: str,
    mode: str,
    output_root: str | Path,
) -> dict[str, Any]:
    if mode not in {"visible-baseline", "private-oracle-control"}:
        raise ValueError(mode)
    benchmark = Path(benchmark_root).resolve()
    manifest = read_json(benchmark / "public" / "benchmark_manifest.json")
    family = _family(manifest, family_id)
    if family.get("evolution_kind") is None:
        raise ValueError("utility_only_family")
    run = Path(output_root).resolve()
    if run.exists():
        shutil.rmtree(run)
    candidate = run / "candidate"
    visible = benchmark / "public" / family["public_family_path"] / "evolution" / "visible"
    copy_tree_clean(visible, candidate)
    injected = mode == "private-oracle-control"
    if injected:
        oracle = benchmark / "_private" / "families" / family_id / "oracle" / "scripts" / "entry.py"
        (candidate / "scripts" / "entry.py").write_bytes(oracle.read_bytes())
    freeze = freeze_candidate(
        candidate,
        run,
        family_id=family_id,
        diagnostic_private_oracle_injected=injected,
    )
    write_json(
        run / "diagnostic_record.json",
        {
            "schema_version": "0.4-diagnostic-candidate-1",
            "mode": mode,
            "formal_model_condition": False,
            "candidate_freeze_hash": canonical_json_hash(freeze),
        },
    )
    return freeze


def evaluate_frozen_candidate(
    benchmark_root: str | Path,
    *,
    run_root: str | Path,
    output: str | Path | None = None,
    require_doc_sync: bool = True,
    allow_diagnostic_oracle: bool = False,
) -> dict[str, Any]:
    benchmark = Path(benchmark_root).resolve()
    run = Path(run_root).resolve()
    freeze = read_json(run / "candidate_frozen.json")
    candidate = run / "candidate"
    current_hashes = hash_tree(candidate)
    failures: list[str] = []
    if freeze.get("hidden_evaluation_loaded") is not False:
        failures.append("hidden_loaded_before_freeze")
    if freeze.get("candidate_hashes") != current_hashes:
        failures.append("candidate_changed_after_freeze")
    if freeze.get("diagnostic_private_oracle_injected") and not allow_diagnostic_oracle:
        failures.append("private_oracle_injected_into_formal_candidate")
    manifest = read_json(benchmark / "public" / "benchmark_manifest.json")
    family = _family(manifest, freeze["family_id"])
    private = benchmark / "_private" / "families" / family["family_id"]
    evaluator = read_json(private / "evolution_evaluator.json")
    candidate_source_path = candidate / "scripts" / "entry.py"
    try:
        candidate_source = candidate_source_path.read_text(encoding="utf-8")
    except OSError as exc:
        candidate_source = ""
        failures.append(f"candidate_source_missing:{exc}")
    baseline_source = (private / "utility_oracle" / "scripts" / "entry.py").read_text(encoding="utf-8")
    signature = _compatibility_signature(baseline_source, candidate_source, family["function_name"])
    compatibility = _evaluate_rows(
        candidate_source,
        family["function_name"],
        evaluator["compatibility_rows"],
    )
    transfer = _evaluate_rows(
        candidate_source,
        family["function_name"],
        evaluator["hidden_transfer_rows"],
    )
    transfer_axes = {
        axis: _evaluate_rows(
            candidate_source,
            family["function_name"],
            [row for row in evaluator["hidden_transfer_rows"] if row.get("transfer_axis") == axis],
        )
        for axis in ("input", "parameter")
        if any(row.get("transfer_axis") == axis for row in evaluator["hidden_transfer_rows"])
    }
    doc_sync = {"status": "pass", "failures": []}
    if require_doc_sync and family["evolution_kind"] == "generalization":
        skill_text = (candidate / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        label = evaluator["private_construction_label"]["parameter"]
        if label not in skill_text:
            doc_sync = {"status": "fail", "failures": [f"missing_parameter_documentation:{label}"]}
    strong_pass = (
        not failures
        and signature["status"] == "pass"
        and compatibility["status"] == "pass"
        and transfer["status"] == "pass"
        and doc_sync["status"] == "pass"
    )
    report = {
        "schema_version": "0.4-evaluation-1",
        "status": "pass" if strong_pass else "fail",
        "family_id": family["family_id"],
        "split": family["split"],
        "evolution_kind": family["evolution_kind"],
        "strong_pass": strong_pass,
        "freeze_integrity": {"status": "pass" if not failures else "fail", "failures": failures},
        "signature_compatibility": signature,
        "behavioral_compatibility": compatibility,
        "hidden_transfer": transfer,
        "hidden_transfer_axes": transfer_axes,
        "doc_sync": doc_sync,
        "candidate_tree_hash": canonical_json_hash(current_hashes),
    }
    if output is not None:
        write_json(output, report)
    return report


def _utility_oracle_selftest(benchmark: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for family in manifest["families"]:
        private = benchmark / "_private" / "families" / family["family_id"]
        expected = read_json(private / "utility_expected.json")["instances"]
        source = (private / "utility_oracle" / "scripts" / "entry.py").read_text(encoding="utf-8")
        report = _evaluate_rows(
            source,
            family["function_name"],
            [
                {"input": row["input"], "expected_output": row["expected_output"]}
                for row in expected
            ],
        )
        rows.append({"family_id": family["family_id"], **report})
    return {
        "status": "pass" if all(row["status"] == "pass" for row in rows) else "fail",
        "family_pass_count": sum(row["status"] == "pass" for row in rows),
        "family_count": len(rows),
        "task_pass_count": sum(row["matched"] for row in rows),
        "task_count": sum(row["requested"] for row in rows),
        "rows": rows,
    }


def selftest_v04_benchmark(
    benchmark_root: str | Path,
    *,
    output_root: str | Path,
) -> dict[str, Any]:
    benchmark = Path(benchmark_root).resolve()
    output = Path(output_root).resolve()
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    manifest = read_json(benchmark / "public" / "benchmark_manifest.json")
    utility = _utility_oracle_selftest(benchmark, manifest)
    baseline_rows = []
    oracle_rows = []
    for family in manifest["families"]:
        if family.get("evolution_kind") is None:
            continue
        baseline_run = output / "visible-baseline" / family["family_id"]
        materialize_diagnostic_candidate(
            benchmark,
            family_id=family["family_id"],
            mode="visible-baseline",
            output_root=baseline_run,
        )
        baseline = evaluate_frozen_candidate(
            benchmark,
            run_root=baseline_run,
            require_doc_sync=False,
        )
        baseline_rows.append(baseline)

        oracle_run = output / "private-oracle-control" / family["family_id"]
        materialize_diagnostic_candidate(
            benchmark,
            family_id=family["family_id"],
            mode="private-oracle-control",
            output_root=oracle_run,
        )
        oracle = evaluate_frozen_candidate(
            benchmark,
            run_root=oracle_run,
            require_doc_sync=False,
            allow_diagnostic_oracle=True,
        )
        oracle_rows.append(oracle)
    baseline_passes = sum(row["strong_pass"] for row in baseline_rows)
    oracle_passes = sum(row["strong_pass"] for row in oracle_rows)
    evolution_count = len(baseline_rows)
    failures = []
    if utility["status"] != "pass":
        failures.append("utility_oracle_failed")
    if baseline_passes:
        failures.append(f"visible_baseline_false_positive:{baseline_passes}/{evolution_count}")
    if oracle_passes != evolution_count:
        failures.append(f"private_oracle_false_negative:{oracle_passes}/{evolution_count}")
    report = {
        "schema_version": "0.4-selftest-1",
        "status": "pass" if not failures else "fail",
        "model_calls": 0,
        "utility": utility,
        "evolution": {
            "case_count": evolution_count,
            "visible_baseline_strong_pass_count": baseline_passes,
            "private_oracle_strong_pass_count": oracle_passes,
            "baseline_rows": baseline_rows,
            "oracle_rows": oracle_rows,
        },
        "failures": failures,
    }
    write_json(output / "selftest.json", report)
    return report
