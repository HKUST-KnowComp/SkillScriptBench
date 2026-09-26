from __future__ import annotations

import argparse
import difflib
import json
import math
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from bvi_skill_evo.proposal_first_closure_gate_v292 import (
    ACCEPT,
    build_proposal_first_closure_report,
)
from skillscriptbench.d35_registry105_proposal_first_experiment_v288 import (
    _discover_package,
    _embedded_hash_valid,
)
from skillscriptbench.d36_registry105_closure_experiment_v293 import (
    _select_visible_facts,
)
from skillscriptbench.io_utils import (
    canonical_json_hash,
    hash_tree,
    read_json,
    sha256_file,
    write_json,
)


SCHEMA_VERSION = "3.00-d36-dual-ast-visible-arbitration-audit-v1"
CANDIDATES = ("historical-prescriptive-ast", "ast-closure-revision")
POLICIES = (
    "closure-default",
    "strict-structural-dominance",
    "coverage-lexicographic",
    "minimal-complete",
)
POLICY_DEFINITIONS = {
    "closure-default": (
        "Always retain the proposal-first closure candidate. This is the reference, not an "
        "arbitration rule."
    ),
    "strict-structural-dominance": (
        "Select the historical prescriptive candidate only when it is structurally safe, "
        "resolves strictly more visible evidence sites, leaves no more residual sites, and "
        "changes no more paths or diff lines than closure; otherwise retain closure."
    ),
    "coverage-lexicographic": (
        "Among structurally safe candidates, maximize resolved visible evidence sites, then "
        "minimize residual sites, changed paths, changed nodes, and diff lines. Ties retain closure."
    ),
    "minimal-complete": (
        "When exactly one structurally safe candidate clears every visible evidence site, select "
        "it. When both clear all sites, select the smaller diff. Otherwise retain closure."
    ),
}


def _tree_hash(package: Path) -> str:
    return canonical_json_hash(hash_tree(package))


