# Reject invalid FX calendar importance tiers before network access

Use the packaged FX macro calendar with an optional minimum-importance tier supplied by a caller.

Required behavior:
- When a tier is supplied, accept only the documented tier domain and reject invalid values before making any network request.
- Preserve the behavior for no tier, valid tiers, currency normalization, limits, and the public API.
