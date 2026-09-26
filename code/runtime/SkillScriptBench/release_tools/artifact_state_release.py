from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from skillscriptbench.io_utils import (
    canonical_json_hash,
    copy_tree_clean,
    hash_tree,
    read_json,
    sha256_file,
    write_json,
)


SCHEMA_VERSION = "skillscriptbench-artifact-state-release-v1"
RELEASE_VERSION = "paired_artifact_states_v1"
SOURCE_REL = Path("benchmark_recovery/package_matrix_pilot100_v0_62_0")
CODE_CAPSULE_REL = Path("benchmark_recovery/package_matrix_v0_62_code_capsule")
DEFAULT_OUTPUT_REL = Path("benchmark_release") / RELEASE_VERSION

REPAIR_STATES = ("clean", "script_fault", "doc_fault", "joint_fault")
EXPECTED_SOURCE_STATE_COUNTS = {
    "capability_narrow": 20,
    "clean": 20,
    "doc_fault": 20,
    "joint_fault": 20,
    "script_fault": 20,
}
CODE_SUFFIX_TO_LANGUAGE = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".sh": "shell",
    ".bash": "shell",
}

PUBLIC_FORBIDDEN_PATH_PARTS = {
    "_audit",
    "_private",
    "baseline_package",
    "oracle_package",
    "repair_bases",
}
PUBLIC_FORBIDDEN_METADATA_KEYS = {
    "base_id",
    "canonical_package_hashes",
    "canonical_package_path",
    "document_contract",
    "expected_visible_changed_files",
    "hidden_artifacts_exposed",
    "operator",
    "parent_case_id",
    "parent_evidence",
    "parent_transformation_id",
    "source_case_id",
    "source_label_sha256",
    "state",
    "target_path",
}
HIGH_ENTROPY_CREDENTIAL_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def _canonical_embedded_hash_valid(payload: dict[str, Any], key: str) -> bool:
    expected = payload.get(key)
    body = dict(payload)
    body.pop(key, None)
    return isinstance(expected, str) and expected == canonical_json_hash(body)


def _jsonl_write(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        for row in rows
    )
    path.write_text(data, encoding="utf-8")


