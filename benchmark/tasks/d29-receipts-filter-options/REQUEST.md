# User Request

Audit the complete skill package and preserve both documented CLI parser behaviors below. Keep SKILL.md and the bundled implementation consistent, make no unrelated changes, and leave an already-correct package unchanged.

## Required Parser Behaviors

1. node <skill-dir>/scripts/mine-transcripts.mjs --days <N> [--repo <substr>] --html /tmp/cc-receipt.html

Both behaviors are implemented by `parseArgs` in `scripts/mine-transcripts.mjs` and must work together.
