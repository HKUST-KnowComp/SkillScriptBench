"""LLM-owned discovery compiled against an unranked, parser-owned inventory.

This successor does not consume Native detector packets or their branch plans.
It reuses exact-byte application, scope reconstruction, and parent-delta rebase.
Detector-specific residual verdicts are not reused as semantic truth.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

from bvi_skill_evo import contextual_unified_public_closure_executor_v517 as editor
from bvi_skill_evo import public_exact_node_delta_rebase_v565 as rebase
from bvi_skill_evo import public_syntax_inventory_v1103 as syntax
from skillscriptbench.io_utils import canonical_json_hash, copy_tree_clean, hash_tree

_path = Path(__file__).resolve().parents[2] / "SkillScriptBench/bvi_skill_evo/semantic_obligation_record_frontend_v3.py"
_spec = importlib.util.spec_from_file_location("discovery_source_records_v1104", _path)
records = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(records)
VERSION = "semantic-discovery-native-bridge-v1109-preservation-r1"
TOOL_NAME = "submit_discovered_contracts"
MAX_EDITS = editor.MAX_EDITABLE_NODES


def seal(body, field):
    result = {k: v for k, v in body.items() if k != field}
    result[field] = canonical_json_hash(result)
    return result


def verify(body, field):
    if seal(body, field)[field] != body.get(field):
        raise ValueError("invalid_" + field)


def build_input(request, parent, proposal):
    inventories = {name: syntax.build_inventory(path, name) for name, path in
                   (("parent", parent), ("proposal", proposal))}
    documents, scripts, nodes = {}, {}, {}
    for name, graph in inventories.items():
        documents[name + "/SKILL.md"] = graph["sources"]["SKILL.md"]
        scripts.update({name + "/" + p: s for p, s in graph["sources"].items() if p != "SKILL.md"})
        nodes.update(graph["nodes"])
    aliases, catalog = records.evidence_catalog(request, documents)
    schema = records.schema(sorted(set(aliases.values())), {}, catalog)
    row_schema = schema["properties"]["records"]["items"]
    row_schema["properties"]["code_assessments"] = records.base._object({
        name: records.base._enum(("REPAIR", "SATISFIED", "UNCERTAIN")) for name in inventories})
    row_schema["required"].append("code_assessments")
    location = records.base._object({"source_id": records.base._enum(tuple(scripts)),
        "symbol": {"type": "string"}, "line_hint": {"type": "integer"},
        "node_type": {"type": "string"}, "quote": {"type": "string"}})
    row_schema["properties"]["bindings"] = records.base._array(records.base._object({
        "location": location, "role": records.base._enum(("subject", "object", "scope")),
        "use": records.base._enum(("EDIT", "CONTEXT"))}))
    tool = {"type": "function", "function": {"name": TOOL_NAME, "strict": True,
        "description": "Discover source-grounded contracts and repair sites from the complete public package.",
        "parameters": schema}}
    original = records.build_extraction_prompt(request, documents, scripts=scripts, nodes={})
    instruction, encoded = original.split("\nPUBLIC_RECORD_INPUTS.json\n", 1)
    public = json.loads(encoded)
    public.pop("HOST_NODES")
    public["HOST_INVENTORY"] = {k: {"node_count": len(g["nodes"]), "rank_filter_used": False,
        "files": {p: {"backend": f["backend"], "language": f["language"]} for p, f in g["files"].items()}}
        for k, g in inventories.items()}
    public["CALL_GRAPH_POLICY"] = "Full parsed graph stays host-side; selected caller/helper closure is supplied to revision after location binding."
    public["PARSER_DIAGNOSTICS"] = {k: g["diagnostics"] for k, g in inventories.items()}
    instruction = instruction.replace("Call submit_semantic_records once.", "Call submit_discovered_contracts once.")
    instruction = instruction.replace("Bind only existing HOST_NODES.",
        "Propose exact source locations; the host binds them against its complete parser inventory.")
    instruction += (
        " The host keeps the complete named syntax inventory of supported public scripts, NOT a "
        "fault-ranked shortlist. Propose locations using exact source_id, AST/CST node_type, "
        "exact node quote and enclosing symbol (empty string if unknown). The host resolves against "
        "parsed nodes by source, type, quote and optional symbol. Never count source lines yourself. "
        "Set line_hint to 0 unless you copy a host-displayed source line number. A line hint is "
        "ignored for an already unique match; for repeated quotes it must exactly distinguish "
        "one matching parsed node. The host never shifts lines or picks the nearest node. "
        "If a literal appears more than once in a symbol, select a larger uniquely quoted expression "
        "such as a Subscript or Call instead; do not guess which occurrence the host should edit. "
        "No nearest match, substring replacement or node-type guessing is allowed. Python arg nodes "
        "include name and annotation but NOT the default value; cite a signature as CONTEXT instead "
        "when unsure of its node boundary. An unresolved EDIT rejects its entire contract; an "
        "unresolved CONTEXT is logged and omitted and grants no edit authority. "
        "Discover issues from the full request and package, independent of legacy detector vocabulary. "
        "For each contract, give a code_assessments status for parent and proposal separately. "
        "Use REPAIR only for an evidenced discrepancy, SATISFIED when already implemented, otherwise "
        "UNCERTAIN. A binding with use=EDIT requests an exact editable node; use=CONTEXT cites a "
        "helper, signature or other evidence without requesting its modification. Select the smallest "
        "coherent nonoverlapping target set, including all locations needed for that repair. Do not "
        "request edits to already-correct context. Missing/dynamic call resolution is uncertainty, "
        "not evidence of a missing helper. Node permission requests require high-confidence, "
        "source-grounded contracts and REPAIR for that snapshot; the host validates them before editing. "
        "Do not invent replacement code during discovery. No fallback rule miner is available."
    )
    public["SCRIPT_CONTENT"] = {k: {"numbered_lines": list(enumerate(v.splitlines(keepends=True), 1))} for k, v in public["SCRIPT_CONTENT"].items()}
    instruction += " SCRIPT_CONTENT numbered_lines preserves all source text with host-generated line numbers for navigation only."
    prompt = instruction + "\nPUBLIC_RECORD_INPUTS.json\n" + json.dumps(public, ensure_ascii=True, separators=(",", ":"))
    if len(prompt.encode()) > 1_500_000:
        raise ValueError("full_inventory_prompt_budget_exceeded_no_truncation")
    return seal({"version": VERSION, "request": request, "documents": documents, "scripts": scripts,
        "inventories": inventories, "nodes": nodes, "tool": tool, "prompt": prompt,
        "legacy_shortlist_consumed": False, "legacy_call_plan_consumed": False}, "input_hash")


def _validate_schema(value, schema):
    expected = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int}[schema["type"]]
    if type(value) is not expected:
        raise ValueError("invalid_schema_type")
    if expected is dict:
        if set(value) != set(schema["required"]):
            raise ValueError("invalid_schema_fields")
        for key in value:
            _validate_schema(value[key], schema["properties"][key])
    elif expected is list:
        for item in value:
            _validate_schema(item, schema["items"])
    elif "enum" in schema and value not in schema["enum"]:
        raise ValueError("invalid_schema_enum")


def resolve_location(bundle, location):
    if location["line_hint"] < 0:
        raise ValueError("invalid_line_hint")
    matches = [n for n in bundle["nodes"].values()
        if n["source_id"] == location["source_id"] and n["node_type"] == location["node_type"]
        and n["source"] == location["quote"]
        and (not location["symbol"] or n["symbol"] == location["symbol"])]
    if len(matches) > 1 and location["line_hint"]:
        matches = [n for n in matches if n["line"] == location["line_hint"]]
    if len(matches) != 1:
        raise ValueError("location_not_unique_parser_node")
    return matches[0]


def compile_discovery(bundle, payload):
    verify(bundle, "input_hash")
    if not isinstance(payload, dict) or set(payload) != {"abstain", "abstain_reason", "records"}:
        raise ValueError("invalid_discovery_top_level")
    if type(payload["abstain"]) is not bool or not isinstance(payload["abstain_reason"], str) or not isinstance(payload["records"], list):
        raise ValueError("invalid_discovery_top_level_types")
    if payload["abstain"] and payload["records"]:
        raise ValueError("explicit_abstain_requires_zero_records")
    schema = bundle["tool"]["function"]["parameters"]["properties"]["records"]["items"]
    contracts, issues = {}, []
    duplicate_ids = set()
    for index, original in enumerate(payload["records"]):
        try:
            _validate_schema(original, schema)
            item = copy.deepcopy(original)
            assessments = item.pop("code_assessments")
            uses, bound = {}, {}
            retained = []
            for binding in item["bindings"]:
                try:
                    node = resolve_location(bundle, binding.pop("location"))
                except ValueError:
                    if binding["use"] != "CONTEXT":
                        raise
                    issues.append({"record": index, "reason": "unresolved_context_omitted_no_permission"})
                    continue
                retained.append(binding)
                binding["node_id"] = node["node_id"]
                uses[node["node_id"]] = binding.pop("use")
                bound[node["node_id"]] = node
            item["bindings"] = retained
            if len(uses) != len(item["bindings"]):
                raise ValueError("duplicate_binding")
            packet = records.validate_extraction({"records": [item]}, bundle["request"], bundle["documents"],
                                                scripts=bundle["scripts"], nodes=bound)
            if len(packet["obligations"]) != 1:
                raise ValueError("invalid_contract_evidence")
            if len(packet["bindings"]) != len(item["bindings"]):
                raise ValueError("incomplete_binding_group")
            obligation = packet["obligations"][0]
            oid = obligation["obligation_id"]
            targets = {name: [] for name in bundle["inventories"]}
            for binding in packet["bindings"]:
                node = bundle["nodes"][binding["node_id"]]
                if uses[binding["node_id"]] != "EDIT":
                    continue
                if not node["editable"]:
                    raise ValueError("node_is_context_only")
                if assessments[node["snapshot"]] != "REPAIR" or obligation["confidence"] != "high":
                    raise ValueError("edit_without_high_confidence_repair_assessment")
                targets[node["snapshot"]].append(node["node_id"])
            if oid in contracts:
                duplicate_ids.add(oid)
                issues.append({"record": index, "reason": "duplicate_contract_no_permission"})
            else:
                contracts[oid] = {"semantic": packet, "targets": targets, "code_assessments": assessments}
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            issues.append({"record": index, "reason": str(exc)[:250]})
    for oid in duplicate_ids:
        contracts.pop(oid, None)
    branches = {}
    for snapshot, graph in bundle["inventories"].items():
        selected = sorted({n for c in contracts.values() for n in c["targets"][snapshot]})
        if not selected:
            continue
        reason = None
        if len(selected) > MAX_EDITS:
            reason = "atomic_target_set_exceeds_editor_bound_no_top_k_pruning"
        spans = {}
        for node_id in selected:
            node = bundle["nodes"][node_id]
            start, end = node["byte_span"]["start"], node["byte_span"]["end"]
            prior = spans.setdefault(node["path"], [])
            if any(start < b and a < end for a, b in prior):
                reason = "overlapping_targets_no_silent_retargeting"
            prior.append((start, end))
        if reason:
            issues.append({"snapshot": snapshot, "reason": reason})
            continue
        view = "parent-signal" if snapshot == "parent" else "raw-residual"
        branches[view] = seal({"version": VERSION, "snapshot": snapshot, "view": view,
            "inventory_hash": graph["inventory_hash"], "input_hash": bundle["input_hash"],
            "editable_nodes": [bundle["nodes"][n] for n in selected],
            "contracts": [c for c in contracts.values() if c["targets"][snapshot]],
            "preservation_contracts": [c for c in contracts.values()
                if c["code_assessments"][snapshot] == "SATISFIED"
                and c["semantic"]["obligations"][0]["confidence"] == "high"],
            "call_context": syntax.call_context(graph, selected),
            "discovery_source": "llm_contracts_bound_to_full_syntax_inventory",
            "legacy_rules_used": False, "hidden_artifacts_consumed": False}, "facts_hash")
    return seal({"version": VERSION, "input_hash": bundle["input_hash"], "branches": branches,
        "contracts": contracts, "issues": issues, "model_abstained": payload["abstain"],
        "fallback": "frozen_raw", "semantic_correctness_inferred": False}, "discovery_hash")


def revision_prompt(bundle, packet):
    snapshot = packet["snapshot"]
    return (
        "Maintain this executable skill. Read the complete public request and package. "
        "Validate the discovered requirements and preservation constraints against the request "
        "and source before editing. The host checks edit scope, syntax, and interface preservation. "
        "Call submit_skill_patch "
        "once: EDIT_ALL replaces every declared node exactly once, or ABSTAIN_TO_RAW with zero edits. "
        "Do not edit context-only nodes. Preserve public signatures and unrelated behavior. "
        "No hidden test, reward, oracle or verifier is available.\n"
        + "PUBLIC_REVISION_INPUT.json\n" + json.dumps({"request": bundle["request"],
            "view": packet["view"], "package": bundle["inventories"][snapshot]["sources"],
            "structural_packet": packet}, ensure_ascii=True, indent=2)
    )


def _candidate_checks(source, candidate, edits):
    source_graph = syntax.build_inventory(source, "parent")
    candidate_graph = syntax.build_inventory(candidate, "proposal")
    scope = editor._analyze_exact_candidate_scope(Path(source), Path(candidate), edits)
    old_hashes, new_hashes = hash_tree(source), hash_tree(candidate)
    changed = sorted(p for p in old_hashes.keys() | new_hashes.keys() if old_hashes.get(p) != new_hashes.get(p))
    checks = {"same_file_envelope": set(old_hashes) == set(new_hashes),
        "changed_paths_match_edits": changed == sorted({e["path"] for e in edits}),
        "exact_node_scope": not scope["outside_selected_node_changes"],
        "supported_sources_parse": not candidate_graph["diagnostics"],
        "public_signatures_preserved": {p: v["signatures"] for p, v in source_graph["files"].items()} ==
            {p: v["signatures"] for p, v in candidate_graph["files"].items()}}
    return checks, scope, candidate_graph


def apply_patch_payload(bundle, packet, payload, source, candidate):
    verify(bundle, "input_hash")
    verify(packet, "facts_hash")
    if packet["input_hash"] != bundle["input_hash"]:
        raise ValueError("packet_from_other_public_input")
    graph = bundle["inventories"][packet["snapshot"]]
    syntax.verify_inventory(graph, source)
    if packet["inventory_hash"] != graph["inventory_hash"]:
        raise ValueError("packet_inventory_mismatch")
    for node in packet["editable_nodes"]:
        if node != graph["nodes"].get(node["node_id"]):
            raise ValueError("editable_node_not_in_parser_inventory")
    if Path(candidate).exists() or Path(source).resolve() in Path(candidate).resolve().parents:
        raise ValueError("fresh_separate_candidate_required")
    parsed, _ = editor._parse_payload(json.dumps(payload))
    if parsed["decision"] == editor.ABSTAIN_TO_RAW:
        if parsed["edits"]:
            raise ValueError("abstain_with_edits")
        copy_tree_clean(source, candidate)
        return {"normalized_edits": [], "structural_gate": {"decision": editor.ABSTAIN_TO_RAW},
                "semantic_residual_status": "UNCERTAIN"}
    registry = {n["node_id"]: n for n in packet["editable_nodes"]}
    edits, seen = [], set()
    for row in parsed["edits"]:
        if set(row) != {"target_node_id", "replacement"}:
            raise ValueError("invalid_compact_edit")
        node_id, replacement = row["target_node_id"], row["replacement"]
        if node_id not in registry or node_id in seen:
            raise ValueError("unknown_or_duplicate_edit_node")
        if not isinstance(replacement, str) or not replacement.strip() or len(replacement.encode()) > 4096:
            raise ValueError("invalid_replacement")
        if replacement == registry[node_id]["observed_source"]:
            raise ValueError("noop_edit")
        seen.add(node_id)
        edits.append({**registry[node_id], "replacement": replacement})
    if seen != set(registry):
        raise ValueError("atomic_target_set_required")
    editor._apply_exact_edits(Path(source), Path(candidate), edits)
    checks, scope, candidate_graph = _candidate_checks(source, candidate, edits)
    return {"version": VERSION, "normalized_edits": edits, "scope_gate": scope,
        "candidate_tree_hash": canonical_json_hash(hash_tree(candidate)),
        "structural_gate": {"decision": editor.ACCEPT_STRUCTURALLY if all(checks.values()) else editor.REVISE, "checks": checks},
        "candidate_inventory_hash": candidate_graph["inventory_hash"],
        "semantic_residual_status": "UNCERTAIN", "legacy_component_residual_gates_used": False,
        "semantic_correctness_inferred": False, "hidden_artifacts_consumed": False}


def select(parent, raw, branch_results, destination):
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    for source in [parent, raw] + [r["candidate"] for r in branch_results.values()]:
        if Path(source).resolve() in destination.resolve().parents:
            raise ValueError("selected_output_must_be_outside_inputs")
    for result in branch_results.values():
        if result["application"]["structural_gate"]["decision"] == editor.ACCEPT_STRUCTURALLY and (
            canonical_json_hash(hash_tree(result["candidate"])) != result["application"]["candidate_tree_hash"]):
            raise ValueError("candidate_changed_after_structural_gate")
    base = Path(raw)
    raw_branch = branch_results.get("raw-residual")
    if raw_branch and raw_branch["application"]["structural_gate"]["decision"] == editor.ACCEPT_STRUCTURALLY:
        base = Path(raw_branch["candidate"])
    parent_branch = branch_results.get("parent-signal")
    receipt = None
    origin = "raw-residual" if base != Path(raw) else "raw"
    if parent_branch and parent_branch["application"]["structural_gate"]["decision"] == editor.ACCEPT_STRUCTURALLY:
        receipt = rebase.rebase_exact_node_delta(parent_package=parent, raw_package=base,
            candidate_package=parent_branch["candidate"], response_application=parent_branch["application"],
            output_package=destination)
        if receipt["decision"] == editor.ACCEPT_STRUCTURALLY:
            base_graph = syntax.build_inventory(base, "parent")
            final_graph = syntax.build_inventory(destination, "proposal")
            safe = not final_graph["diagnostics"] and {
                p: f["signatures"] for p, f in base_graph["files"].items()} == {
                p: f["signatures"] for p, f in final_graph["files"].items()}
            if safe:
                origin += "+parent-exact-delta"
            else:
                receipt["post_rebase_parser_gate"] = "REJECT"
                copy_tree_clean(base, destination)
        else:
            copy_tree_clean(base, destination)
    else:
        copy_tree_clean(base, destination)
    return seal({"version": VERSION, "origin": origin, "rebase": receipt,
        "selected_tree_hash": canonical_json_hash(hash_tree(destination)),
        "hidden_artifacts_consumed": False, "semantic_correctness_inferred": False}, "selection_hash")
