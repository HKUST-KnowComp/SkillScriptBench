"""Strict completion decoding with non-content diagnostics and bounded transport retries."""
import json
import http.client
import time
import urllib.request
from urllib.parse import urlsplit

from skillscriptbench.io_utils import write_json, read_json, canonical_json_hash


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward a credential-bearing request to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("provider_redirect_refused")


def decode(stream, metadata):
    if "text/event-stream" not in stream.headers.get("Content-Type", ""):
        value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError("invalid_provider_response_shape")
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
        if not isinstance(value, dict):
            raise ValueError("invalid_provider_response_shape")
        metadata["chunks"] += 1
        if "error" in value:
            raise ValueError("provider_stream_error")
        choices = value.get("choices") or []
        if not isinstance(choices, list):
            raise ValueError("invalid_provider_response_shape")
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
            if not isinstance(choice, dict):
                raise ValueError("invalid_provider_response_shape")
            if choice.get("index", 0) != 0:
                raise ValueError("unexpected_stream_choice")
            if choice.get("finish_reason"):
                finished = choice["finish_reason"]
                metadata["finish_reasons"].append(finished)
            if choice.get("message"):
                if not isinstance(choice["message"], dict):
                    raise ValueError("invalid_provider_response_shape")
                if full_message is not None or calls or content:
                    raise ValueError("mixed_message_and_delta_completion")
                full_message = choice["message"]
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise ValueError("invalid_provider_response_shape")
            if full_message and (delta.get("content") or delta.get("tool_calls")):
                raise ValueError("mixed_message_and_delta_completion")
            if delta.get("content"):
                content.append(delta["content"])
            tool_calls = delta.get("tool_calls") or []
            if not isinstance(tool_calls, list):
                raise ValueError("invalid_provider_response_shape")
            for call in tool_calls:
                if not isinstance(call, dict) or not isinstance(call.get("function", {}), dict):
                    raise ValueError("invalid_provider_response_shape")
                index = call["index"]
                if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                    raise ValueError("invalid_provider_response_shape")
                target = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    if target["id"] and target["id"] != call["id"]:
                        raise ValueError("provider_tool_identity_changed")
                    target["id"] = call["id"]
                for key in ("name", "arguments"):
                    fragment = call.get("function", {}).get(key) or ""
                    if not isinstance(fragment, str):
                        raise ValueError("invalid_provider_response_shape")
                    target["function"][key] += fragment
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
    opener = urllib.request.build_opener(NoRedirect())
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
                try:
                    with opener.open(request, timeout=600) as response:
                        value = decode(response, meta)
                except http.client.HTTPException as exc:
                    raise OSError("provider_connection_interrupted") from exc
                if secret_pattern.search(json.dumps(value)):
                    raise ValueError("credential_like_response")
                write_json(directory / "RESPONSE.json", value)
                if value.get("model") != p["model"]:
                    raise ValueError("provider_exact_model_mismatch")
                choices = value.get("choices", [])
                if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict) or choices[0].get("finish_reason") not in {"stop", "tool_calls"}:
                    raise ValueError("incomplete_provider_response")
                message = choices[0].get("message")
                if not isinstance(message, dict):
                    raise ValueError("invalid_provider_response_shape")
                calls = message.get("tool_calls", [])
                if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict) or not isinstance(calls[0].get("function"), dict) or calls[0]["function"].get("name") != tool["function"]["name"]:
                    raise ValueError("provider_tool_mismatch")
                args = calls[0]["function"]["arguments"]
                args = json.loads(args) if isinstance(args, str) else args
                if not isinstance(args, dict):
                    raise ValueError("invalid_provider_response_shape")
                meta.update(status="accepted", runtime_seconds=time.monotonic() - started)
                write_json(directory / f"ATTEMPT_{attempt+1}.json", meta)
                write_json(directory / "USAGE.json", {"usage": value.get("usage"), "runtime_seconds": meta["runtime_seconds"]})
                return args
            except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
                known = {"provider_stream_error", "provider_stream_identity_changed", "provider_tool_identity_changed", "unexpected_stream_choice",
                         "mixed_message_and_delta_completion", "completion_token_limit_exhausted", "provider_stream_incomplete",
                         "provider_exact_model_mismatch", "incomplete_provider_response", "provider_tool_mismatch", "credential_like_response",
                         "provider_redirect_refused", "invalid_provider_response_shape"}
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
