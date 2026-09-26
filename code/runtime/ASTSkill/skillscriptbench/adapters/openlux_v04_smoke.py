from __future__ import annotations

import argparse
import datetime as dt
import getpass
import http.client
import json
import shutil
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from skillscriptbench.io_utils import (
    canonical_json_hash,
    copy_tree_clean,
    hash_tree,
    read_json,
    write_json,
)
from skillscriptbench.v04_eval import freeze_candidate
from skillscriptbench.v04_catalog import _run_probe
from skillscriptbench.v04_utility import (
    V04_EXPERIMENTAL_UTILITY_CONDITIONS,
    V04_UTILITY_CONDITIONS,
    _utility_scope_report,
    build_v04_utility_prompt,
    freeze_v04_utility_answer,
    materialize_v04_utility_workspace,
)


def _model_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        content = choices[0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("chat completion content is not a string")
        return content

    top_level = payload.get("output_text")
    if isinstance(top_level, str):
        return top_level

    text_parts: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                text_parts.append(content["text"])
    if text_parts:
        return "".join(text_parts)
    raise TypeError("provider response has no string model output")


def _normalized_usage(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    normalized = dict(payload)
    normalized.setdefault("prompt_tokens", int(payload.get("input_tokens", 0)))
    normalized.setdefault("completion_tokens", int(payload.get("output_tokens", 0)))
    normalized.setdefault(
        "total_tokens",
        normalized["prompt_tokens"] + normalized["completion_tokens"],
    )
    return normalized


def _parse_model_json(content: str) -> Any:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines)
    return json.loads(stripped)


def _call_chat_completion(
    *,
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    timeout: int,
    max_attempts: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any]]:
    if messages is None:
        if system_prompt is None or user_prompt is None:
            raise ValueError("system_prompt and user_prompt are required when messages are omitted")
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    request_payload: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        "messages": messages,
    }
    if tools is not None:
        request_payload["tools"] = tools
    encoded = json.dumps(request_payload).encode("utf-8")
    attempts: list[dict[str, Any]] = []
    response_payload: dict[str, Any] | None = None
    for attempt in range(1, max_attempts + 1):
        started = time.monotonic()
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/chat/completions",
            data=encoded,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
            attempts.append(
                {
                    "attempt": attempt,
                    "status": "success",
                    "http_status": response.status,
                    "elapsed_seconds": time.monotonic() - started,
                }
            )
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:2000]
            attempts.append(
                {
                    "attempt": attempt,
                    "status": "http_error",
                    "http_status": exc.code,
                    "elapsed_seconds": time.monotonic() - started,
                    "error": body,
                }
            )
            if exc.code not in {408, 409, 429, 500, 502, 503, 504}:
                break
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionResetError,
            http.client.RemoteDisconnected,
            ssl.SSLError,
        ) as exc:
            attempts.append(
                {
                    "attempt": attempt,
                    "status": "transport_error",
                    "elapsed_seconds": time.monotonic() - started,
                    "error": f"{type(exc).__name__}:{exc}",
                }
            )
        if attempt < max_attempts:
            time.sleep(5)
    return response_payload, attempts, request_payload


def _call_responses(
    *,
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    timeout: int,
    max_attempts: int,
    max_output_tokens: int = 8_192,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any]]:
    if tools is not None:
        raise ValueError("Responses tool conversion is not implemented for this benchmark adapter")
    if messages is None:
        if system_prompt is None or user_prompt is None:
            raise ValueError("system_prompt and user_prompt are required when messages are omitted")
        request_payload: dict[str, Any] = {
            "model": model,
            "instructions": system_prompt,
            "input": user_prompt,
            "max_output_tokens": max_output_tokens,
        }
    else:
        request_payload = {
            "model": model,
            "input": messages,
            "max_output_tokens": max_output_tokens,
        }
    encoded = json.dumps(request_payload).encode("utf-8")
    attempts: list[dict[str, Any]] = []
    response_payload: dict[str, Any] | None = None
    for attempt in range(1, max_attempts + 1):
        started = time.monotonic()
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/responses",
            data=encoded,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
            attempts.append(
                {
                    "attempt": attempt,
                    "status": "success",
                    "http_status": response.status,
                    "elapsed_seconds": time.monotonic() - started,
                }
            )
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:2000]
            attempts.append(
                {
                    "attempt": attempt,
                    "status": "http_error",
                    "http_status": exc.code,
                    "elapsed_seconds": time.monotonic() - started,
                    "error": body,
                }
            )
            if exc.code not in {408, 409, 429, 500, 502, 503, 504}:
                break
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionResetError,
            http.client.RemoteDisconnected,
            ssl.SSLError,
        ) as exc:
            attempts.append(
                {
                    "attempt": attempt,
                    "status": "transport_error",
                    "elapsed_seconds": time.monotonic() - started,
                    "error": f"{type(exc).__name__}:{exc}",
                }
            )
        if attempt < max_attempts:
            time.sleep(5)
    return response_payload, attempts, request_payload


