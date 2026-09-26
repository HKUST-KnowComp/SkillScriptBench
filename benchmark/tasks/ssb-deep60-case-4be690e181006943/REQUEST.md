# User Request

Audit and repair the complete supplied skill package. Inspect SKILL.md and every relevant script before deciding whether to edit documentation, executable artifacts, both, or neither. Preserve public interfaces and unrelated behavior. Leave an already-correct package unchanged.

## Observed problem

Scanning one MATLAB source can resolve files relative to the wrong root or report unstable relative identifiers.


## Package Maintenance Contract

Audit all documented local invocations in this package, not only the example above.
With valid user inputs substituted for placeholders, commands, arguments, paths,
and generated artifacts must agree with the supplied implementation and the
stated use case. Preserve existing supported invocations and unrelated behavior.
Do not delete a documented workflow merely to avoid repairing it.
