from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from bvi_skill_evo import public_runtime_multilang_dual_v410 as ast_backend
from bvi_skill_evo.public_runtime_multilang_dual_v410 import (
    build_public_multilang_contract_set,
    validate_public_multilang_contract_set,
)
from release_tools.artifact_state_release import (
    RELEASE_VERSION as BENCHMARK_RELEASE_VERSION,
    _canonical_embedded_hash_valid,
    _jsonl_read,
)
from skillscriptbench.io_utils import (
    canonical_json_hash,
    hash_tree,
    read_json,
    sha256_file,
    write_json,
)


SCHEMA_VERSION = "skillscriptbench-paired-smoke-protocol-v1"
PROTOCOL_VERSION = "paired_artifact_states_smoke_v1"
DEFAULT_BENCHMARK_REL = Path("benchmark_release") / BENCHMARK_RELEASE_VERSION
DEFAULT_OUTPUT_REL = Path("final_results/skillscriptbench") / PROTOCOL_VERSION

# One shell default, one JavaScript path literal, and one TypeScript ordering contract.
SELECTED_SOURCE_BASE_IDS = (
    "base-869fda1a8bfa2c3bd3b1",
    "base-7edbac184d95a1f0310a",
    "base-de2269fd547dd1bc855d",
)
CONDITIONS = (
    "no-evolution",
    "md-only-self-evolution",
    "raw-package-self-evolution",
    "ast-package-self-evolution",
)
MODEL_CONDITIONS = CONDITIONS[1:]
MAX_TEXT_FILE_BYTES = 250_000
MAX_PACKAGE_PROMPT_BYTES = 1_500_000

RAW_REVISION_TEMPLATE = """You are revising a proposed executable-skill package repair.

You may use only the public parent package, request, the frozen proposal, and generic public
checks. Hidden tests, state labels, mutation operators, oracle files, and benchmark outcomes are
unavailable. Repair only what is needed, preserve unrelated behavior, and return a bounded patch
or ABSTAIN.

<FROZEN_PROPOSAL>
{proposal}
</FROZEN_PROPOSAL>

<GENERIC_PUBLIC_CHECKS>
{generic_checks}
</GENERIC_PUBLIC_CHECKS>
"""

AST_REVISION_TEMPLATE = """You are revising a proposed executable-skill package repair.

You may use only the public parent package, request, the frozen proposal, generic public checks,
and the runtime-generated public structural packet below. The packet may identify relevant nodes
and constrain code edits, but it does not provide the correct replacement semantics. Hidden tests,
state labels, mutation operators, oracle files, and benchmark outcomes are unavailable. Repair
only what is needed, preserve unrelated behavior, and return a bounded patch or ABSTAIN.

<FROZEN_PROPOSAL>
{proposal}
</FROZEN_PROPOSAL>

<GENERIC_PUBLIC_CHECKS>
{generic_checks}
</GENERIC_PUBLIC_CHECKS>

<PUBLIC_STRUCTURAL_PACKET>
{ast_facts}
</PUBLIC_STRUCTURAL_PACKET>
"""

MD_REVISION_TEMPLATE = """You are revising a Markdown-only skill repair.

Scripts remain unavailable and immutable. Use only the visible Markdown, request, frozen proposal,
and generic Markdown checks. Hidden tests, state labels, mutation operators, oracle files, and
benchmark outcomes are unavailable. Return a bounded Markdown patch or ABSTAIN.

<FROZEN_PROPOSAL>
{proposal}
</FROZEN_PROPOSAL>

<GENERIC_PUBLIC_CHECKS>
{generic_checks}
</GENERIC_PUBLIC_CHECKS>
"""


def _text_file(path: Path) -> bool:
    if path.stat().st_size > MAX_TEXT_FILE_BYTES:
        return False
    try:
        path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _render_file_bundle(root: Path, paths: list[Path]) -> str:
    chunks: list[str] = []
    total = 0
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if not _text_file(path):
            chunks.append(
                f"\n### {relative}\n[BINARY_OR_LARGE_FILE sha256={sha256_file(path)}]\n"
            )
            continue
        content = path.read_text(encoding="utf-8")
        encoded = content.encode("utf-8")
        if total + len(encoded) > MAX_PACKAGE_PROMPT_BYTES:
            chunks.append(
                f"\n### {relative}\n[OMITTED_BY_PUBLIC_SIZE_BOUND sha256={sha256_file(path)}]\n"
            )
            continue
        total += len(encoded)
        chunks.append(f"\n### {relative}\n```text\n{content}\n```\n")
    return "".join(chunks)


