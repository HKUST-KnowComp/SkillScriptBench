# User Request

Audit the complete skill package and preserve both documented CLI parser behaviors below. Keep SKILL.md and the bundled implementation consistent, make no unrelated changes, and leave an already-correct package unchanged.

## Required Parser Behaviors

1. node skills/security/alert-triage/scripts/run-query.js --query-file query.esql --type esql

Both behaviors are implemented by `parseArgs` in `scripts/run-query.js` and must work together.
