from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from bvi_skill_evo.balanced_hybrid_ast_v316 import _assert_public_package, _tree_hash
from bvi_skill_evo.public_runtime_multilang_dual_v410 import (
    _dedupe_nodes,
    _enumerate_non_python_nodes,
    _focus_request,
    _node_evidence,
    _tokens,
)
from skillscriptbench.io_utils import canonical_json_hash, sha256_file
from skillscriptbench.multilang_structural_v66 import node_public_view, rank_script_nodes


SCHEMA_VERSION = "skillscriptbench-artifact-scope-ast-v1"
METHOD_ID = "artifact_scope_public_typed_discrepancy_v1"
MAX_EDITABLE_NODES = 4

_FAMILY_PRIORITY = {
    "output_stream_contract": 9,
    "failure_exit_status_contract": 9,
    "comparison_boundary_contract": 8,
    "path_kind_contract": 8,
    "prohibited_environment_fallback": 8,
    "branch_literal_contract": 7,
    "sort_direction_contract": 7,
    "omitted_default_contract": 7,
    "extension_contract": 7,
}


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _observed(node: dict[str, Any]) -> str:
    return str(node.get("observed_source") or "")


def _window(node: dict[str, Any]) -> str:
    return str(node.get("window") or "")


def _source_line(node: dict[str, Any], offset: int = 0) -> str:
    target = int(node.get("line") or 0) + offset
    for row in _window(node).splitlines():
        match = re.match(r"\s*(\d+):\s?(.*)$", row)
        if match and int(match.group(1)) == target:
            return match.group(2)
    return ""


def _identifier_terms(value: str) -> set[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    terms = {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9]+", expanded)
        if len(token) >= 2
    }
    return {token[:-1] if len(token) > 3 and token.endswith("s") else token for token in terms}


def _value_in_focus(value: str, focus: str) -> bool:
    value = value.strip().strip("\"'")
    if not value:
        return False
    if value.startswith("--"):
        return value.casefold() in {
            token.casefold() for token in re.findall(r"--[A-Za-z0-9][A-Za-z0-9-]*", focus)
        }
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_-]){re.escape(value)}(?![A-Za-z0-9_-])",
            focus,
            flags=re.IGNORECASE,
        )
    )


def _request_direction(focus: str) -> str | None:
    lower = focus.casefold()
    if re.search(r"newest[-\s]+first|most\s+relevant|descending", lower):
        return "descending"
    if re.search(r"oldest[-\s]+first|least\s+relevant|ascending", lower):
        return "ascending"
    return None


def _sort_is_wrong(source: str, direction: str) -> bool:
    compact = re.sub(r"\s+", "", source)
    a_index = compact.find("a.")
    b_index = compact.find("b.")
    minus = compact.find("-")
    if min(a_index, b_index, minus) < 0:
        return False
    observed = "ascending" if a_index < minus < b_index else "descending" if b_index < minus < a_index else None
    return observed is not None and observed != direction


def _comparison_boundary_candidates(
    nodes: list[dict[str, Any]], focus: str
) -> list[dict[str, Any]]:
    request_numbers = set(
        re.findall(r"(?<![A-Za-z0-9])(\d{3})s?(?![A-Za-z0-9])", focus)
    )
    result = []
    for node in nodes:
        if node.get("role") != "comparison_operator":
            continue
        facts = node.get("facts") or {}
        operator = str(facts.get("operator") or _observed(node))
        right = str(facts.get("rightSource") or "")
        if operator != ">" or not (request_numbers & set(re.findall(r"\d{3}", right))):
            continue
        if not re.search(r"status|http|put|preview|error|fail", _window(node), re.IGNORECASE):
            continue
        result.append(node)
    return result


def _path_kind_candidates(
    nodes: list[dict[str, Any]], focus: str
) -> list[dict[str, Any]]:
    lower = focus.casefold()
    wants_directory = bool(re.search(r"\bdirector(?:y|ies)\b|\bfolder\b", lower))
    wants_file = bool(re.search(r"\bregular file\b|\bagent file\b|\bfile structure\b", lower))
    result = []
    for node in nodes:
        if node.get("role") != "path_kind_guard":
            continue
        observed = _observed(node).strip()
        if (wants_directory and not wants_file and observed == "-f") or (
            wants_file and not wants_directory and observed == "-d"
        ):
            result.append(node)
    return result


