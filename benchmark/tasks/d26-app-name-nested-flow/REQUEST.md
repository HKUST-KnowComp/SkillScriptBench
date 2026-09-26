# User Request

Repair the public `app_name_from_image` helper. It already accepts the optional `delimiter` parameter using `':'` as the compatibility default, but a non-default value currently does not reach the format or path policy behavior. Restore that data flow so callers can vary this behavior, while calls that omit the parameter remain backward compatible. Keep `SKILL.md` and the public signature consistent, and preserve unrelated package behavior.
