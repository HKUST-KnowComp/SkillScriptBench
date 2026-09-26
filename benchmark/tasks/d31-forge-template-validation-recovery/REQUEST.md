# Recover when Forge template validation is unavailable

Validate a Forge template through the packaged registry lookup when registry access itself may fail.

Required behavior:
- Report the validation problem and retain the package's permissive fallback result when validation cannot be completed.
- Preserve normal template matching, suggestion ranking, and the public tuple return contract.
