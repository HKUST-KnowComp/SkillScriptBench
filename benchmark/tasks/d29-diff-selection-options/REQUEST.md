# User Request

Audit the complete skill package and preserve both documented CLI parser behaviors below. Keep SKILL.md and the bundled implementation consistent, make no unrelated changes, and leave an already-correct package unchanged.

## Required Parser Behaviors

1. node skills/diff/scripts/visual-diff.mjs   "$PROTO" "$BUILD" --profile eds --sections ".hero"

Both behaviors are implemented by `parseArgs` in `scripts/visual-diff.mjs` and must work together.
