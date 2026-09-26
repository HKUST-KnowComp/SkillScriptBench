from __future__ import annotations

import ast
import builtins
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .io_utils import canonical_json_hash, read_json, sha256_file, write_json


FORMAL_LICENSE_STATUSES = {
    "inherits_redistributable_ancestor_license",
    "inherits_redistributable_repository_license",
    "redistributable_package_declaration",
    "redistributable_package_license_file",
}
PATH_PARAMETER_TOKENS = {
    "dir",
    "directory",
    "file",
    "filename",
    "folder",
    "output_dir",
    "output_file",
    "output_path",
    "path",
    "root",
}
FORBIDDEN_CALL_TERMINALS = {
    "chdir",
    "connect",
    "delete",
    "download",
    "getenv",
    "input",
    "mkdir",
    "open",
    "post",
    "put",
    "read_bytes",
    "read_text",
    "remove",
    "rename",
    "request",
    "rmdir",
    "run",
    "send",
    "sleep",
    "system",
    "unlink",
    "upload",
    "write_bytes",
    "write_text",
}
NONDETERMINISTIC_MODULES = {
    "datetime",
    "httpx",
    "numpy.random",
    "random",
    "requests",
    "secrets",
    "socket",
    "subprocess",
    "time",
    "urllib",
}
RUNTIME_STATE_PREFIXES = {
    "os.environ",
    "os.getenv",
    "os.getcwd",
    "os.uname",
    "pathlib.Path.cwd",
    "platform",
    "sys.argv",
    "sys.executable",
    "sys.platform",
}
INFERRED_TYPES = {
    "active": "bool",
    "case_sensitive": "bool",
    "count": "int",
    "cutoff": "float",
    "data": "dict[str, int]",
    "delimiter": "str",
    "distance": "float",
    "duration": "float",
    "enabled": "bool",
    "field": "str",
    "format": "str",
    "index": "int",
    "items": "list[int]",
    "key": "str",
    "limit": "int",
    "mapping": "dict[str, int]",
    "n": "int",
    "name": "str",
    "numbers": "list[int]",
    "query": "str",
    "rank": "int",
    "record": "dict[str, int]",
    "records": "list[dict[str, int]]",
    "reverse": "bool",
    "row": "dict[str, int]",
    "rows": "list[dict[str, int]]",
    "scores": "list[float]",
    "segments": "list[Any]",
    "sep": "str",
    "suffix": "str",
    "text": "str",
    "threshold": "float",
    "tolerance": "float",
    "value": "int",
    "values": "list[int]",
}
PROBE_RUNNER = r"""
import contextlib
import inspect
import io
import json
import sys

payload = json.loads(sys.stdin.read())
namespace = {"__name__": "skillscriptbench_probe"}
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    exec(compile(payload["source"], "<source-derived-skill>", "exec"), namespace)
function = namespace[payload["function_name"]]
signature = inspect.signature(function)
rows = []
for values in payload["calls"]:
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            bound = signature.bind(**values)
            result = function(*bound.args, **bound.kwargs)
        json.dumps(result, sort_keys=True, allow_nan=False)
        rows.append({"status": "ok", "input": values, "output": result})
    except Exception as exc:
        rows.append({"status": "error", "input": values, "error": type(exc).__name__})
print(json.dumps(rows, sort_keys=True, allow_nan=False))
"""