def _primary_prompt(case_root: Path, *, markdown_only: bool) -> str:
    package = case_root / "package"
    request = (case_root / "REQUEST.md").read_text(encoding="utf-8")
    task = (case_root / "TASK.json").read_text(encoding="utf-8")
    if markdown_only:
        paths = sorted(path for path in package.rglob("*") if path.is_file() and path.suffix.lower() == ".md")
        scope = (
            "Only Markdown files are visible and editable. Scripts are unavailable and immutable."
        )
    else:
        paths = sorted(path for path in package.rglob("*") if path.is_file())
        scope = (
            "The complete public package is visible. Markdown and executable package files are editable."
        )
    bundle = _render_file_bundle(package, paths)
    return f"""Audit and repair the supplied agent skill package.

{scope}

Do not infer access to hidden tests, state labels, mutation operators, oracle files, or verifier
outcomes. Inspect the visible artifacts, preserve unrelated behavior and public interfaces, and
return a bounded patch or ABSTAIN when no change is required.

<TASK>
{task}
</TASK>

<REQUEST>
{request}
</REQUEST>

<VISIBLE_PACKAGE>
{bundle}
</VISIBLE_PACKAGE>
"""


def _visible_preflight(case_root: Path, facts: dict[str, Any]) -> dict[str, Any]:
    package = case_root / "package"
    scripts = [path for path in package.rglob("*") if path.is_file() and "scripts" in path.parts]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": "STATIC_PREFLIGHT_PASS",
        "skill_markdown_present": (package / "SKILL.md").is_file(),
        "script_file_count": len(scripts),
        "package_file_count": len(hash_tree(package)),
        "runtime_structural_extraction_completed": True,
        "task_correctness_assessed": False,
        "behavioral_verifier_executed": False,
        "hidden_artifacts_consumed": False,
        "observed_summary": (
            "The public package is readable and its executable artifacts admit structural "
            "extraction. This preflight does not determine whether the requested behavior is correct."
        ),
        "structural_packet_hash": facts["facts_hash"],
    }
    payload["preflight_record_hash"] = canonical_json_hash(payload)
    return payload


def _selected_matrix(benchmark_root: Path) -> list[dict[str, Any]]:
    audit_rows = _jsonl_read(benchmark_root / "_audit" / "construction_registry.jsonl")
    selected = [
        row for row in audit_rows if row["source_base_id"] in SELECTED_SOURCE_BASE_IDS
    ]
    selected.sort(key=lambda row: (row["source_base_id"], row["state"]))
    return selected