def _visible_files(root: Path) -> list[dict[str, str]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "content": path.read_text(encoding="utf-8", errors="replace"),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "answer.json"
    ]


def _utility_api_prompt(
    workspace: Path,
    task_prompt: str,
    *,
    script_tool_available: bool = False,
) -> str:
    tool_note = (
        "A `run_installed_skill` tool is available. Use it to execute the installed script on "
        "the exact TASK.json input before returning the answer."
        if script_tool_available
        else "No script execution tool is available in this environment."
    )
    return (
        f"{task_prompt}\n"
        "The API has no filesystem browser, so the visible local files are embedded below. "
        f"{tool_note} Use only these resources. Return strict JSON with exactly one top-level key named "
        "`answer`; its value must be the JSON value that would be written to answer.json.\n\n"
        f"Visible files:\n{json.dumps(_visible_files(workspace), indent=2, ensure_ascii=True)}\n"
    )


def _usage_sum(payloads: list[dict[str, Any]]) -> dict[str, Any] | None:
    usage_rows = [payload.get("usage") for payload in payloads if payload.get("usage")]
    if not usage_rows:
        return None
    result: dict[str, Any] = {
        "prompt_tokens": sum(int(row.get("prompt_tokens", 0)) for row in usage_rows),
        "completion_tokens": sum(int(row.get("completion_tokens", 0)) for row in usage_rows),
        "total_tokens": sum(int(row.get("total_tokens", 0)) for row in usage_rows),
        "completion_tokens_details": {
            "reasoning_tokens": sum(
                int((row.get("completion_tokens_details") or {}).get("reasoning_tokens", 0))
                for row in usage_rows
            )
        },
    }
    return result


def _installed_skill_tool() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "run_installed_skill",
                "description": (
                    "Execute the installed skill function on the exact input from TASK.json and "
                    "return its JSON-serializable result."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }
    ]


def _execute_installed_skill(
    public_root: Path,
    workspace: Path,
    family_id: str,
) -> dict[str, Any]:
    manifest = read_json(public_root / "benchmark_manifest.json")
    family = _family(manifest, family_id)
    task = read_json(workspace / "TASK.json")
    source = (workspace / "skill" / "scripts" / "entry.py").read_text(encoding="utf-8")
    rows = _run_probe(source, family["function_name"], [task["input"]], timeout=12)
    if len(rows) != 1:
        raise RuntimeError("installed skill probe did not return exactly one row")
    return rows[0]


