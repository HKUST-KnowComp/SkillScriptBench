# User Request

Audit and repair the complete supplied executable skill package. Inspect SKILL.md and all relevant scripts before deciding whether to edit documentation, executable artifacts, both, or neither. Preserve public interfaces, compatibility behavior, and unrelated branches. Leave an already-correct package unchanged.

## Observed problem

The hint normalization and news evidence assembly can become inconsistent across multiple package layers under non-default or malformed inputs.

## Expected behavior

Malformed timestamp and numeric values must recover safely, and normalized news evidence must retain every required identity field throughout downstream assembly.
