"""Strict completion decoding with non-content diagnostics and bounded transport retries."""
import json
import time
import urllib.request
from urllib.parse import urlsplit

from skillscriptbench.io_utils import write_json, read_json, canonical_json_hash


def decode(stream, metadata):
    if "text/event-stream" not in stream.headers.get("Content-Type", ""):
        value = json.load(stream)
        metadata["shape"] = "json"
        return value
    metadata.update(shape="sse", chunks=0, identities=[], finish_reasons=[], done=False)
    identity, calls, content, finished = {}, {}, [], None
    result = {}
    full_message = None
    for line in stream:
        if not line.strip().startswith(b"data:"):
            continue
        data = line.strip()[5:].strip()
        if data == b"[DONE]":
            metadata["done"] = True
            break
        if not data:
            continue
        value = json.loads(data)
        metadata["chunks"] += 1
        if "error" in value:
            raise ValueError("provider_stream_error")
        choices = value.get("choices") or []
        observed = {k: str(value[k])[:100] for k in ("id", "model") if value.get(k)}
        observed["has_choices"] = bool(choices)
        if observed not in metadata["identities"]:
            metadata["identities"].append(observed)
        if value.get("usage"):
            result["usage"] = value["usage"]
            metadata["usage"] = value["usage"]
        # Usage trailers and empty keepalives do not own completion content.
        if not choices:
            continue
        for key in ("id", "model"):
            if value.get(key):
                if key in identity and identity[key] != value[key]:
                    raise ValueError("provider_stream_identity_changed")
                identity[key] = value[key]
        for choice in choices:
            if choice.get("index", 0) != 0:
                raise ValueError("unexpected_stream_choice")
            if choice.get("finish_reason"):
                finished = choice["finish_reason"]
                metadata["finish_reasons"].append(finished)
            if choice.get("message"):
                if full_message is not None or calls or content:
                    raise ValueError("mixed_message_and_delta_completion")
                full_message = choice["message"]
            delta = choice.get("delta") or {}
            if full_message and (delta.get("content") or delta.get("tool_calls")):
                raise ValueError("mixed_message_and_delta_completion")
            if delta.get("content"):
                content.append(delta["content"])
            for call in delta.get("tool_calls") or []:
                index = call["index"]
                target = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    if target["id"] and target["id"] != call["id"]:
                        raise ValueError("provider_tool_identity_changed")
                    target["id"] = call["id"]
                for key in ("name", "arguments"):
                    target["function"][key] += call.get("function", {}).get(key) or ""
    if finished == "length":
        raise ValueError("completion_token_limit_exhausted")
    if not metadata["done"] or finished not in {"stop", "tool_calls"}:
        raise ValueError("provider_stream_incomplete")
    result.update(identity)
    result["choices"] = [{"index": 0, "finish_reason": finished, "message": full_message or {
        "role": "assistant", "content": "".join(content), "tool_calls": [calls[k] for k in sorted(calls)]}}]
    return result


def provider_callback(root, key, secret_pattern, *, base_url="https://api.openlux.ai/v1"):
    endpoint = urlsplit(base_url)
    if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username
            or endpoint.password or endpoint.query or endpoint.fragment):
        raise ValueError("https_api_base_url_required")
    completion_url = base_url.rstrip("/") + "/chat/completions"
    p = read_json(root / "PROTOCOL.json")
    def call(prompt, tool, stage):
        payload = {"model": p["model"], "temperature": p["temperature"], "stream": True,
            "stream_options": {"include_usage": True},
            "max_completion_tokens": p["extraction_token_limit"] if stage == "discovery" else p["revision_token_limit"],
            "messages": [{"role": "system", "content": "Maintain executable skills using only public source-grounded evidence."},
                         {"role": "user", "content": prompt}], "tools": [tool],
            "tool_choice": {"type": "function", "function": {"name": tool["function"]["name"]}}}
        if secret_pattern.search(json.dumps(payload)):
            raise ValueError("credential_like_prompt")
        directory = root / "provider" / stage
        write_json(directory / "REQUEST.json", payload)
        for attempt in range(2):
            meta = {"attempt": attempt + 1, "request_hash": canonical_json_hash(payload)}
            started = time.monotonic()
            try:
                request = urllib.request.Request(completion_url,
                    data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
                with urllib.request.urlopen(request, timeout=600) as response:
                    value = decode(response, meta)
                if secret_pattern.search(json.dumps(value)):
                    raise ValueError("credential_like_response")
                write_json(directory / "RESPONSE.json", value)
                if value.get("model") != p["model"]:
                    raise ValueError("provider_exact_model_mismatch")
                choices = value.get("choices", [])
                if len(choices) != 1 or choices[0].get("finish_reason") not in {"stop", "tool_calls"}:
                    raise ValueError("incomplete_provider_response")
                calls = choices[0].get("message", {}).get("tool_calls", [])
                if len(calls) != 1 or calls[0]["function"]["name"] != tool["function"]["name"]:
                    raise ValueError("provider_tool_mismatch")
                args = calls[0]["function"]["arguments"]
                args = json.loads(args) if isinstance(args, str) else args
                meta.update(status="accepted", runtime_seconds=time.monotonic() - started)
                write_json(directory / f"ATTEMPT_{attempt+1}.json", meta)
                write_json(directory / "USAGE.json", {"usage": value.get("usage"), "runtime_seconds": meta["runtime_seconds"]})
                return args
            except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
                known = {"provider_stream_error", "provider_stream_identity_changed", "provider_tool_identity_changed", "unexpected_stream_choice",
                         "mixed_message_and_delta_completion", "completion_token_limit_exhausted", "provider_stream_incomplete",
                         "provider_exact_model_mismatch", "incomplete_provider_response", "provider_tool_mismatch", "credential_like_response"}
                reason = str(exc) if str(exc) in known else type(exc).__name__
                meta.update(status="rejected", reason=reason, http_status=getattr(exc, "code", None), runtime_seconds=time.monotonic()-started)
                if secret_pattern.search(json.dumps(meta)):
                    meta = {"status": "rejected", "reason": "credential_like_metadata"}
                write_json(directory / f"ATTEMPT_{attempt+1}.json", meta)
                retry = reason in {"provider_stream_error", "provider_stream_incomplete", "provider_stream_identity_changed"} or isinstance(exc, OSError)
                if (directory / "RESPONSE.json").exists() or not retry or attempt == 1 or meta.get("http_status") in {400,401,403,404}:
                    write_json(directory / "TRANSPORT_FAILURE.json", meta)
                    raise
        raise RuntimeError("transport_exhausted")
    return call
