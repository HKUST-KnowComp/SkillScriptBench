from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
from typing import Any

from .expansion_operators import (
    _call_terminal,
    _call_path,
    _expression_path,
    _function_argument_names,
    _operator_template_fingerprint,
    _scope_parent_map,
    _scope_nodes,
    _top_level_functions,
    build_operator_audit,
    validate_operator_audit,
)
from .io_utils import canonical_json_hash, read_json, sha256_file, write_json


SENSITIVE_SCHEMA_TOKENS = {
    "api",
    "auth",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
}
SERIALIZER_KEYWORDS = {
    "delimiter",
    "encoding",
    "ensure_ascii",
    "indent",
    "newline",
    "orient",
    "separators",
    "sort_keys",
}
V06_DIMENSION_MAP = {
    "parameter_or_threshold": "parameter_or_domain",
    "schema_or_format": "schema_or_serialization",
    "cross_script_composition_or_dataflow": "composition_or_dataflow",
    "error_dependency_or_doc_code_contract": "validation_or_recovery",
    "validation_or_precondition": "validation_or_recovery",
}


def _identifier_tokens(value: str) -> set[str]:
    return {token for token in value.lower().replace("-", "_").split("_") if token}


def _v06_candidate(payload: dict[str, Any]) -> dict[str, Any]:
    identity = {
        key: payload.get(key)
        for key in (
            "operator",
            "dimension",
            "path",
            "symbol",
            "line",
            "column",
            "guard_role",
            "field_index",
            "field_name",
            "keyword",
            "fallback_kind",
        )
    }
    return {"operator_candidate_id": canonical_json_hash(identity)[:24], **payload}


def _normalize_v06_candidate(
    candidate: dict[str, Any],
    package_id: str,
) -> dict[str, Any]:
    row = dict(candidate)
    legacy_dimension = str(row["dimension"])
    row["dimension"] = V06_DIMENSION_MAP.get(legacy_dimension, legacy_dimension)
    if row["dimension"] != legacy_dimension:
        row["legacy_dimension"] = legacy_dimension
    identity = {"package_id": package_id}
    identity.update(
        {
            key: row.get(key)
            for key in (
            "operator",
            "dimension",
            "path",
            "symbol",
            "line",
            "column",
            "parameter",
            "callee_symbol",
            "callee_path",
            "guard_role",
            "field_index",
            "field_name",
            "keyword",
            "fallback_kind",
            "original_value",
            )
        }
    )
    row["operator_candidate_id"] = canonical_json_hash(identity)[:24]
    return row


def _guard_role(test: ast.AST) -> str:
    nodes = list(ast.walk(test))
    if any(
        isinstance(node, ast.Call) and _call_terminal(node) in {"isinstance", "issubclass"}
        for node in nodes
    ):
        return "type_check"
    if any(isinstance(node, ast.Call) and _call_terminal(node) == "len" for node in nodes):
        return "cardinality_check"
    for node in nodes:
        if not isinstance(node, ast.Compare):
            continue
        if any(isinstance(operator, (ast.In, ast.NotIn)) for operator in node.ops):
            return "membership_check"
        if any(
            isinstance(comparator, ast.Constant) and comparator.value is None
            for comparator in node.comparators
        ):
            return "none_check"
        if any(
            isinstance(operator, (ast.Lt, ast.LtE, ast.Gt, ast.GtE))
            for operator in node.ops
        ):
            return "range_check"
        return "comparison_check"
    if isinstance(test, ast.BoolOp):
        return "compound_check"
    if isinstance(test, (ast.Name, ast.Attribute, ast.UnaryOp)):
        return "truthiness_check"
    return "general_precondition"


def _raised_exception(statement: ast.Raise) -> str:
    if statement.exc is None:
        return "bare_raise"
    expression = statement.exc.func if isinstance(statement.exc, ast.Call) else statement.exc
    return _expression_path(expression) or type(expression).__name__


