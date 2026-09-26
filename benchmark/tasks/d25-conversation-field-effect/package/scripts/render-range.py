#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

_rc_path = Path(__file__).resolve().parent / "render-conversation.py"
_spec = importlib.util.spec_from_file_location("render_conversation", _rc_path)
_rc = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_rc)  # type: ignore[union-attr]

render_markdown = _rc.render_markdown
load_conversations = _rc.load_conversations


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a range of conversations from a chat history export.")
    parser.add_argument("export_path", help="Path to export folder or zip file")
    parser.add_argument("--start-index", type=int, required=True, help="First global conversation index to render")
    parser.add_argument("--end-index", type=int, required=True, help="Last global conversation index to render")
    parser.add_argument("--compact-nontext", action="store_true", help="Summarize non-text payloads instead of dumping them")
    parser.add_argument("--skip-thoughts", action="store_true", help="Skip messages that contain only thinking content")
    role_filter = parser.add_mutually_exclusive_group()
    role_filter.add_argument("--user-only", action="store_true", help="Render only user messages")
    role_filter.add_argument("--assistant-only", action="store_true", help="Render only assistant messages")
    output_group = parser.add_mutually_exclusive_group(required=True)
    output_group.add_argument("--output-dir", help="Directory for one markdown file per conversation")
    output_group.add_argument("--concat-output", help="Write one concatenated markdown file for the whole range")
    args = parser.parse_args()

    export_path = Path(args.export_path).expanduser()
    conversations, schema = load_conversations(export_path)
    if args.end_index < args.start_index:
        raise SystemExit("--end-index must be greater than or equal to --start-index")

    allowed_roles = None
    if args.user_only:
        allowed_roles = {"user", "human"}
    elif args.assistant_only:
        allowed_roles = {"assistant"}

    indexes = range(max(0, args.start_index), min(len(conversations) - 1, args.end_index) + 1)
    rendered = []
    for index in indexes:
        rendered.append(
            (
                index,
                render_markdown(
                    conversations[index],
                    schema=schema,
                    index=index,
                    compact_nontext=args.compact_nontext,
                    skip_thoughts=args.skip_thoughts,
                    allowed_roles=allowed_roles,
                ),
            )
        )

    if args.output_dir:
        output_dir = Path(args.output_dir).expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)
        for index, markdown in rendered:
            (output_dir / f"{index:04d}.md").write_text(markdown, encoding="utf-8")
        print(f"Wrote {len(rendered)} conversations to {output_dir}")
        return

    concat_path = Path(args.concat_output).expanduser()
    concat_path.parent.mkdir(parents=True, exist_ok=True)
    concat_path.write_text("\n\n---\n\n".join(markdown for _, markdown in rendered), encoding="utf-8")
    print(f"Wrote {len(rendered)} conversations to {concat_path}")


if __name__ == "__main__":
    main()