def _clean_files(package: Path) -> dict[str, Path]:
    rows: dict[str, Path] = {}
    for path in sorted(package.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(package)
        if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        rows[relative.as_posix()] = path
    return rows


def _diff_stats(parent: Path, candidate: Path) -> dict[str, Any]:
    before = _clean_files(parent)
    after = _clean_files(candidate)
    changed_paths: list[str] = []
    added_lines = 0
    removed_lines = 0
    binary_paths = 0
    for relative in sorted(set(before) | set(after)):
        left = before.get(relative)
        right = after.get(relative)
        left_bytes = left.read_bytes() if left else b""
        right_bytes = right.read_bytes() if right else b""
        if left_bytes == right_bytes:
            continue
        changed_paths.append(relative)
        try:
            left_lines = left_bytes.decode("utf-8").splitlines()
            right_lines = right_bytes.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            binary_paths += 1
            continue
        for line in difflib.ndiff(left_lines, right_lines):
            if line.startswith("+ "):
                added_lines += 1
            elif line.startswith("- "):
                removed_lines += 1
    return {
        "changed_path_count": len(changed_paths),
        "changed_paths": changed_paths,
        "added_line_count": added_lines,
        "removed_line_count": removed_lines,
        "diff_line_count": added_lines + removed_lines,
        "binary_changed_path_count": binary_paths,
    }


def _site_counts(report: dict[str, Any]) -> dict[str, int]:
    visible = report.get("candidate_aware_visible_structural_facts") or {}
    raw = visible.get("site_status_counts") or {}
    counts = {str(key): int(value) for key, value in raw.items()}
    resolved = sum(
        value
        for key, value in counts.items()
        if key == "changed_or_absent" or key.startswith("resolved")
    )
    residual = sum(
        value
        for key, value in counts.items()
        if key.startswith("still_present") or key.startswith("residual")
    )
    return {
        "visible_site_count": sum(counts.values()),
        "resolved_visible_site_count": resolved,
        "residual_visible_site_count": residual,
    }


def _candidate_features(
    parent: Path,
    candidate: Path,
    request_text: str,
    visible_facts: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    report = build_proposal_first_closure_report(
        parent,
        candidate,
        request_text,
        visible_structural_facts=visible_facts,
    )
    diff = _diff_stats(parent, candidate)
    sites = _site_counts(report)
    checks = report.get("checks") or {}
    features = {
        "gate_decision": report.get("decision"),
        "structurally_safe": report.get("decision") == ACCEPT,
        "candidate_diff_nonempty": bool(checks.get("candidate_diff_nonempty")),
        "syntax_valid": bool(checks.get("candidate_syntax_valid")),
        "public_contract_preserved": bool(
            checks.get("public_declaration_signatures_preserved")
        ),
        "parse_failure_count": len((report.get("parse_failures") or {}).get("candidate") or []),
        "removed_public_declaration_count": len(
            report.get("removed_public_declarations") or []
        ),
        "unresolved_name_finding_count": len(report.get("unresolved_name_findings") or []),
        "changed_node_count": max(
            len(report.get("changed_nodes_before") or []),
            len(report.get("changed_nodes_after") or []),
        ),
        "call_edge_churn": len(report.get("call_edges_added") or [])
        + len(report.get("call_edges_removed") or []),
        **sites,
        **diff,
    }
    return features, report


def _safe(feature: dict[str, Any]) -> bool:
    return bool(feature["structurally_safe"])


def _coverage_key(feature: dict[str, Any]) -> tuple[int, int, int, int, int]:
    return (
        int(feature["resolved_visible_site_count"]),
        -int(feature["residual_visible_site_count"]),
        -int(feature["changed_path_count"]),
        -int(feature["changed_node_count"]),
        -int(feature["diff_line_count"]),
    )


def choose_candidate(
    policy: str, features: dict[str, dict[str, Any]]
) -> tuple[str, str]:
    historical = features["historical-prescriptive-ast"]
    closure = features["ast-closure-revision"]
    if policy == "closure-default":
        return "ast-closure-revision", "reference_default"
    if policy == "strict-structural-dominance":
        dominates = (
            _safe(historical)
            and int(historical["resolved_visible_site_count"])
            > int(closure["resolved_visible_site_count"])
            and int(historical["residual_visible_site_count"])
            <= int(closure["residual_visible_site_count"])
            and int(historical["changed_path_count"])
            <= int(closure["changed_path_count"])
            and int(historical["diff_line_count"])
            <= int(closure["diff_line_count"])
        )
        if dominates:
            return "historical-prescriptive-ast", "strict_structural_dominance"
        return "ast-closure-revision", "no_strict_dominance"
    if policy == "coverage-lexicographic":
        eligible = [name for name in CANDIDATES if _safe(features[name])]
        if not eligible:
            return "ast-closure-revision", "no_structurally_safe_candidate"
        best_key = max(_coverage_key(features[name]) for name in eligible)
        best = [name for name in eligible if _coverage_key(features[name]) == best_key]
        selected = (
            "ast-closure-revision"
            if "ast-closure-revision" in best
            else "historical-prescriptive-ast"
        )
        return selected, "lexicographic_visible_structure"
    if policy == "minimal-complete":
        complete = [
            name
            for name in CANDIDATES
            if _safe(features[name])
            and int(features[name]["visible_site_count"]) > 0
            and int(features[name]["residual_visible_site_count"]) == 0
        ]
        if len(complete) == 1:
            return complete[0], "only_structurally_complete_candidate"
        if len(complete) == 2:
            historical_size = (
                int(historical["changed_path_count"]),
                int(historical["diff_line_count"]),
                int(historical["changed_node_count"]),
            )
            closure_size = (
                int(closure["changed_path_count"]),
                int(closure["diff_line_count"]),
                int(closure["changed_node_count"]),
            )
            if historical_size < closure_size:
                return "historical-prescriptive-ast", "smaller_complete_diff"
        return "ast-closure-revision", "closure_default_without_unique_complete_candidate"
    raise ValueError(f"unknown_policy:{policy}")


def _validate_selection(package_root: Path) -> dict[str, Any]:
    freeze = read_json(package_root.parent / "SELECTION_FREEZE.json")
    if not _embedded_hash_valid(freeze, "selection_freeze_hash"):
        raise ValueError(f"selection_freeze_hash_invalid:{package_root}")
    current = _tree_hash(package_root)
    if freeze.get("selected_tree_hash") != current:
        raise ValueError(f"selected_tree_changed:{package_root}")
    return freeze


def freeze_visible_arbitration(
    closure_experiment: str | Path,
    historical_experiment: str | Path,
    output_root: str | Path,
) -> dict[str, Any]:
    closure_root = Path(closure_experiment).resolve()
    historical_root = Path(historical_experiment).resolve()
    output = Path(output_root).resolve()
    output.mkdir(parents=True, exist_ok=False)
    reports_root = output / "visible_reports"
    reports_root.mkdir()

    closure_summary = read_json(closure_root / "selected" / "all" / "SELECTION_SUMMARY.json")
    historical_summary = read_json(
        historical_root / "selected" / "all" / "SELECTION_SUMMARY.json"
    )
    if not _embedded_hash_valid(closure_summary, "selection_hash"):
        raise ValueError("closure_selection_summary_invalid")
    if not _embedded_hash_valid(historical_summary, "selection_hash"):
        raise ValueError("historical_selection_summary_invalid")

    origins = read_json(closure_root / "stage" / "CASE_ORIGINS.json")["rows"]
    if len(origins) != 105:
        raise ValueError(f"registry105_required:{len(origins)}")
    rows: list[dict[str, Any]] = []
    for origin in sorted(origins, key=lambda row: str(row["task_id"])):
        task_id = str(origin["task_id"])
        case = closure_root / "stage" / "public" / "cases" / task_id
        _, parent = _discover_package(case, str(origin["skill_name"]))
        request_text = (case / "task" / "task.md").read_text(encoding="utf-8")
        visible_facts, visible_sha256 = _select_visible_facts(Path(origin["source_case"]))
        if visible_sha256 != origin.get("visible_structural_facts_sha256"):
            raise ValueError(f"visible_facts_changed:{task_id}")

        packages = {
            "historical-prescriptive-ast": historical_root
            / "selected"
            / "all"
            / "historical-prescriptive-ast"
            / task_id
            / "package",
            "ast-closure-revision": closure_root
            / "selected"
            / "all"
            / "ast-closure-revision"
            / task_id
            / "package",
        }
        features: dict[str, dict[str, Any]] = {}
        report_hashes: dict[str, str] = {}
        source_freezes: dict[str, str] = {}
        tree_hashes: dict[str, str] = {}
        for condition, package in packages.items():
            freeze = _validate_selection(package)
            candidate_features, report = _candidate_features(
                parent, package, request_text, visible_facts
            )
            report_path = reports_root / f"{task_id}--{condition}.json"
            write_json(report_path, report)
            features[condition] = candidate_features
            report_hashes[condition] = sha256_file(report_path)
            source_freezes[condition] = str(freeze["selection_freeze_hash"])
            tree_hashes[condition] = str(freeze["selected_tree_hash"])

        selections = {}
        for policy in POLICIES:
            selected, reason = choose_candidate(policy, features)
            selections[policy] = {
                "selected_candidate": selected,
                "selected_tree_hash": tree_hashes[selected],
                "reason": reason,
            }
        rows.append(
            {
                "task_id": task_id,
                "batch": str(origin["batch"]),
                "language": str(origin["language"]),
                "visible_structural_facts_sha256": visible_sha256,
                "candidate_tree_hashes": tree_hashes,
                "source_selection_freeze_hashes": source_freezes,
                "visible_report_sha256": report_hashes,
                "features": features,
                "policy_selections": selections,
            }
        )

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "visible_arbitration_frozen_before_result_join",
        "created_at_unix": time.time(),
        "task_count": len(rows),
        "candidate_conditions": list(CANDIDATES),
        "policies": list(POLICIES),
        "policy_definitions": POLICY_DEFINITIONS,
        "closure_experiment": str(closure_root),
        "closure_selection_hash": closure_summary["selection_hash"],
        "historical_experiment": str(historical_root),
        "historical_selection_hash": historical_summary["selection_hash"],
        "visible_reports_tree_hash": canonical_json_hash(hash_tree(reports_root)),
        "hidden_artifacts_loaded_during_freeze": False,
        "task_verifier_or_reward_used_for_arbitration": False,
        "model_calls": 0,
        "retrospective_registry105_hypothesis_generation": True,
        "claim_boundary": (
            "Registry105 outcomes were already observed during method development. Policies are "
            "answer-free at execution time but their evaluation is post-hoc hypothesis generation, "
            "not held-out evidence. Structural dominance is not semantic correctness."
        ),
        "rows": rows,
    }
    payload["freeze_hash"] = canonical_json_hash(payload)
    write_json(output / "VISIBLE_ARBITRATION_FREEZE.json", payload)
    return payload


def _mcnemar_exact(left_only: int, right_only: int) -> float:
    discordant = left_only + right_only
    if not discordant:
        return 1.0
    tail = sum(math.comb(discordant, index) for index in range(min(left_only, right_only) + 1))
    return min(1.0, 2.0 * tail / (2**discordant))


def evaluate_frozen_arbitration(
    frozen_root: str | Path,
    closure_hidden_result: str | Path,
    historical_hidden_result: str | Path,
) -> dict[str, Any]:
    output = Path(frozen_root).resolve()
    freeze = read_json(output / "VISIBLE_ARBITRATION_FREEZE.json")
    if not _embedded_hash_valid(freeze, "freeze_hash"):
        raise ValueError("visible_arbitration_freeze_invalid")
    if canonical_json_hash(hash_tree(output / "visible_reports")) != freeze.get(
        "visible_reports_tree_hash"
    ):
        raise ValueError("visible_reports_changed_after_freeze")

    for row in freeze["rows"]:
        task_id = row["task_id"]
        roots = {
            "historical-prescriptive-ast": Path(freeze["historical_experiment"])
            / "selected"
            / "all"
            / "historical-prescriptive-ast"
            / task_id
            / "package",
            "ast-closure-revision": Path(freeze["closure_experiment"])
            / "selected"
            / "all"
            / "ast-closure-revision"
            / task_id
            / "package",
        }
        for condition, package in roots.items():
            if _tree_hash(package) != row["candidate_tree_hashes"][condition]:
                raise ValueError(f"candidate_changed_after_arbitration_freeze:{task_id}:{condition}")

    closure_hidden = read_json(Path(closure_hidden_result).resolve())
    historical_hidden = read_json(Path(historical_hidden_result).resolve())
    if not _embedded_hash_valid(closure_hidden, "hidden_result_hash"):
        raise ValueError("closure_hidden_result_invalid")
    if not _embedded_hash_valid(historical_hidden, "hidden_result_hash"):
        raise ValueError("historical_hidden_result_invalid")
    closure_rows = {
        row["task_id"]: row
        for row in closure_hidden["rows"]
        if row["condition"] == "ast-closure-revision"
    }
    historical_rows = {row["task_id"]: row for row in historical_hidden["rows"]}

    joined_rows: list[dict[str, Any]] = []
    policy_counts = {policy: Counter() for policy in POLICIES}
    selected_counts = {policy: Counter() for policy in POLICIES}
    overlap = Counter()
    for row in freeze["rows"]:
        task_id = row["task_id"]
        statuses = {
            "historical-prescriptive-ast": historical_rows[task_id]["status"],
            "ast-closure-revision": closure_rows[task_id]["status"],
        }
        historical_pass = statuses["historical-prescriptive-ast"] == "pass"
        closure_pass = statuses["ast-closure-revision"] == "pass"
        overlap[
            "both_pass"
            if historical_pass and closure_pass
            else "historical_only_pass"
            if historical_pass
            else "closure_only_pass"
            if closure_pass
            else "both_fail"
        ] += 1
        outcomes = {}
        for policy, selection in row["policy_selections"].items():
            selected = selection["selected_candidate"]
            status = statuses[selected]
            policy_counts[policy][status] += 1
            selected_counts[policy][selected] += 1
            outcomes[policy] = {
                "selected_candidate": selected,
                "status": status,
                "reason": selection["reason"],
            }
        joined_rows.append(
            {
                "task_id": task_id,
                "batch": row["batch"],
                "candidate_statuses": statuses,
                "policy_outcomes": outcomes,
            }
        )

    comparisons: dict[str, Any] = {}
    closure_status = {task_id: row["status"] for task_id, row in closure_rows.items()}
    for policy in POLICIES[1:]:
        gain = 0
        loss = 0
        same_pass = 0
        same_fail = 0
        for row in joined_rows:
            policy_pass = row["policy_outcomes"][policy]["status"] == "pass"
            reference_pass = closure_status[row["task_id"]] == "pass"
            if policy_pass and not reference_pass:
                gain += 1
            elif reference_pass and not policy_pass:
                loss += 1
            elif policy_pass:
                same_pass += 1
            else:
                same_fail += 1
        comparisons[policy] = {
            "reference": "closure-default",
            "gains": gain,
            "losses": loss,
            "net_gain": gain - loss,
            "both_pass": same_pass,
            "both_fail": same_fail,
            "mcnemar_exact_p": _mcnemar_exact(gain, loss),
        }

    summary = {
        policy: {
            "task_count": sum(policy_counts[policy].values()),
            "pass_count": policy_counts[policy]["pass"],
            "fail_count": policy_counts[policy]["fail"],
            "abstain_count": policy_counts[policy]["abstain"],
            "pass_rate": policy_counts[policy]["pass"] / len(joined_rows),
            "selected_candidate_counts": dict(selected_counts[policy]),
        }
        for policy in POLICIES
    }
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete_posthoc_result_join",
        "created_at_unix": time.time(),
        "task_count": len(joined_rows),
        "visible_arbitration_freeze_hash": freeze["freeze_hash"],
        "closure_hidden_result_hash": closure_hidden["hidden_result_hash"],
        "historical_hidden_result_hash": historical_hidden["hidden_result_hash"],
        "candidate_overlap": dict(overlap),
        "oracle_union_pass_count_non_deployable": overlap["both_pass"]
        + overlap["historical_only_pass"]
        + overlap["closure_only_pass"],
        "policy_summary": summary,
        "policy_vs_closure": comparisons,
        "hidden_results_used_for_arbitration": False,
        "hidden_results_loaded_only_after_policy_freeze": True,
        "retrospective_registry105_hypothesis_generation": True,
        "claim_boundary": freeze["claim_boundary"],
        "rows": joined_rows,
    }
    result["result_hash"] = canonical_json_hash(result)
    write_json(output / "POSTHOC_ARBITRATION_RESULT.json", result)
    _write_report(output, result)
    return result


