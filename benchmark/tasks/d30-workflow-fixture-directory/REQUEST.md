# Create dry-run fixture files in a dedicated directory

Use the packaged skill-integration tester to generate synthetic dry-run fixture files for multiple workflows in a fresh output location.

Required behavior:
- Ensure the dedicated fixtures directory exists, including missing parents, before writing any fixture.
- Preserve cross-workflow deduplication, returned paths, JSON formatting, and the public function interface.