def _jsonl_read(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _opaque_case_id(source_case_id: str) -> str:
    digest = canonical_json_hash(f"{SCHEMA_VERSION}\0case\0{source_case_id}")[:16]
    return f"ssb-state-{digest}"


def _opaque_base_id(source_base_id: str) -> str:
    digest = canonical_json_hash(f"{SCHEMA_VERSION}\0base\0{source_base_id}")[:16]
    return f"ssb-base-{digest}"


def _tree_hash(root: Path) -> str:
    return canonical_json_hash(hash_tree(root))


def _tree_diff(left: Path, right: Path) -> list[str]:
    left_hashes = hash_tree(left)
    right_hashes = hash_tree(right)
    return sorted(
        path
        for path in set(left_hashes) | set(right_hashes)
        if left_hashes.get(path) != right_hashes.get(path)
    )


def _language_for_target(target_path: str) -> str:
    suffix = Path(target_path).suffix.lower()
    return CODE_SUFFIX_TO_LANGUAGE.get(suffix, suffix.removeprefix(".") or "unknown")


def _walk_json_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        keys.update(str(key) for key in value)
        for nested in value.values():
            keys.update(_walk_json_keys(nested))
    elif isinstance(value, list):
        for nested in value:
            keys.update(_walk_json_keys(nested))
    return keys


def _is_release_metadata(path: Path, public_root: Path) -> bool:
    relative = path.relative_to(public_root)
    if "package" in relative.parts:
        return False
    return path.suffix.lower() in {".json", ".jsonl"}


def _scan_public_release(
    public_root: Path,
    private_markers: set[str],
) -> list[str]:
    violations: list[str] = []
    for path in sorted(public_root.rglob("*")):
        relative = path.relative_to(public_root)
        if path.is_symlink():
            violations.append(f"symlink:{relative.as_posix()}")
            continue
        if any(part in PUBLIC_FORBIDDEN_PATH_PARTS for part in relative.parts):
            violations.append(f"forbidden_path:{relative.as_posix()}")
        if not path.is_file() or path.stat().st_size > 5_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for marker in private_markers:
            if len(marker) >= 8 and marker in text:
                violations.append(f"private_marker:{relative.as_posix()}:{marker}")
        for pattern in HIGH_ENTROPY_CREDENTIAL_PATTERNS:
            if pattern.search(text):
                violations.append(
                    f"credential_pattern:{relative.as_posix()}:{pattern.pattern}"
                )
        if _is_release_metadata(path, public_root):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                if path.suffix.lower() == ".jsonl":
                    try:
                        payload = [json.loads(line) for line in text.splitlines() if line]
                    except json.JSONDecodeError:
                        violations.append(f"invalid_json_metadata:{relative.as_posix()}")
                        continue
                else:
                    violations.append(f"invalid_json_metadata:{relative.as_posix()}")
                    continue
            forbidden = _walk_json_keys(payload) & PUBLIC_FORBIDDEN_METADATA_KEYS
            for key in sorted(forbidden):
                violations.append(f"forbidden_metadata_key:{relative.as_posix()}:{key}")
    return sorted(set(violations))


def _source_payloads(source_root: Path) -> dict[str, Any]:
    return {
        "build": read_json(source_root / "BUILD_REPORT.json"),
        "freeze": read_json(source_root / "FREEZE_MANIFEST.json"),
        "validation": read_json(source_root / "VALIDATION_REPORT.json"),
        "public": read_json(source_root / "public" / "manifest.json"),
        "private": read_json(source_root / "_private" / "manifest.json"),
    }


def _closure_path(workspace_root: Path, source_root: Path, relative: str) -> Path | None:
    candidates = (
        workspace_root / relative,
        source_root.parent / "package_matrix_v0_62_code_capsule" / relative,
    )
    return next((path for path in candidates if path.is_file()), None)


def _label_index(source_root: Path) -> dict[str, dict[str, Any]]:
    labels = {
        path.parent.name: read_json(path)
        for path in sorted((source_root / "_private" / "cases").glob("*/label.json"))
    }
    return labels


def _source_private_markers(labels: Iterable[dict[str, Any]]) -> set[str]:
    markers: set[str] = set()
    for label in labels:
        for key in ("base_id", "parent_case_id", "parent_transformation_id"):
            value = label.get(key)
            if isinstance(value, str) and value:
                markers.add(value)
        operator = label.get("operator") or {}
        for key in ("operator_candidate_id", "mutation_id"):
            value = operator.get(key)
            if isinstance(value, str) and value:
                markers.add(value)
    return markers


def _paired_source_checks(
    source_root: Path,
    labels: dict[str, dict[str, Any]],
) -> tuple[dict[str, bool], dict[str, Any]]:
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for label in labels.values():
        if label.get("track") == "package_maintenance":
            grouped[str(label["base_id"])][str(label["state"])] = label

    exact_state_sets = all(set(group) == set(REPAIR_STATES) for group in grouped.values())
    expected_diffs = {
        "clean": set(),
        "doc_fault": {"SKILL.md"},
        "script_fault": None,
        "joint_fault": None,
    }
    diff_ok = True
    composition_ok = True
    request_match = True
    source_match = True
    contract_text_ok = True
    package_hash_ok = True
    details: list[dict[str, Any]] = []

    for base_id, states in sorted(grouped.items()):
        if set(states) != set(REPAIR_STATES):
            continue
        base_root = source_root / "_private" / "repair_bases" / base_id / "oracle_package"
        packages = {
            state: source_root / "public" / "cases" / label["case_id"] / "package"
            for state, label in states.items()
        }
        hashes = {state: hash_tree(package) for state, package in packages.items()}
        target = str(states["clean"]["target_path"])
        observed_diffs = {
            state: _tree_diff(base_root, package) for state, package in packages.items()
        }
        expected = {
            "clean": [],
            "doc_fault": ["SKILL.md"],
            "script_fault": [target],
            "joint_fault": sorted({"SKILL.md", target}),
        }
        base_diff_ok = observed_diffs == expected
        diff_ok = diff_ok and base_diff_ok

        all_paths = set().union(*(mapping.keys() for mapping in hashes.values()))
        base_composition_ok = True
        for path in all_paths:
            values = {state: hashes[state].get(path) for state in REPAIR_STATES}
            if path == "SKILL.md":
                valid = (
                    values["clean"] == values["script_fault"]
                    and values["doc_fault"] == values["joint_fault"]
                    and values["clean"] != values["doc_fault"]
                )
            elif path == target:
                valid = (
                    values["clean"] == values["doc_fault"]
                    and values["script_fault"] == values["joint_fault"]
                    and values["clean"] != values["script_fault"]
                )
            else:
                valid = len(set(values.values())) == 1
            base_composition_ok = base_composition_ok and valid
        composition_ok = composition_ok and base_composition_ok

        requests = {
            sha256_file(source_root / "public" / "cases" / label["case_id"] / "REQUEST.md")
            for label in states.values()
        }
        sources = {
            sha256_file(source_root / "public" / "cases" / label["case_id"] / "SOURCE.json")
            for label in states.values()
        }
        request_match = request_match and len(requests) == 1
        source_match = source_match and len(sources) == 1

        contract = states["clean"]["document_contract"]
        required = str(contract["required_text"])
        fault = str(contract["fault_text"])
        texts = {
            state: (package / "SKILL.md").read_text(encoding="utf-8")
            for state, package in packages.items()
        }
        base_contract_ok = all(
            required in texts[state] and fault not in texts[state]
            for state in ("clean", "script_fault")
        ) and all(
            fault in texts[state] and required not in texts[state]
            for state in ("doc_fault", "joint_fault")
        )
        contract_text_ok = contract_text_ok and base_contract_ok

        base_package_hash_ok = all(
            hashes[state] == states[state]["visible_package_hashes"]
            for state in REPAIR_STATES
        ) and hash_tree(base_root) == states["clean"]["canonical_package_hashes"]
        package_hash_ok = package_hash_ok and base_package_hash_ok

        details.append(
            {
                "base_id": base_id,
                "states": {state: states[state]["case_id"] for state in REPAIR_STATES},
                "target_path": target,
                "language": _language_for_target(target),
                "observed_diffs": observed_diffs,
                "diff_ok": base_diff_ok,
                "composition_ok": base_composition_ok,
                "contract_text_ok": base_contract_ok,
                "package_hash_ok": base_package_hash_ok,
            }
        )

    checks = {
        "repair_base_count_20": len(grouped) == 20,
        "each_base_has_exact_four_states": exact_state_sets,
        "paired_state_diffs_exact": diff_ok,
        "paired_state_composition_exact": composition_ok,
        "paired_requests_match": request_match,
        "paired_source_records_match": source_match,
        "paired_document_contracts_match_state": contract_text_ok,
        "paired_package_hashes_match_labels": package_hash_ok,
    }
    summary = {
        "base_count": len(grouped),
        "language_distribution": dict(
            sorted(Counter(row["language"] for row in details).items())
        ),
        "bases": details,
    }
    return checks, summary


def audit_source_archive(workspace_root: Path, source_root: Path) -> dict[str, Any]:
    workspace_root = workspace_root.resolve()
    source_root = source_root.resolve()
    payloads = _source_payloads(source_root)
    build = payloads["build"]
    freeze = payloads["freeze"]
    validation = payloads["validation"]
    public = payloads["public"]
    private = payloads["private"]
    labels = _label_index(source_root)
    public_by_id = {row["case_id"]: row for row in public["cases"]}
    private_by_id = {row["case_id"]: row for row in private["cases"]}

    embedded_checks = {
        "build_embedded_hash": _canonical_embedded_hash_valid(build, "build_hash"),
        "validation_embedded_hash": _canonical_embedded_hash_valid(
            validation, "validation_hash"
        ),
        "public_manifest_embedded_hash": _canonical_embedded_hash_valid(
            public, "manifest_hash"
        ),
        "private_manifest_embedded_hash": _canonical_embedded_hash_valid(
            private, "manifest_hash"
        ),
        "freeze_embedded_hash": _canonical_embedded_hash_valid(freeze, "freeze_hash"),
    }

    artifact_bindings: list[dict[str, Any]] = []
    for relative, expected in sorted(freeze["artifact_file_sha256"].items()):
        candidate = source_root / relative
        if not candidate.is_file():
            candidate = _closure_path(workspace_root, source_root, relative)
        actual = sha256_file(candidate) if candidate and candidate.is_file() else None
        artifact_bindings.append(
            {
                "path": relative,
                "resolved_path": (
                    candidate.resolve().relative_to(workspace_root).as_posix()
                    if candidate and candidate.is_file()
                    else None
                ),
                "expected_sha256": expected,
                "actual_sha256": actual,
                "match": actual == expected,
            }
        )

    code_bindings: list[dict[str, Any]] = []
    for relative, expected in sorted(freeze["code_sha256"].items()):
        candidate = _closure_path(workspace_root, source_root, relative)
        actual = sha256_file(candidate) if candidate and candidate.is_file() else None
        code_bindings.append(
            {
                "path": relative,
                "resolved_path": (
                    candidate.resolve().relative_to(workspace_root).as_posix()
                    if candidate and candidate.is_file()
                    else None
                ),
                "expected_sha256": expected,
                "actual_sha256": actual,
                "match": actual == expected,
            }
        )

    case_hashes_ok = True
    label_hashes_ok = True
    source_metadata_ok = True
    for case_id, public_row in public_by_id.items():
        case_root = source_root / "public" / "cases" / case_id
        package = case_root / "package"
        case_hashes_ok = case_hashes_ok and all(
            (
                public_row["package_hashes"] == hash_tree(package),
                public_row["task_sha256"] == sha256_file(case_root / "TASK.json"),
                public_row["request_sha256"] == sha256_file(case_root / "REQUEST.md"),
                public_row["source_sha256"] == sha256_file(case_root / "SOURCE.json"),
                public_row["license_sha256"]
                == sha256_file(case_root / "SOURCE_LICENSE.txt"),
            )
        )
        source_metadata_ok = source_metadata_ok and (
            read_json(case_root / "SOURCE.json") == public_row["source"]
        )
        private_row = private_by_id.get(case_id, {})
        label_path = source_root / "_private" / "cases" / case_id / "label.json"
        label_hashes_ok = label_hashes_ok and (
            label_path.is_file()
            and private_row.get("label_sha256") == sha256_file(label_path)
            and labels.get(case_id, {}).get("case_id") == case_id
        )

    paired_checks, paired_summary = _paired_source_checks(source_root, labels)
    private_markers = _source_private_markers(labels.values())
    public_violations = _scan_public_release(source_root / "public", private_markers)
    state_counts = Counter(label.get("state") for label in labels.values())
    core_checks = {
        "source_status_frozen": freeze.get("status") == "frozen",
        "build_status_complete": build.get("status") == "complete",
        "validation_status_complete": validation.get("status") == "complete",
        "source_case_count_100": len(labels) == 100,
        "source_state_counts_exact": dict(sorted(state_counts.items()))
        == EXPECTED_SOURCE_STATE_COUNTS,
        "public_private_id_bijection": set(public_by_id) == set(private_by_id) == set(labels),
        "public_tree_hash_matches_freeze": _tree_hash(source_root / "public")
        == freeze.get("public_tree_hash"),
        "private_tree_hash_matches_freeze": _tree_hash(source_root / "_private")
        == freeze.get("private_tree_hash"),
        "all_root_artifact_bindings_match": all(row["match"] for row in artifact_bindings),
        "all_code_bindings_match": all(row["match"] for row in code_bindings),
        "all_case_hashes_match_public_manifest": case_hashes_ok,
        "all_label_hashes_match_private_manifest": label_hashes_ok,
        "all_source_metadata_match": source_metadata_ok,
        "public_leak_scan_clean": not public_violations,
        "source_contains_no_symlinks": not any(path.is_symlink() for path in source_root.rglob("*")),
        "source_model_calls_zero": freeze.get("model_calls") == 0,
    }
    checks = {**embedded_checks, **core_checks, **paired_checks}
    report = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "historical_source_archive_recovery",
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "check_count": len(checks),
        "passed_check_count": sum(bool(value) for value in checks.values()),
        "source_root": source_root.relative_to(workspace_root).as_posix(),
        "source_freeze_hash": freeze.get("freeze_hash"),
        "source_freeze_manifest_sha256": sha256_file(source_root / "FREEZE_MANIFEST.json"),
        "artifact_bindings": artifact_bindings,
        "code_bindings": code_bindings,
        "public_scan_violations": public_violations,
        "state_counts": dict(sorted(state_counts.items())),
        "paired_summary": paired_summary,
        "model_calls": 0,
        "behavioral_evaluator_loaded": False,
        "claim_boundary": (
            "This audit establishes archive integrity, hidden isolation, and exact paired "
            "construction. It does not replay repair behavior or establish semantic adequacy "
            "of the historical evaluator."
        ),
    }
    report["source_audit_hash"] = canonical_json_hash(report)
    return report


