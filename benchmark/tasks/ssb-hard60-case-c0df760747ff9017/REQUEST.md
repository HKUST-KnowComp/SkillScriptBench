# User Request

Audit and repair the complete supplied executable skill package. Inspect SKILL.md and all relevant scripts before deciding whether to edit documentation, executable artifacts, both, or neither. Preserve public interfaces, compatibility behavior, and unrelated branches. Leave an already-correct package unchanged.

## Observed problem

The sector parsing, cycle estimation, and ranking can become inconsistent across multiple package layers under non-default or malformed inputs.

## Expected behavior

Malformed sector rows must recover independently, cycle evidence must follow canonical phase ordering, and final sector rankings must list highest scores first.
