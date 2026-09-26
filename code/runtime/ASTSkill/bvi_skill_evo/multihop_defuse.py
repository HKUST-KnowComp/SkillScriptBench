from __future__ import annotations

import ast
import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .io_utils import hash_tree, read_json, stable_hash, tree_hash
from .visible_counterfactual import _canonical_dump


SCHEMA_VERSION = "bvi.multihop_visible_defuse.v1"
ACCEPT_MULTIHOP = "ACCEPT_DEFUSE"
REPAIR_MULTIHOP = "REPAIR_DEFUSE"
ABSTAIN_MULTIHOP = "ABSTAIN_DEFUSE"
REJECT_MULTIHOP = "REJECT_DEFUSE"


@dataclass(frozen=True)
class FunctionRecord:
    path: str
    source: str
    node: ast.FunctionDef | ast.AsyncFunctionDef


@dataclass
class AbstractValue:
    origins: set[str] = field(default_factory=set)
    mapping: dict[str, "AbstractValue"] | None = None
    disconnects: list[dict[str, Any]] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)

    def clone(self) -> "AbstractValue":
        return AbstractValue(
            origins=set(self.origins),
            mapping={key: value.clone() for key, value in self.mapping.items()}
            if self.mapping is not None
            else None,
            disconnects=copy.deepcopy(self.disconnects),
            trace=copy.deepcopy(self.trace),
        )


def _empty() -> AbstractValue:
    return AbstractValue()


def _merge(values: list[AbstractValue]) -> AbstractValue:
    result = AbstractValue()
    for value in values:
        result.origins.update(value.origins)
        result.disconnects.extend(copy.deepcopy(value.disconnects))
        result.trace.extend(copy.deepcopy(value.trace))
    return result


def _callee_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ast.unparse(call.func)


def _parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    return [
        argument.arg
        for argument in [
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        ]
    ]


def _optional_parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    positional = [*function.args.posonlyargs, *function.args.args]
    first_default = len(positional) - len(function.args.defaults)
    result = [argument.arg for argument in positional[first_default:]]
    result.extend(
        argument.arg
        for argument, default in zip(function.args.kwonlyargs, function.args.kw_defaults)
        if default is not None
    )
    return result


def _node_view(record: FunctionRecord, node: ast.AST, *, reason: str) -> dict[str, Any]:
    segment = ast.get_source_segment(record.source, node) or ast.unparse(node)
    view = {
        "path": record.path,
        "line": int(getattr(node, "lineno", 0)),
        "column": int(getattr(node, "col_offset", 0)),
        "end_line": int(getattr(node, "end_lineno", getattr(node, "lineno", 0))),
        "end_column": int(getattr(node, "end_col_offset", 0)),
        "node_type": type(node).__name__,
        "source": segment,
        "source_sha256": stable_hash(segment),
        "reason": reason,
        "symbol": record.node.name,
    }
    view["node_id"] = stable_hash(view)[:24]
    return view


def _index_package(package: Path) -> tuple[dict[str, FunctionRecord], dict[str, ast.Module], list[str]]:
    by_name: dict[str, list[FunctionRecord]] = {}
    trees: dict[str, ast.Module] = {}
    failures: list[str] = []
    for path in sorted((package / "scripts").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package).as_posix()
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            failures.append(f"{relative}:{exc}")
            continue
        trees[relative] = tree
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                by_name.setdefault(node.name, []).append(FunctionRecord(relative, source, node))
    unique = {name: rows[0] for name, rows in by_name.items() if len(rows) == 1}
    return unique, trees, failures


def _literal_key(node: ast.AST) -> str | None:
    return str(node.value) if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