def _exit_status_candidates(
    nodes: list[dict[str, Any]], focus: str
) -> list[dict[str, Any]]:
    if not re.search(r"non[-\s]?zero|exit\s+1|failure", focus, re.IGNORECASE):
        return []
    result = []
    for node in nodes:
        if node.get("role") != "exit_status" or _observed(node).strip() != "0":
            continue
        if not re.match(r"\s*exit\s+0(?:\s|$)", _source_line(node)):
            continue
        preceding = _source_line(node, -1)
        if re.search(r"\b(?:echo|printf)\b", preceding) and re.search(
            r"error|fail|invalid|required|missing|not found|does not look|refus",
            preceding,
            re.IGNORECASE,
        ):
            result.append(node)
    return result


def _output_stream_candidates(
    nodes: list[dict[str, Any]], focus: str
) -> list[dict[str, Any]]:
    if not re.search(r"stdout|stderr|json|piping|parsing", focus, re.IGNORECASE):
        return []
    return [
        node
        for node in nodes
        if node.get("role") in {"output_stream_redirection", "output_stream_route"}
        and _observed(node).strip() in {"1>&1", ">&1"}
        and re.search(
            r"error|failed|unreachable|unknown|refusing",
            _source_line(node),
            re.IGNORECASE,
        )
    ]


def _environment_fallback_candidates(
    nodes: list[dict[str, Any]], focus: str
) -> list[dict[str, Any]]:
    if not re.search(r"no\s+silent\s+default|never\s+auto|must\s+elicit|must\s+ask", focus, re.IGNORECASE):
        return []
    normalized_focus = re.sub(r"[^a-z0-9]", "", focus.casefold())
    result = []
    for node in nodes:
        if node.get("role") != "parameter_expansion":
            continue
        observed = _observed(node)
        match = re.fullmatch(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*):-.*\}", observed)
        if not match:
            continue
        binding = re.sub(r"[^a-z0-9]", "", match.group("name").casefold())
        if binding and binding in normalized_focus:
            result.append(node)
    return _dedupe_nodes(result)


def _omitted_default_candidates(
    nodes: list[dict[str, Any]], focus: str
) -> list[dict[str, Any]]:
    lower = focus.casefold()
    if "default" not in lower:
        return []
    default_contexts = [
        row
        for row in re.split(r"[\n.!?]+", focus)
        if re.search(r"\bdefault\b", row, re.IGNORECASE)
    ]
    result = []
    for node in nodes:
        if node.get("role") not in {"binding_initializer", "binding_assignment"}:
            continue
        observed = _observed(node)
        facts = node.get("facts") or {}
        binding = str(facts.get("bindingName") or "")
        lacks_fallback = not re.search(r"\|\||\?\?|\$\{[^}]*:-", observed)
        simple_positional = bool(re.fullmatch(r"[^\n]*\$\{\d+\}[^\n]*", observed))
        dynamic_source = bool(
            re.search(r"\b(?:args|argv|positionals)\b|parse(?:Int|Float)?\s*\(|\$\{\d+\}", observed)
        )
        binding_terms = _identifier_terms(binding)
        binding_linked = False
        for context in default_contexts:
            prefix = re.split(r"\bdefault\b", context, maxsplit=1, flags=re.IGNORECASE)[0]
            subjects = re.findall(r"--[A-Za-z0-9-]+|<[A-Za-z0-9_-]+>", prefix)
            for subject in subjects:
                value = subject.strip("<>")
                subject_terms = _identifier_terms(value)
                binding_key = "".join(sorted(binding_terms))
                subject_key = "".join(sorted(subject_terms))
                if binding_key and binding_key == subject_key:
                    binding_linked = True
                    break
            if binding_linked:
                break
        if lacks_fallback and dynamic_source and (simple_positional or binding_linked):
            result.append(node)
    return _dedupe_nodes(result)