def prepare_protocol(
    workspace_root: Path,
    benchmark_root: Path,
    output_root: Path,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    workspace_root = workspace_root.resolve()
    benchmark_root = benchmark_root.resolve()
    output_root = output_root.resolve()
    benchmark_preflight = read_json(
        benchmark_root / "_audit" / "ZERO_MODEL_PREFLIGHT.json"
    )
    if benchmark_preflight.get("status") != "pass" or not _canonical_embedded_hash_valid(
        benchmark_preflight, "preflight_hash"
    ):
        raise ValueError("paired_benchmark_preflight_invalid")
    matrix = _selected_matrix(benchmark_root)
    if len(matrix) != 12:
        raise ValueError(f"smoke_case_count_not_12:{len(matrix)}")

    if output_root.exists() and not overwrite:
        raise FileExistsError(f"protocol_exists:{output_root}")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(
        tempfile.mkdtemp(prefix=f".{output_root.name}.building-", dir=output_root.parent)
    )
    try:
        visible_root = temporary_root / "visible"
        audit_root = temporary_root / "_audit"
        (visible_root / "feedback").mkdir(parents=True)
        (visible_root / "ast_facts").mkdir(parents=True)
        (visible_root / "prompts" / "md-only-primary").mkdir(parents=True)
        (visible_root / "prompts" / "package-shared-primary").mkdir(parents=True)
        audit_root.mkdir(parents=True)

        visible_rows: list[dict[str, Any]] = []
        private_rows: list[dict[str, Any]] = []
        for row in matrix:
            case_id = row["case_id"]
            case_root = benchmark_root / "public" / "cases" / case_id
            request_text = (case_root / "REQUEST.md").read_text(encoding="utf-8")
            facts = build_public_multilang_contract_set(
                case_root / "package", request_text
            )
            validate_public_multilang_contract_set(facts, case_root / "package")
            feedback = _visible_preflight(case_root, facts)
            md_prompt = _primary_prompt(case_root, markdown_only=True)
            package_prompt = _primary_prompt(case_root, markdown_only=False)
            facts_path = visible_root / "ast_facts" / f"{case_id}.json"
            feedback_path = visible_root / "feedback" / f"{case_id}.json"
            md_prompt_path = visible_root / "prompts" / "md-only-primary" / f"{case_id}.txt"
            package_prompt_path = (
                visible_root / "prompts" / "package-shared-primary" / f"{case_id}.txt"
            )
            write_json(facts_path, facts)
            write_json(feedback_path, feedback)
            md_prompt_path.write_text(md_prompt, encoding="utf-8")
            package_prompt_path.write_text(package_prompt, encoding="utf-8")
            visible_rows.append(
                {
                    "case_id": case_id,
                    "feedback_path": feedback_path.relative_to(temporary_root).as_posix(),
                    "feedback_sha256": sha256_file(feedback_path),
                    "ast_facts_path": facts_path.relative_to(temporary_root).as_posix(),
                    "ast_facts_sha256": sha256_file(facts_path),
                    "ast_facts_hash": facts["facts_hash"],
                    "ast_localization_decision": facts["localization_decision"]["decision"],
                    "ast_selected_family": facts["localization_decision"].get(
                        "selected_family"
                    ),
                    "md_primary_prompt_path": md_prompt_path.relative_to(
                        temporary_root
                    ).as_posix(),
                    "md_primary_prompt_sha256": sha256_file(md_prompt_path),
                    "package_shared_primary_prompt_path": package_prompt_path.relative_to(
                        temporary_root
                    ).as_posix(),
                    "package_shared_primary_prompt_sha256": sha256_file(
                        package_prompt_path
                    ),
                }
            )
            private_rows.append(
                {
                    "case_id": case_id,
                    "base_id": row["base_id"],
                    "state": row["state"],
                    "source_base_id": row["source_base_id"],
                    "target_path": row["target_path"],
                    "language": next(
                        item["language"]
                        for item in _jsonl_read(benchmark_root / "public" / "registry.jsonl")
                        if item["case_id"] == case_id
                    ),
                }
            )

        write_json(
            visible_root / "VISIBLE_INPUT_MANIFEST.json",
            {
                "schema_version": SCHEMA_VERSION,
                "case_count": len(visible_rows),
                "rows": visible_rows,
            },
        )
        write_json(
            audit_root / "CASE_MATRIX.json",
            {
                "schema_version": SCHEMA_VERSION,
                "case_count": len(private_rows),
                "rows": private_rows,
            },
        )
        templates = {
            "schema_version": SCHEMA_VERSION,
            "md_revision_template": MD_REVISION_TEMPLATE,
            "raw_revision_template": RAW_REVISION_TEMPLATE,
            "ast_revision_template": AST_REVISION_TEMPLATE,
        }
        templates["templates_hash"] = canonical_json_hash(templates)
        write_json(audit_root / "REVISION_TEMPLATES.json", templates)

        protocol = {
            "schema_version": SCHEMA_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "status": "frozen_zero_model_smoke_protocol",
            "scientific_role": "pipeline_smoke_not_paper_main_result",
            "benchmark_release": BENCHMARK_RELEASE_VERSION,
            "benchmark_preflight_hash": benchmark_preflight["preflight_hash"],
            "case_count": 12,
            "base_count": 3,
            "conditions": list(CONDITIONS),
            "model_conditions": list(MODEL_CONDITIONS),
            "model": "gpt-5.5",
            "temperature": 0,
            "worker_count": 6,
            "logical_call_budget_per_model_condition": 2,
            "proposal_policy": {
                "md_only": "independent_markdown_proposal",
                "raw_and_ast": "one_shared_frozen_raw_package_proposal",
            },
            "revision_policy": {
                "md_only": "markdown_generic_public_checks",
                "raw_package": "generic_public_checks",
                "ast_package": "same_generic_checks_plus_public_runtime_structural_packet",
            },
            "provider_call_count": {
                "md_primary": 12,
                "md_revision": 12,
                "shared_package_primary": 12,
                "raw_revision": 12,
                "ast_revision": 12,
                "total": 60,
            },
            "public_feedback": (
                "static package readability and structural extraction only; no task correctness"
            ),
            "candidate_selection_uses_hidden": False,
            "hidden_outcomes_feed_back": False,
            "model_calls": 0,
            "behavioral_evaluator_loaded": False,
            "ready_for_model_calls": False,
            "code_bindings": {
                "protocol_builder_sha256": sha256_file(Path(__file__)),
                "public_ast_backend_sha256": sha256_file(Path(ast_backend.__file__)),
            },
            "blocking_gate": (
                "Smoke behavior evaluator and generic candidate materializer must be upgraded "
                "and frozen before provider calls."
            ),
            "claim_boundary": (
                "The smoke may validate pipeline mechanics only. Historical repair labels use "
                "source-semantic metamorphic evidence rather than source-native candidate tests."
            ),
            "visible_input_manifest_sha256": sha256_file(
                visible_root / "VISIBLE_INPUT_MANIFEST.json"
            ),
            "case_matrix_sha256": sha256_file(audit_root / "CASE_MATRIX.json"),
            "revision_templates_sha256": sha256_file(
                audit_root / "REVISION_TEMPLATES.json"
            ),
        }
        protocol["protocol_hash"] = canonical_json_hash(protocol)
        write_json(audit_root / "FROZEN_PROTOCOL.json", protocol)

        preflight = audit_protocol(temporary_root, benchmark_root)
        write_json(audit_root / "ZERO_MODEL_PREFLIGHT.json", preflight)
        if preflight["status"] != "pass":
            raise ValueError("paired_smoke_zero_model_preflight_failed")

        if output_root.exists():
            if not overwrite:
                raise FileExistsError(f"protocol_exists:{output_root}")
            shutil.rmtree(output_root)
        os.replace(temporary_root, output_root)
        return preflight
    except Exception:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise


def audit_protocol(protocol_root: Path, benchmark_root: Path) -> dict[str, Any]:
    protocol_root = protocol_root.resolve()
    benchmark_root = benchmark_root.resolve()
    audit_root = protocol_root / "_audit"
    visible_root = protocol_root / "visible"
    protocol = read_json(audit_root / "FROZEN_PROTOCOL.json")
    templates = read_json(audit_root / "REVISION_TEMPLATES.json")
    matrix = read_json(audit_root / "CASE_MATRIX.json")["rows"]
    visible = read_json(visible_root / "VISIBLE_INPUT_MANIFEST.json")["rows"]
    benchmark_preflight = read_json(
        benchmark_root / "_audit" / "ZERO_MODEL_PREFLIGHT.json"
    )
    benchmark_registry = {
        row["case_id"]: row
        for row in _jsonl_read(benchmark_root / "public" / "registry.jsonl")
    }

    facts_valid = True
    visible_hashes_valid = True
    private_markers_absent = True
    decisions = Counter()
    for row in visible:
        case_id = row["case_id"]
        case_root = benchmark_root / "public" / "cases" / case_id
        facts_path = protocol_root / row["ast_facts_path"]
        feedback_path = protocol_root / row["feedback_path"]
        md_prompt_path = protocol_root / row["md_primary_prompt_path"]
        package_prompt_path = protocol_root / row["package_shared_primary_prompt_path"]
        facts = read_json(facts_path)
        try:
            validate_public_multilang_contract_set(facts, case_root / "package")
        except ValueError:
            facts_valid = False
        decisions[facts["localization_decision"]["decision"]] += 1
        visible_hashes_valid = visible_hashes_valid and all(
            (
                sha256_file(facts_path) == row["ast_facts_sha256"],
                sha256_file(feedback_path) == row["feedback_sha256"],
                sha256_file(md_prompt_path) == row["md_primary_prompt_sha256"],
                sha256_file(package_prompt_path)
                == row["package_shared_primary_prompt_sha256"],
            )
        )
        visible_text = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (facts_path, feedback_path, md_prompt_path, package_prompt_path)
        )
        private_row = next(item for item in matrix if item["case_id"] == case_id)
        private_markers_absent = private_markers_absent and all(
            marker not in visible_text
            for marker in (
                private_row["base_id"],
                private_row["source_base_id"],
                private_row["state"],
            )
            if len(marker) >= 8
        )

    grouped: dict[str, set[str]] = defaultdict(set)
    for row in matrix:
        grouped[row["base_id"]].add(row["state"])
    checks = {
        "benchmark_preflight_valid": benchmark_preflight.get("status") == "pass"
        and _canonical_embedded_hash_valid(benchmark_preflight, "preflight_hash"),
        "protocol_hash_valid": _canonical_embedded_hash_valid(protocol, "protocol_hash"),
        "protocol_builder_sha256_match": protocol["code_bindings"][
            "protocol_builder_sha256"
        ]
        == sha256_file(Path(__file__)),
        "public_ast_backend_sha256_match": protocol["code_bindings"][
            "public_ast_backend_sha256"
        ]
        == sha256_file(Path(ast_backend.__file__)),
        "revision_templates_hash_valid": _canonical_embedded_hash_valid(
            templates, "templates_hash"
        ),
        "case_count_12": len(matrix) == len(visible) == 12,
        "case_ids_unique": len({row["case_id"] for row in matrix}) == 12,
        "three_bases_four_states_each": len(grouped) == 3
        and all(states == {"clean", "doc_fault", "script_fault", "joint_fault"} for states in grouped.values()),
        "selected_source_bases_exact": {row["source_base_id"] for row in matrix}
        == set(SELECTED_SOURCE_BASE_IDS),
        "languages_cover_js_ts_shell": {row["language"] for row in matrix}
        == {"javascript", "typescript", "shell"},
        "all_cases_exist_in_benchmark": all(
            row["case_id"] in benchmark_registry for row in matrix
        ),
        "visible_artifact_hashes_valid": visible_hashes_valid,
        "public_ast_facts_validate_against_packages": facts_valid,
        "ast_decision_mix_expected": decisions == Counter({"PROPOSE": 8, "ABSTAIN": 4}),
        "private_markers_absent_from_visible_inputs": private_markers_absent,
        "raw_ast_primary_prompt_shared_by_construction": all(
            "package-shared-primary" in row["package_shared_primary_prompt_path"]
            for row in visible
        ),
        "protocol_provider_calls_60": protocol["provider_call_count"]["total"] == 60,
        "protocol_model_exact_gpt55": protocol.get("model") == "gpt-5.5",
        "protocol_worker_count_6": protocol.get("worker_count") == 6,
        "model_calls_zero": protocol.get("model_calls") == 0,
        "behavioral_evaluator_not_loaded": protocol.get("behavioral_evaluator_loaded")
        is False,
        "provider_calls_fail_closed": protocol.get("ready_for_model_calls") is False,
        "credential_not_persisted": not any(
            part in {".env", "credentials", "secrets"}
            for path in protocol_root.rglob("*")
            for part in path.parts
        ),
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "check_count": len(checks),
        "passed_check_count": sum(bool(value) for value in checks.values()),
        "case_count": len(matrix),
        "base_count": len(grouped),
        "state_counts": dict(sorted(Counter(row["state"] for row in matrix).items())),
        "language_counts": dict(
            sorted(Counter(row["language"] for row in matrix).items())
        ),
        "ast_localization_decisions": dict(sorted(decisions.items())),
        "model_calls": 0,
        "behavioral_evaluator_loaded": False,
        "ready_for_model_calls": False,
        "protocol_hash": protocol.get("protocol_hash"),
        "benchmark_preflight_hash": benchmark_preflight.get("preflight_hash"),
    }
    report["preflight_hash"] = canonical_json_hash(report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "preflight"))
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--benchmark-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    workspace_root = args.workspace_root.resolve()
    benchmark_root = (
        args.benchmark_root or (workspace_root / DEFAULT_BENCHMARK_REL)
    ).resolve()
    output = (args.output or (workspace_root / DEFAULT_OUTPUT_REL)).resolve()
    if args.command == "prepare":
        report = prepare_protocol(
            workspace_root, benchmark_root, output, overwrite=args.overwrite
        )
    else:
        report = audit_protocol(output, benchmark_root)
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True))
    if report["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
