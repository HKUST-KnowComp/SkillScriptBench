from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any


SCHEMA_VERSION = "0.1"
BENCHMARK_NAME = "SkillScriptBench"
ARC_ROLES = ("T1", "T2", "T3", "T4", "T5", "T6")
VISIBLE_ROLES = ("T1", "T2", "T3")
HIDDEN_ROLES = ("T4", "T5", "T6")
ROLE_VISIBILITY = {
    "T1": "acquisition_visible",
    "T2": "acquisition_visible",
    "T3": "acquisition_visible",
    "T4": "post_freeze_hidden",
    "T5": "post_freeze_hidden",
    "T6": "post_freeze_hidden",
}
PUBLIC_FORBIDDEN_KEYS = {
    "construction_family",
    "expected",
    "expected_output",
    "hidden_evaluator_hash",
    "hidden_task_hash",
    "mutation_label",
    "oracle_hashes",
    "oracle_path",
    "private_canary",
    "target_parameter",
}


class ManifestError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ManifestError(message)


def _safe_relative_path(value: str, *, field: str) -> None:
    path = PurePosixPath(value)
    _require(bool(value), f"{field}:empty_path")
    _require(not path.is_absolute(), f"{field}:absolute_path")
    _require(".." not in path.parts, f"{field}:parent_traversal")
    _require("_private" not in path.parts, f"{field}:private_path_exposed")


def _walk_keys(value: Any, prefix: str = "$"):
    if isinstance(value, dict):
        for key, child in value.items():
            yield prefix, str(key)
            yield from _walk_keys(child, f"{prefix}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_keys(child, f"{prefix}[{index}]")


def _walk_task_file_markers(value: Any, prefix: str = "$"):
    if isinstance(value, dict):
        if set(value) == {"$task_file"}:
            yield prefix, value["$task_file"]
            return
        for key, child in value.items():
            yield from _walk_task_file_markers(child, f"{prefix}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_task_file_markers(child, f"{prefix}[{index}]")


def validate_public_manifest(manifest: dict[str, Any]) -> None:
    _require(manifest.get("schema_version") == SCHEMA_VERSION, "public:schema_version")
    _require(manifest.get("benchmark") == BENCHMARK_NAME, "public:benchmark_name")
    arcs = manifest.get("arcs")
    _require(isinstance(arcs, list) and arcs, "public:arcs_missing")
    arc_ids: set[str] = set()
    for arc in arcs:
        _require(isinstance(arc, dict), "public:arc_not_object")
        arc_id = arc.get("arc_id")
        _require(isinstance(arc_id, str) and arc_id, "public:arc_id")
        _require(arc_id not in arc_ids, f"public:duplicate_arc:{arc_id}")
        arc_ids.add(arc_id)
        _safe_relative_path(str(arc.get("parent_skill_path", "")), field=f"{arc_id}:parent_skill_path")
        roles = arc.get("roles")
        _require(isinstance(roles, dict), f"{arc_id}:roles")
        _require(tuple(roles) == ARC_ROLES, f"{arc_id}:role_order_or_membership")
        for role in ARC_ROLES:
            record = roles[role]
            _require(isinstance(record, dict), f"{arc_id}:{role}:record")
            expected_visibility = ROLE_VISIBILITY[role]
            _require(record.get("visibility") == expected_visibility, f"{arc_id}:{role}:visibility")
            if role in VISIBLE_ROLES:
                _safe_relative_path(str(record.get("task_path", "")), field=f"{arc_id}:{role}:task_path")
            else:
                _require(set(record) == {"visibility"}, f"{arc_id}:{role}:hidden_metadata_exposed")
    for prefix, key in _walk_keys(manifest):
        _require(key not in PUBLIC_FORBIDDEN_KEYS, f"public:forbidden_key:{prefix}.{key}")


def validate_control_manifest(manifest: dict[str, Any]) -> None:
    _require(manifest.get("schema_version") == SCHEMA_VERSION, "control:schema_version")
    _require(manifest.get("benchmark") == BENCHMARK_NAME, "control:benchmark_name")
    _require(isinstance(manifest.get("private_canary"), str), "control:private_canary")
    arcs = manifest.get("arcs")
    _require(isinstance(arcs, list) and arcs, "control:arcs_missing")
    for arc in arcs:
        arc_id = arc.get("arc_id")
        _require(isinstance(arc_id, str) and arc_id, "control:arc_id")
        _require(set(arc.get("roles", {})) == set(ARC_ROLES), f"control:{arc_id}:roles")
        _require(arc.get("expected_parent_pass_roles") == ["T1"], f"control:{arc_id}:parent_pass_roles")
        _require(
            arc.get("expected_parent_fail_roles") == ["T2", "T3", "T4", "T5", "T6"],
            f"control:{arc_id}:parent_fail_roles",
        )


def validate_public_task(task: dict[str, Any]) -> None:
    _require(task.get("schema_version") == SCHEMA_VERSION, "task:schema_version")
    role = task.get("role")
    _require(role in VISIBLE_ROLES, "task:role_not_visible")
    _require(task.get("visibility") == ROLE_VISIBILITY[role], "task:visibility")
    invocation = task.get("invocation")
    _require(isinstance(invocation, dict), "task:invocation")
    kind = invocation.get("kind", "python_function")
    _require(kind in {"python_function", "python_method"}, "task:invocation_kind")
    _safe_relative_path(str(invocation.get("module", "")), field="task:module")
    _require(isinstance(invocation.get("callable"), str), "task:callable")
    if kind == "python_method":
        _require(isinstance(invocation.get("class"), str), "task:class")
        _require(isinstance(invocation.get("constructor_args", []), list), "task:constructor_args")
        _require(isinstance(invocation.get("constructor_kwargs", {}), dict), "task:constructor_kwargs")
    for prefix, path in _walk_task_file_markers(invocation):
        _require(isinstance(path, str), f"task:file_marker_type:{prefix}")
        _safe_relative_path(path, field=f"task:file_marker:{prefix}")
    postprocess = invocation.get("postprocess")
    if postprocess is not None:
        _require(isinstance(postprocess, dict), "task:postprocess")
        _require(postprocess.get("operation") == "component_mass", "task:postprocess_operation")
        _require(isinstance(postprocess.get("density_by_material"), dict), "task:density_map")
    for prefix, key in _walk_keys(task):
        _require(key not in PUBLIC_FORBIDDEN_KEYS, f"task:forbidden_key:{prefix}.{key}")
