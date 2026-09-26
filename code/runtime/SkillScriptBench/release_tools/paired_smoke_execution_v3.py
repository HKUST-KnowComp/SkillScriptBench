from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
from typing import Any

from release_tools import paired_smoke_execution_v2 as v2
from skillscriptbench.io_utils import canonical_json_hash, read_json, sha256_file, write_json
from skillscriptbench.package_matrix_conditions_v64 import _parse_final_edit_json


SCHEMA_VERSION = "skillscriptbench-paired-smoke-execution-v3"
PROTOCOL_VERSION = "paired_artifact_states_smoke_v3"
BASE_APPLY_PACKAGE_EDITS = v2.parse_and_apply_package_edits


def _normalize_tool_arguments(content: str) -> tuple[str, list[list[str]]]:
    payload = _parse_final_edit_json(content)
    if not isinstance(payload, dict) or set(payload) != {"edits", "summary"}:
        return content, []
    edits = payload.get("edits")
    if not isinstance(edits, list):
        return content, []
    defaulted_by_edit: list[list[str]] = []
    for edit in edits:
        defaulted: list[str] = []
        if not isinstance(edit, dict):
            defaulted_by_edit.append(defaulted)
            continue
        if "target_node_id" not in edit:
            edit["target_node_id"] = ""
            defaulted.append("target_node_id")
        if "start_line" not in edit and isinstance(edit.get("end_line"), int):
            edit["start_line"] = edit["end_line"]
            defaulted.append("start_line_from_end_line")
        if "end_line" not in edit and isinstance(edit.get("start_line"), int):
            edit["end_line"] = edit["start_line"]
            defaulted.append("end_line_from_start_line")
        defaulted_by_edit.append(defaulted)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False), defaulted_by_edit