def _write_public_readme(public_root: Path) -> None:
    text = """# SkillScriptBench Paired Artifact States

This release contains 80 method-neutral package-maintenance cases derived from 20 source
skill packages. Each source package has four hidden construction states. State and pairing
labels are not part of the public inputs.

Every public case contains `REQUEST.md`, `TASK.json`, source/license metadata, and the complete
visible `package/` with `SKILL.md` plus executable artifacts. `registry.jsonl` is the public
case index. Hidden canonical packages and state labels are distributed separately from this
public directory.

The benchmark supports matched comparisons of Markdown-only, raw-package, and structured
package evolution. It does not prescribe or bundle any model-specific AST facts.
"""
    (public_root / "README.md").write_text(text, encoding="utf-8")


def _evaluator_map() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "public_release_contains_evaluation_materials": False,
        "historical_backend": {
            "module": "skillscriptbench.package_matrix_v62",
            "callable": "evaluate_repair_candidate_v62",
            "classification": "lexical_syntax_scope_restoration_check",
            "checks": [
                "target-file syntax",
                "exact document-contract substring restoration",
                "canonical target or operator-fragment restoration",
                "changed-file scope",
                "exact no-op requirement for clean cases",
            ],
            "limitations": [
                "does not run source-native behavior tests for repair candidates",
                "operator-fragment restoration is weaker than semantic equivalence",
                "document correctness is checked by exact substring presence and absence",
                "must be supplemented before it is used as the paper's final behavioral evaluator",
            ],
        },
        "release_behavior_status": "historical_selftests_preserved_behavioral_upgrade_pending",
    }


