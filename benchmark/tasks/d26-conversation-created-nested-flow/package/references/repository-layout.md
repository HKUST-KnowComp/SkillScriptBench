# Repository layout for chat history reference memory

Use `reference/chatgpt/` as the root of the long-lived archive.

## Recommended shape

- `index.md` — master index of known exports, source paths, schema notes, and navigation strategy
- `export-YYYY-MM-DD.md` — one file per export inventory
- `chatgpt-memory-summary-YYYY-MM-DD.md` — `memories.json` content when present
- `projects-YYYY-MM-DD.md` — `projects.json` summary when useful
- `transcripts/NNN-slug.md` — curated conversation summaries
- `notes/topic-name.md` — topic-level notes mined from one or more conversations

## Writing rule

Default to progressive memory.

- Put raw or semi-raw findings in `reference/chatgpt/`
- Link outward from `system/human.md` only when a reference file becomes important enough to matter regularly
- Promote into `system/human.md` only when a fact is current, durable, and broadly useful

## Retrieval rule

When a question depends on historical nuance:

1. read `index.md`
2. read the relevant export summary
3. search the raw export if the archive notes are insufficient
4. preserve any new useful findings back into `reference/chatgpt/`
