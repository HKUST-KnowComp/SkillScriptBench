# User Request

Audit the complete skill package and preserve both documented CLI parser behaviors below. Keep SKILL.md and the bundled implementation consistent, make no unrelated changes, and leave an already-correct package unchanged.

## Required Parser Behaviors

1. Acknowledge ALL related alerts together. Use `--dry-run` first to confirm scope, then run without it:
2. node skills/security/alert-triage/scripts/acknowledge-alert.js --related --agent <id> --timestamp <ts> --window 60 --yes

Both behaviors are implemented by `parseArgs` in `scripts/acknowledge-alert.js` and must work together.
