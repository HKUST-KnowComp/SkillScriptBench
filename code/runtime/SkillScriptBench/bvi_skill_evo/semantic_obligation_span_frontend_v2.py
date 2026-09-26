"""Line-addressed evidence transport for the same semantic obligation method."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

from skillscriptbench.io_utils import canonical_json_hash

_spec = importlib.util.spec_from_file_location("semantic_quote_frontend_v1", Path(__file__).with_name("semantic_obligation_frontend_v1.py"))
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)
TOOL_NAME = "submit_semantic_obligation_spans"
EXTRACTION_TOOL = {"function": {"name": TOOL_NAME}}
align_parent_proposal = base.align_parent_proposal


def _span(ids, source=True):
    properties = {"start_line": {"type": "integer", "minimum": 1},
                  "end_line": {"type": "integer", "minimum": 1}}
    if source:
        properties["source_id"] = {"type": "string", "enum": list(ids)}
    return base._object(properties)


def schema(documents, nodes):
    result = copy.deepcopy(base.PAYLOAD_SCHEMA)
    variants = []
    for kind in base.KINDS:
        item = copy.deepcopy(base._OBLIGATION)
        item["properties"]["source_kind"] = {"type": "string", "enum": [kind]}
        ids = list(documents) if kind == "document_claim" else ["request"]
        for field in ("evidence", "condition_evidence"):
            item["properties"][field] = base._array(_span(ids))
        variants.append(item)
    result["properties"]["obligations"]["items"] = {"anyOf": variants}
    document = result["properties"]["documents"]["items"]
    document["properties"]["document_id"] = {"type": "string", "enum": list(documents)}
    evidence = document["properties"]["assessments"]["items"]["properties"]["evidence"]["items"]
    evidence["properties"].pop("quote")
    evidence["properties"].update(_span([], source=False)["properties"])
    evidence["required"] = list(evidence["properties"])
    bindings = result["properties"]["bindings"]
    if nodes:
        bindings["items"]["properties"]["node_id"] = {"type": "string", "enum": list(nodes)}
    else:
        bindings["maxItems"] = 0
    return result


def tool_for_prompt(prompt):
    payload = json.loads(prompt.split("\nPUBLIC_LINE_INPUTS.json\n", 1)[1])
    return {"type": "function", "function": {"name": TOOL_NAME, "strict": True,
        "description": "Extract semantic obligations with exact source IDs and line spans; never quote or invent source text.",
        "parameters": schema(payload["document_ids"], payload["HOST_NODES"])}}


def build_extraction_prompt(request_text, documents, *, scripts=None, nodes=None, frozen_contract=None):
    scripts, nodes = scripts or {}, nodes or {}
    if frozen_contract is not None:
        raise ValueError("span_frontend_frozen_contract_not_implemented")
    original = base.build_extraction_prompt(request_text, documents, scripts=scripts, nodes=nodes)
    instruction = original.split("\n\nPUBLIC_INPUTS.json\n", 1)[0]
    instruction = instruction.replace("Call submit_semantic_obligations once.", f"Call {TOOL_NAME} once.")
    instruction += (
        "\nEVIDENCE WIRE FORMAT: Do not output quotations. Cite one-based inclusive start_line/end_line "
        "ranges; the host retrieves the original text. Include the full condition in the selected span. "
        "Source IDs are exact identifiers, not JSON paths. Explicit requests and observed-problem "
        "inferences may cite ONLY source_id=request. Document claims may cite ONLY a document_ids "
        "entry. No normative evidence can cite scripts. Split a request obligation and a supporting "
        "document claim into separate obligations; never mix sources under one source_kind. "
        "The documents array must contain exactly the document_ids entries once each, NOT the request "
        "or scripts. In each document assessment, line spans refer ONLY to that document, never "
        "to the request or a script. If that document contains no relevant statement, evidence=[]; "
        "do not borrow a quote from another source. Assess all obligations in both documents. "
        "An unsupported or underdetermined relation must remain low-confidence/uncertain, not invented. "
        "Use script content only to understand current behavior and bind existing HOST_NODES. "
        "Do not add normative obligations inferred solely from the code. Each lines entry is "
        "[line_number, exact_text]. same_content_as is a lossless alias: that source has exactly "
        "the same complete text and line numbers as the named source, but retains its own source ID."
    )
    sources = {"request": request_text, **documents, **scripts}
    catalog, first_by_text = {}, {}
    for name, text in sources.items():
        lines = text.splitlines(keepends=True)
        row = {"role": "request" if name == "request" else "document" if name in documents else "implementation",
               "line_count": len(lines)}
        if text in first_by_text:
            row["same_content_as"] = first_by_text[text]
        else:
            row["lines"] = [[index, line] for index, line in enumerate(lines, 1)]
            first_by_text[text] = name
        catalog[name] = row
    payload = {"document_ids": list(documents), "HOST_NODES": nodes, "sources": catalog}
    return instruction + "\nPUBLIC_LINE_INPUTS.json\n" + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _validate(value, spec):
    if "anyOf" in spec:
        for option in spec["anyOf"]:
            try:
                _validate(value, option)
                return
            except ValueError:
                pass
        raise ValueError("span_source_kind_or_source_id_invalid")
    kind = spec["type"]
    expected = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int}[kind]
    if type(value) is not expected:
        raise ValueError("span_schema_type_invalid")
    if kind == "object":
        if set(value) != set(spec["required"]):
            raise ValueError("span_schema_fields_invalid")
        for key, item in value.items():
            _validate(item, spec["properties"][key])
    elif kind == "array":
        if "maxItems" in spec and len(value) > spec["maxItems"]:
            raise ValueError("span_schema_array_limit")
        for item in value:
            _validate(item, spec["items"])
    if "enum" in spec and value not in spec["enum"]:
        raise ValueError("span_schema_enum_invalid")
    if "minimum" in spec and value < spec["minimum"]:
        raise ValueError("span_schema_range_invalid")


def tool_arguments(response):
    if response.get("model") != "gpt-5.5":
        raise ValueError("provider_model_mismatch")
    choices = response.get("choices", [])
    if len(choices) != 1 or choices[0].get("finish_reason") not in {"stop", "tool_calls"}:
        raise ValueError("invalid_or_incomplete_choices")
    calls = choices[0].get("message", {}).get("tool_calls", [])
    if len(calls) != 1 or calls[0].get("function", {}).get("name") != TOOL_NAME:
        raise ValueError("invalid_tool_identity")
    value = calls[0]["function"]["arguments"]
    return json.loads(value) if isinstance(value, str) else value


def validate_extraction(payload, request_text, documents, *, scripts=None, nodes=None, frozen_contract=None):
    _validate(payload, schema(documents, nodes or {}))
    sources = {"request": request_text, **documents, **(scripts or {})}
    value = copy.deepcopy(payload)
    spans = []
    def quote(ref, source_id):
        lines = sources[source_id].splitlines(keepends=True)
        start, end = ref["start_line"], ref["end_line"]
        if not 1 <= start <= end <= len(lines):
            raise ValueError("source_line_span_out_of_range")
        result = "".join(lines[start - 1:end])
        if not result.strip():
            raise ValueError("empty_line_evidence")
        spans.append({"source_id": source_id, "start_line": start, "end_line": end,
                      "source_hash": canonical_json_hash({"text": sources[source_id]})})
        return result
    for obligation in value["obligations"]:
        for field in ("evidence", "condition_evidence"):
            obligation[field] = [{"source_id": r["source_id"], "quote": quote(r, r["source_id"])}
                                 for r in obligation[field]]
    for document in value["documents"]:
        for assessment in document["assessments"]:
            assessment["evidence"] = [{"quote": quote(e, document["document_id"]),
                **{k: v for k, v in e.items() if k not in {"start_line", "end_line"}}}
                for e in assessment["evidence"]]
    packet = base.validate_extraction(value, request_text, documents, scripts=scripts, nodes=nodes,
                                      frozen_contract=frozen_contract)
    packet.pop("extraction_hash")
    packet.update(evidence_transport="host_resolved_inclusive_line_spans_v2", source_line_spans=spans)
    packet["extraction_hash"] = canonical_json_hash(packet)
    return packet