def _normalized_apply(
    content: str,
    source_package: Path,
    candidate_package: Path,
    *,
    structural_nodes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized, defaulted = _normalize_tool_arguments(content)
    result = BASE_APPLY_PACKAGE_EDITS(
        normalized,
        source_package,
        candidate_package,
        structural_nodes=structural_nodes,
    )
    result["transport_schema_normalization"] = {
        "policy": "deterministic_nonsemantic_missing_field_completion_v1",
        "defaulted_fields_by_edit": defaulted,
        "semantic_fields_inferred": False,
        "replacement_text_changed": False,
        "paths_changed": False,
    }
    return result


def _v3_code_hash() -> str:
    return sha256_file(Path(__file__))


def _upgrade_protocol(experiment: Path) -> dict[str, Any]:
    protocol_path = experiment / "stage" / "FROZEN_PROTOCOL.json"
    protocol = read_json(protocol_path)
    protocol.pop("protocol_hash", None)
    protocol.update(
        {
            "schema_version": SCHEMA_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "status": "frozen_before_primary_model_calls",
            "supersedes_protocol_version": v2.PROTOCOL_VERSION,
            "supersession_reason": (
                "The prior provider smoke exposed missing mechanically derivable line-editor "
                "fields. This version freezes generic nonsemantic completion before new calls."
            ),
            "transport_schema_normalization": {
                "target_node_id_missing": "empty_string",
                "start_line_missing_with_end_line": "copy_end_line",
                "end_line_missing_with_start_line": "copy_start_line",
                "all_other_missing_fields": "reject",
                "replacement_or_path_inference": False,
            },
        }
    )
    code_hashes = dict(protocol.get("code_sha256") or {})
    code_hashes["release_tools/paired_smoke_execution_v3.py"] = _v3_code_hash()
    protocol["code_sha256"] = dict(sorted(code_hashes.items()))
    protocol["protocol_hash"] = canonical_json_hash(protocol)
    write_json(protocol_path, protocol)
    return protocol


def prepare_execution(
    workspace_root: Path,
    benchmark_root: Path,
    parent_protocol_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    v2.prepare_execution(
        workspace_root, benchmark_root, parent_protocol_root, output_root
    )
    _upgrade_protocol(output_root.resolve())
    report = audit_execution(output_root, benchmark_root, parent_protocol_root)
    write_json(output_root.resolve() / "_audit" / "ZERO_CALL_PREFLIGHT.json", report)
    if report["status"] != "pass":
        raise ValueError(f"paired_smoke_v3_preflight_failed:{report['failed_checks']}")
    return report


def audit_execution(
    experiment_root: Path, benchmark_root: Path, parent_protocol_root: Path
) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    report = v2.audit_execution(experiment, benchmark_root, parent_protocol_root)
    protocol = read_json(experiment / "stage" / "FROZEN_PROTOCOL.json")
    checks = dict(report["checks"])
    checks.update(
        {
            "v3_schema_frozen": protocol.get("schema_version") == SCHEMA_VERSION,
            "v3_protocol_version_frozen": protocol.get("protocol_version")
            == PROTOCOL_VERSION,
            "v3_adapter_hash_frozen": (protocol.get("code_sha256") or {}).get(
                "release_tools/paired_smoke_execution_v3.py"
            )
            == _v3_code_hash(),
            "normalizer_policy_frozen": (
                protocol.get("transport_schema_normalization") or {}
            ).get("replacement_or_path_inference")
            is False,
        }
    )
    report.update(
        {
            "schema_version": SCHEMA_VERSION,
            "status": "pass" if all(checks.values()) else "fail",
            "checks": checks,
            "failed_checks": [name for name, value in checks.items() if not value],
            "check_count": len(checks),
            "passed_check_count": sum(bool(value) for value in checks.values()),
            "protocol_hash": protocol["protocol_hash"],
        }
    )
    report.pop("preflight_hash", None)
    report["preflight_hash"] = canonical_json_hash(report)
    return report


def _assert_v3_preflight(experiment: Path) -> None:
    protocol = read_json(experiment / "stage" / "FROZEN_PROTOCOL.json")
    preflight = read_json(experiment / "_audit" / "ZERO_CALL_PREFLIGHT.json")
    if (
        protocol.get("schema_version") != SCHEMA_VERSION
        or (protocol.get("code_sha256") or {}).get(
            "release_tools/paired_smoke_execution_v3.py"
        )
        != _v3_code_hash()
        or preflight.get("status") != "pass"
        or not v2._embedded_hash_valid(preflight, "preflight_hash")
        or preflight.get("protocol_hash") != protocol.get("protocol_hash")
    ):
        raise ValueError("paired_smoke_v3_valid_preflight_required")


def run_calls(experiment_root: Path, *, phase: str, api_key: str) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    _assert_v3_preflight(experiment)
    original = v2.parse_and_apply_package_edits
    v2.parse_and_apply_package_edits = _normalized_apply
    try:
        return v2._run_calls(experiment, phase=phase, api_key=api_key)
    finally:
        v2.parse_and_apply_package_edits = original


def prepare_revisions(experiment_root: Path) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    _assert_v3_preflight(experiment)
    return v2.freeze_primary_and_prepare_revisions(experiment)


def freeze_final(experiment_root: Path) -> dict[str, Any]:
    experiment = experiment_root.resolve()
    _assert_v3_preflight(experiment)
    return v2.freeze_final_candidates(experiment)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--workspace-root", type=Path, default=v2._workspace())
    prepare.add_argument("--benchmark-root", type=Path, required=True)
    prepare.add_argument("--parent-protocol-root", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    audit = commands.add_parser("preflight")
    audit.add_argument("--experiment-root", type=Path, required=True)
    audit.add_argument("--benchmark-root", type=Path, required=True)
    audit.add_argument("--parent-protocol-root", type=Path, required=True)
    for name in ("run-primary", "run-revisions", "prepare-revisions", "freeze-final"):
        command = commands.add_parser(name)
        command.add_argument("--experiment-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "prepare":
        result = prepare_execution(
            args.workspace_root,
            args.benchmark_root,
            args.parent_protocol_root,
            args.output,
        )
    elif args.command == "preflight":
        result = audit_execution(
            args.experiment_root, args.benchmark_root, args.parent_protocol_root
        )
    elif args.command == "run-primary":
        result = run_calls(
            args.experiment_root,
            phase="primary",
            api_key=getpass.getpass("OpenLux API key: "),
        )
    elif args.command == "prepare-revisions":
        result = prepare_revisions(args.experiment_root)
    elif args.command == "run-revisions":
        result = run_calls(
            args.experiment_root,
            phase="revision",
            api_key=getpass.getpass("OpenLux API key: "),
        )
    else:
        result = freeze_final(args.experiment_root)
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
