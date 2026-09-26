# Recover from unreadable exposure inputs

Load optional packaged exposure-coach JSON inputs that may be missing, unreadable, or malformed.

Required behavior:
- Report unreadable or malformed inputs through the existing warning channel and return None rather than propagating the exception.
- Preserve missing-path behavior, valid JSON loading, warning content, and the public function interface.
