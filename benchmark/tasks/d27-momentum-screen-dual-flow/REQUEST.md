# User Request

Repair every listed public helper contract in this skill package:

- `score_to_rating`: its optional `threshold` parameter uses `90` as the compatibility default, but non-default values currently do not reach the documented reusable behavior.
- `score_to_state`: its optional `threshold` parameter uses `80` as the compatibility default, but non-default values currently do not reach the documented reusable behavior.

Restore all listed data flows in one coherent package edit. Calls that omit each parameter must remain backward compatible. Keep `SKILL.md`, public signatures, defaults, and unrelated package behavior unchanged. The request intentionally does not identify source files, helper implementations, or AST locations.
