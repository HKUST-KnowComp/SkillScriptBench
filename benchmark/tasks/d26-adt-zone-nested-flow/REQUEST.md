# User Request

Repair the public `calculate_adt_zone` helper. It already accepts the optional `threshold` parameter using `5` as the compatibility default, but a non-default value currently does not reach the decision threshold behavior. Restore that data flow so callers can vary this behavior, while calls that omit the parameter remain backward compatible. Keep `SKILL.md` and the public signature consistent, and preserve unrelated package behavior.
