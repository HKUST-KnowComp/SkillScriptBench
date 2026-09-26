# User Request

Audit and repair the complete supplied executable skill package. Inspect SKILL.md and all relevant scripts before deciding whether to edit documentation, executable artifacts, both, or neither. Preserve public interfaces, compatibility behavior, and unrelated branches. Leave an already-correct package unchanged.

## Observed problem

The Forge template discovery and preflight validation can become inconsistent across multiple package layers under non-default or malformed inputs.

## Expected behavior

Template discovery, prerequisite checks, and template validation must preserve their documented recovery results when local operations fail.


## Package Maintenance Contract

Audit all documented local invocations in this package, not only the example above.
With valid user inputs substituted for placeholders, commands, arguments, paths,
and generated artifacts must agree with the supplied implementation and the
stated use case. Preserve existing supported invocations and unrelated behavior.
Do not delete a documented workflow merely to avoid repairing it.
