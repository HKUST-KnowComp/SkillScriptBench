# User Request

Audit the complete skill package and preserve both documented CLI parser behaviors below. Keep SKILL.md and the bundled implementation consistent, make no unrelated changes, and leave an already-correct package unchanged.

## Required Parser Behaviors

1. node skills/observability/service-health/scripts/apm-correlations.js latency-correlations --service-name <name> [--start <iso>] [--end <iso>] [--last-minutes 60] [--transaction-type <t>] [--transaction-name <n>] [--space <id>] [--json]

Both behaviors are implemented by `parseArgs` in `scripts/apm-correlations.js` and must work together.
