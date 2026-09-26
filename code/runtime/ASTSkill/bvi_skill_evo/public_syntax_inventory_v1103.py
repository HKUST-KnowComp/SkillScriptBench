"""Parser-only public inventory. No request keywords, fault labels, or miner ranking."""
from __future__ import annotations

import ast
from collections import defaultdict, deque
import importlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess

from skillscriptbench.io_utils import canonical_json_hash, sha256_bytes

VERSION = "public-syntax-inventory-v1103"
SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".sh", ".bash"}
IGNORED = {".git", "__pycache__", "node_modules", ".pytest_cache", "tests", "test", "_private", "_oracle", "hidden", "evolution", "credentials"}
JS_HELPER = Path(__file__).resolve().parents[1] / "skillscriptbench/js_parser/extract_full_syntax_inventory_v1103.mjs"


def public_sources(package):
    root = Path(package).resolve()
    doc = root / "SKILL.md"
    if not doc.is_file() or doc.is_symlink():
        raise ValueError("regular_public_SKILL_md_required")
    sources = {"SKILL.md": doc.read_text(encoding="utf-8")}
    scripts = root / "scripts"
    if scripts.is_symlink():
        raise ValueError("public_scripts_symlink")
    for path in sorted(scripts.rglob("*")) if scripts.is_dir() else []:
        relative = path.relative_to(root)
        if any(part in IGNORED or part.startswith(".env") for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError("public_source_symlink")
        if not path.is_file() or path.suffix.lower() not in SUFFIXES or path.name.startswith("test_"):
            continue
        sources[relative.as_posix()] = path.read_text(encoding="utf-8")
    return sources


def _python(source):
    tree = ast.parse(source)
    lines = source.encode().splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    rows, signatures, imports, shadows = [], [], {}, {}

    def local_bindings(statements, *, include_imports=False):
        bound = set()
        pending = list(statements)
        while pending:
            item = pending.pop()
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
                bound.add(item.id)
            if include_imports and isinstance(item, (ast.Import, ast.ImportFrom)):
                bound.update(a.asname or a.name.split(".")[0] for a in item.names)
            pending.extend(ast.iter_child_nodes(item))
        return bound

    shadows["<module>"] = local_bindings(tree.body)

    def walk(node, parent=None, role="root", scope="<module>", protected=False):
        definition = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        next_scope = ("" if scope == "<module>" else scope + ".") + node.name if definition else scope
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            args += [a for a in (node.args.vararg, node.args.kwarg) if a is not None]
            shadows[next_scope] = {a.arg for a in args} | local_bindings(node.body, include_imports=True)
        current = parent
        if hasattr(node, "end_lineno") and node.end_lineno is not None:
            current = len(rows)
            facts = {}
            if isinstance(node, ast.Call):
                facts["callee"] = ast.unparse(node.func)
                facts["shadowed_root"] = facts["callee"].split(".")[0] in (
                    shadows.get(scope, set()) | shadows["<module>"])
                facts["arguments"] = [{"position": i, "source": ast.unparse(a), "dynamic": isinstance(a, ast.Starred)}
                    for i, a in enumerate(node.args)] + [{"keyword": a.arg, "source": ast.unparse(a.value), "dynamic": a.arg is None}
                    for a in node.keywords]
            rows.append({"id": current, "parent": parent, "role": role, "type": type(node).__name__,
                "start": offsets[node.lineno - 1] + node.col_offset,
                "end": offsets[node.end_lineno - 1] + node.end_col_offset,
                "line": node.lineno, "end_line": node.end_lineno, "symbol": next_scope,
                "editable": isinstance(node, (ast.expr, ast.stmt)) and not definition and not protected,
                "defines": next_scope if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None,
                "facts": {**facts, "signature_context": protected}})
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            signatures.append({"symbol": next_scope, "params": ast.dump(node.args, include_attributes=False),
                               "async": isinstance(node, ast.AsyncFunctionDef)})
        if isinstance(node, ast.ImportFrom) and scope == "<module>":
            for alias in node.names:
                imports[alias.asname or alias.name] = {"module": "." * node.level + (node.module or ""), "name": alias.name}
        elif isinstance(node, ast.Import) and scope == "<module>":
            for alias in node.names:
                imports[alias.asname or alias.name] = {"module": alias.name, "name": "*"}
        for field, value in ast.iter_fields(node):
            protect = protected or isinstance(node, ast.arguments) or (definition and field in {"args", "returns", "decorator_list"})
            children = enumerate(value) if isinstance(value, list) else [(None, value)]
            for index, child in children:
                if isinstance(child, ast.AST):
                    walk(child, current, f"{field}[{index}]" if index is not None else field, next_scope, protect)

    walk(tree)
    return {"nodes": rows, "signatures": signatures, "imports": imports}


def _shell(source):
    ts = importlib.import_module("tree_sitter")
    bash = importlib.import_module("tree_sitter_bash")
    language = ts.Language(bash.language())
    parser = ts.Parser(language)
    data = source.encode()
    tree = parser.parse(data)
    if tree.root_node.has_error:
        raise SyntaxError("tree_sitter_bash_parse_error")
    rows, signatures = [], []

    def walk(node, parent=None, role="root", scope="<module>"):
        definition = node.type == "function_definition"
        if definition:
            name = node.child_by_field_name("name")
            scope = data[name.start_byte:name.end_byte].decode() if name else "<anonymous>"
            signatures.append({"symbol": scope})
        current = len(rows)
        facts = {}
        if node.type == "command":
            name = node.child_by_field_name("name")
            facts["callee"] = data[name.start_byte:name.end_byte].decode() if name else ""
        rows.append({"id": current, "parent": parent, "role": role, "type": node.type,
            "start": node.start_byte, "end": node.end_byte, "line": data[:node.start_byte].count(b"\n") + 1,
            "end_line": data[:node.end_byte].count(b"\n") + 1, "symbol": scope,
            "editable": node.type not in {"program", "comment", "function_definition"} and not (definition and role == "name"),
            "defines": scope if definition else None, "facts": facts})
        for index, child in enumerate(node.children):
            if child.is_named:
                walk(child, current, node.field_name_for_child(index) or child.type, scope)
    walk(tree.root_node)
    return {"nodes": rows, "signatures": signatures, "imports": {}}


def parse_source(path, source):
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        return "python", "python_ast", _python(source)
    if suffix in {".sh", ".bash"}:
        return "shell", "tree_sitter_bash", _shell(source)
    result = subprocess.run([shutil.which("node") or "node", str(JS_HELPER)],
        input=json.dumps({"filename": path, "source": source}), text=True, capture_output=True,
        timeout=30, check=False)
    if result.returncode:
        raise SyntaxError("babel_parse_failed:" + result.stderr[-300:])
    language = "typescript" if suffix in {".ts", ".tsx", ".mts", ".cts"} else "javascript"
    return language, "babel_ast", json.loads(result.stdout)


def build_inventory(package, snapshot):
    if snapshot not in {"parent", "proposal"}:
        raise ValueError("invalid_snapshot")
    sources = public_sources(package)
    nodes, files, diagnostics = {}, {}, []
    for path, source in sources.items():
        if path == "SKILL.md":
            continue
        try:
            language, backend, parsed = parse_source(path, source)
        except (ImportError, SyntaxError, OSError, ValueError, subprocess.SubprocessError) as exc:
            diagnostics.append({"path": path, "status": "UNSUPPORTED_OR_PARSE_ERROR", "reason": str(exc)[:350]})
            continue
        data = source.encode()
        file_hash = sha256_bytes(data)
        text_hash = canonical_json_hash({"text": source})
        positions = sorted({v for row in parsed["nodes"] for v in (row["start"], row["end"])})
        char_offsets, prior, count = {}, 0, 0
        for position in positions:
            count += len(data[prior:position].decode())
            char_offsets[position] = count
            prior = position
        ids = {row["id"]: snapshot + "::syn-" + canonical_json_hash([
            path, file_hash, row["type"], row["role"], row["start"], row["end"]])[:24] for row in parsed["nodes"]}
        files[path] = {"language": language, "backend": backend, "signatures": parsed["signatures"], "imports": parsed["imports"]}
        for row in parsed["nodes"]:
            start, end = row["start"], row["end"]
            if start == end:
                continue
            identity = ids[row["id"]]
            nodes[identity] = {"node_id": identity, "target_node_id": identity, "snapshot": snapshot,
                "source_id": snapshot + "/" + path, "path": path, "language": language, "backend": backend,
                "source_hash": text_hash, "file_sha256": file_hash,
                "start": char_offsets[start], "end": char_offsets[end],
                "byte_span": {"start": start, "end": end}, "source": data[start:end].decode(),
                "observed_source": data[start:end].decode(), "node_type": row["type"], "role": row["role"],
                "line": row["line"], "end_line": row["end_line"], "symbol": row["symbol"],
                "column": len(source[:char_offsets[start]].rsplit("\n", 1)[-1]),
                "end_column": len(source[:char_offsets[end]].rsplit("\n", 1)[-1]),
                "parent_id": ids.get(row["parent"]), "editable": row["editable"],
                "defines": row["defines"], "facts": row["facts"]}
    graph = {"snapshot": snapshot, "nodes": nodes, "files": files, "sources": sources,
             "diagnostics": diagnostics, "request_used_for_inventory": False, "rank_filter_used": False}
    graph["calls"] = _calls(graph)
    graph["inventory_hash"] = canonical_json_hash(graph)
    return graph


def _calls(graph):
    definitions = defaultdict(list)
    for n in graph["nodes"].values():
        if n["defines"]:
            definitions[(n["path"], n["defines"])].append(n["node_id"])
    result = []
    for node in graph["nodes"].values():
        callee = node["facts"].get("callee")
        if not callee:
            continue
        names = [(node["path"], callee)]
        scope = node["symbol"].split(".") if node["symbol"] != "<module>" else []
        while scope:
            names.insert(0, (node["path"], ".".join(scope + [callee])))
            scope.pop()
        first, _, rest = callee.partition(".")
        imported = graph["files"][node["path"]]["imports"].get(first)
        if imported:
            module = imported["module"]
            base = PurePosixPath(node["path"]).parent
            if module.startswith("."):
                if node["language"] == "python":
                    level = len(module) - len(module.lstrip("."))
                    for _ in range(level - 1):
                        base = base.parent
                    stem = base / module.lstrip(".").replace(".", "/")
                else:
                    stem = base / module
            else:
                stem = PurePosixPath("scripts") / module.replace(".", "/")
            symbol = rest if imported["name"] == "*" else imported["name"] + ("." + rest if rest else "")
            names += [(str(stem) + suffix, symbol) for suffix in (".py", ".js", ".ts", ".mjs", ".cjs")]
            names += [(str(stem), symbol)]
        candidates = []
        for key in names:
            candidates.extend(definitions.get(key, []))
        candidates = sorted(set(candidates))
        if node["facts"].get("shadowed_root"):
            candidates = []
        result.append({"call_node_id": node["node_id"], "caller": node["symbol"], "path": node["path"],
            "callee": callee, "target_node_ids": candidates,
            "argument_expressions": node["facts"].get("arguments", []),
            "status": "RESOLVED" if len(candidates) == 1 else "AMBIGUOUS" if candidates else "EXTERNAL_OR_DYNAMIC"})
    return result


def call_context(graph, selected):
    """Follow resolved caller/helper edges as context; never expand edit permissions."""
    nodes = graph["nodes"]
    functions = {(nodes[n]["path"], nodes[n]["symbol"]) for n in selected}
    pending = deque(functions)
    edges = {}
    while pending:
        path, symbol = pending.popleft()
        for edge in graph["calls"]:
            target = nodes[edge["target_node_ids"][0]] if edge["status"] == "RESOLVED" else None
            if (edge["path"], edge["caller"]) != (path, symbol) and not (target and (target["path"], target["symbol"]) == (path, symbol)):
                continue
            edges[edge["call_node_id"]] = edge
            for pair in [(edge["path"], edge["caller"])] + ([(target["path"], target["symbol"])] if target else []):
                if pair not in functions:
                    functions.add(pair)
                    pending.append(pair)
    context = [n["node_id"] for n in nodes.values() if n["defines"] and (n["path"], n["symbol"]) in functions]
    return {"definition_node_ids": sorted(context), "calls": list(edges.values()),
            "context_grants_edit_permissions": False, "dynamic_calls_claimed_resolved": False}


def verify_inventory(graph, package):
    body = dict(graph)
    expected = body.pop("inventory_hash")
    if canonical_json_hash(body) != expected:
        raise ValueError("inventory_hash_mismatch")
    if public_sources(package) != graph["sources"]:
        raise ValueError("public_snapshot_changed")
    return expected
