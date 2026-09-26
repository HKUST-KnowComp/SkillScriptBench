# User Request

Audit and repair the complete supplied executable skill package. Inspect SKILL.md and all relevant scripts before deciding whether to edit documentation, executable artifacts, both, or neither. Preserve public interfaces, compatibility behavior, and unrelated branches. Leave an already-correct package unchanged.

## Observed problem

The hardware classification can become inconsistent across multiple package layers under non-default or malformed inputs.

## Expected behavior

Across hardware classification, caller-provided configuration must propagate through every internal helper boundary while default invocations remain compatible.
