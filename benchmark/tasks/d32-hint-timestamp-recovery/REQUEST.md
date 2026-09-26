# Handle malformed hint timestamps

Normalize optional timestamps while building packaged edge hints.

Required behavior:
- Return None for an absent, non-string, or malformed ISO timestamp instead of propagating a parse error.
- Preserve valid ISO and Z-suffix parsing and the public date return contract.
