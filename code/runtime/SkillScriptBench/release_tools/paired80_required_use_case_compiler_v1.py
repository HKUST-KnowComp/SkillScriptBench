from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from bvi_skill_evo import artifact_state_gate_v2 as gate
from release_tools import paired80_artifact_state_ast_only_v3 as source_experiment
from skillscriptbench.io_utils import canonical_json_hash, copy_tree_clean, hash_tree, read_json, sha256_file, write_json


SCHEMA_VERSION = "skillscriptbench-paired80-required-use-case-compiler-v1"
SOURCE_CONDITION = source_experiment.CONDITION
CONDITION = "artifact-state-ast-v3-plus-required-use-case-compiler-v1"
REPEAT_INDEX = 1
ELIGIBLE_ROUTES = {gate.ROUTE_MARKDOWN, gate.ROUTE_JOINT}
BEGIN_MARKER = "<!-- PUBLIC-REQUIRED-USE-CASE-BEGIN -->"
END_MARKER = "<!-- PUBLIC-REQUIRED-USE-CASE-END -->"


def _embedded_hash_valid(payload: dict[str, Any], field: str) -> bool:
    expected = payload.get(field)
    body = dict(payload)
    body.pop(field, None)
    return isinstance(expected, str) and canonical_json_hash(body) == expected


def _append_required_use_case(skill_text: str, required: str) -> str:
    if BEGIN_MARKER in skill_text or END_MARKER in skill_text:
        raise ValueError("paired80_required_use_case_marker_collision")
    return (
        skill_text.rstrip()
        + "\n\n## Preserved Required Use Case\n\n"
        + BEGIN_MARKER
        + "\n"
        + required.strip()
        + "\n"
        + END_MARKER
        + "\n"
    )