def build_release(
    workspace_root: Path,
    source_root: Path,
    output_root: Path,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    workspace_root = workspace_root.resolve()
    source_root = source_root.resolve()
    output_root = output_root.resolve()
    source_audit = audit_source_archive(workspace_root, source_root)
    if source_audit["status"] != "pass":
        raise ValueError("source_archive_audit_failed")

    payloads = _source_payloads(source_root)
    public_by_id = {row["case_id"]: row for row in payloads["public"]["cases"]}
    labels = _label_index(source_root)
    repair_labels = {
        case_id: label
        for case_id, label in labels.items()
        if label.get("track") == "package_maintenance"
    }
    source_bases = {
        row["base_id"]: row for row in payloads["private"]["repair_bases"]
    }
    case_id_map = {case_id: _opaque_case_id(case_id) for case_id in repair_labels}
    base_id_map = {
        base_id: _opaque_base_id(base_id)
        for base_id in {label["base_id"] for label in repair_labels.values()}
    }

    if output_root.exists() and not overwrite:
        raise FileExistsError(f"release_exists:{output_root}")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(
        tempfile.mkdtemp(prefix=f".{output_root.name}.building-", dir=output_root.parent)
    )
    try:
        public_root = temporary_root / "public"
        private_root = temporary_root / "_private"
        audit_root = temporary_root / "_audit"
        (public_root / "cases").mkdir(parents=True)
        (private_root / "cases").mkdir(parents=True)
        (private_root / "repair_bases").mkdir(parents=True)
        audit_root.mkdir(parents=True)

        public_rows: list[dict[str, Any]] = []
        private_rows: list[dict[str, Any]] = []
        audit_rows: list[dict[str, Any]] = []
        private_base_rows: list[dict[str, Any]] = []

        for source_base_id, base_record in sorted(source_bases.items()):
            if source_base_id not in base_id_map:
                continue
            base_id = base_id_map[source_base_id]
            source_oracle = (
                source_root / "_private" / "repair_bases" / source_base_id / "oracle_package"
            )
            destination_oracle = private_root / "repair_bases" / base_id / "oracle_package"
            copy_tree_clean(source_oracle, destination_oracle)
            private_base_rows.append(
                {
                    "base_id": base_id,
                    "source_base_id": source_base_id,
                    "source": {
                        "source_id": base_record["source_id"],
                        "repository": base_record["source_repo_url"],
                        "commit": base_record["source_commit"],
                        "license": base_record["license"],
                        "license_file_sha256": base_record["license_file_sha256"],
                    },
                    "target_path": base_record["target_path"],
                    "oracle_package_hashes": hash_tree(destination_oracle),
                    "source_parent_case_id": base_record["parent_case_id"],
                    "source_parent_transformation_id": base_record[
                        "parent_transformation_id"
                    ],
                    "parent_evidence_tier": base_record["parent_evidence_tier"],
                    "repair_direction": base_record["repair_direction"],
                }
            )

        for source_case_id, label in sorted(repair_labels.items()):
            case_id = case_id_map[source_case_id]
            base_id = base_id_map[label["base_id"]]
            source_case = source_root / "public" / "cases" / source_case_id
            destination_case = public_root / "cases" / case_id
            copy_tree_clean(source_case, destination_case)
            task_path = destination_case / "TASK.json"
            task = read_json(task_path)
            task["task_id"] = case_id
            write_json(task_path, task)

            package = destination_case / "package"
            package_hashes = hash_tree(package)
            source = read_json(destination_case / "SOURCE.json")
            public_row = {
                "case_id": case_id,
                "track": "paired_artifact_state_repair",
                "public_case_path": f"cases/{case_id}",
                "package_path": f"cases/{case_id}/package",
                "request_path": f"cases/{case_id}/REQUEST.md",
                "task_path": f"cases/{case_id}/TASK.json",
                "source": source,
                "language": _language_for_target(str(label["target_path"])),
                "package_file_count": len(package_hashes),
                "package_tree_hash": canonical_json_hash(package_hashes),
                "request_sha256": sha256_file(destination_case / "REQUEST.md"),
                "task_sha256": sha256_file(task_path),
                "source_sha256": sha256_file(destination_case / "SOURCE.json"),
                "license_sha256": sha256_file(destination_case / "SOURCE_LICENSE.txt"),
            }
            case_record = {"schema_version": SCHEMA_VERSION, **public_row}
            case_record["case_record_hash"] = canonical_json_hash(case_record)
            write_json(destination_case / "CASE.json", case_record)
            public_rows.append(public_row)

            private_label = {
                "schema_version": SCHEMA_VERSION,
                "case_id": case_id,
                "base_id": base_id,
                "state": label["state"],
                "track": "paired_artifact_state_repair",
                "target_path": label["target_path"],
                "canonical_package_path": f"../../repair_bases/{base_id}/oracle_package",
                "expected_visible_changed_files": label["expected_visible_changed_files"],
                "document_contract": label["document_contract"],
                "operator": label["operator"],
                "source": label["source"],
                "visible_package_hashes": package_hashes,
                "canonical_package_hashes": label["canonical_package_hashes"],
                "request_sha256": sha256_file(destination_case / "REQUEST.md"),
                "hidden_artifacts_exposed": False,
                "source_case_id": source_case_id,
                "source_label_sha256": sha256_file(
                    source_root / "_private" / "cases" / source_case_id / "label.json"
                ),
            }
            private_label["label_hash"] = canonical_json_hash(private_label)
            write_json(private_root / "cases" / case_id / "label.json", private_label)
            private_rows.append(
                {
                    "case_id": case_id,
                    "base_id": base_id,
                    "state": label["state"],
                    "label_hash": private_label["label_hash"],
                }
            )

            audit_row = {
                "schema_version": SCHEMA_VERSION,
                "case_id": case_id,
                "base_id": base_id,
                "state": label["state"],
                "source_case_id": source_case_id,
                "source_base_id": label["base_id"],
                "source_label_sha256": private_label["source_label_sha256"],
                "source_public_manifest_row_hash": canonical_json_hash(
                    public_by_id[source_case_id]
                ),
                "target_path": label["target_path"],
                "expected_visible_changed_files": label[
                    "expected_visible_changed_files"
                ],
                "parent_case_id": label["parent_case_id"],
                "parent_transformation_id": label["parent_transformation_id"],
                "parent_evidence_tier": (label.get("parent_evidence") or {}).get(
                    "evidence_tier"
                ),
            }
            audit_row["audit_record_hash"] = canonical_json_hash(audit_row)
            audit_rows.append(audit_row)

        _jsonl_write(public_root / "registry.jsonl", public_rows)
        _jsonl_write(audit_root / "construction_registry.jsonl", audit_rows)
        _write_public_readme(public_root)
        evaluator_map = _evaluator_map()
        write_json(audit_root / "EVALUATOR_MAP.json", evaluator_map)
        write_json(audit_root / "SOURCE_ARCHIVE_AUDIT.json", source_audit)

        public_manifest = {
            "schema_version": SCHEMA_VERSION,
            "release_version": RELEASE_VERSION,
            "status": "static_pairing_freeze_complete_behavioral_upgrade_pending",
            "case_count": len(public_rows),
            "source_base_count": len(base_id_map),
            "track": "paired_artifact_state_repair",
            "language_distribution": dict(
                sorted(Counter(row["language"] for row in public_rows).items())
            ),
            "public_states_disclosed": False,
            "method_specific_facts_included": False,
            "evaluation_materials_included": False,
            "registry_sha256": sha256_file(public_root / "registry.jsonl"),
            "cases_tree_hash": _tree_hash(public_root / "cases"),
            "claim_boundary": (
                "This public release provides matched executable skill-package inputs. "
                "State labels, canonical packages, and evaluator materials are private."
            ),
        }
        public_manifest["release_manifest_hash"] = canonical_json_hash(public_manifest)
        write_json(public_root / "RELEASE_MANIFEST.json", public_manifest)

        private_manifest = {
            "schema_version": SCHEMA_VERSION,
            "release_version": RELEASE_VERSION,
            "status": "frozen_private_pairing_and_canonical_packages",
            "case_count": len(private_rows),
            "base_count": len(private_base_rows),
            "state_counts": dict(
                sorted(Counter(row["state"] for row in private_rows).items())
            ),
            "cases": sorted(private_rows, key=lambda row: row["case_id"]),
            "repair_bases": sorted(private_base_rows, key=lambda row: row["base_id"]),
        }
        private_manifest["manifest_hash"] = canonical_json_hash(private_manifest)
        write_json(private_root / "manifest.json", private_manifest)

        build_receipt = {
            "schema_version": SCHEMA_VERSION,
            "release_version": RELEASE_VERSION,
            "status": "complete",
            "model_calls": 0,
            "behavioral_evaluator_loaded": False,
            "credential_persisted": False,
            "builder_sha256": sha256_file(Path(__file__)),
            "source_root": source_root.relative_to(workspace_root).as_posix(),
            "source_freeze_hash": payloads["freeze"]["freeze_hash"],
            "source_freeze_manifest_sha256": sha256_file(
                source_root / "FREEZE_MANIFEST.json"
            ),
            "source_audit_hash": source_audit["source_audit_hash"],
            "excluded_capability_case_count": sum(
                label.get("state") == "capability_narrow" for label in labels.values()
            ),
            "public_tree_hash": _tree_hash(public_root),
            "private_tree_hash": _tree_hash(private_root),
            "audit_registry_sha256": sha256_file(
                audit_root / "construction_registry.jsonl"
            ),
            "evaluator_map_sha256": sha256_file(audit_root / "EVALUATOR_MAP.json"),
            "source_archive_audit_sha256": sha256_file(
                audit_root / "SOURCE_ARCHIVE_AUDIT.json"
            ),
        }
        build_receipt["build_receipt_hash"] = canonical_json_hash(build_receipt)
        write_json(audit_root / "BUILD_RECEIPT.json", build_receipt)

        preflight = audit_release(temporary_root)
        write_json(audit_root / "ZERO_MODEL_PREFLIGHT.json", preflight)
        if preflight["status"] != "pass":
            raise ValueError("artifact_state_release_preflight_failed")

        if output_root.exists():
            if not overwrite:
                raise FileExistsError(f"release_exists:{output_root}")
            shutil.rmtree(output_root)
        os.replace(temporary_root, output_root)
        return preflight
    except Exception:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise


def _release_pairing_checks(
    release_root: Path,
    private_rows: list[dict[str, Any]],
) -> dict[str, bool]:
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in private_rows:
        grouped[row["base_id"]][row["state"]] = row
    exact_states = len(grouped) == 20 and all(
        set(states) == set(REPAIR_STATES) for states in grouped.values()
    )
    labels_valid = True
    diffs_valid = True
    composition_valid = True
    for base_id, states in grouped.items():
        if set(states) != set(REPAIR_STATES):
            continue
        labels = {
            state: read_json(
                release_root / "_private" / "cases" / row["case_id"] / "label.json"
            )
            for state, row in states.items()
        }
        labels_valid = labels_valid and all(
            _canonical_embedded_hash_valid(label, "label_hash")
            and label["state"] == state
            and label["base_id"] == base_id
            for state, label in labels.items()
        )
        oracle = release_root / "_private" / "repair_bases" / base_id / "oracle_package"
        packages = {
            state: release_root / "public" / "cases" / row["case_id"] / "package"
            for state, row in states.items()
        }
        target = labels["clean"]["target_path"]
        expected = {
            "clean": [],
            "doc_fault": ["SKILL.md"],
            "script_fault": [target],
            "joint_fault": sorted({"SKILL.md", target}),
        }
        diffs_valid = diffs_valid and all(
            _tree_diff(oracle, packages[state]) == expected[state]
            for state in REPAIR_STATES
        )
        hashes = {state: hash_tree(package) for state, package in packages.items()}
        paths = set().union(*(mapping.keys() for mapping in hashes.values()))
        for path in paths:
            values = {state: hashes[state].get(path) for state in REPAIR_STATES}
            if path == "SKILL.md":
                valid = (
                    values["clean"] == values["script_fault"]
                    and values["doc_fault"] == values["joint_fault"]
                    and values["clean"] != values["doc_fault"]
                )
            elif path == target:
                valid = (
                    values["clean"] == values["doc_fault"]
                    and values["script_fault"] == values["joint_fault"]
                    and values["clean"] != values["script_fault"]
                )
            else:
                valid = len(set(values.values())) == 1
            composition_valid = composition_valid and valid
    return {
        "release_base_count_20_and_four_states_each": exact_states,
        "release_private_label_hashes_valid": labels_valid,
        "release_state_diffs_exact": diffs_valid,
        "release_state_composition_exact": composition_valid,
    }


def audit_release(release_root: Path) -> dict[str, Any]:
    release_root = release_root.resolve()
    public_root = release_root / "public"
    private_root = release_root / "_private"
    audit_root = release_root / "_audit"
    public_rows = _jsonl_read(public_root / "registry.jsonl")
    audit_rows = _jsonl_read(audit_root / "construction_registry.jsonl")
    private_manifest = read_json(private_root / "manifest.json")
    private_rows = private_manifest["cases"]
    public_manifest = read_json(public_root / "RELEASE_MANIFEST.json")
    build_receipt = read_json(audit_root / "BUILD_RECEIPT.json")

    public_ids = {row["case_id"] for row in public_rows}
    audit_ids = {row["case_id"] for row in audit_rows}
    private_ids = {row["case_id"] for row in private_rows}
    private_markers = {
        str(value)
        for row in audit_rows
        for key in ("source_case_id", "source_base_id", "parent_case_id", "parent_transformation_id")
        for value in (row.get(key),)
        if isinstance(value, str) and value
    }
    private_markers.update(row["base_id"] for row in audit_rows)
    public_violations = _scan_public_release(public_root, private_markers)

    distribution = Counter(row["language"] for row in public_rows)
    case_checks = {
        "release_case_count_80": len(public_rows) == 80,
        "release_case_ids_unique": len(public_ids) == 80,
        "public_private_audit_id_bijection": public_ids == audit_ids == private_ids,
        "all_public_case_directories_present": all(
            (public_root / row["public_case_path"]).is_dir() for row in public_rows
        ),
        "all_public_skill_markdown_present": all(
            (public_root / row["package_path"] / "SKILL.md").is_file()
            for row in public_rows
        ),
        "all_public_package_hashes_match": all(
            _tree_hash(public_root / row["package_path"]) == row["package_tree_hash"]
            for row in public_rows
        ),
        "all_public_request_hashes_match": all(
            sha256_file(public_root / row["request_path"]) == row["request_sha256"]
            for row in public_rows
        ),
        "all_public_task_hashes_match": all(
            sha256_file(public_root / row["task_path"]) == row["task_sha256"]
            for row in public_rows
        ),
        "all_case_record_hashes_valid": all(
            _canonical_embedded_hash_valid(
                read_json(public_root / row["public_case_path"] / "CASE.json"),
                "case_record_hash",
            )
            for row in public_rows
        ),
        "all_audit_record_hashes_valid": all(
            _canonical_embedded_hash_valid(row, "audit_record_hash") for row in audit_rows
        ),
        "language_distribution_nonempty": bool(distribution),
    }
    integrity_checks = {
        "release_manifest_hash_valid": _canonical_embedded_hash_valid(
            public_manifest, "release_manifest_hash"
        ),
        "private_manifest_hash_valid": _canonical_embedded_hash_valid(
            private_manifest, "manifest_hash"
        ),
        "build_receipt_hash_valid": _canonical_embedded_hash_valid(
            build_receipt, "build_receipt_hash"
        ),
        "release_registry_sha256_match": sha256_file(public_root / "registry.jsonl")
        == public_manifest.get("registry_sha256"),
        "release_cases_tree_hash_match": _tree_hash(public_root / "cases")
        == public_manifest.get("cases_tree_hash"),
        "release_public_tree_hash_match": _tree_hash(public_root)
        == build_receipt.get("public_tree_hash"),
        "release_private_tree_hash_match": _tree_hash(private_root)
        == build_receipt.get("private_tree_hash"),
        "audit_registry_sha256_match": sha256_file(
            audit_root / "construction_registry.jsonl"
        )
        == build_receipt.get("audit_registry_sha256"),
        "evaluator_map_sha256_match": sha256_file(audit_root / "EVALUATOR_MAP.json")
        == build_receipt.get("evaluator_map_sha256"),
        "source_archive_audit_sha256_match": sha256_file(
            audit_root / "SOURCE_ARCHIVE_AUDIT.json"
        )
        == build_receipt.get("source_archive_audit_sha256"),
    }
    isolation_checks = {
        "public_leak_scan_clean": not public_violations,
        "public_contains_no_symlinks": not any(path.is_symlink() for path in public_root.rglob("*")),
        "public_states_not_disclosed": public_manifest.get("public_states_disclosed") is False,
        "method_specific_facts_not_included": public_manifest.get(
            "method_specific_facts_included"
        )
        is False,
        "model_calls_zero": build_receipt.get("model_calls") == 0,
        "behavioral_evaluator_not_loaded": build_receipt.get(
            "behavioral_evaluator_loaded"
        )
        is False,
        "credential_not_persisted": build_receipt.get("credential_persisted") is False,
    }
    pairing_checks = _release_pairing_checks(release_root, private_rows)
    checks = {**case_checks, **integrity_checks, **isolation_checks, **pairing_checks}
    report = {
        "schema_version": SCHEMA_VERSION,
        "release_version": RELEASE_VERSION,
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "check_count": len(checks),
        "passed_check_count": sum(bool(value) for value in checks.values()),
        "case_count": len(public_rows),
        "base_count": private_manifest.get("base_count"),
        "state_counts": private_manifest.get("state_counts"),
        "language_distribution": dict(sorted(distribution.items())),
        "public_scan_violations": public_violations,
        "model_calls": 0,
        "behavioral_evaluator_loaded": False,
        "credential_persisted": False,
        "behavior_replay_status": "historical_selftests_preserved_behavioral_upgrade_pending",
        "artifact_bindings": {
            "release_manifest_hash": public_manifest.get("release_manifest_hash"),
            "private_manifest_hash": private_manifest.get("manifest_hash"),
            "build_receipt_hash": build_receipt.get("build_receipt_hash"),
            "public_tree_hash": build_receipt.get("public_tree_hash"),
            "private_tree_hash": build_receipt.get("private_tree_hash"),
        },
    }
    report["preflight_hash"] = canonical_json_hash(report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("source-audit", "build", "preflight"), help="operation"
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    workspace_root = args.workspace_root.resolve()
    source_root = (args.source_root or (workspace_root / SOURCE_REL)).resolve()
    output = (args.output or (workspace_root / DEFAULT_OUTPUT_REL)).resolve()
    if args.command == "source-audit":
        payload = audit_source_archive(workspace_root, source_root)
    elif args.command == "build":
        payload = build_release(
            workspace_root, source_root, output, overwrite=args.overwrite
        )
    else:
        payload = audit_release(output)
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True))
    if payload["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
