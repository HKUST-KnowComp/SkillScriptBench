# User Request

Audit and repair the complete supplied skill package. Inspect SKILL.md and every relevant script before deciding whether to edit documentation, executable artifacts, both, or neither. Preserve public interfaces and unrelated behavior. Leave an already-correct package unchanged.

## Observed problem

Adding a BED input can derive its local path from metadata instead of raw input.

## Required behavior

Derive the BED local path from the supplied raw BED input.