def enumerate_validation_guard_operators(
    package_root: str | Path,
    script_files: list[str] | None = None,
) -> list[dict[str, Any]]:
    root = Path(package_root).resolve()
    relative_paths = (
        sorted(script_files)
        if script_files is not None
        else sorted(
            path.relative_to(root).as_posix()
            for directory in (root / "scripts", root / "script")
            if directory.is_dir()
            for path in directory.rglob("*.py")
        )
    )
    candidates: list[dict[str, Any]] = []
    for relative in relative_paths:
        path = root / relative
        if path.suffix.lower() != ".py" or path.name.startswith("test_"):
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        source_hash = sha256_file(path)
        for function in _top_level_functions(tree):
            parameters = _function_argument_names(function) - {"self", "cls"}
            for node in _scope_nodes(function):
                if not isinstance(node, ast.If) or node.orelse or len(node.body) != 1:
                    continue
                statement = node.body[0]
                if not isinstance(statement, ast.Raise) or statement.exc is None:
                    continue
                referenced_parameters = sorted(
                    {
                        child.id
                        for child in ast.walk(node.test)
                        if isinstance(child, ast.Name)
                        and isinstance(child.ctx, ast.Load)
                        and child.id in parameters
                    }
                )
                if not referenced_parameters:
                    continue
                role = _guard_role(node.test)
                operator_subfamily = f"remove_validation_guard:{role}"
                candidates.append(
                    _v06_candidate(
                        {
                            "operator": "remove_validation_guard",
                            "dimension": "validation_or_precondition",
                            "path": relative,
                            "symbol": function.name,
                            "line": node.lineno,
                            "column": node.col_offset,
                            "guard_role": role,
                            "referenced_parameters": referenced_parameters,
                            "raised_exception": _raised_exception(statement),
                            "structural_roles": [role],
                            "operator_subfamily": operator_subfamily,
                            "operator_template_fingerprint": _operator_template_fingerprint(
                                function,
                                operator_subfamily,
                            ),
                            "source_hash": source_hash,
                            "construction_claim": (
                                "The transformed implementation removes one parameter-dependent "
                                "precondition that previously raised an exception."
                            ),
                        }
                    )
                )
    deduped = {row["operator_candidate_id"]: row for row in candidates}
    return sorted(
        deduped.values(),
        key=lambda row: (
            row["dimension"],
            row["path"],
            row["line"],
            row["column"],
            row["operator_candidate_id"],
        ),
    )


def _serializer_family(call: ast.Call) -> str | None:
    terminal = _call_terminal(call)
    path = _call_path(call).lower()
    if terminal in {"dump", "dumps", "safe_dump", "safe_dump_all"} and (
        path.startswith(("json.", "yaml.", "yml.")) or terminal.startswith("safe_")
    ):
        return "structured_text"
    if terminal in {"writer", "dictwriter", "to_csv"}:
        return "tabular_text"
    if terminal == "to_json":
        return "structured_text"
    if terminal in {"write_text", "open"}:
        return "text_io"
    return None


