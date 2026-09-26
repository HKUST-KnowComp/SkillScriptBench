#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import (
    compact_json,
    conversation_created,
    conversation_messages,
    conversation_title,
    conversation_updated,
    load_conversations,
    message_has_nontext,
    message_has_thinking,
    normalize_iso,
    role_matches,
)


def render_nontext(message: dict, *, compact_nontext: bool) -> str:
    content = message.get("content")
    item_types = message.get("item_types") or []
    if not content or not item_types or item_types == ["text"]:
        return ""

    if compact_nontext:
        return "Non-text content types: " + ", ".join(item_types)

    return "```json\n" + compact_json(content, limit=4000) + "\n```"


def render_markdown(conversation: dict, *, schema: str, index: int, compact_nontext: bool, skip_thoughts: bool, allowed_roles: set[str] | None) -> str:
    title = conversation_title(conversation, schema)
    created = normalize_iso(conversation_created(conversation, schema))
    updated = normalize_iso(conversation_updated(conversation, schema))
    summary = (conversation.get("summary") or "").strip() if schema == "modern" else ""

    lines = [
        f"# {title}",
        "",
        f"- Index: {index}",
        f"- Schema: `{schema}`",
        f"- Created: {created}",
        f"- Updated: {updated}",
        "",
    ]
    if summary:
        lines.extend(["## Summary", "", summary, ""])

    messages = conversation_messages(conversation, schema)
    rendered_count = 0
    for message in messages:
        if skip_thoughts and message_has_thinking(message) and not (message.get("text") or "").strip():
            continue
        if not role_matches(message.get("sender") or "unknown", allowed_roles):
            continue

        rendered_count += 1
        sender = message.get("sender") or "unknown"
        created_at = normalize_iso(message.get("created_at"))
        text = (message.get("text") or "").strip()
        nontext = render_nontext(message, compact_nontext=compact_nontext)

        lines.append(f"## {rendered_count:03d}. {sender} — {created_at}")
        lines.append("")
        if text:
            lines.append(text)
            lines.append("")
        if nontext:
            lines.append(nontext)
            lines.append("")
        if not text and not nontext:
            lines.append("(no renderable content)")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Render one conversation from a chat history export as markdown.")
    parser.add_argument("export_path", help="Path to export folder or zip file")
    parser.add_argument("--index", type=int, required=True, help="Global conversation index to render")
    parser.add_argument("--compact-nontext", action="store_true", help="Summarize non-text payloads instead of dumping them")
    parser.add_argument("--skip-thoughts", action="store_true", help="Skip messages that contain only thinking content")
    role_filter = parser.add_mutually_exclusive_group()
    role_filter.add_argument("--user-only", action="store_true", help="Render only user messages")
    role_filter.add_argument("--assistant-only", action="store_true", help="Render only assistant messages")
    parser.add_argument("--output", help="Write markdown to this file instead of stdout")
    args = parser.parse_args()

    export_path = Path(args.export_path).expanduser()
    conversations, schema = load_conversations(export_path)
    if args.index < 0 or args.index >= len(conversations):
        raise SystemExit(f"Conversation index out of range: {args.index} (0-{len(conversations)-1})")

    allowed_roles = None
    if args.user_only:
        allowed_roles = {"user", "human"}
    elif args.assistant_only:
        allowed_roles = {"assistant"}

    output = render_markdown(
        conversations[args.index],
        schema=schema,
        index=args.index,
        compact_nontext=args.compact_nontext,
        skip_thoughts=args.skip_thoughts,
        allowed_roles=allowed_roles,
    )

    if args.output:
        output_path = Path(args.output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output, encoding="utf-8")
        print(f"Wrote {output_path}")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
