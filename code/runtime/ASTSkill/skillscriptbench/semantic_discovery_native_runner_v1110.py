"""One public package, one discovery call, at most two node-bound revisions.

Preparation requires only request/parent/Raw, never a historical CALL_PLAN or facts
packet. There is deliberately no behavior-evaluation subcommand in this runner.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import time
import urllib.request

from bvi_skill_evo import semantic_discovery_native_bridge_v1109 as bridge
from skillscriptbench.io_utils import canonical_json_hash, copy_tree_clean, hash_tree, read_json, write_json

VERSION = "semantic-discovery-native-runner-v1110"
KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")


def assert_public_package(package):
    root = Path(package).resolve()
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise ValueError("public_envelope_symlink")
        if any(part.casefold() in {"_oracle", "_private", "hidden", "credentials", "evolution"}
               or (part.startswith(".env") and part != ".env.example") for part in relative.parts) or path.suffix in {".pem", ".key"}:
            raise ValueError("nonpublic_file_in_input_envelope")


def prepare(parent, raw, request, output, *, model="gpt-5.6-sol"):
    root = Path(output).resolve()
    if root.exists():
        raise FileExistsError(root)
    if model not in {"gpt-5.5", "gpt-5.6-sol"}:
        raise ValueError("unsupported_exact_model")
    for path in (parent, raw):
        assert_public_package(path)
        if Path(path).resolve() == root or Path(path).resolve() in root.parents:
            raise ValueError("output_must_be_outside_input_packages")
    bundle = bridge.build_input(request, parent, raw)
    if KEY_PATTERN.search(bundle["prompt"]):
        raise ValueError("credential_like_public_input")
    root.mkdir(parents=True)
    for name, path in (("parent", parent), ("proposal", raw)):
        copy_tree_clean(path, root / "public" / name)
    write_json(root / "INPUT.json", bundle)
    protocol = bridge.seal({"version": VERSION, "model": model, "input_hash": bundle["input_hash"],
        "parent_tree_hash": canonical_json_hash(hash_tree(root / "public/parent")),
        "raw_tree_hash": canonical_json_hash(hash_tree(root / "public/proposal")),
        "extraction_calls": 1, "revision_calls_max": 2, "temperature": 0,
        "transport_attempts_max": 2, "location_binding": "unique_source_type_quote_symbol_no_line_arithmetic",
        "extraction_token_limit": 12000, "revision_token_limit": 8192,
        "legacy_node_shortlist_used": False, "legacy_branch_plan_used": False,
        "discovery_failure_fallback": "frozen_raw_without_rule_miner",
        "hidden_evaluator_available": False}, "protocol_hash")
    write_json(root / "PROTOCOL.json", protocol)
    return {"status": "prepared", "model_calls": 0, "nodes": len(bundle["nodes"]),
            "parser_diagnostics": {k: v["diagnostics"] for k, v in bundle["inventories"].items()}}


def run_prepared(output, call_model):
    root = Path(output).resolve()
    protocol = read_json(root / "PROTOCOL.json")
    bridge.verify(protocol, "protocol_hash")
    bundle = read_json(root / "INPUT.json")
    bridge.verify(bundle, "input_hash")
    if bundle["input_hash"] != protocol["input_hash"]:
        raise ValueError("input_protocol_mismatch")
    for snapshot, key in (("parent", "parent_tree_hash"), ("proposal", "raw_tree_hash")):
        assert_public_package(root / "public" / snapshot)
        if canonical_json_hash(hash_tree(root / "public" / snapshot)) != protocol[key]:
            raise ValueError("public_envelope_changed")
    # A single exclusive marker prevents accidental replay of a partially run case.
    with (root / "STARTED.json").open("x", encoding="utf-8") as handle:
        json.dump({"started_at": time.time(), "protocol_hash": protocol["protocol_hash"]}, handle)
    errors, branches, calls = [], {}, 0
    try:
        calls += 1
        payload = call_model(bundle["prompt"], bundle["tool"], "discovery")
        write_json(root / "DISCOVERY_RESPONSE.json", payload)
        discovery = bridge.compile_discovery(bundle, payload)
    except (ValueError, KeyError, TypeError, RuntimeError, OSError) as exc:
        errors.append({"stage": "discovery", "error_class": type(exc).__name__})
        discovery = bridge.compile_discovery(bundle, {"abstain": True, "abstain_reason": "invalid_or_unavailable_extraction", "records": []})
    write_json(root / "DISCOVERY.json", discovery)
    for view, packet in discovery["branches"].items():
        directory = root / "branches" / view
        write_json(directory / "PACKET.json", packet)
        prompt = bridge.revision_prompt(bundle, packet)
        (directory / "PROMPT.txt").write_text(prompt, encoding="utf-8")
        try:
            calls += 1
            payload = call_model(prompt, bridge.editor.UNIFIED_NODE_PATCH_TOOL, view)
            write_json(directory / "RESPONSE.json", payload)
            candidate = directory / "candidate"
            application = bridge.apply_patch_payload(bundle, packet, payload,
                root / "public" / packet["snapshot"], candidate)
            write_json(directory / "APPLICATION.json", application)
            branches[view] = {"application": application, "candidate": str(candidate)}
        except (ValueError, KeyError, TypeError, RuntimeError, OSError) as exc:
            errors.append({"stage": view, "error_class": type(exc).__name__})
    selection = bridge.select(root / "public/parent", root / "public/proposal", branches, root / "selected/package")
    write_json(root / "selected/SELECTION.json", selection)
    result = {"version": VERSION, "status": "frozen", "call_attempts": calls, "errors": errors,
        "planned_revision_views": list(discovery["branches"]), "selection": selection,
        "hidden_evaluator_loaded": False, "legacy_detectors_invoked": False}
    write_json(root / "RESULT.json", result)
    return result


def provider_callback(root, key):
    from skillscriptbench.discovery_transport_v1108 import provider_callback as create
    return create(root, key, KEY_PATTERN)


def audit_public_inventory(source_run, destination):
    """Read only public task requests and snapshots; do not load outcomes or facts."""
    source = Path(source_run).resolve()
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    matrix = read_json(source / "stage/TASK_MATRIX.json")["rows"]
    rows = []
    for task in matrix:
        task_id = task["task_id"]
        if Path(task_id).name != task_id:
            raise ValueError("unsafe_task_identity")
        parent = source / "public" / task_id / "parent/package"
        proposal = source / "public" / task_id / "proposal/package"
        started = time.monotonic()
        try:
            bundle = bridge.build_input(task["request"], parent, proposal)
            row = {"task_id": task_id, "status": "indexed", "input_hash": bundle["input_hash"],
                "prompt_bytes": len(bundle["prompt"].encode()), "node_count": len(bundle["nodes"]),
                "snapshots": {s: {"files": len(g["files"]), "nodes": len(g["nodes"]),
                    "editable_nodes": sum(n["editable"] for n in g["nodes"].values()),
                    "languages": sorted({f["language"] for f in g["files"].values()}),
                    "calls": len(g["calls"]), "resolved_calls": sum(e["status"] == "RESOLVED" for e in g["calls"]),
                    "diagnostics": g["diagnostics"], "inventory_hash": g["inventory_hash"]}
                    for s, g in bundle["inventories"].items()}}
        except (ValueError, OSError, TypeError) as exc:
            row = {"task_id": task_id, "status": "blocked", "reason": str(exc)[:300]}
        row["runtime_seconds"] = time.monotonic() - started
        rows.append(row)
        write_json(destination.with_name("INVENTORY_PROGRESS.json"), {"completed": len(rows), "tasks": len(matrix),
            "model_calls": 0, "rows": rows})
        print(json.dumps({"inventory_progress": len(rows), "total": len(matrix), "status": row["status"]}), flush=True)
    result = bridge.seal({"version": VERSION, "tasks": len(rows), "indexed": sum(r["status"] == "indexed" for r in rows),
        "model_calls": 0, "hidden_evaluator_loaded": False, "legacy_facts_loaded": False,
        "legacy_call_plan_loaded": False, "rows": rows}, "audit_hash")
    write_json(destination, result)
    return {k: v for k, v in result.items() if k != "rows"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "run", "audit-public-inventory"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--parent", type=Path)
    parser.add_argument("--raw", type=Path)
    parser.add_argument("--request-file", type=Path)
    parser.add_argument("--model", choices=("gpt-5.5", "gpt-5.6-sol"), default="gpt-5.6-sol")
    parser.add_argument("--credential-fd", type=int, default=3)
    parser.add_argument("--source-run", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        if any(value is None for value in (args.parent, args.raw, args.request_file)):
            parser.error("prepare requires --parent, --raw and --request-file")
        result = prepare(args.parent, args.raw, args.request_file.read_text(), args.root, model=args.model)
    elif args.action == "audit-public-inventory":
        if args.source_run is None:
            parser.error("audit-public-inventory requires --source-run")
        result = audit_public_inventory(args.source_run, args.root)
    else:
        with os.fdopen(os.dup(args.credential_fd), "r") as stream:
            key = stream.readline(4096).strip()
        if not key:
            raise ValueError("empty_credential_fd")
        result = run_prepared(args.root, provider_callback(args.root, key))
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()