def enumerate_multidimensional_operators(
    package_root: str | Path,
    script_files: list[str] | None = None,
) -> list[dict[str, Any]]:
    root = Path(package_root).resolve()
    relative_paths = (
        sorted(script_files)
        if script_files is not None
        else sorted(
            path.relative_to(root).as_posix()
            for directory in (root / "scripts", root / "script")
            if directory.is_dir()
            for path in directory.rglob("*.py")
        )
    )
    candidates: list[dict[str, Any]] = []
    for relative in relative_paths:
        path = root / relative
        if path.suffix.lower() != ".py" or path.name.startswith("test_"):
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        source_hash = sha256_file(path)
        for function in _top_level_functions(tree):
            parents = _scope_parent_map(function)
            for node in _scope_nodes(function):
                if isinstance(node, ast.Call):
                    call_path = _call_path(node)
                    terminal = _call_terminal(node)
                    if terminal in {"sort", "sorted"}:
                        for keyword in node.keywords:
                            if keyword.arg == "key":
                                operator_subfamily = "remove_order_key:call_keyword"
                                candidates.append(
                                    _v06_candidate(
                                        {
                                            "operator": "remove_order_key",
                                            "dimension": "cardinality_or_order",
                                            "path": relative,
                                            "symbol": function.name,
                                            "line": node.lineno,
                                            "column": node.col_offset,
                                            "call": call_path,
                                            "keyword": "key",
                                            "structural_roles": ["sort_key"],
                                            "operator_subfamily": operator_subfamily,
                                            "operator_template_fingerprint": _operator_template_fingerprint(
                                                function,
                                                operator_subfamily,
                                            ),
                                            "source_hash": source_hash,
                                            "construction_claim": (
                                                "The transformed implementation removes one explicit sort key."
                                            ),
                                        }
                                    )
                                )
                            if (
                                keyword.arg == "reverse"
                                and isinstance(keyword.value, ast.Constant)
                                and isinstance(keyword.value.value, bool)
                            ):
                                operator_subfamily = "toggle_sort_direction:boolean_keyword"
                                candidates.append(
                                    _v06_candidate(
                                        {
                                            "operator": "toggle_sort_direction",
                                            "dimension": "cardinality_or_order",
                                            "path": relative,
                                            "symbol": function.name,
                                            "line": node.lineno,
                                            "column": node.col_offset,
                                            "call": call_path,
                                            "keyword": "reverse",
                                            "original_value": keyword.value.value,
                                            "structural_roles": ["sort_direction"],
                                            "operator_subfamily": operator_subfamily,
                                            "operator_template_fingerprint": _operator_template_fingerprint(
                                                function,
                                                operator_subfamily,
                                            ),
                                            "source_hash": source_hash,
                                            "construction_claim": (
                                                "The transformed implementation reverses one explicit sort direction."
                                            ),
                                        }
                                    )
                                )

                    serializer_family = _serializer_family(node)
                    if serializer_family is not None:
                        for keyword in node.keywords:
                            if keyword.arg not in SERIALIZER_KEYWORDS:
                                continue
                            operator_subfamily = (
                                f"remove_serializer_option:{serializer_family}:{keyword.arg}"
                            )
                            candidates.append(
                                _v06_candidate(
                                    {
                                        "operator": "remove_serializer_option",
                                        "dimension": "schema_or_serialization",
                                        "path": relative,
                                        "symbol": function.name,
                                        "line": node.lineno,
                                        "column": node.col_offset,
                                        "call": call_path,
                                        "keyword": keyword.arg,
                                        "serializer_family": serializer_family,
                                        "structural_roles": [
                                            serializer_family,
                                            keyword.arg,
                                        ],
                                        "operator_subfamily": operator_subfamily,
                                        "operator_template_fingerprint": _operator_template_fingerprint(
                                            function,
                                            operator_subfamily,
                                        ),
                                        "source_hash": source_hash,
                                        "construction_claim": (
                                            "The transformed implementation removes one explicit "
                                            "serialization or text-I/O option."
                                        ),
                                    }
                                )
                            )

                    environment_lookup = terminal == "getenv" or call_path == "os.environ.get"
                    if environment_lookup:
                        fallback_kind: str | None = None
                        if len(node.args) == 2:
                            fallback_kind = "positional"
                        elif any(keyword.arg == "default" for keyword in node.keywords):
                            fallback_kind = "keyword"
                        if fallback_kind is not None:
                            operator_subfamily = (
                                f"remove_environment_fallback:{fallback_kind}"
                            )
                            candidates.append(
                                _v06_candidate(
                                    {
                                        "operator": "remove_environment_fallback",
                                        "dimension": "dependency_or_environment",
                                        "path": relative,
                                        "symbol": function.name,
                                        "line": node.lineno,
                                        "column": node.col_offset,
                                        "call": call_path,
                                        "fallback_kind": fallback_kind,
                                        "structural_roles": ["environment_default"],
                                        "operator_subfamily": operator_subfamily,
                                        "operator_template_fingerprint": _operator_template_fingerprint(
                                            function,
                                            operator_subfamily,
                                        ),
                                        "source_hash": source_hash,
                                        "construction_claim": (
                                            "The transformed implementation removes one explicit "
                                            "environment-variable fallback."
                                        ),
                                    }
                                )
                            )

                if not isinstance(node, ast.Dict) or not isinstance(
                    parents.get(node), ast.Return
                ):
                    continue
                literal_fields = [
                    (index, key.value)
                    for index, key in enumerate(node.keys)
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                ]
                if len(literal_fields) < 2:
                    continue
                for field_index, field_name in literal_fields:
                    if _identifier_tokens(field_name) & SENSITIVE_SCHEMA_TOKENS:
                        continue
                    operator_subfamily = "drop_output_schema_field:literal_key"
                    candidates.append(
                        _v06_candidate(
                            {
                                "operator": "drop_output_schema_field",
                                "dimension": "schema_or_serialization",
                                "path": relative,
                                "symbol": function.name,
                                "line": node.lineno,
                                "column": node.col_offset,
                                "field_index": field_index,
                                "field_name": field_name,
                                "structural_roles": ["returned_mapping_field"],
                                "operator_subfamily": operator_subfamily,
                                "operator_template_fingerprint": _operator_template_fingerprint(
                                    function,
                                    operator_subfamily,
                                ),
                                "source_hash": source_hash,
                                "construction_claim": (
                                    "The transformed implementation removes one literal field from "
                                    "a directly returned mapping."
                                ),
                            }
                        )
                    )
    deduped = {row["operator_candidate_id"]: row for row in candidates}
    return sorted(
        deduped.values(),
        key=lambda row: (
            row["dimension"],
            row["operator"],
            row["path"],
            row["line"],
            row["column"],
            row["operator_candidate_id"],
        ),
    )


