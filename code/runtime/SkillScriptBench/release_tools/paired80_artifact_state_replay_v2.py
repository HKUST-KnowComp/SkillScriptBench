from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from bvi_skill_evo.artifact_state_gate_v2 import (
    METHOD_ID,
    choose_public_projection,
)
from release_tools import paired80_execution_v1 as source_execution
from skillscriptbench.io_utils import (
    canonical_json_hash,
    read_json,
    sha256_file,
    write_json,
)


SCHEMA_VERSION = "skillscriptbench-paired80-artifact-state-replay-v2"
CONDITION = "artifact-state-ast-v2"
EXPECTED_CASES = 80
EXPECTED_REPEATS = 3


def _tree_hash(path: Path) -> str:
    return source_execution._tree_hash(path.resolve())


def _embedded_hash_valid(payload: dict[str, Any], field: str) -> bool:
    body = dict(payload)
    expected = str(body.pop(field, ""))
    return bool(expected) and canonical_json_hash(body) == expected


def _source_rows(source_root: Path) -> tuple[dict[tuple[int, str], dict[str, Any]], dict[tuple[int, str], dict[str, Any]]]:
    selection_path = source_root / "selected" / "CANDIDATE_SELECTION.json"
    primary_path = source_root / "selected_primary" / "PRIMARY_SELECTION.json"
    selection = read_json(selection_path)
    primary = read_json(primary_path)
    if not _embedded_hash_valid(selection, "selection_hash"):
        raise ValueError("paired80_source_selection_hash_invalid")
    if not _embedded_hash_valid(primary, "selection_hash"):
        raise ValueError("paired80_source_primary_selection_hash_invalid")

    ast_rows = {
        (int(row["repeat_index"]), str(row["case_id"])): row
        for row in selection["rows"]
        if row["condition"] == "ast-package-self-evolution"
    }
    package_primary_rows = {
        (int(row["repeat_index"]), str(row["case_id"])): row
        for row in primary["rows"]
        if row["view"] == "package-view"
    }
    expected = EXPECTED_CASES * EXPECTED_REPEATS
    if len(ast_rows) != expected or len(package_primary_rows) != expected:
        raise ValueError(
            f"paired80_source_matrix_incomplete:{len(ast_rows)}:{len(package_primary_rows)}"
        )
    if set(ast_rows) != set(package_primary_rows):
        raise ValueError("paired80_source_matrix_keys_differ")
    return ast_rows, package_primary_rows


def _public_case_ids(benchmark_root: Path) -> list[str]:
    rows = source_execution.base._jsonl_read(
        benchmark_root / "public" / "registry.jsonl"
    )
    case_ids = sorted(str(row["case_id"]) for row in rows)
    if len(case_ids) != EXPECTED_CASES or len(set(case_ids)) != EXPECTED_CASES:
        raise ValueError(f"paired80_public_registry_invalid:{len(case_ids)}")
    return case_ids


def _code_paths() -> dict[str, Path]:
    workspace = Path(__file__).resolve().parents[1]
    return {
        "bvi_skill_evo/artifact_scope_ast_v1.py": workspace
        / "bvi_skill_evo"
        / "artifact_scope_ast_v1.py",
        "bvi_skill_evo/artifact_state_gate_v2.py": workspace
        / "bvi_skill_evo"
        / "artifact_state_gate_v2.py",
        "release_tools/paired80_artifact_state_replay_v2.py": Path(__file__).resolve(),
    }