def run_utility(
    public_root: Path,
    *,
    family_id: str,
    task_id: str,
    condition: str,
    output_root: Path,
    api_key: str,
    base_url: str,
    model: str,
    timeout: int,
    max_attempts: int,
) -> dict[str, Any]:
    if condition not in (*V04_UTILITY_CONDITIONS, *V04_EXPERIMENTAL_UTILITY_CONDITIONS):
        raise ValueError(condition)
    run = output_root.resolve()
    if run.exists():
        raise FileExistsError(run)
    run.mkdir(parents=True)
    workspace = run / "workspace"
    workspace_record = materialize_v04_utility_workspace(
        public_root,
        family_id=family_id,
        task_id=task_id,
        condition=condition,
        workspace=workspace,
    )
    baseline_hashes = hash_tree(workspace)
    script_tool_available = condition in {"full-package", "scripts-only-executable"}
    prompt = _utility_api_prompt(
        workspace,
        build_v04_utility_prompt(public_root, family_id, task_id),
        script_tool_available=script_tool_available,
    )
    (run / "prompt.txt").write_text(prompt, encoding="utf-8")
    system_prompt = (
        "You solve a public Script Utility task. Never request or infer hidden tests, "
        "verifiers, gold answers, or oracle behavior. Return strict JSON only."
    )
    tools = _installed_skill_tool() if script_tool_available else None
    response, attempts, request_payload = _call_chat_completion(
        api_key=api_key,
        base_url=base_url,
        model=model,
        system_prompt=system_prompt,
        user_prompt=prompt,
        timeout=timeout,
        max_attempts=max_attempts,
        tools=tools,
    )
    responses = [response] if response is not None else []
    request_payloads = [request_payload]
    tool_calls_executed = 0
    tool_execution_status = "not_available" if not script_tool_available else "not_requested"
    tool_result_hash: str | None = None
    if response is not None and script_tool_available:
        assistant_message = response["choices"][0]["message"]
        tool_calls = assistant_message.get("tool_calls") or []
        if tool_calls:
            if len(tool_calls) != 1 or tool_calls[0].get("function", {}).get("name") != "run_installed_skill":
                raise ValueError("model requested an unsupported utility tool call")
            tool_call = tool_calls[0]
            tool_result = _execute_installed_skill(public_root.resolve(), workspace, family_id)
            tool_calls_executed = 1
            tool_execution_status = tool_result.get("status", "unknown")
            tool_result_hash = canonical_json_hash(tool_result)
            followup_messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
                {
                    "role": "assistant",
                    "content": assistant_message.get("content"),
                    "tool_calls": tool_calls,
                },
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": json.dumps(tool_result, sort_keys=True, ensure_ascii=True),
                },
            ]
            followup, followup_attempts, followup_request = _call_chat_completion(
                api_key=api_key,
                base_url=base_url,
                model=model,
                messages=followup_messages,
                timeout=timeout,
                max_attempts=max_attempts,
            )
            attempts.extend(followup_attempts)
            request_payloads.append(followup_request)
            response = followup
            if followup is not None:
                responses.append(followup)
    content = _model_content(response) if response is not None else ""
    (run / "raw_response.txt").write_text(content, encoding="utf-8")
    write_json(run / "provider_responses.json", responses)
    parse_error: str | None = None
    try:
        parsed = _parse_model_json(content)
        if not isinstance(parsed, dict) or set(parsed) != {"answer"}:
            raise ValueError("response must contain exactly the `answer` key")
        write_json(workspace / "answer.json", parsed["answer"])
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        parse_error = f"{type(exc).__name__}:{exc}"
    scope = _utility_scope_report(workspace, baseline_hashes)
    freeze = freeze_v04_utility_answer(
        workspace,
        run,
        family_id=family_id,
        task_id=task_id,
        condition=condition,
    )
    valid = response is not None and parse_error is None and scope["status"] == "pass" and freeze["status"] == "candidate_frozen"
    record = {
        "schema_version": "0.4-openlux-utility-run-2",
        "status": "candidate_frozen" if valid else "invalid_trial",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "provider": base_url,
        "model": model,
        "family_id": family_id,
        "task_id": task_id,
        "condition": condition,
        "model_calls": len(attempts),
        "request_hash": canonical_json_hash(request_payloads),
        "prompt_hash": canonical_json_hash(prompt),
        "workspace_record_hash": canonical_json_hash(workspace_record),
        "attempts": attempts,
        "response_id": response.get("id") if response else None,
        "usage": _usage_sum(responses),
        "script_tool_available": script_tool_available,
        "script_tool_calls_executed": tool_calls_executed,
        "script_tool_execution_status": tool_execution_status,
        "script_tool_result_hash": tool_result_hash,
        "skill_markdown_visible": (workspace / "skill" / "SKILL.md").is_file(),
        "script_source_visible": (workspace / "skill" / "scripts" / "entry.py").is_file(),
        "parse_error": parse_error,
        "scope_report": scope,
        "answer_freeze_hash": canonical_json_hash(freeze),
        "hidden_evaluation_loaded": False,
        "credential_persisted": False,
    }
    write_json(run / "run_record.json", record)
    return record


def _family(manifest: dict[str, Any], family_id: str) -> dict[str, Any]:
    try:
        return next(row for row in manifest["families"] if row["family_id"] == family_id)
    except StopIteration as exc:
        raise KeyError(family_id) from exc


def _evolution_api_prompt(task: dict[str, Any], candidate: Path) -> str:
    return (
        "Modify the visible Agent Skill package to satisfy TASK.json. Preserve existing behavior "
        "unless TASK.json explicitly generalizes it. Do not look for or infer hidden tests, gold "
        "answers, mutation labels, or oracle behavior. Return strict JSON with this exact schema: "
        "{\"files\":{\"SKILL.md\":\"complete file\",\"scripts/entry.py\":\"complete file\"},"
        "\"summary\":\"brief description\"}. Return both complete files, even if one is unchanged.\n\n"
        f"TASK.json:\n{json.dumps(task, indent=2, sort_keys=True, ensure_ascii=True)}\n\n"
        f"Visible files:\n{json.dumps(_visible_files(candidate), indent=2, ensure_ascii=True)}\n"
    )