def _call_terminal(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _call_root(node: ast.Call) -> str:
    current: ast.AST = node.func
    while isinstance(current, ast.Attribute):
        current = current.value
    return current.id if isinstance(current, ast.Name) else ""


def _attribute_path(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _attribute_path(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _function_parameters(function: ast.FunctionDef) -> list[ast.arg]:
    return [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]


def _default_nodes(function: ast.FunctionDef) -> dict[str, ast.AST]:
    positional = [*function.args.posonlyargs, *function.args.args]
    defaults: dict[str, ast.AST] = {}
    if function.args.defaults:
        defaults.update(
            {
                argument.arg: default
                for argument, default in zip(
                    positional[-len(function.args.defaults) :],
                    function.args.defaults,
                )
            }
        )
    defaults.update(
        {
            argument.arg: default
            for argument, default in zip(function.args.kwonlyargs, function.args.kw_defaults)
            if default is not None
        }
    )
    return defaults


def _infer_type_from_body(function: ast.FunctionDef, name: str) -> str | None:
    string_methods = {
        "capitalize",
        "casefold",
        "endswith",
        "find",
        "format",
        "join",
        "lower",
        "lstrip",
        "partition",
        "removeprefix",
        "removesuffix",
        "replace",
        "rsplit",
        "rstrip",
        "split",
        "startswith",
        "strip",
        "title",
        "upper",
    }
    mapping_methods = {"get", "items", "keys", "setdefault", "update", "values"}
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == name
        ):
            if node.func.attr in string_methods:
                return "str"
            if node.func.attr in mapping_methods:
                return "dict[str, Any]"
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == name:
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                return "dict[str, Any]"
            return "list[Any]"
        if isinstance(node, (ast.For, ast.comprehension)) and isinstance(node.iter, ast.Name) and node.iter.id == name:
            return "list[Any]"
        if isinstance(node, ast.Call) and any(
            isinstance(argument, ast.Name) and argument.id == name
            for argument in [*node.args, *(keyword.value for keyword in node.keywords)]
        ):
            terminal = _call_terminal(node)
            if terminal in {"enumerate", "len", "max", "min", "sorted", "sum"}:
                return "list[Any]"
            if terminal in {"compile", "escape", "findall", "finditer", "fullmatch", "match", "search"}:
                return "str"
        if isinstance(node, ast.BinOp) and any(
            isinstance(child, ast.Name) and child.id == name for child in ast.walk(node)
        ):
            constants = [
                child.value
                for child in ast.walk(node)
                if isinstance(child, ast.Constant) and isinstance(child.value, (int, float)) and not isinstance(child.value, bool)
            ]
            if constants:
                return "float" if any(isinstance(value, float) for value in constants) else "int"
        if isinstance(node, ast.JoinedStr) and any(
            isinstance(child, ast.Name) and child.id == name for child in ast.walk(node)
        ):
            return "str"
        if isinstance(node, ast.Compare) and any(
            isinstance(child, ast.Name) and child.id == name for child in ast.walk(node)
        ):
            constants = [
                child.value
                for child in ast.walk(node)
                if isinstance(child, ast.Constant) and not isinstance(child.value, bool)
            ]
            if any(isinstance(value, str) for value in constants):
                return "str"
            if any(isinstance(value, float) for value in constants):
                return "float"
            if any(isinstance(value, int) for value in constants):
                return "int"
    return None


def _normalize_annotation(
    annotation: ast.AST | None,
    name: str,
    function: ast.FunctionDef,
    default_node: ast.AST | None,
) -> str | None:
    if annotation is None:
        if default_node is not None:
            try:
                value = ast.literal_eval(default_node)
            except Exception:
                value = None
            inferred = {
                bool: "bool",
                dict: "dict[str, Any]",
                float: "float",
                int: "int",
                list: "list[Any]",
                str: "str",
                tuple: "tuple[Any, ...]",
            }.get(type(value))
            if inferred:
                return inferred
        return INFERRED_TYPES.get(name.lower()) or _infer_type_from_body(function, name)
    rendered = ast.unparse(annotation).replace("typing.", "")
    rendered = rendered.replace("NoneType", "None")
    return rendered


def _base_type(annotation: str | None) -> str | None:
    if not annotation:
        return None
    compact = re.sub(r"\s+", "", annotation).replace("List", "list").replace("Dict", "dict")
    compact = compact.replace("Sequence", "list").replace("Tuple", "tuple")
    optional = re.fullmatch(r"Optional\[(.+)]", compact)
    if optional:
        compact = optional.group(1)
    union = compact.split("|")
    non_null = [piece for piece in union if piece not in {"None", "NoneType"}]
    if len(non_null) == 1:
        compact = non_null[0]
    if compact in {"Any", "bool", "dict", "float", "int", "list", "str", "tuple"}:
        if compact == "dict":
            return "dict[str,any]"
        if compact == "list":
            return "list[any]"
        if compact == "tuple":
            return "tuple[any,...]"
        return compact.lower()
    compact = compact.replace("Iterable", "list").replace("Collection", "list")
    compact = compact.replace("Mapping", "dict").replace("MutableMapping", "dict")
    if re.fullmatch(r"list\[(?:Any|bool|float|int|str)]", compact):
        return compact.lower()
    if compact in {"list[dict]", "list[dict[str,Any]]"}:
        return "list[dict[str,any]]"
    if re.fullmatch(r"tuple\[(?:Any|bool|float|int|str)(?:,\.\.\.)?]", compact):
        return compact.lower()
    if re.fullmatch(r"dict\[str,(?:Any|bool|float|int|str)]", compact):
        return compact.lower()
    if compact in {"dict[str,dict]", "dict[str,dict[str,Any]]"}:
        return "dict[str,dict[str,any]]"
    return None


def _parameter_schema(function: ast.FunctionDef) -> tuple[list[dict[str, Any]], list[str]]:
    reasons: list[str] = []
    if function.args.vararg or function.args.kwarg:
        reasons.append("variadic_signature")
    if function.args.posonlyargs:
        reasons.append("positional_only_signature")
    defaults = _default_nodes(function)
    schema: list[dict[str, Any]] = []
    for argument in _function_parameters(function):
        if argument.arg in {"self", "cls"}:
            reasons.append("method_receiver")
            continue
        annotation = _normalize_annotation(
            argument.annotation,
            argument.arg,
            function,
            defaults.get(argument.arg),
        )
        base_type = _base_type(annotation)
        if base_type is None:
            reasons.append(f"unsupported_parameter_type:{argument.arg}:{annotation or 'unknown'}")
        default_value: Any = None
        has_default = argument.arg in defaults
        if has_default:
            try:
                default_value = ast.literal_eval(defaults[argument.arg])
                json.dumps(default_value, allow_nan=False)
            except (TypeError, ValueError):
                reasons.append(f"non_json_literal_default:{argument.arg}")
        schema.append(
            {
                "name": argument.arg,
                "annotation": annotation,
                "base_type": base_type,
                "has_default": has_default,
                "default": default_value,
                "kind": "keyword_only" if argument in function.args.kwonlyargs else "positional",
            }
        )
    if not schema:
        reasons.append("no_parameters")
    if len(schema) > 6:
        reasons.append("too_many_parameters")
    if any(
        token in parameter["name"].lower().split("_")
        or parameter["name"].lower() in PATH_PARAMETER_TOKENS
        for parameter in schema
        for token in PATH_PARAMETER_TOKENS
    ):
        reasons.append("path_or_file_parameter")
    return schema, sorted(set(reasons))


def _module_maps(tree: ast.Module) -> tuple[dict[str, ast.stmt], dict[str, ast.FunctionDef], set[str]]:
    providers: dict[str, ast.stmt] = {}
    functions: dict[str, ast.FunctionDef] = {}
    classes: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                names = [alias.asname or alias.name.split(".")[0] for alias in node.names]
            else:
                names = [alias.asname or alias.name for alias in node.names]
            providers.update({name: node for name in names})
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    providers[target.id] = node
        elif isinstance(node, ast.FunctionDef):
            functions[node.name] = node
            providers[node.name] = node
        elif isinstance(node, ast.ClassDef):
            classes.add(node.name)
            providers[node.name] = node
    return providers, functions, classes


def _body_loaded_names(function: ast.FunctionDef) -> set[str]:
    return {
        node.id
        for statement in function.body
        for node in ast.walk(statement)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def _body_assigned_names(function: ast.FunctionDef) -> set[str]:
    assigned = {argument.arg for argument in _function_parameters(function)}
    for statement in function.body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Param)):
                assigned.add(node.id)
            elif isinstance(node, ast.arg):
                assigned.add(node.arg)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                assigned.add(node.name)
            elif isinstance(node, ast.Import):
                assigned.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assigned.update(alias.asname or alias.name for alias in node.names)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                assigned.add(node.name)
    return assigned


def _dependency_source(
    source: str,
    tree: ast.Module,
    target: ast.FunctionDef,
) -> tuple[str, list[str], list[str]]:
    providers, functions, classes = _module_maps(tree)
    import_aliases = _import_aliases(tree)
    closure: dict[str, ast.FunctionDef] = {target.name: target}
    queue = [target]
    reasons: list[str] = []
    while queue:
        function = queue.pop()
        for node in ast.walk(function):
            raw_path = _attribute_path(node)
            if not raw_path:
                continue
            root, *tail = raw_path.split(".")
            if root not in import_aliases:
                continue
            path = ".".join([import_aliases[root], *tail])
            for prefix in RUNTIME_STATE_PREFIXES:
                if path == prefix or path.startswith(f"{prefix}."):
                    reasons.append(f"runtime_state_dependency:{prefix}")
        for call in (node for statement in function.body for node in ast.walk(statement) if isinstance(node, ast.Call)):
            terminal = _call_terminal(call)
            root = _call_root(call)
            if terminal in FORBIDDEN_CALL_TERMINALS:
                reasons.append(f"side_effect_call:{terminal}")
            if root in NONDETERMINISTIC_MODULES or any(root.startswith(f"{module}.") for module in NONDETERMINISTIC_MODULES):
                reasons.append(f"external_or_nondeterministic_call:{root or terminal}")
            if terminal in functions and terminal not in closure:
                closure[terminal] = functions[terminal]
                queue.append(functions[terminal])
    loaded = set().union(*(_body_loaded_names(function) for function in closure.values()))
    assigned = set().union(*(_body_assigned_names(function) for function in closure.values()))
    required_names = loaded - assigned - set(dir(builtins)) - set(closure)
    support_nodes: list[ast.stmt] = []
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    for name in sorted(required_names):
        provider = providers.get(name)
        if provider is None:
            reasons.append(f"unresolved_global:{name}")
            continue
        if isinstance(provider, ast.ClassDef) or name in classes:
            reasons.append(f"class_dependency:{name}")
            continue
        if isinstance(provider, ast.Import):
            modules = [alias.name.split(".")[0] for alias in provider.names]
            if any(module not in stdlib and module != "__future__" for module in modules):
                reasons.append(f"external_import:{','.join(modules)}")
                continue
        elif isinstance(provider, ast.ImportFrom):
            module = (provider.module or "").split(".")[0]
            if provider.level or (module not in stdlib and module != "__future__"):
                reasons.append(f"external_import:{provider.module or '.'}")
                continue
        elif isinstance(provider, (ast.Assign, ast.AnnAssign)):
            try:
                ast.literal_eval(provider.value)
            except Exception:
                reasons.append(f"nonliteral_global:{name}")
                continue
        if provider not in support_nodes:
            support_nodes.append(provider)
    helper_nodes = [node for name, node in closure.items() if name != target.name]
    selected = sorted({*support_nodes, *helper_nodes}, key=lambda node: node.lineno)
    rendered = ["from __future__ import annotations"]
    rendered.extend(ast.get_source_segment(source, node) or ast.unparse(node) for node in selected)
    rendered.append(ast.get_source_segment(source, target) or ast.unparse(target))
    dependency_names = [node.name for node in helper_nodes]
    return "\n\n".join(rendered).strip() + "\n", sorted(set(reasons)), sorted(dependency_names)


def _mapping_hints(function: ast.FunctionDef, parameter_name: str) -> list[dict[str, Any]]:
    hints: dict[str, dict[str, Any]] = {}
    for node in ast.walk(function):
        key: str | None = None
        default: Any = None
        has_default = False
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == parameter_name
            and node.func.attr in {"get", "setdefault"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            key = node.args[0].value
            if len(node.args) > 1:
                try:
                    default = ast.literal_eval(node.args[1])
                    has_default = True
                except Exception:
                    pass
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == parameter_name
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            key = node.slice.value
        if key is not None and key not in hints:
            hints[key] = {"key": key, "has_default": has_default, "default": default}
    return list(hints.values())


def _mapping_hint_value(hint: dict[str, Any], index: int) -> Any:
    key = str(hint["key"])
    normalized = key.lower()
    default = hint.get("default")
    if any(token in normalized for token in ("dim", "shape", "resolution")):
        base = (64, 96, 128, 192, 256)[index % 5]
        return [base, min(base * 2, 512), 256]
    if any(token in normalized for token in ("step", "count", "index", "limit", "rank", "line")):
        return (1, 2, 5, 10, 20, 30, 40)[index % 7]
    if any(token in normalized for token in ("enabled", "active", "allow", "flag")):
        return bool(index % 2)
    if any(token in normalized for token in ("sensor", "segment", "item", "value")) and isinstance(default, list):
        return list(range(1 + index % 4))
    if hint.get("has_default"):
        if isinstance(default, dict):
            return {}
        if isinstance(default, list):
            return list(range(1 + index % 4))
        if isinstance(default, bool):
            return bool(index % 2)
        if isinstance(default, int):
            return max(0, default + index % 3)
        if isinstance(default, float):
            return default + (index % 3) * 0.5
        if isinstance(default, str):
            return default if index % 2 == 0 else f"{default}-{index}"
    if any(token in normalized for token in ("score", "threshold", "ratio", "percent")):
        return (0.0, 0.25, 0.5, 1.0)[index % 4]
    return f"{key}-{index}"


def _type_value(
    base_type: str,
    name: str,
    index: int,
    string_constants: list[str],
    *,
    mapping_hints: list[dict[str, Any]] | None = None,
) -> Any:
    if base_type == "bool":
        return bool(index % 2)
    if base_type == "int":
        boundaries = (-2, -1, 0, 1, 2, 5, 10)
        return boundaries[index] if index < len(boundaries) else index - 20
    if base_type == "float":
        boundaries = (-1.0, 0.0, 0.25, 0.5, 1.0, 2.5, 10.0)
        return boundaries[index] if index < len(boundaries) else (index - 20) / 3.0
    if base_type == "str" or base_type == "any":
        variants = [
            f"value-{index}",
            f"alpha-beta-{index}",
            f"alpha({index}) tail",
            f"suite|task-{index}|{index % 5}",
            "true" if index % 2 else "false",
            *string_constants,
        ]
        if any(token in name.lower() for token in ("text", "content", "query")):
            variants.insert(0, f"alpha beta item {index}")
        return variants[index % len(variants)]
    container = next(
        (name for name in ("list", "tuple") if base_type.startswith(f"{name}[") and base_type.endswith("]")),
        None,
    )
    if container is not None:
        item_type = base_type[len(container) + 1 : -1]
        if container == "tuple" and item_type.endswith(",..."):
            item_type = item_type[:-4]
        length = index % 6
        if "segment" in name.lower():
            values = [
                {"start": float(offset * 2), "end": float(offset * 2 + 1)}
                for offset in range(length)
            ]
        elif "pattern" in name.lower():
            values = [r"alpha", rf"item[- ]?{index}", r"task"][:length]
        else:
            values = [_type_value(item_type, name, index + offset + 1, string_constants) for offset in range(length)]
        return values if container == "list" else tuple(values)
    mapping_match = re.match(r"dict\[str,(.+)]", base_type)
    if mapping_match:
        value_type = mapping_match.group(1)
        if mapping_hints:
            count = index % (len(mapping_hints) + 1)
            selected = mapping_hints[:count]
            return {
                hint["key"]: _mapping_hint_value(hint, index + offset)
                for offset, hint in enumerate(selected)
            }
        keys = list(dict.fromkeys([*string_constants[:4], "value", "score", "name", "data"]))
        count = 1 + index % min(4, len(keys))
        return {
            key: _type_value(value_type, key, index + offset + 1, string_constants)
            for offset, key in enumerate(keys[:count])
        }
    raise ValueError(f"unsupported base type: {base_type}")


def generate_calls(
    function: ast.FunctionDef,
    schema: list[dict[str, Any]],
    *,
    samples: int = 24,
) -> list[dict[str, Any]]:
    docstring = ast.get_docstring(function, clean=False)
    constants = [
        node.value
        for statement in function.body
        for node in ast.walk(statement)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value != docstring
        and node.value
        and "\n" not in node.value
        and len(node.value) <= 40
    ]
    mapping_hints = {
        parameter["name"]: _mapping_hints(function, parameter["name"])
        for parameter in schema
        if parameter["base_type"].startswith("dict[")
    }
    calls: list[dict[str, Any]] = []
    for index in range(samples * 3):
        values: dict[str, Any] = {}
        for offset, parameter in enumerate(schema):
            if parameter["has_default"] and (index + offset) % 4 == 0:
                continue
            values[parameter["name"]] = _type_value(
                parameter["base_type"],
                parameter["name"],
                index + offset,
                constants,
                mapping_hints=mapping_hints.get(parameter["name"]),
            )
        if "patterns" in values and "text" in values and index % 2:
            values["patterns"] = ["alpha", "task"]
            values["text"] = f"alpha task item {index}"
        fingerprint = canonical_json_hash(values)
        if all(canonical_json_hash(existing) != fingerprint for existing in calls):
            calls.append(values)
        if len(calls) == samples:
            break
    return calls


def _run_probe(source: str, function_name: str, calls: list[dict[str, Any]], timeout: int) -> list[dict[str, Any]]:
    payload = json.dumps(
        {"source": source, "function_name": function_name, "calls": calls},
        sort_keys=True,
        allow_nan=False,
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", PROBE_RUNNER],
        input=payload,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"probe_process_failed:{completed.stderr[-400:]}")
    return json.loads(completed.stdout)


def _probe_function(
    source: str,
    function_name: str,
    calls: list[dict[str, Any]],
    *,
    timeout: int = 5,
) -> dict[str, Any]:
    try:
        first = _run_probe(source, function_name, calls, timeout)
        second = _run_probe(source, function_name, calls, timeout)
    except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        return {
            "status": "fail",
            "reason": f"{type(exc).__name__}:{exc}",
            "valid_call_count": 0,
            "unique_output_count": 0,
        }
    valid = [row for row in first if row.get("status") == "ok"]
    second_valid = [row for row in second if row.get("status") == "ok"]
    deterministic = canonical_json_hash(valid) == canonical_json_hash(second_valid)
    unique_outputs = {
        canonical_json_hash(row["output"])
        for row in valid
    }
    status = "pass" if deterministic and len(valid) >= 6 and len(unique_outputs) >= 2 else "fail"
    reasons = []
    if not deterministic:
        reasons.append("nondeterministic_outputs")
    if len(valid) < 6:
        reasons.append("insufficient_valid_generated_calls")
    if len(unique_outputs) < 2:
        reasons.append("insufficient_output_variation")
    return {
        "status": status,
        "reason": ",".join(reasons) or None,
        "requested_call_count": len(calls),
        "valid_call_count": len(valid),
        "unique_output_count": len(unique_outputs),
        "valid_input_hash": canonical_json_hash([row["input"] for row in valid]),
        "output_hash": canonical_json_hash([row["output"] for row in valid]),
        "selected_utility_inputs": [row["input"] for row in valid[:3]],
    }


def _function_record(path: Path, package_root: Path, source: str, tree: ast.Module, function: ast.FunctionDef) -> dict[str, Any]:
    reasons: list[str] = []
    visibility = "internal" if function.name.startswith("_") else "public"
    if function.name.startswith("__") and function.name.endswith("__"):
        reasons.append("dunder_function")
    if function.decorator_list:
        reasons.append("decorated_function")
    if any(isinstance(node, (ast.Await, ast.Yield, ast.YieldFrom)) for node in ast.walk(function)):
        reasons.append("async_or_generator_body")
    schema, schema_reasons = _parameter_schema(function)
    reasons.extend(schema_reasons)
    isolated_source, dependency_reasons, helper_names = _dependency_source(source, tree, function)
    reasons.extend(dependency_reasons)
    calls: list[dict[str, Any]] = []
    probe: dict[str, Any] = {
        "status": "not_run",
        "valid_call_count": 0,
        "unique_output_count": 0,
    }
    if not reasons:
        calls = generate_calls(function, schema)
        probe = _probe_function(isolated_source, function.name, calls)
        if probe["status"] != "pass":
            reasons.append(f"runtime_probe:{probe.get('reason') or 'failed'}")
    relative = path.relative_to(package_root).as_posix()
    return {
        "function_id": f"{relative}:{function.name}:{function.lineno}",
        "source_path": relative,
        "function_name": function.name,
        "visibility": visibility,
        "line": function.lineno,
        "end_line": function.end_lineno,
        "signature": ast.unparse(function.args),
        "parameter_schema": schema,
        "helper_names": helper_names,
        "isolated_source": isolated_source,
        "isolated_source_hash": canonical_json_hash(isolated_source),
        "source_file_hash": sha256_file(path),
        "ast_node_count": sum(1 for _ in ast.walk(function)),
        "eligibility_status": "eligible" if not reasons else "rejected",
        "rejection_reasons": sorted(set(reasons)),
        "probe": probe,
    }


def _scan_package(package: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    package_root = Path(source["local_root"]) / package["relative_root"]
    functions: list[dict[str, Any]] = []
    parse_failures: list[str] = []
    for relative in package.get("script_files", []):
        path = package_root / relative
        if path.suffix.lower() != ".py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as exc:
            parse_failures.append(f"{relative}:{type(exc).__name__}:{exc}")
            continue
        functions.extend(
            _function_record(path, package_root, text, tree, function)
            for function in tree.body
            if isinstance(function, ast.FunctionDef)
        )
    eligible = [row for row in functions if row["eligibility_status"] == "eligible"]
    eligible.sort(
        key=lambda row: (
            row["visibility"] != "public",
            -row["probe"]["unique_output_count"],
            -row["probe"]["valid_call_count"],
            -min(row["ast_node_count"], 80),
            row["function_id"],
        )
    )
    formal_license = package.get("redistribution_status") in FORMAL_LICENSE_STATUSES
    family_reasons: list[str] = []
    if not formal_license:
        family_reasons.append(f"license:{package.get('redistribution_status')}")
    if not eligible:
        family_reasons.append("no_deterministic_json_function")
    return {
        "family_id": re.sub(r"[^a-z0-9]+", "-", package["package_id"].lower()).strip("-"),
        "package_id": package["package_id"],
        "source_id": package["source_id"],
        "source_commit": source["commit"],
        "source_repo_url": source["repo_url"],
        "source_local_root": source["local_root"],
        "relative_root": package["relative_root"],
        "skill_name": package["skill_name"],
        "effective_license_label": package.get("effective_license_label"),
        "redistribution_status": package.get("redistribution_status"),
        "package_license_files": package.get("package_license_files", {}),
        "detected_package_license_files": package.get("detected_package_license_files", {}),
        "split_group": package.get("script_content_component_id"),
        "split_group_member_count": package.get("script_content_component_member_count"),
        "documented_script_reference_count": package.get("documented_script_reference_count", 0),
        "script_files": package.get("script_files", []),
        "function_count": len(functions),
        "eligible_function_count": len(eligible),
        "family_status": "eligible" if not family_reasons else "rejected",
        "family_rejection_reasons": family_reasons,
        "selected_function_id": eligible[0]["function_id"] if eligible and formal_license else None,
        "functions": functions,
        "parse_failures": parse_failures,
    }


def build_v04_catalog(
    inventory: str | Path | dict[str, Any],
    output: str | Path | None = None,
    *,
    source_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    payload = read_json(inventory) if isinstance(inventory, (str, Path)) else inventory
    allowed_sources = set(source_ids or [])
    families: list[dict[str, Any]] = []
    for scan in payload.get("sources", []):
        source = scan["source"]
        if allowed_sources and source["source_id"] not in allowed_sources:
            continue
        for package in scan.get("packages", []):
            if not package.get("strict_script_bearing") or not package.get("language_counts", {}).get("python"):
                continue
            families.append(_scan_package(package, source))
    families.sort(
        key=lambda row: (
            row["family_status"] != "eligible",
            -row["eligible_function_count"],
            row["source_id"],
            row["relative_root"],
        )
    )
    eligible = [row for row in families if row["family_status"] == "eligible"]
    result = {
        "schema_version": "0.4-catalog-1",
        "benchmark": "SkillScriptBench",
        "status": "zero_model_source_function_catalog_not_frozen_cases",
        "formal_family_definition": (
            "One strict script-bearing skill package with a permissive effective license, at least one "
            "top-level deterministic JSON-callable Python function, and an inseparable exact-content split group."
        ),
        "safety_scope": (
            "Static and subprocess probes establish local determinism and serializability only; they do not "
            "establish semantic correctness or evolution value."
        ),
        "summary": {
            "scanned_family_count": len(families),
            "eligible_family_count": len(eligible),
            "eligible_source_count": len({row["source_id"] for row in eligible}),
            "eligible_source_counts": dict(sorted(Counter(row["source_id"] for row in eligible).items())),
            "eligible_function_count": sum(row["eligible_function_count"] for row in eligible),
            "rejected_family_count": len(families) - len(eligible),
        },
        "families": families,
    }
    result["catalog_hash"] = canonical_json_hash(result)
    if output is not None:
        write_json(output, result)
    return result