def _extension_candidates(
    nodes: list[dict[str, Any]], focus: str
) -> list[dict[str, Any]]:
    request_extensions = {
        value.casefold() for value in re.findall(r"\.[A-Za-z0-9]{2,8}\b", focus)
    }
    if not request_extensions:
        return []
    result = []
    for node in nodes:
        if node.get("role") != "path_suffix":
            continue
        observed_extensions = {
            value.casefold()
            for value in re.findall(r"\.[A-Za-z0-9]{2,8}\b", _observed(node))
        }
        if not observed_extensions or not observed_extensions.isdisjoint(request_extensions):
            continue
        binding = str((node.get("facts") or {}).get("enclosingBinding") or "")
        requested_kinds = {value.lstrip(".") for value in request_extensions}
        if _identifier_terms(binding) & requested_kinds:
            result.append(node)
    return _dedupe_nodes(result)


def _branch_literal_candidates(
    nodes: list[dict[str, Any]], focus: str
) -> list[dict[str, Any]]:
    focus_tokens = _tokens(focus)
    literals = [
        node
        for node in nodes
        if node.get("role") == "literal"
        and str((node.get("facts") or {}).get("parentType") or "")
        in {"BinaryExpression", "SwitchCase"}
    ]
    by_value: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for node in literals:
        value = _observed(node).strip().strip("\"'").casefold()
        by_value.setdefault(
            (str(node.get("path") or ""), str(node.get("symbol") or ""), value), []
        ).append(node)
    result: list[dict[str, Any]] = []
    for (_, _, value), siblings in by_value.items():
        if len(siblings) <= 1 or not value.startswith("--"):
            continue
        value_terms = _identifier_terms(value)
        for node in siblings:
            context = _source_line(node)
            assignment = re.search(
                r"(?:args|options|opts)\.([A-Za-z_][A-Za-z0-9_]*)\s*=", context
            )
            if assignment and not (value_terms & _identifier_terms(assignment.group(1))):
                result.append(node)

    request_choice_sets = []
    for match in re.finditer(
        r"--[A-Za-z0-9-]+\s+([A-Za-z][A-Za-z0-9-]*(?:\|[A-Za-z][A-Za-z0-9-]*)+)",
        focus,
    ):
        request_choice_sets.append(
            {value.casefold() for value in match.group(1).split("|")}
        )
    by_expression: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for node in literals:
        facts = node.get("facts") or {}
        expression = str(facts.get("grandparentSource") or "")
        if facts.get("parentType") == "BinaryExpression" and expression:
            by_expression.setdefault(
                (str(node.get("path") or ""), str(node.get("symbol") or ""), expression),
                [],
            ).append(node)
    for siblings in by_expression.values():
        values = {
            str((node.get("facts") or {}).get("value") or "").casefold(): node
            for node in siblings
            if isinstance((node.get("facts") or {}).get("value"), str)
        }
        for requested in request_choice_sets:
            if values.keys() & requested and requested - values.keys() and values.keys() - requested:
                result.extend(values[value] for value in values.keys() - requested)

    cli_terms: set[str] = set()
    for row in focus.splitlines():
        if not re.search(r"\b(?:node|bun|deno)\b", row):
            continue
        for token in re.findall(r"[A-Za-z][A-Za-z0-9-]*", row):
            lowered = token.casefold()
            if lowered in {"node", "bun", "deno", "json"} or lowered.startswith("--"):
                continue
            if re.search(rf"(?:/|\.){re.escape(token)}(?:/|\.|\s|$)", row, re.IGNORECASE):
                continue
            cli_terms.update(_identifier_terms(token))
    for node in literals:
        value = _observed(node).strip().strip("\"'").casefold()
        facts = node.get("facts") or {}
        if (
            not value
            or not isinstance(facts.get("value"), str)
            or (len(value) < 3 and not value.startswith("--"))
            or facts.get("parentType") != "BinaryExpression"
            or _value_in_focus(value, focus)
        ):
            continue
        context = str(facts.get("grandparentSource") or "")
        left_source = str(facts.get("leftSource") or "").casefold()
        if not left_source:
            parent_match = re.match(
                r"\s*([A-Za-z_$][A-Za-z0-9_$.]*)\s*(?:===|!==|==|!=)",
                str(facts.get("parentSource") or ""),
            )
            left_source = parent_match.group(1).casefold() if parent_match else ""
        if (
            value.startswith("--")
            and "command" in left_source
            and (_identifier_terms(context) & cli_terms)
        ):
            result.append(node)
    ranked = sorted(
        _dedupe_nodes(result),
        key=lambda node: (
            -len(_tokens(_window(node)) & focus_tokens),
            str(node["path"]),
            int(node["byte_span"]["start"]),
        ),
    )
    return ranked[:MAX_EDITABLE_NODES]