def run_public_replay(
    source_experiment: Path,
    benchmark_root: Path,
    output_root: Path,
    *,
    case_ids: Iterable[str] | None = None,
    repeat_indices: Iterable[int] | None = None,
) -> dict[str, Any]:
    source = source_experiment.resolve()
    benchmark = benchmark_root.resolve()
    output = output_root.resolve()
    if output.exists():
        raise FileExistsError(output)

    ast_rows, primary_rows = _source_rows(source)
    all_case_ids = _public_case_ids(benchmark)
    selected_case_ids = sorted(set(case_ids or all_case_ids))
    selected_repeats = sorted(set(repeat_indices or range(1, EXPECTED_REPEATS + 1)))
    if not set(selected_case_ids).issubset(all_case_ids):
        raise ValueError("paired80_replay_unknown_case")
    if not set(selected_repeats).issubset(range(1, EXPECTED_REPEATS + 1)):
        raise ValueError("paired80_replay_unknown_repeat")

    source_selection_path = source / "selected" / "CANDIDATE_SELECTION.json"
    source_primary_path = source / "selected_primary" / "PRIMARY_SELECTION.json"
    code_paths = _code_paths()
    if not all(path.is_file() for path in code_paths.values()):
        raise FileNotFoundError("paired80_replay_code_missing")
    protocol: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "frozen_before_public_projection",
        "scientific_role": "development_posthoc_public_projection_replay",
        "condition": CONDITION,
        "method": METHOD_ID,
        "case_count": len(selected_case_ids),
        "repeat_count": len(selected_repeats),
        "candidate_rows": len(selected_case_ids) * len(selected_repeats),
        "source_experiment": source.name,
        "source_selection_sha256": sha256_file(source_selection_path),
        "source_primary_selection_sha256": sha256_file(source_primary_path),
        "benchmark_public_registry_sha256": sha256_file(
            benchmark / "public" / "registry.jsonl"
        ),
        "case_ids": selected_case_ids,
        "repeat_indices": selected_repeats,
        "candidate_sources": ["ast-v1-final", "frozen-package-primary"],
        "model_calls": 0,
        "behavioral_evaluator_loaded": False,
        "hidden_artifacts_consumed": False,
        "candidate_selection_uses_hidden": False,
        "code_sha256": {
            name: sha256_file(path) for name, path in sorted(code_paths.items())
        },
        "claim_boundary": (
            "This deterministic development replay reuses candidates frozen before the "
            "original hidden evaluation. It may diagnose artifact routing, but it is not a "
            "fresh blind model experiment."
        ),
    }
    protocol["protocol_hash"] = canonical_json_hash(protocol)

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.building-", dir=output.parent))
    try:
        write_json(temporary / "stage" / "FROZEN_REPLAY_PROTOCOL.json", protocol)
        rows: list[dict[str, Any]] = []
        for repeat_index in selected_repeats:
            for case_id in selected_case_ids:
                key = (repeat_index, case_id)
                parent = benchmark / "public" / "cases" / case_id / "package"
                request_path = benchmark / "public" / "cases" / case_id / "REQUEST.md"
                request_text = request_path.read_text(encoding="utf-8")
                ast_row = ast_rows[key]
                primary_row = primary_rows[key]
                ast_candidate = source / str(ast_row["package_path"])
                primary_candidate = source / str(primary_row["package_path"])
                if _tree_hash(ast_candidate) != ast_row["candidate_tree_hash"]:
                    raise ValueError(f"paired80_source_ast_candidate_changed:{key}")
                if _tree_hash(primary_candidate) != primary_row["package_tree_hash"]:
                    raise ValueError(f"paired80_source_primary_candidate_changed:{key}")
                if _tree_hash(parent) != ast_row["parent_tree_hash"]:
                    raise ValueError(f"paired80_source_parent_mismatch:{key}")

                candidate_root = (
                    temporary
                    / "selected"
                    / f"repeat-{repeat_index:02d}"
                    / CONDITION
                    / case_id
                )
                package = candidate_root / "package"
                selection = choose_public_projection(
                    parent,
                    (
                        ("ast-v1-final", ast_candidate),
                        ("frozen-package-primary", primary_candidate),
                    ),
                    package,
                    request_text,
                )
                freeze: dict[str, Any] = {
                    "schema_version": SCHEMA_VERSION,
                    "condition": CONDITION,
                    "method": METHOD_ID,
                    "repeat_index": repeat_index,
                    "case_id": case_id,
                    "origin": "public_artifact_state_projection",
                    "package_path": package.relative_to(temporary).as_posix(),
                    "candidate_tree_hash": _tree_hash(package),
                    "parent_tree_hash": _tree_hash(parent),
                    "source_ast_tree_hash": ast_row["candidate_tree_hash"],
                    "source_primary_tree_hash": primary_row["package_tree_hash"],
                    "source_ast_freeze_hash": ast_row["final_candidate_freeze_hash"],
                    "source_ast_run_record_hash": ast_row["run_record_hash"],
                    "source_primary_run_record_hash": primary_row["run_record_hash"],
                    "projection_selection_hash": selection["selection_hash"],
                    "selected_source": selection["selected_source"],
                    "artifact_route": selection["selected_route"],
                    "protocol_hash": protocol["protocol_hash"],
                    "behavioral_evaluator_loaded": False,
                    "hidden_artifacts_consumed": False,
                    "candidate_selection_uses_hidden": False,
                    "credential_persisted": False,
                }
                freeze["final_candidate_freeze_hash"] = canonical_json_hash(freeze)
                write_json(candidate_root / "ARTIFACT_STATE_SELECTION.json", selection)
                write_json(candidate_root / "FINAL_CANDIDATE_FREEZE.json", freeze)
                rows.append(dict(freeze))

        rows.sort(key=lambda row: (int(row["repeat_index"]), str(row["case_id"])))
        selection_payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": "complete",
            "scientific_role": protocol["scientific_role"],
            "condition": CONDITION,
            "method": METHOD_ID,
            "case_count": len(selected_case_ids),
            "repeat_count": len(selected_repeats),
            "row_count": len(rows),
            "rows": rows,
            "model_calls": 0,
            "behavioral_evaluator_loaded": False,
            "hidden_artifacts_consumed": False,
            "candidate_selection_uses_hidden": False,
            "credential_persisted": False,
        }
        selection_payload["selection_hash"] = canonical_json_hash(selection_payload)
        write_json(temporary / "selected" / "CANDIDATE_SELECTION.json", selection_payload)
        os.replace(temporary, output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    audit = prehidden_audit(output, source, benchmark)
    write_json(output / "_audit" / "PREHIDDEN_AUDIT.json", audit)
    if audit["status"] != "pass":
        raise ValueError(f"paired80_replay_prehidden_failed:{audit['failed_checks']}")
    return audit


def prehidden_audit(
    output_root: Path, source_experiment: Path, benchmark_root: Path
) -> dict[str, Any]:
    output = output_root.resolve()
    source = source_experiment.resolve()
    benchmark = benchmark_root.resolve()
    protocol = read_json(output / "stage" / "FROZEN_REPLAY_PROTOCOL.json")
    selection = read_json(output / "selected" / "CANDIDATE_SELECTION.json")
    rows = list(selection.get("rows") or [])
    freezes_valid = True
    trees_valid = True
    identities: list[tuple[int, str, str]] = []
    for row in rows:
        package = output / str(row["package_path"])
        freeze = read_json(package.parent / "FINAL_CANDIDATE_FREEZE.json")
        freezes_valid = freezes_valid and _embedded_hash_valid(
            freeze, "final_candidate_freeze_hash"
        )
        trees_valid = trees_valid and _tree_hash(package) == row["candidate_tree_hash"]
        identities.append((int(row["repeat_index"]), str(row["case_id"]), str(row["condition"])))

    code_hashes_current = protocol.get("code_sha256") == {
        name: sha256_file(path) for name, path in sorted(_code_paths().items())
    }
    high_entropy_hits: list[str] = []
    for path in output.rglob("*"):
        if not path.is_file() or path.stat().st_size > 5_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if source_execution.base.HIGH_ENTROPY_SECRET.search(text):
            high_entropy_hits.append(path.relative_to(output).as_posix())

    expected_rows = int(protocol["candidate_rows"])
    checks = {
        "protocol_hash_valid": _embedded_hash_valid(protocol, "protocol_hash"),
        "selection_hash_valid": _embedded_hash_valid(selection, "selection_hash"),
        "source_selection_unchanged": sha256_file(
            source / "selected" / "CANDIDATE_SELECTION.json"
        )
        == protocol["source_selection_sha256"],
        "source_primary_selection_unchanged": sha256_file(
            source / "selected_primary" / "PRIMARY_SELECTION.json"
        )
        == protocol["source_primary_selection_sha256"],
        "benchmark_public_registry_unchanged": sha256_file(
            benchmark / "public" / "registry.jsonl"
        )
        == protocol["benchmark_public_registry_sha256"],
        "code_hashes_current": code_hashes_current,
        "candidate_rows_exact": len(rows) == expected_rows,
        "candidate_identity_unique": len(set(identities)) == expected_rows,
        "condition_exact": set(row["condition"] for row in rows) == {CONDITION},
        "final_freezes_valid": freezes_valid,
        "candidate_tree_hashes_valid": trees_valid,
        "model_calls_zero": selection.get("model_calls") == 0,
        "hidden_flags_false": selection.get("behavioral_evaluator_loaded") is False
        and selection.get("hidden_artifacts_consumed") is False,
        "selection_hidden_independent": selection.get("candidate_selection_uses_hidden")
        is False,
        "experiment_has_no_private_directory": not (output / "_private").exists(),
        "no_high_entropy_secret": not high_entropy_hits,
        "credential_not_persisted": selection.get("credential_persisted") is False,
    }
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "failed_checks": [name for name, value in checks.items() if not value],
        "check_count": len(checks),
        "passed_check_count": sum(bool(value) for value in checks.values()),
        "candidate_row_count": len(rows),
        "route_counts": dict(sorted(Counter(row["artifact_route"] for row in rows).items())),
        "selected_source_counts": dict(
            sorted(Counter(row["selected_source"] for row in rows).items())
        ),
        "model_calls": 0,
        "behavioral_evaluator_loaded": False,
        "hidden_artifacts_consumed": False,
        "credential_persisted": False,
        "high_entropy_secret_hits": high_entropy_hits,
    }
    result["audit_hash"] = canonical_json_hash(result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-experiment", type=Path, required=True)
    parser.add_argument("--benchmark-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = run_public_replay(
        args.source_experiment, args.benchmark_root, args.output
    )
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
