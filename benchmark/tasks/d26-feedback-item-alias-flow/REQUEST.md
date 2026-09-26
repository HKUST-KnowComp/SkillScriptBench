# User Request

Repair the public `extract_feedback_item` helper. It already accepts the optional `field` parameter using `'outdated'` as the compatibility default, but a non-default value currently does not reach the schema field behavior. Restore that data flow so callers can vary this behavior, while calls that omit the parameter remain backward compatible. Keep `SKILL.md` and the public signature consistent, and preserve unrelated package behavior.