def _sort_candidates(
    nodes: list[dict[str, Any]], focus: str
) -> list[dict[str, Any]]:
    direction = _request_direction(focus)
    if direction is None:
        return []
    return [
        node
        for node in nodes
        if node.get("role") == "sort_comparator"
        and _sort_is_wrong(_observed(node), direction)
    ]


def _obligations(
    nodes: list[dict[str, Any]], ranked: list[dict[str, Any]], request_text: str
) -> list[dict[str, Any]]:
    focus = _focus_request(request_text)
    builders = (
        ("output_stream_contract", 0.98, _output_stream_candidates),
        ("failure_exit_status_contract", 0.98, _exit_status_candidates),
        ("comparison_boundary_contract", 0.95, _comparison_boundary_candidates),
        ("path_kind_contract", 0.95, _path_kind_candidates),
        ("prohibited_environment_fallback", 0.96, _environment_fallback_candidates),
        ("branch_literal_contract", 0.92, _branch_literal_candidates),
        ("sort_direction_contract", 0.96, _sort_candidates),
        ("omitted_default_contract", 0.94, _omitted_default_candidates),
        ("extension_contract", 0.96, _extension_candidates),
    )
    rank = {str(row["site_id"]): index for index, row in enumerate(ranked)}
    result = []
    for family, confidence, builder in builders:
        candidates = _dedupe_nodes(builder(nodes, focus))
        candidates.sort(key=lambda row: rank.get(str(row["site_id"]), 10**9))
        if candidates:
            result.append(
                {
                    "family": family,
                    "confidence": confidence,
                    "editable": candidates[:MAX_EDITABLE_NODES],
                    "reasons": [
                        "typed_runtime_role",
                        "public_request_and_script_contract_disagree",
                    ],
                }
            )
    result.sort(
        key=lambda row: (
            _FAMILY_PRIORITY[row["family"]],
            row["confidence"],
            -min(rank.get(str(node["site_id"]), 10**9) for node in row["editable"]),
        ),
        reverse=True,
    )
    return result


def _editable_view(
    node: dict[str, Any], *, package: Path, family: str, rank: int, reasons: list[str]
) -> dict[str, Any]:
    result = node_public_view(node)
    result.update(
        {
            "file_sha256": sha256_file(package / str(node["path"])),
            "byte_span": {
                "start": int(node["byte_span"]["start"]),
                "end": int(node["byte_span"]["end"]),
            },
            "rank": rank,
            "family": family,
            "family_priority": _FAMILY_PRIORITY[family],
            "localization_score": float(node.get("localization_score") or 0.0),
            "selection_reasons": reasons,
            "selection_evidence": _node_evidence(node),
        }
    )
    return result