class _Interpreter:
    def __init__(
        self,
        functions: dict[str, FunctionRecord],
        *,
        entry: FunctionRecord,
        target_parameter: str,
        expected_source_parameter: str,
    ) -> None:
        self.functions = functions
        self.entry = entry
        self.target_parameter = target_parameter
        self.expected_origin = f"{entry.node.name}:{expected_source_parameter}"
        self.sinks: list[dict[str, Any]] = []
        self.edges: list[dict[str, Any]] = []
        self.active: set[str] = set()

    def _event(
        self,
        record: FunctionRecord,
        node: ast.AST,
        *,
        kind: str,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "site": _node_view(record, node, reason=reason),
            "observed_site_symbol": node.id if isinstance(node, ast.Name) else None,
        }

    def _expected_env_symbol(self, env: dict[str, AbstractValue]) -> str | None:
        scalar_candidates = sorted(
            name for name, value in env.items() if self.expected_origin in value.origins
            and value.mapping is None
        )
        if len(scalar_candidates) == 1:
            return scalar_candidates[0]
        candidates = sorted(
            name for name, value in env.items() if self.expected_origin in value.origins
        )
        return candidates[0] if len(candidates) == 1 else None

    def _expected_argument_symbol(
        self,
        values: list[tuple[AbstractValue, ast.AST]],
    ) -> str | None:
        scalar_candidates = sorted(
            {
                expression.id
                for value, expression in values
                if self.expected_origin in value.origins
                and value.mapping is None
                and isinstance(expression, ast.Name)
            }
        )
        if len(scalar_candidates) == 1:
            return scalar_candidates[0]
        candidates = sorted(
            {
                expression.id
                for value, expression in values
                if self.expected_origin in value.origins and isinstance(expression, ast.Name)
            }
        )
        return candidates[0] if len(candidates) == 1 else None

    def _bind_call(
        self,
        record: FunctionRecord,
        caller_record: FunctionRecord,
        call: ast.Call,
        values: list[tuple[AbstractValue, ast.AST]],
        keyword_values: dict[str, tuple[AbstractValue, ast.AST]],
    ) -> dict[str, AbstractValue]:
        parameters = _parameters(record.node)
        all_arguments = [*values, *keyword_values.values()]
        expected_site_symbol = self._expected_argument_symbol(all_arguments)
        env: dict[str, AbstractValue] = {}
        for index, (value, expression) in enumerate(values):
            if index < len(parameters):
                name = parameters[index]
                bound = value.clone()
                self.edges.append(
                    {
                        "kind": "positional_call_binding",
                        "from_symbol": caller_record.node.name,
                        "to_symbol": record.node.name,
                        "callee_parameter": name,
                        "expression": ast.unparse(expression),
                        "origins": sorted(bound.origins),
                        "site": _node_view(
                            caller_record,
                            expression,
                            reason=f"positionally binds {record.node.name}.{name}",
                        ),
                    }
                )
                if name == self.target_parameter and self.expected_origin not in bound.origins:
                    reason = (
                        f"positional call binds {name} from {ast.unparse(expression)} instead of the "
                        "expected public source"
                    )
                    event = self._event(
                        caller_record,
                        expression,
                        kind="intermodule_positional_misroute",
                        reason=reason,
                    )
                    event["expected_site_symbol"] = expected_site_symbol
                    bound.disconnects.append(event)
                env[name] = bound
        for name, (value, expression) in keyword_values.items():
            if name not in parameters:
                continue
            bound = value.clone()
            self.edges.append(
                {
                    "kind": "call_binding",
                    "from_symbol": caller_record.node.name,
                    "to_symbol": record.node.name,
                    "callee_parameter": name,
                    "expression": ast.unparse(expression),
                    "origins": sorted(bound.origins),
                    "site": _node_view(caller_record, expression, reason=f"binds {record.node.name}.{name}"),
                }
            )
            if name == self.target_parameter and self.expected_origin not in bound.origins:
                reason = (
                    f"call binds {name} from {ast.unparse(expression)} instead of the expected public source"
                )
                event = self._event(
                    caller_record,
                    expression,
                    kind="intermodule_call_misroute",
                    reason=reason,
                )
                event["expected_site_symbol"] = expected_site_symbol
                bound.disconnects.append(event)
            env[name] = bound
        for name in parameters:
            env.setdefault(name, _empty())
        return env

    def _eval_call(
        self,
        call: ast.Call,
        env: dict[str, AbstractValue],
        current: FunctionRecord,
    ) -> AbstractValue:
        name = _callee_name(call)
        record = self.functions.get(name)
        positional = [(self._eval(argument, env, current), argument) for argument in call.args]
        keywords = {
            keyword.arg: (self._eval(keyword.value, env, current), keyword.value)
            for keyword in call.keywords
            if keyword.arg is not None
        }
        expanded = [self._eval(keyword.value, env, current) for keyword in call.keywords if keyword.arg is None]
        if record is None:
            return _merge(
                [
                    *(value for value, _ in positional),
                    *(value for value, _ in keywords.values()),
                    *expanded,
                ]
            )

        if Path(record.path).name == "operations.py" and self.target_parameter in _parameters(record.node):
            sink_value = keywords.get(self.target_parameter, (_empty(), call))[0].clone()
            if self.target_parameter not in keywords:
                target_index = _parameters(record.node).index(self.target_parameter)
                if target_index < len(positional):
                    sink_value = positional[target_index][0].clone()
                for value in expanded:
                    if value.mapping and self.target_parameter in value.mapping:
                        sink_value = value.mapping[self.target_parameter].clone()
                        break
            self.sinks.append(
                {
                    "callee": name,
                    "path": record.path,
                    "keyword": self.target_parameter,
                    "call_site": _node_view(current, call, reason="terminal operation sink"),
                    "origins": sorted(sink_value.origins),
                    "disconnects": copy.deepcopy(sink_value.disconnects),
                    "trace": copy.deepcopy(sink_value.trace),
                    "flow_closed": self.expected_origin in sink_value.origins,
                    "origin_pure": sink_value.origins == {self.expected_origin},
                }
            )
            self.edges.append(
                {
                    "kind": "operation_sink",
                    "from_symbol": current.node.name,
                    "to_symbol": name,
                    "callee_parameter": self.target_parameter,
                    "origins": sorted(sink_value.origins),
                    "site": _node_view(current, call, reason="terminal operation sink"),
                }
            )
            return _empty()

        if record.node.name in self.active:
            return _empty()
        bound = self._bind_call(record, current, call, positional, keywords)
        self.active.add(record.node.name)
        try:
            result = self._execute(record, bound)
        finally:
            self.active.remove(record.node.name)
        if self.expected_origin in result.origins or result.disconnects:
            return result
        all_arguments = [*positional, *keywords.values()]
        expected_site_symbol = self._expected_argument_symbol(all_arguments)
        observed = [
            (value, expression)
            for value, expression in all_arguments
            if value.origins.intersection(result.origins)
            and self.expected_origin not in value.origins
            and isinstance(expression, ast.Name)
        ]
        observed_symbols = {expression.id for _value, expression in observed}
        if expected_site_symbol is not None and len(observed_symbols) == 1:
            expression = observed[0][1]
            reason = (
                f"call returns provenance from {expression.id} while the public source is available as "
                f"{expected_site_symbol}"
            )
            event = self._event(
                current,
                expression,
                kind="intermodule_source_substitution",
                reason=reason,
            )
            event["expected_site_symbol"] = expected_site_symbol
            result.disconnects.append(event)
            self.edges.append(
                {
                    "kind": "intermodule_source_substitution",
                    "from_symbol": current.node.name,
                    "to_symbol": record.node.name,
                    "callee_parameter": self.target_parameter,
                    "origins": sorted(result.origins),
                    "site": event["site"],
                }
            )
        return result

    def _eval(
        self,
        expression: ast.AST,
        env: dict[str, AbstractValue],
        current: FunctionRecord,
    ) -> AbstractValue:
        if isinstance(expression, ast.Name):
            return env.get(expression.id, _empty()).clone()
        if isinstance(expression, ast.Constant):
            return _empty()
        if isinstance(expression, ast.Call):
            return self._eval_call(expression, env, current)
        if isinstance(expression, ast.Dict):
            mapping: dict[str, AbstractValue] = {}
            expanded_seen = False
            for key_node, value_node in zip(expression.keys, expression.values):
                if key_node is None:
                    expanded = self._eval(value_node, env, current)
                    if expanded.mapping is not None:
                        mapping.update(
                            {key: value.clone() for key, value in expanded.mapping.items()}
                        )
                    expanded_seen = True
                    continue
                key = _literal_key(key_node) if key_node is not None else None
                if key is not None:
                    value = self._eval(value_node, env, current)
                    previous = mapping.get(key, _empty())
                    if (
                        key == self.target_parameter
                        and self.expected_origin in previous.origins
                        and self.expected_origin not in value.origins
                    ):
                        kind = "dict_unpack_shadow" if expanded_seen else "mapping_literal_shadow"
                        reason = f"mapping literal key {key} shadows the public source"
                        value.disconnects.append(
                            {
                                **self._event(current, value_node, kind=kind, reason=reason),
                                "expected_site_symbol": self._expected_env_symbol(env),
                            }
                        )
                        self.edges.append(
                            {
                                "kind": "mapping_literal_shadow",
                                "from_symbol": current.node.name,
                                "to_symbol": current.node.name,
                                "callee_parameter": key,
                                "origins": sorted(value.origins),
                                "site": _node_view(current, value_node, reason=reason),
                            }
                        )
                    mapping[key] = value
            result = _merge(list(mapping.values()))
            result.mapping = mapping
            return result
        if isinstance(expression, ast.Subscript):
            container = self._eval(expression.value, env, current)
            key = _literal_key(expression.slice)
            if container.mapping is not None and key in container.mapping:
                return container.mapping[key].clone()
            return _empty()
        if isinstance(expression, ast.IfExp):
            return _merge([self._eval(expression.body, env, current), self._eval(expression.orelse, env, current)])
        if isinstance(expression, (ast.Tuple, ast.List, ast.Set)):
            return _merge([self._eval(item, env, current) for item in expression.elts])
        if isinstance(expression, ast.BinOp):
            return _merge([self._eval(expression.left, env, current), self._eval(expression.right, env, current)])
        if isinstance(expression, ast.UnaryOp):
            return self._eval(expression.operand, env, current)
        return _empty()

    def _assign(
        self,
        target: ast.AST,
        value_node: ast.AST,
        env: dict[str, AbstractValue],
        current: FunctionRecord,
    ) -> None:
        value = self._eval(value_node, env, current)
        if isinstance(target, ast.Name):
            env[target.id] = value
            return
        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
            key = _literal_key(target.slice)
            container = env.get(target.value.id)
            if key is None or container is None or container.mapping is None:
                return
            previous = container.mapping.get(key, _empty())
            if (
                key == self.target_parameter
                and self.expected_origin in previous.origins
                and self.expected_origin not in value.origins
            ):
                reason = f"mapping key {key} is overwritten from a non-public source"
                value.disconnects.append(
                    {
                        **self._event(
                            current,
                            value_node,
                            kind="mapping_key_overwrite",
                            reason=reason,
                        ),
                        "expected_site_symbol": self._expected_env_symbol(env),
                    }
                )
                self.edges.append(
                    {
                        "kind": "mapping_overwrite",
                        "from_symbol": current.node.name,
                        "to_symbol": current.node.name,
                        "callee_parameter": key,
                        "origins": sorted(value.origins),
                        "site": _node_view(current, value_node, reason=reason),
                    }
                )
            container.mapping[key] = value.clone()
            refreshed = _merge(list(container.mapping.values()))
            container.origins = refreshed.origins
            container.disconnects = refreshed.disconnects
            container.trace = refreshed.trace

    def _execute(self, record: FunctionRecord, initial: dict[str, AbstractValue]) -> AbstractValue:
        env = {name: value.clone() for name, value in initial.items()}
        for statement in record.node.body:
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                self._assign(statement.targets[0], statement.value, env, record)
            elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                self._assign(statement.target, statement.value, env, record)
            elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                self._eval(statement.value, env, record)
            elif isinstance(statement, ast.Return):
                value = self._eval(statement.value, env, record) if statement.value is not None else _empty()
                target_input = env.get(self.target_parameter, _empty())
                if (
                    self.expected_origin in target_input.origins
                    and self.expected_origin not in value.origins
                    and not value.disconnects
                    and statement.value is not None
                ):
                    reason = f"{record.node.name} receives {self.target_parameter} but returns another source"
                    event = self._event(
                        record,
                        statement.value,
                        kind="intermodule_return_reset",
                        reason=reason,
                    )
                    event["expected_site_symbol"] = self._expected_env_symbol(env)
                    value.disconnects.append(event)
                self.edges.append(
                    {
                        "kind": "function_return",
                        "from_symbol": record.node.name,
                        "to_symbol": "<caller>",
                        "callee_parameter": self.target_parameter,
                        "origins": sorted(value.origins),
                        "site": _node_view(record, statement, reason=f"returns from {record.node.name}"),
                    }
                )
                return value
        return _empty()

    def run(self) -> list[dict[str, Any]]:
        initial = {
            name: AbstractValue(origins={f"{self.entry.node.name}:{name}"})
            for name in _parameters(self.entry.node)
        }
        self.active.add(self.entry.node.name)
        try:
            self._execute(self.entry, initial)
        finally:
            self.active.remove(self.entry.node.name)
        return self.sinks


