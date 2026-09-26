# User Request

Repair every listed public helper contract in this skill package:

- `classify`: its optional `field` parameter uses `'vram_gb'` as the compatibility default, but non-default values currently do not reach the documented reusable behavior.
- `classify`: its optional `threshold` parameter uses `8` as the compatibility default, but non-default values currently do not reach the documented reusable behavior.

Restore all listed data flows in one coherent package edit. Calls that omit each parameter must remain backward compatible. Keep `SKILL.md`, public signatures, defaults, and unrelated package behavior unchanged. The request intentionally does not identify source files, helper implementations, or AST locations.