def build_public_multilang_contract_set(
    package_root: str | Path, request_text: str
) -> dict[str, Any]:
    package = Path(package_root).resolve()
    _assert_public_package(package)
    nodes = _enumerate_non_python_nodes(package)
    ranked = rank_script_nodes(nodes, request_text, package)
    obligations = _obligations(nodes, ranked, request_text)
    selected = obligations[0] if obligations else None
    selected_family = str(selected["family"]) if selected else None
    rank_by_id = {str(row["site_id"]): index + 1 for index, row in enumerate(ranked)}
    editable = [
        _editable_view(
            node,
            package=package,
            family=selected_family or "branch_literal_contract",
            rank=rank_by_id.get(str(node["site_id"]), len(ranked) + 1),
            reasons=list(selected.get("reasons") or []) if selected else [],
        )
        for node in list(selected.get("editable") or [])[:MAX_EDITABLE_NODES]
    ] if selected else []
    decision = "PROPOSE" if editable else "ABSTAIN"
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "source_scope": "public_request_and_runtime_scripts_only",
        "package_tree_hash": _tree_hash(package),
        "localization_decision": {
            "decision": decision,
            "selected_family": selected_family,
            "confidence": float(selected.get("confidence") or 0.0) if selected else 0.0,
            "editable_node_count": len(editable),
            "alternative_family_count": max(0, len(obligations) - 1),
            "abstain_reason": None if editable else "no_public_typed_discrepancy",
        },
        "obligation": (
            {
                "family": selected_family,
                "confidence": float(selected.get("confidence") or 0.0),
                "repair_goal": "Reconcile only the listed executable nodes with the public request and package contract.",
                "reasons": list(selected.get("reasons") or []),
            }
            if selected
            else None
        ),
        "editable_nodes": editable,
        "context_nodes": [],
        "alternative_families": [
            {
                "family": row["family"],
                "confidence": float(row["confidence"]),
                "editable_node_count": len(row["editable"]),
            }
            for row in obligations[1:6]
        ],
        "counts": {
            "non_python_script_node_count": len(nodes),
            "ranked_node_count": len(ranked),
            "typed_discrepancy_family_count": len(obligations),
            "editable_node_count": len(editable),
        },
        "edit_contract": {
            "maximum_edits": 2,
            "exact_node_binding_required": True,
            "outside_selected_nodes_preserved_by_construction": True,
            "semantic_correctness_inferred": False,
        },
        "benchmark_bundled_facts_consumed": False,
        "hidden_artifacts_consumed": False,
        "task_verifier_consumed": False,
        "gold_or_oracle_consumed": False,
        "reward_consumed": False,
        "semantic_correctness_inferred": False,
        "claim_boundary": (
            "The packet identifies public request-to-code structural discrepancies and bounds executable edits. "
            "It does not reveal hidden labels or replacement code."
        ),
    }
    result["facts_hash"] = canonical_json_hash(result)
    return result


def validate_public_multilang_contract_set(
    facts: dict[str, Any], package_root: str | Path
) -> str:
    body = dict(facts)
    expected = str(body.pop("facts_hash", ""))
    if not expected or canonical_json_hash(body) != expected:
        raise ValueError("artifact_scope_facts_hash_invalid")
    if facts.get("method") != METHOD_ID:
        raise ValueError("artifact_scope_method_invalid")
    if facts.get("package_tree_hash") != _tree_hash(Path(package_root).resolve()):
        raise ValueError("artifact_scope_package_hash_invalid")
    if any(
        facts.get(field) is not False
        for field in (
            "benchmark_bundled_facts_consumed",
            "hidden_artifacts_consumed",
            "task_verifier_consumed",
            "gold_or_oracle_consumed",
            "reward_consumed",
        )
    ):
        raise ValueError("artifact_scope_nonpublic_input_flag")
    editable = list(facts.get("editable_nodes") or [])
    if (facts.get("localization_decision") or {}).get("decision") == "PROPOSE" and not editable:
        raise ValueError("artifact_scope_propose_without_nodes")
    package = Path(package_root).resolve()
    for node in editable:
        target = package / str(node["path"])
        if not target.is_file() or sha256_file(target) != node.get("file_sha256"):
            raise ValueError("artifact_scope_editable_node_file_invalid")
        data = target.read_bytes()
        start = int(node["byte_span"]["start"])
        end = int(node["byte_span"]["end"])
        if not 0 <= start < end <= len(data):
            raise ValueError("artifact_scope_editable_node_span_invalid")
    return expected