def analyze_multihop_parameter(
    package_root: str | Path,
    *,
    entry_symbol: str,
    parameter: str,
    expected_source_parameter: str | None = None,
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    functions, trees, failures = _index_package(package)
    entry = functions.get(entry_symbol)
    if failures or entry is None:
        return {
            "status": "ABSTAIN",
            "reason": "parse_failure_or_entry_missing",
            "parse_failures": failures,
        }
    source = expected_source_parameter or parameter
    interpreter = _Interpreter(
        functions,
        entry=entry,
        target_parameter=parameter,
        expected_source_parameter=source,
    )
    sinks = interpreter.run()
    if len(sinks) != 1:
        return {
            "status": "ABSTAIN",
            "reason": "terminal_operation_sink_not_unique",
            "sink_count": len(sinks),
            "sinks": sinks,
        }
    sink = sinks[0]
    return {
        "status": "READY",
        "entry_symbol": entry_symbol,
        "entry_path": entry.path,
        "parameter": parameter,
        "expected_source_parameter": source,
        "expected_origin": f"{entry_symbol}:{source}",
        "flow_closed": sink["flow_closed"],
        "origin_pure": sink["origin_pure"],
        "sink": sink,
        "edges": interpreter.edges,
        "module_count": len(trees),
    }


def _matched_parameter(
    entry: ast.FunctionDef | ast.AsyncFunctionDef,
    parameter: str,
    observed_origins: list[str],
) -> str | None:
    parameters = _parameters(entry)
    prefix = f"{entry.name}:"
    observed = sorted(
        {
            origin.removeprefix(prefix)
            for origin in observed_origins
            if origin.startswith(prefix)
            and origin.removeprefix(prefix) in parameters
            and origin.removeprefix(prefix) != parameter
        }
    )
    if len(observed) == 1:
        return observed[0]
    candidates = [
        name
        for name in parameters
        if name != parameter
        and name not in {"input_path", "output_path", "records", "rows"}
        and (name.startswith("fallback_") or name.startswith("backup_"))
    ]
    return candidates[0] if len(candidates) == 1 else None


def build_visible_multihop_facts(package_root: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    package = Path(package_root).resolve()
    functions, trees, failures = _index_package(package)
    findings: list[dict[str, Any]] = []
    for entry in sorted(functions.values(), key=lambda row: (row.path, row.node.name)):
        if not entry.node.name.startswith("run_"):
            continue
        for parameter in _optional_parameters(entry.node):
            if parameter in {"input_path", "output_path", "path", "source", "destination"}:
                continue
            analysis = analyze_multihop_parameter(
                package,
                entry_symbol=entry.node.name,
                parameter=parameter,
            )
            if analysis.get("status") != "READY" or analysis.get("flow_closed"):
                continue
            disconnects = analysis["sink"].get("disconnects") or []
            if not disconnects:
                continue
            site = disconnects[-1]
            decoy = _matched_parameter(entry.node, parameter, analysis["sink"]["origins"])
            if decoy is None:
                continue
            expected_site_symbol = site.get("expected_site_symbol") or parameter
            observed_site_symbol = site.get("observed_site_symbol") or decoy
            if not expected_site_symbol or not observed_site_symbol:
                continue
            findings.append(
                {
                    "kind": site["kind"],
                    "parameter": parameter,
                    "decoy_parameter": decoy,
                    "expected_site_symbol": expected_site_symbol,
                    "observed_site_symbol": observed_site_symbol,
                    "caller": entry.node.name,
                    "callee": analysis["sink"]["callee"],
                    "confidence": 0.97,
                    "evidence": site["site"]["reason"],
                    "entry": {"path": entry.path, "symbol": entry.node.name},
                    "sink": {
                        "path": analysis["sink"]["path"],
                        "symbol": analysis["sink"]["callee"],
                        "keyword": parameter,
                        "call_site": analysis["sink"]["call_site"],
                    },
                    "site": site["site"],
                    "observed_origins": analysis["sink"]["origins"],
                    "trace": analysis.get("edges") or [],
                    "path_length_lower_bound": 3,
                }
            )
    status = (
        "single_visible_disconnect"
        if len(findings) == 1
        else "no_visible_disconnect"
        if not findings
        else "ambiguous_visible_disconnects"
    )
    packet = {
        "schema_version": SCHEMA_VERSION,
        "package_tree_hash": tree_hash(package),
        "provenance": {
            "uses_expected_output": False,
            "uses_gold_package": False,
            "uses_oracle": False,
            "uses_task_verifier": False,
            "uses_reward": False,
        },
        "parser": "python-ast-multimodule-symbolic-defuse-v1",
        "parse_failures": failures,
        "def_use_graph": {
            "backend": "multihop_symbolic",
            "status": status,
            "parameter": findings[0]["parameter"] if len(findings) == 1 else None,
            "findings": findings,
        },
        "claim_boundary": (
            "The packet establishes only visible symbolic parameter provenance across package-local Python "
            "calls. It does not establish task correctness or expected outputs."
        ),
    }
    packet["packet_hash"] = stable_hash(packet)
    audit = {
        "schema_version": SCHEMA_VERSION,
        "package_root": str(package),
        "python_module_count": len(trees),
        "unique_function_count": len(functions),
        "finding_count": len(findings),
        "finding_kinds": [finding["kind"] for finding in findings],
        "packet_hash": packet["packet_hash"],
        "private_assets_loaded": False,
        "task_verifier_used": False,
    }
    return packet, audit


def _skill_root(case: Path) -> Path | None:
    roots = [
        path
        for path in sorted((case / "task" / "environment" / "skills").glob("*"))
        if path.is_dir() and (path / "SKILL.md").is_file()
    ]
    return roots[0] if len(roots) == 1 else None


def extract_visible_multihop_contract(case_root: str | Path) -> dict[str, Any]:
    case = Path(case_root).resolve()
    parent = _skill_root(case)
    facts_path = case / "evolution" / "AST_FACTS.json"
    if parent is None or not facts_path.is_file():
        return {"status": "ABSTAIN", "reason": "skill_or_facts_missing", "answer_free": True}
    facts = read_json(facts_path)
    provenance = facts.get("provenance") or {}
    if any(
        provenance.get(key)
        for key in ("uses_expected_output", "uses_gold_package", "uses_oracle", "uses_task_verifier", "uses_reward")
    ):
        return {"status": "ABSTAIN", "reason": "facts_not_hidden_isolated", "answer_free": False}
    graph = facts.get("def_use_graph") or {}
    findings = graph.get("findings") or []
    if graph.get("backend") != "multihop_symbolic" or graph.get("status") != "single_visible_disconnect" or len(findings) != 1:
        return {"status": "ABSTAIN", "reason": "unique_multihop_disconnect_missing", "answer_free": True}
    finding = findings[0]
    contract = {
        "schema_version": "bvi.multihop_visible_contract.v1",
        "status": "READY",
        "reason": "unique_visible_multihop_parameter_disconnect",
        "kind": finding["kind"],
        "parameter": finding["parameter"],
        "decoy_parameter": finding["decoy_parameter"],
        "expected_site_symbol": finding.get("expected_site_symbol", finding["parameter"]),
        "observed_site_symbol": finding.get("observed_site_symbol", finding["decoy_parameter"]),
        "caller": finding["caller"],
        "callee": finding["callee"],
        "entry": finding["entry"],
        "sink": finding["sink"],
        "site": finding["site"],
        "parent_skill_root": str(parent),
        "facts_packet_hash": facts["packet_hash"],
        "answer_free": True,
        "task_verifier_used": False,
        "hidden_artifacts_used": False,
    }
    contract["contract_hash"] = stable_hash(contract)
    return contract


def _argument_default_dump(function: ast.FunctionDef | ast.AsyncFunctionDef, parameter: str) -> str | None:
    positional = [*function.args.posonlyargs, *function.args.args]
    names = [argument.arg for argument in positional]
    if parameter in names:
        index = names.index(parameter)
        first_default = len(positional) - len(function.args.defaults)
        return "<required>" if index < first_default else _canonical_dump(function.args.defaults[index - first_default])
    names = [argument.arg for argument in function.args.kwonlyargs]
    if parameter not in names:
        return None
    default = function.args.kw_defaults[names.index(parameter)]
    return "<required>" if default is None else _canonical_dump(default)


def _find_node_path(root: ast.AST, target: ast.AST) -> list[tuple[str, int | None]] | None:
    if root is target:
        return []
    for field_name, value in ast.iter_fields(root):
        if isinstance(value, ast.AST):
            found = _find_node_path(value, target)
            if found is not None:
                return [(field_name, None), *found]
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if not isinstance(item, ast.AST):
                    continue
                found = _find_node_path(item, target)
                if found is not None:
                    return [(field_name, index), *found]
    return None


def _node_at_path(root: ast.AST, path: list[tuple[str, int | None]]) -> ast.AST | None:
    current: Any = root
    for field_name, index in path:
        current = getattr(current, field_name, None)
        if index is not None:
            if not isinstance(current, list) or index >= len(current):
                return None
            current = current[index]
        if not isinstance(current, ast.AST):
            return None
    return current


def _replace_at_path(root: ast.AST, path: list[tuple[str, int | None]], replacement: ast.AST) -> bool:
    if not path:
        return False
    current: Any = root
    for field_name, index in path[:-1]:
        current = getattr(current, field_name, None)
        if index is not None:
            if not isinstance(current, list) or index >= len(current):
                return False
            current = current[index]
        if not isinstance(current, ast.AST):
            return False
    field_name, index = path[-1]
    if index is None:
        setattr(current, field_name, copy.deepcopy(replacement))
    else:
        values = getattr(current, field_name, None)
        if not isinstance(values, list) or index >= len(values):
            return False
        values[index] = copy.deepcopy(replacement)
    return True


def _delete_at_path(root: ast.AST, path: list[tuple[str, int | None]]) -> bool:
    if not path:
        return False
    current: Any = root
    for field_name, index in path[:-1]:
        current = getattr(current, field_name, None)
        if index is not None:
            if not isinstance(current, list) or index >= len(current):
                return False
            current = current[index]
        if not isinstance(current, ast.AST):
            return False
    field_name, index = path[-1]
    values = getattr(current, field_name, None)
    if index is None or not isinstance(values, list) or index >= len(values):
        return False
    values.pop(index)
    return True


def _delete_dict_entry_at_value_path(
    root: ast.AST,
    path: list[tuple[str, int | None]],
) -> bool:
    current: Any = root
    for position, (field_name, index) in enumerate(path):
        if (
            isinstance(current, ast.Dict)
            and field_name == "values"
            and index is not None
            and position == len(path) - 1
            and index < len(current.values)
            and index < len(current.keys)
        ):
            current.values.pop(index)
            current.keys.pop(index)
            return True
        current = getattr(current, field_name, None)
        if index is not None:
            if not isinstance(current, list) or index >= len(current):
                return False
            current = current[index]
        if not isinstance(current, ast.AST):
            return False
    return False


def _enclosing_statement_path(
    root: ast.AST,
    path: list[tuple[str, int | None]],
) -> list[tuple[str, int | None]] | None:
    current: Any = root
    prefix: list[tuple[str, int | None]] = []
    statement_path: list[tuple[str, int | None]] | None = None
    for field_name, index in path:
        current = getattr(current, field_name, None)
        if index is not None:
            if not isinstance(current, list) or index >= len(current):
                return None
            current = current[index]
        if not isinstance(current, ast.AST):
            return None
        prefix.append((field_name, index))
        if isinstance(current, ast.stmt):
            statement_path = list(prefix)
    return statement_path


def _entry_parameter_is_non_null(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    parameter: str,
) -> bool:
    arguments = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
    argument = next((row for row in arguments if row.arg == parameter), None)
    if argument is None or argument.annotation is None:
        return False
    annotation = ast.unparse(argument.annotation)
    if "None" in annotation or "Optional" in annotation:
        return False
    default = _argument_default_dump(function, parameter)
    return default not in {None, "<required>", _canonical_dump(ast.Constant(value=None))}


def _is_guarded_primary_fallback(
    node: ast.AST | None,
    *,
    primary: str,
    fallback: str,
) -> bool:
    if not isinstance(node, ast.IfExp):
        return False
    test = node.test
    return bool(
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == primary
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.IsNot)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value is None
        and isinstance(node.body, ast.Name)
        and node.body.id == primary
        and isinstance(node.orelse, ast.Name)
        and node.orelse.id == fallback
    )


def _site_node(tree: ast.Module, site: dict[str, Any]) -> ast.AST | None:
    matches = [
        node
        for node in ast.walk(tree)
        if type(node).__name__ == site["node_type"]
        and int(getattr(node, "lineno", -1)) == int(site["line"])
        and int(getattr(node, "col_offset", -1)) == int(site["column"])
    ]
    return matches[0] if len(matches) == 1 else None


def evaluate_visible_multihop_candidate(
    case_root: str | Path,
    candidate_root: str | Path | None,
    *,
    sham: bool = False,
) -> dict[str, Any]:
    case = Path(case_root).resolve()
    contract = extract_visible_multihop_contract(case)
    base = {
        "schema_version": "bvi.multihop_visible_gate.v1",
        "contract": contract,
        "sham": sham,
        "answer_free": True,
        "task_verifier_used": False,
        "hidden_artifacts_used": False,
    }
    if candidate_root is None:
        report = {**base, "decision": REJECT_MULTIHOP, "decision_reason": "candidate_unavailable"}
        report["report_hash"] = stable_hash(report)
        return report
    if contract.get("status") != "READY":
        report = {**base, "decision": ABSTAIN_MULTIHOP, "decision_reason": contract.get("reason")}
        report["report_hash"] = stable_hash(report)
        return report
    candidate = Path(candidate_root).resolve()
    parent = Path(contract["parent_skill_root"])
    parent_functions, parent_trees, parent_failures = _index_package(parent)
    candidate_functions, candidate_trees, candidate_failures = _index_package(candidate)
    expected_entry_source = contract["decoy_parameter"] if sham else contract["parameter"]
    expected_source = (
        contract.get("observed_site_symbol", contract["decoy_parameter"])
        if sham
        else contract.get("expected_site_symbol", contract["parameter"])
    )
    flow = analyze_multihop_parameter(
        candidate,
        entry_symbol=contract["entry"]["symbol"],
        parameter=contract["parameter"],
        expected_source_parameter=expected_entry_source,
    )
    target_path = contract["site"]["path"]
    generic_structure = (
        not parent_failures
        and not candidate_failures
        and set(parent_trees) == set(candidate_trees)
        and (candidate / "SKILL.md").is_file()
        and (parent / "SKILL.md").is_file()
        and target_path in parent_trees
    )
    if not generic_structure:
        report = {
            **base,
            "decision": REJECT_MULTIHOP,
            "decision_reason": "generic_structure_failure",
            "parent_parse_failures": parent_failures,
            "candidate_parse_failures": candidate_failures,
        }
        report["report_hash"] = stable_hash(report)
        return report

    parent_tree = parent_trees[target_path]
    candidate_tree = candidate_trees[target_path]
    parent_site = _site_node(parent_tree, contract["site"])
    path = _find_node_path(parent_tree, parent_site) if parent_site is not None else None
    candidate_site = _node_at_path(candidate_tree, path) if path is not None else None
    replacement_exact = isinstance(candidate_site, ast.Name) and candidate_site.id == expected_source
    fallback_source = (
        contract.get("expected_site_symbol", contract["parameter"])
        if sham
        else contract.get("observed_site_symbol", contract["decoy_parameter"])
    )
    entry_parent = parent_functions.get(contract["entry"]["symbol"])
    entry_candidate = candidate_functions.get(contract["entry"]["symbol"])
    expected_source_non_null = bool(
        entry_parent is not None
        and _entry_parameter_is_non_null(entry_parent.node, expected_entry_source)
    )
    guarded_primary_fallback = bool(
        expected_source_non_null
        and _is_guarded_primary_fallback(
            candidate_site,
            primary=expected_source,
            fallback=fallback_source,
        )
    )
    normalized = copy.deepcopy(candidate_tree)
    target_node_only = bool(
        path is not None
        and parent_site is not None
        and _replace_at_path(normalized, path, parent_site)
        and _canonical_dump(normalized) == _canonical_dump(parent_tree)
        and all(
            path_name == target_path
            or _canonical_dump(candidate_trees[path_name]) == _canonical_dump(parent_trees[path_name])
            for path_name in parent_trees
        )
    )
    statement_path = _enclosing_statement_path(parent_tree, path) if path is not None else None
    deletion_variant = copy.deepcopy(parent_tree)
    disconnected_statement_deleted = bool(
        contract["kind"] == "mapping_key_overwrite"
        and statement_path is not None
        and _delete_at_path(deletion_variant, statement_path)
        and _canonical_dump(deletion_variant) == _canonical_dump(candidate_tree)
    )
    dict_entry_deletion_variant = copy.deepcopy(parent_tree)
    disconnected_dict_entry_deleted = bool(
        contract["kind"] in {"dict_unpack_shadow", "mapping_literal_shadow"}
        and path is not None
        and _delete_dict_entry_at_value_path(dict_entry_deletion_variant, path)
        and _canonical_dump(dict_entry_deletion_variant) == _canonical_dump(candidate_tree)
    )
    default_preserved = bool(
        entry_parent is not None
        and entry_candidate is not None
        and _argument_default_dump(entry_parent.node, contract["parameter"])
        == _argument_default_dump(entry_candidate.node, contract["parameter"])
    )
    docs_preserved = (candidate / "SKILL.md").read_bytes() == (parent / "SKILL.md").read_bytes()
    parent_hashes = hash_tree(parent)
    candidate_hashes = hash_tree(candidate)
    changed_paths = sorted(
        path_name
        for path_name in set(parent_hashes) | set(candidate_hashes)
        if parent_hashes.get(path_name) != candidate_hashes.get(path_name)
    )
    changed_path_bounded = bool(changed_paths) and set(changed_paths) == {target_path}
    flow_closed = flow.get("status") == "READY" and bool(flow.get("flow_closed"))
    origin_pure = bool(flow.get("origin_pure"))
    flow_contract_satisfied = flow_closed and (origin_pure or guarded_primary_fallback)
    bounded_flow_rewrite = bool(
        replacement_exact
        or guarded_primary_fallback
        or disconnected_statement_deleted
        or disconnected_dict_entry_deleted
    )
    target_scope_only = (
        target_node_only
        or disconnected_statement_deleted
        or disconnected_dict_entry_deleted
    )
    if (
        flow_contract_satisfied
        and bounded_flow_rewrite
        and target_scope_only
        and default_preserved
        and docs_preserved
        and (changed_path_bounded or (sham and not changed_paths))
    ):
        decision = ACCEPT_MULTIHOP
        reason = "visible_multihop_bounded_flow_rewrite_satisfied"
    elif not changed_paths:
        decision = REPAIR_MULTIHOP
        reason = "visible_multihop_parameter_flow_still_disconnected"
    else:
        decision = ABSTAIN_MULTIHOP
        reason = "extra_or_nonconforming_multihop_delta"
    report = {
        **base,
        "decision": decision,
        "decision_reason": reason,
        "expected_source_parameter": expected_source,
        "expected_entry_source_parameter": expected_entry_source,
        "flow_status": flow.get("status"),
        "flow_closed": flow_closed,
        "origin_pure": origin_pure,
        "flow_contract_satisfied": flow_contract_satisfied,
        "observed_origins": (flow.get("sink") or {}).get("origins", []),
        "replacement_exact": replacement_exact,
        "guarded_primary_fallback": guarded_primary_fallback,
        "expected_source_non_null": expected_source_non_null,
        "disconnected_statement_deleted": disconnected_statement_deleted,
        "disconnected_dict_entry_deleted": disconnected_dict_entry_deleted,
        "bounded_flow_rewrite": bounded_flow_rewrite,
        "target_node_only": target_node_only,
        "target_scope_only": target_scope_only,
        "default_preserved": default_preserved,
        "docs_preserved": docs_preserved,
        "changed_path_bounded": changed_path_bounded,
        "changed_paths": changed_paths,
        "candidate_tree_hash": stable_hash(candidate_hashes),
        "claim_boundary": (
            "Acceptance certifies only a bounded visible flow rewrite, public parameter provenance, the public "
            "default, documentation, and unchanged unrelated AST. It is not downstream semantic correctness."
        ),
    }
    report["report_hash"] = stable_hash(report)
    return report