def build_operator_audit_v06(
    expansion_audit: str | Path | dict[str, Any],
    output: str | Path | None = None,
    *,
    formal_only: bool = True,
) -> dict[str, Any]:
    expansion = (
        read_json(expansion_audit)
        if isinstance(expansion_audit, (str, Path))
        else expansion_audit
    )
    base = build_operator_audit(expansion, formal_only=formal_only)
    packages = {
        row["package_id"]: {
            **row,
            "operators": [
                _normalize_v06_candidate(operator, row["package_id"])
                for operator in row.get("operators", [])
            ],
        }
        for row in base.get("packages", [])
    }
    for row in expansion.get("packages", []):
        if formal_only and row.get("license_tier") != "formal_redistributable":
            continue
        root = Path(row["source_local_root"]) / row["relative_root"]
        v06_operators = [
            _normalize_v06_candidate(operator, row["package_id"])
            for operator in [
                *enumerate_validation_guard_operators(root, row.get("script_files")),
                *enumerate_multidimensional_operators(root, row.get("script_files")),
            ]
        ]
        if not v06_operators:
            continue
        package = packages.setdefault(
            row["package_id"],
            {
                "package_id": row["package_id"],
                "source_id": row["source_id"],
                "source_commit": row["source_commit"],
                "source_repo_url": row["source_repo_url"],
                "relative_root": row["relative_root"],
                "split_group": row.get("split_group"),
                "license_tier": row.get("license_tier"),
                "package_hashes": row.get("package_hashes", {}),
                "operators": [],
            },
        )
        combined = {
            operator["operator_candidate_id"]: operator
            for operator in [*package.get("operators", []), *v06_operators]
        }
        package["operators"] = sorted(
            combined.values(),
            key=lambda operator: (
                operator["dimension"],
                operator["operator"],
                operator["path"],
                operator["line"],
                operator["column"],
                operator["operator_candidate_id"],
            ),
        )
    package_rows = sorted(packages.values(), key=lambda row: row["package_id"])
    all_operators = [operator for package in package_rows for operator in package["operators"]]
    result = {
        "schema_version": "0.6-package-operator-audit-1",
        "benchmark": "SkillScriptBench",
        "status": "zero_model_static_operator_audit_not_executable_cases",
        "claim_boundary": (
            "An operator candidate is a reversible construction site, not evidence that the mutation "
            "is behaviorally discriminating, valuable, safe, or suitable for a benchmark case."
        ),
        "expansion_audit_hash": expansion.get("audit_hash")
        or canonical_json_hash(expansion),
        "formal_only": formal_only,
        "summary": {
            "package_count": len(package_rows),
            "operator_candidate_count": len(all_operators),
            "source_count": len({package["source_id"] for package in package_rows}),
            "content_component_count": len(
                {
                    package["split_group"]
                    for package in package_rows
                    if package.get("split_group")
                }
            ),
            "operator_counts": dict(
                sorted(Counter(row["operator"] for row in all_operators).items())
            ),
            "dimension_counts": dict(
                sorted(Counter(row["dimension"] for row in all_operators).items())
            ),
            "operator_subfamily_counts": dict(
                sorted(Counter(row["operator_subfamily"] for row in all_operators).items())
            ),
            "operator_template_count": len(
                {row["operator_template_fingerprint"] for row in all_operators}
            ),
            "source_package_counts": dict(
                sorted(Counter(package["source_id"] for package in package_rows).items())
            ),
        },
        "packages": package_rows,
    }
    result["operator_audit_hash"] = canonical_json_hash(result)
    if output is not None:
        write_json(output, result)
    return result


def validate_operator_audit_v06(
    operator_audit: str | Path | dict[str, Any],
    expansion_audit: str | Path | dict[str, Any],
    output: str | Path | None = None,
) -> dict[str, Any]:
    result = validate_operator_audit(operator_audit, expansion_audit)
    result["schema_version"] = "0.6-package-operator-generation-check-1"
    result["validation_hash"] = canonical_json_hash(
        {key: value for key, value in result.items() if key != "validation_hash"}
    )
    if output is not None:
        write_json(output, result)
    return result
