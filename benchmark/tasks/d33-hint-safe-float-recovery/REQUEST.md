# Keep optional numeric hints recoverable

Normalize optional numeric values while building the packaged edge hints.

Required behavior:
- Return the supplied default when float conversion raises TypeError or ValueError instead of propagating it.
- Preserve valid numeric conversion, the default value parameter, and the public float return contract.