def prepare(
    benchmark_root: Path, source_root: Path, output_root: Path
) -> dict[str, Any]:
    benchmark = benchmark_root.resolve()
    source = source_root.resolve()
    output = output_root.resolve()
    if output.exists():
        raise FileExistsError(output)
    source_selection_path = source / "selected" / "CANDIDATE_SELECTION.json"
    source_selection = read_json(source_selection_path)
    if not _embedded_hash_valid(source_selection, "selection_hash"):
        raise ValueError("paired80_required_use_case_source_selection_invalid")
    source_rows = [
        row for row in source_selection["rows"] if int(row["repeat_index"]) == REPEAT_INDEX
    ]
    matrix = read_json(source / "stage" / "CASE_MATRIX.json")
    routes = {
        str(row["case_id"]): str(row["artifact_route"])
        for row in matrix["rows"]
        if int(row["repeat_index"]) == REPEAT_INDEX
    }
    if len(source_rows) != 80 or len(routes) != 80:
        raise ValueError("paired80_required_use_case_source_matrix_incomplete")

    output.parent.mkdir(parents=True, exist_ok=True)
    container = Path(tempfile.mkdtemp(prefix=f".{output.name}.building-", dir=output.parent))
    experiment = container / "experiment"
    try:
        rows = []
        for row in sorted(source_rows, key=lambda item: str(item["case_id"])):
            case_id = str(row["case_id"])
            source_package = source / str(row["package_path"])
            if source_experiment._tree_hash(source_package) != row["candidate_tree_hash"]:
                raise ValueError(f"paired80_required_use_case_source_changed:{case_id}")
            source_freeze = read_json(source_package.parent / "FINAL_CANDIDATE_FREEZE.json")
            if (
                not _embedded_hash_valid(source_freeze, "final_candidate_freeze_hash")
                or source_freeze["final_candidate_freeze_hash"]
                != row["final_candidate_freeze_hash"]
            ):
                raise ValueError(f"paired80_required_use_case_source_freeze_invalid:{case_id}")
            destination = experiment / "selected" / CONDITION / case_id / "package"
            copy_tree_clean(source_package, destination)
            case = benchmark / "public" / "cases" / case_id
            request_text = (case / "REQUEST.md").read_text(encoding="utf-8")
            required = gate.extract_required_use_case(request_text)
            if required is None:
                raise ValueError(f"paired80_required_use_case_missing:{case_id}")
            skill_path = destination / "SKILL.md"
            status = gate.markdown_contract_status(
                request_text, skill_path.read_text(encoding="utf-8")
            )
            route = routes[case_id]
            eligible = route in ELIGIBLE_ROUTES
            if eligible and status["status"] != gate.DOC_SATISFIED:
                skill_path.write_text(
                    _append_required_use_case(
                        skill_path.read_text(encoding="utf-8"), required
                    ),
                    encoding="utf-8",
                )
                document_origin = "append_exact_public_required_use_case"
            elif eligible:
                document_origin = "preserve_publicly_satisfied_document"
            else:
                document_origin = "preserve_route_ineligible_candidate"
            source_tree = hash_tree(source_package)
            candidate_tree = hash_tree(destination)
            changed = sorted(
                path
                for path in set(source_tree) | set(candidate_tree)
                if source_tree.get(path) != candidate_tree.get(path)
            )
            if any(path != "SKILL.md" for path in changed):
                raise ValueError(f"paired80_required_use_case_non_document_change:{case_id}:{changed}")
            if not eligible and changed:
                raise ValueError(f"paired80_required_use_case_route_ineligible_change:{case_id}")
            candidate_tree_hash = source_experiment._tree_hash(destination)
            freeze: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "case_id": case_id,
                "condition": CONDITION,
                "repeat_index": REPEAT_INDEX,
                "source_condition": SOURCE_CONDITION,
                "source_candidate_tree_hash": row["candidate_tree_hash"],
                "source_final_candidate_freeze_hash": row["final_candidate_freeze_hash"],
                "candidate_tree_hash": candidate_tree_hash,
                "artifact_route": route,
                "assessment_eligible": eligible,
                "source_document_status": status["status"],
                "required_use_case_hash": status["required_use_case_hash"],
                "document_origin": document_origin,
                "changed_paths": changed,
                "hidden_artifacts_consumed": False,
                "candidate_selection_uses_hidden": False,
                "model_calls": 0,
            }
            freeze["freeze_hash"] = canonical_json_hash(freeze)
            write_json(destination.parent / "FINAL_CANDIDATE_FREEZE.json", freeze)
            rows.append(
                {
                    "case_id": case_id,
                    "condition": CONDITION,
                    "repeat_index": REPEAT_INDEX,
                    "package_path": destination.relative_to(experiment).as_posix(),
                    "source_candidate_tree_hash": row["candidate_tree_hash"],
                    "candidate_tree_hash": candidate_tree_hash,
                    "freeze_hash": freeze["freeze_hash"],
                    "artifact_route": route,
                    "assessment_eligible": eligible,
                    "document_origin": document_origin,
                    "changed_paths": changed,
                }
            )
        selection: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": "frozen_before_hidden_evaluation",
            "case_count": len(rows),
            "row_count": len(rows),
            "condition": CONDITION,
            "repeat_index": REPEAT_INDEX,
            "document_origin_counts": dict(sorted(Counter(row["document_origin"] for row in rows).items())),
            "route_counts": dict(sorted(Counter(row["artifact_route"] for row in rows).items())),
            "changed_candidate_count": sum(bool(row["changed_paths"]) for row in rows),
            "rows": rows,
            "hidden_artifacts_consumed": False,
            "candidate_selection_uses_hidden": False,
            "model_calls": 0,
        }
        selection["selection_hash"] = canonical_json_hash(selection)
        write_json(experiment / "selected" / "CANDIDATE_SELECTION.json", selection)
        protocol: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": "frozen_before_hidden_evaluation",
            "benchmark_root": benchmark.as_posix(),
            "source_root": source.as_posix(),
            "source_selection_sha256": sha256_file(source_selection_path),
            "source_selection_hash": source_selection["selection_hash"],
            "source_condition": SOURCE_CONDITION,
            "target_condition": CONDITION,
            "repeat_index": REPEAT_INDEX,
            "method": "route_constrained_exact_public_required_use_case_compilation",
            "driver_sha256": sha256_file(Path(__file__).resolve()),
            "selection_hash": selection["selection_hash"],
            "model_calls": 0,
            "hidden_artifacts_consumed": False,
        }
        protocol["protocol_hash"] = canonical_json_hash(protocol)
        write_json(experiment / "stage" / "FROZEN_PROTOCOL.json", protocol)
        checks = {
            "eighty_rows": len(rows) == 80,
            "forty_route_eligible": sum(row["assessment_eligible"] for row in rows) == 40,
            "selection_hash_valid": _embedded_hash_valid(selection, "selection_hash"),
            "protocol_hash_valid": _embedded_hash_valid(protocol, "protocol_hash"),
            "all_freezes_valid": all(
                _embedded_hash_valid(
                    read_json(experiment / row["package_path"] / ".." / "FINAL_CANDIDATE_FREEZE.json"),
                    "freeze_hash",
                )
                for row in rows
            ),
            "route_ineligible_unchanged": all(
                not row["changed_paths"] for row in rows if not row["assessment_eligible"]
            ),
            "hidden_absent": not any(experiment.rglob("HIDDEN_RESULT.json")),
            "private_absent": not any("_private" in path.parts for path in experiment.rglob("*")),
        }
        audit: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": "pass" if all(checks.values()) else "fail",
            "checks": checks,
            "failed_checks": [key for key, value in checks.items() if not value],
            "selection_hash": selection["selection_hash"],
            "behavioral_evaluator_loaded": False,
            "hidden_evaluation_executed": False,
            "model_calls": 0,
        }
        audit["audit_hash"] = canonical_json_hash(audit)
        write_json(experiment / "_audit" / "PREHIDDEN_AUDIT.json", audit)
        if audit["status"] != "pass":
            raise ValueError(f"paired80_required_use_case_prepare_failed:{audit['failed_checks']}")
        os.replace(experiment, output)
        return {"selection": selection, "prehidden": audit}
    finally:
        shutil.rmtree(container, ignore_errors=True)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = prepare(args.benchmark_root, args.source_root, args.output_root)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
