# Report onboarding manifest load failures

Run the packaged onboarding CLI with a manifest that may be unreadable or invalid.

Required behavior:
- Record the manifest error through the package report and terminate through the stable finish path with the existing usage-error code.
- Preserve valid-manifest commands, JSON output mode, dry-run behavior, and the CLI interface.
