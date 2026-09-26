# User Request

Audit and repair the complete supplied executable skill package. Inspect SKILL.md and all relevant scripts before deciding whether to edit documentation, executable artifacts, both, or neither. Preserve public interfaces, compatibility behavior, and unrelated branches. Leave an already-correct package unchanged.

## Observed problem

The service-health query and reporting commands can become inconsistent across multiple package layers under non-default or malformed inputs.

## Expected behavior

Service-health parsers must preserve every documented CLI control as an independent binding and forward each value only to its matching query or reporting behavior.
