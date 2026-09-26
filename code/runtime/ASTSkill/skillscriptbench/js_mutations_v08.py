from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from .io_utils import canonical_json_hash, read_json, sha256_bytes, sha256_file, write_json
from .js_ts_discrimination import JS_TS_SUFFIXES, PARSER_ROOT, _node_check, _node_executable


MUTATION_HELPER = PARSER_ROOT / "extract_mutation_sites_v08.mjs"
BATCH_VALIDATION_HELPER = PARSER_ROOT / "validate_sources_batch.mjs"


def _mutation_sites(
    source: str,
    filename: str,
    *,
    node_executable: str | None = None,
) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [node_executable or _node_executable(), str(MUTATION_HELPER)],
        input=json.dumps({"source": source, "filename": filename}),
        text=True,
        capture_output=True,
        cwd=PARSER_ROOT,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(f"babel_mutation_parse_failed:{completed.stderr[-800:]}")
    return json.loads(completed.stdout)["opportunities"]


def _batch_babel_validate(
    sources: list[str],
    filename: str,
    *,
    node_executable: str | None = None,
    max_batch_items: int = 64,
    max_batch_bytes: int = 8_000_000,
) -> list[tuple[bool, str | None]]:
    if not sources:
        return []
    results: list[tuple[bool, str | None]] = []
    start = 0
    while start < len(sources):
        items = []
        payload_bytes = 0
        while start + len(items) < len(sources) and len(items) < max_batch_items:
            source = sources[start + len(items)]
            encoded_size = len(source.encode("utf-8"))
            if items and payload_bytes + encoded_size > max_batch_bytes:
                break
            items.append({"source": source, "filename": filename})
            payload_bytes += encoded_size
        completed = subprocess.run(
            [node_executable or _node_executable(), str(BATCH_VALIDATION_HELPER)],
            input=json.dumps({"items": items}),
            text=True,
            capture_output=True,
            cwd=PARSER_ROOT,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError(f"babel_batch_validation_failed:{completed.stderr[-800:]}")
        parsed = json.loads(completed.stdout).get("results", [])
        if len(parsed) != len(items):
            raise ValueError("babel_batch_validation_result_count_mismatch")
        results.extend(
            (bool(row.get("ok")), row.get("error")) for row in parsed
        )
        start += len(items)
    return results


def _excluded_target(path: str) -> bool:
    lowered = path.lower()
    parts = Path(lowered).parts
    name = Path(lowered).name
    return (
        any(part in {"fixture", "fixtures", "test", "tests"} for part in parts)
        or ".test." in name
        or ".spec." in name
        or "bundle" in name
        or name.endswith(".min.js")
    )


def enumerate_js_mutations_v08(
    source: str,
    *,
    source_hash: str,
    path: str,
    package_id: str,
    node_executable: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for site in _mutation_sites(source, path, node_executable=node_executable):
        start = int(site["start"])
        end = int(site["end"])
        original = source[start:end]
        replacement = site["replacement"]
        if original != site["original"] or not original or original == replacement:
            continue
        template = {
            "family": site["family"],
            "kind": site["kind"],
            "node_type": site.get("nodeType"),
            "typescript": Path(path).suffix.lower() == ".ts",
        }
        identity = {
            "package_id": package_id,
            "path": path,
            "start": start,
            "end": end,
            "family": site["family"],
            "replacement": replacement,
        }
        metadata = {
            key: value
            for key, value in site.items()
            if key not in {"start", "end", "original", "replacement"}
        }
        rows.append(
            {
                "mutation_id": canonical_json_hash(identity)[:24],
                "family": site["family"],
                "dimension": site["dimension"],
                "language": (
                    "typescript" if Path(path).suffix.lower() == ".ts" else "javascript"
                ),
                "path": path,
                "source_hash": source_hash,
                "line": source.count("\n", 0, start) + 1,
                "start": start,
                "end": end,
                "original_fragment": original,
                "original_fragment_hash": sha256_bytes(original.encode("utf-8")),
                "replacement_fragment": replacement,
                "mutation_template_fingerprint": canonical_json_hash(template),
                "site_metadata": metadata,
                "construction_claim": (
                    "One typed JavaScript/TypeScript behavior site is changed while all other "
                    "source bytes are preserved. Parse validity is necessary but not behavioral "
                    "discrimination."
                ),
            }
        )
    return rows


def apply_js_mutation_v08(source: str, mutation: dict[str, Any]) -> str:
    if sha256_bytes(source.encode("utf-8")) != mutation["source_hash"]:
        raise ValueError("source_hash_mismatch")
    start = int(mutation["start"])
    end = int(mutation["end"])
    original = source[start:end]
    if original != mutation["original_fragment"]:
        raise ValueError("original_fragment_mismatch")
    if sha256_bytes(original.encode("utf-8")) != mutation["original_fragment_hash"]:
        raise ValueError("original_fragment_hash_mismatch")
    transformed = source[:start] + mutation["replacement_fragment"] + source[end:]
    if transformed == source:
        raise ValueError("mutation_did_not_change_source")
    return transformed


def build_js_mutation_audit_v08(
    expansion_audit: str | Path | dict[str, Any],
    output: str | Path | None = None,
    *,
    max_file_bytes: int = 500_000,
) -> dict[str, Any]:
    payload = (
        read_json(expansion_audit)
        if isinstance(expansion_audit, (str, Path))
        else expansion_audit
    )
    packages: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    excluded_files: list[dict[str, Any]] = []
    for package in payload.get("packages", []):
        if package.get("license_tier") != "formal_redistributable":
            continue
        root = Path(package["source_local_root"]) / package["relative_root"]
        mutations: list[dict[str, Any]] = []
        for relative in package.get("script_files", []):
            path = root / relative
            suffix = path.suffix.lower()
            if suffix not in JS_TS_SUFFIXES:
                continue
            if _excluded_target(relative):
                excluded_files.append(
                    {"package_id": package["package_id"], "path": relative, "reason": "test_or_generated"}
                )
                continue
            if path.stat().st_size > max_file_bytes:
                excluded_files.append(
                    {"package_id": package["package_id"], "path": relative, "reason": "file_too_large"}
                )
                continue
            try:
                source = path.read_text(encoding="utf-8")
                source_hash = sha256_file(path)
                source_ok, source_error = _node_check(source, suffix)
                if not source_ok:
                    raise ValueError(f"source_node_check_failed:{source_error}")
                file_mutations = enumerate_js_mutations_v08(
                    source,
                    source_hash=source_hash,
                    path=relative,
                    package_id=package["package_id"],
                )
                transformed_sources = [
                    apply_js_mutation_v08(source, mutation)
                    for mutation in file_mutations
                ]
                validation = _batch_babel_validate(transformed_sources, relative)
                invalid = [
                    (index, error)
                    for index, (ok, error) in enumerate(validation)
                    if not ok
                ]
                if invalid:
                    raise ValueError(
                        f"mutant_babel_batch_check_failed:{invalid[0]}"
                    )
                mutations.extend(file_mutations)
            except Exception as exc:
                failures.append(
                    {
                        "package_id": package["package_id"],
                        "source_id": package["source_id"],
                        "path": relative,
                        "reason": f"{type(exc).__name__}:{exc}",
                    }
                )
        if mutations:
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
                    "mutations": mutations,
                }
            )
    all_mutations = [row for package in packages for row in package["mutations"]]
    result = {
        "schema_version": "0.8-js-ts-mutation-audit-v1",
        "benchmark": "SkillScriptBench",
        "status": "zero_model_static_typed_mutation_audit",
        "claim_boundary": (
            "This audit establishes source-hash replayability and parse validity only. A mutation "
            "becomes a benchmark case only after an independent behavioral contract kills it."
        ),
        "expansion_audit_hash": payload.get("audit_hash") or canonical_json_hash(payload),
        "max_file_bytes": max_file_bytes,
        "parser_runtime": {
            "node_version": subprocess.run(
                [_node_executable(), "--version"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip(),
            "mutation_helper_sha256": sha256_file(MUTATION_HELPER),
            "batch_validation_helper_sha256": sha256_file(
                BATCH_VALIDATION_HELPER
            ),
            "parser_lock_sha256": sha256_file(PARSER_ROOT / "package-lock.json"),
        },
        "model_calls": 0,
        "behavior_executions": 0,
        "summary": {
            "package_count": len(packages),
            "source_count": len({package["source_id"] for package in packages}),
            "content_component_count": len(
                {package["split_group"] for package in packages if package.get("split_group")}
            ),
            "mutation_count": len(all_mutations),
            "family_counts": dict(sorted(Counter(row["family"] for row in all_mutations).items())),
            "dimension_counts": dict(
                sorted(Counter(row["dimension"] for row in all_mutations).items())
            ),
            "language_counts": dict(
                sorted(Counter(row["language"] for row in all_mutations).items())
            ),
            "template_count": len(
                {row["mutation_template_fingerprint"] for row in all_mutations}
            ),
            "excluded_file_count": len(excluded_files),
            "failure_count": len(failures),
        },
        "packages": packages,
        "excluded_files": excluded_files,
        "failures": failures,
    }
    result["mutation_audit_hash"] = canonical_json_hash(result)
    if output is not None:
        write_json(output, result)
    return result


def build_js_behavior_operator_audit_v16(
    expansion_audit: str | Path | dict[str, Any],
) -> dict[str, Any]:
    mutation_audit = build_js_mutation_audit_v08(expansion_audit)
    packages = []
    for package in mutation_audit.get("packages", []):
        operators = []
        for mutation in package.get("mutations", []):
            dimension = (
                "threshold_or_boundary"
                if mutation["dimension"] == "threshold_or_validation"
                else mutation["dimension"]
            )
            operators.append(
                {
                    **mutation,
                    "operator_candidate_id": mutation["mutation_id"],
                    "operator": "typed_js_ts_mutation_v08",
                    "operator_subfamily": mutation["family"],
                    "dimension": dimension,
                    "operator_template_fingerprint": mutation[
                        "mutation_template_fingerprint"
                    ],
                }
            )
        packages.append(
            {
                **{key: value for key, value in package.items() if key != "mutations"},
                "operators": operators,
            }
        )
    all_operators = [
        operator for package in packages for operator in package["operators"]
    ]
    result = {
        "schema_version": "0.16-js-ts-behavior-operator-audit-v1",
        "benchmark": "SkillScriptBench",
        "status": "zero_model_typed_js_ts_operators_ready",
        "claim_boundary": (
            "This compatibility layer exposes pre-existing V08 source-hash-locked Babel mutations "
            "to the V16 catalog. Parse validity is established; source-test discrimination and "
            "model repairability remain separate gates."
        ),
        "mutation_audit_hash": mutation_audit["mutation_audit_hash"],
        "parser_runtime": mutation_audit["parser_runtime"],
        "model_calls": 0,
        "behavior_executions": 0,
        "summary": {
            "package_count": len(packages),
            "source_count": len({package["source_id"] for package in packages}),
            "operator_count": len(all_operators),
            "operator_subfamily_counts": dict(
                sorted(Counter(row["operator_subfamily"] for row in all_operators).items())
            ),
            "language_counts": dict(
                sorted(Counter(row["language"] for row in all_operators).items())
            ),
            "dimension_counts": dict(
                sorted(Counter(row["dimension"] for row in all_operators).items())
            ),
            "failure_count": len(mutation_audit.get("failures", [])),
            "excluded_file_count": len(mutation_audit.get("excluded_files", [])),
        },
        "packages": packages,
        "failures": mutation_audit.get("failures", []),
        "excluded_files": mutation_audit.get("excluded_files", []),
    }
    result["operator_audit_hash"] = canonical_json_hash(result)
    return result
