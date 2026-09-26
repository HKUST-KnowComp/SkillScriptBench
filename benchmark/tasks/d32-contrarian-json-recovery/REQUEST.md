# Degrade malformed contrarian inputs

Load packaged contrarian-gate JSON input that may be unreadable, malformed, or excessively nested.

Required behavior:
- Convert JSON decoding and recursion failures into the established parse_error result instead of propagating the parser exception.
- Preserve unreadable and non-finite classifications, valid payload loading, and the public tuple return contract.
