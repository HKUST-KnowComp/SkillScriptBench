# User Request

Audit the complete skill package and preserve both documented CLI parser behaviors below. Keep SKILL.md and the bundled implementation consistent, make no unrelated changes, and leave an already-correct package unchanged.

## Required Parser Behaviors

1. node scripts/replica/stitch-shot.mjs "$LIVE"  stardust/replica/gates/<slug>-1440/live.png  --width 1440 --settle

Both behaviors are implemented by `parseArgs` in `scripts/stitch-shot.mjs` and must work together.
