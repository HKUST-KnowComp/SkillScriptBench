# User Request

Repair every listed public helper contract in this skill package:

- `sfio_options`: its optional `field` parameter uses `'sfio'` as the compatibility default, but non-default values currently do not reach the documented reusable behavior.
- `suggested_sfio`: its optional `field` parameter uses `'customer_usage'` as the compatibility default, but non-default values currently do not reach the documented reusable behavior.

Restore all listed data flows in one coherent package edit. Calls that omit each parameter must remain backward compatible. Keep `SKILL.md`, public signatures, defaults, and unrelated package behavior unchanged. The request intentionally does not identify source files, helper implementations, or AST locations.
