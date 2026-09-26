# Recover cleanly when the Forge template registry is unavailable

Use the packaged Forge app builder in a non-interactive workflow that retrieves the public template registry. The retrieval may fail before any template data is available.

Required behavior:
- On retrieval failure, emit an actionable package-level error that identifies the registry URL and terminate through the skill's stable CLI failure path instead of leaking the raw network exception.
- Successful registry retrieval and the existing public command interface must remain unchanged.
