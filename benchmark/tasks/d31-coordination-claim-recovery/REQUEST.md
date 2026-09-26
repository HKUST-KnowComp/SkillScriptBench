# Report rejected coordination claims

Claim coordination-board work through the packaged project-management CLI when the locked update may reject the claim.

Required behavior:
- Print the package-level refusal message to stderr and return the established conflict exit code instead of propagating the runtime exception.
- Preserve successful claims, lock handling, table validation, and the public command contract.