def _write_report(output: Path, result: dict[str, Any]) -> None:
    lines = [
        "# D36 Dual-AST 可见结构仲裁审计",
        "",
        "本审计比较 prescriptive AST 与 proposal-first closure 两份冻结候选。选择规则在结果 join 前冻结，",
        "但 Registry105 已被研究过程观察过，因此这里只能用于形成 held-out 假设，不能作为确认性结果。",
        "",
        "## 候选互补性",
        "",
        f"- 共同通过：{result['candidate_overlap'].get('both_pass', 0)}/105",
        f"- 仅 prescriptive 通过：{result['candidate_overlap'].get('historical_only_pass', 0)}/105",
        f"- 仅 closure 通过：{result['candidate_overlap'].get('closure_only_pass', 0)}/105",
        f"- 共同失败：{result['candidate_overlap'].get('both_fail', 0)}/105",
        f"- 偷看 hidden label 的不可部署 oracle union：{result['oracle_union_pass_count_non_deployable']}/105",
        "",
        "## 可部署规则的事后表现",
        "",
        "| policy | pass | selected prescriptive | selected closure | vs closure net | p |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for policy in POLICIES:
        row = result["policy_summary"][policy]
        comparison = result["policy_vs_closure"].get(policy, {})
        counts = row["selected_candidate_counts"]
        lines.append(
            f"| {policy} | {row['pass_count']}/105 | "
            f"{counts.get('historical-prescriptive-ast', 0)} | "
            f"{counts.get('ast-closure-revision', 0)} | "
            f"{comparison.get('net_gain', 0)} | "
            f"{comparison.get('mcnemar_exact_p', 1.0):.4f} |"
        )
    lines.extend(
        [
            "",
            "## 解释边界",
            "",
            "AST gate 只能证明语法、公开接口、修改范围、调用闭包和可见结构位点等风险属性。",
            "当两个候选都满足结构约束却实现不同语义时，结构仲裁必须承认无法判定；其结果不能称为语义 correctness。",
        ]
    )
    (output / "D36_DUAL_AST_ARBITRATION_REPORT_ZH.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--closure-experiment", type=Path, required=True)
    freeze.add_argument("--historical-experiment", type=Path, required=True)
    freeze.add_argument("--output-root", type=Path, required=True)
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--frozen-root", type=Path, required=True)
    evaluate.add_argument("--closure-hidden-result", type=Path, required=True)
    evaluate.add_argument("--historical-hidden-result", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "freeze":
        result = freeze_visible_arbitration(
            args.closure_experiment,
            args.historical_experiment,
            args.output_root,
        )
    else:
        result = evaluate_frozen_arbitration(
            args.frozen_root,
            args.closure_hidden_result,
            args.historical_hidden_result,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