def run_evolution(
    public_root: Path,
    *,
    family_id: str,
    output_root: Path,
    api_key: str,
    base_url: str,
    model: str,
    timeout: int,
    max_attempts: int,
) -> dict[str, Any]:
    public = public_root.resolve()
    run = output_root.resolve()
    if run.exists():
        raise FileExistsError(run)
    run.mkdir(parents=True)
    manifest = read_json(public / "benchmark_manifest.json")
    family = _family(manifest, family_id)
    if not family.get("evolution_kind"):
        raise ValueError("utility-only family")
    family_root = public / family["public_family_path"] / "evolution"
    candidate = run / "candidate"
    copy_tree_clean(family_root / "visible", candidate)
    baseline_hashes = hash_tree(candidate)
    task = read_json(family_root / "TASK.json")
    prompt = _evolution_api_prompt(task, candidate)
    (run / "prompt.txt").write_text(prompt, encoding="utf-8")
    system_prompt = (
        "You perform one bounded repair of a public Agent Skill package. Use only supplied public "
        "evidence and return strict JSON containing complete replacement files."
    )
    response, attempts, request_payload = _call_chat_completion(
        api_key=api_key,
        base_url=base_url,
        model=model,
        system_prompt=system_prompt,
        user_prompt=prompt,
        timeout=timeout,
        max_attempts=max_attempts,
    )
    content = _model_content(response) if response is not None else ""
    (run / "raw_response.txt").write_text(content, encoding="utf-8")
    parse_error: str | None = None
    allowed_paths = {"SKILL.md", "scripts/entry.py"}
    try:
        parsed = _parse_model_json(content)
        files = parsed["files"]
        if not isinstance(files, dict) or set(files) != allowed_paths:
            raise ValueError("response files must be exactly SKILL.md and scripts/entry.py")
        if not all(isinstance(value, str) for value in files.values()):
            raise TypeError("replacement file values must be strings")
        for relative, value in files.items():
            (candidate / relative).write_text(value, encoding="utf-8")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        parse_error = f"{type(exc).__name__}:{exc}"
    current_hashes = hash_tree(candidate)
    changed_paths = sorted(
        path
        for path in set(baseline_hashes) | set(current_hashes)
        if baseline_hashes.get(path) != current_hashes.get(path)
    )
    scope = {
        "status": "pass" if set(changed_paths) <= allowed_paths else "fail",
        "changed_paths": changed_paths,
        "violations": sorted(set(changed_paths) - allowed_paths),
    }
    freeze = freeze_candidate(candidate, run, family_id=family_id)
    valid = response is not None and parse_error is None and scope["status"] == "pass" and bool(changed_paths)
    record = {
        "schema_version": "0.4-openlux-evolution-run-1",
        "status": "candidate_frozen" if valid else "invalid_trial",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "provider": base_url,
        "model": model,
        "family_id": family_id,
        "condition": "raw-source-api",
        "model_calls": len(attempts),
        "request_hash": canonical_json_hash(request_payload),
        "prompt_hash": canonical_json_hash(prompt),
        "attempts": attempts,
        "response_id": response.get("id") if response else None,
        "usage": response.get("usage") if response else None,
        "parse_error": parse_error,
        "scope_report": scope,
        "candidate_freeze_hash": canonical_json_hash(freeze),
        "hidden_evaluation_loaded": False,
        "credential_persisted": False,
    }
    write_json(run / "run_record.json", record)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a public-only v0.4 API smoke.")
    parser.add_argument("track", choices=("utility", "evolution"))
    parser.add_argument("--public-root", type=Path, required=True)
    parser.add_argument("--family-id", required=True)
    parser.add_argument("--task-id")
    parser.add_argument("--condition", choices=V04_UTILITY_CONDITIONS)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--base-url", default="https://api.openlux.ai/v1")
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--max-attempts", type=int, default=2)
    args = parser.parse_args()
    api_key = getpass.getpass("OpenLux API key: ")
    if not api_key:
        raise SystemExit("API key is required")
    common = {
        "public_root": args.public_root,
        "family_id": args.family_id,
        "output_root": args.output_root,
        "api_key": api_key,
        "base_url": args.base_url,
        "model": args.model,
        "timeout": args.timeout,
        "max_attempts": args.max_attempts,
    }
    if args.track == "utility":
        if not args.task_id or not args.condition:
            raise SystemExit("utility requires --task-id and --condition")
        record = run_utility(
            **common,
            task_id=args.task_id,
            condition=args.condition,
        )
    else:
        record = run_evolution(**common)
    print(
        json.dumps(
            {
                "status": record["status"],
                "model": record["model"],
                "model_calls": record["model_calls"],
                "parse_error": record["parse_error"],
                "scope_status": record["scope_report"]["status"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if record["status"] == "candidate_frozen" else 2


if __name__ == "__main__":
    raise SystemExit(main())
