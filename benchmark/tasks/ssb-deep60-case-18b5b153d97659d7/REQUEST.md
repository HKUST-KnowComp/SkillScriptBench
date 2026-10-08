# User Request

Audit and repair the complete supplied skill package. Inspect SKILL.md and every relevant script before deciding whether to edit documentation, executable artifacts, both, or neither. Preserve public interfaces and unrelated behavior. Leave an already-correct package unchanged.

## Observed problem

DICOM pseudonyms can be derived from a field keyword instead of their scope.

## Required behavior

Derive each DICOM pseudonym token from the configured pseudonym scope.
