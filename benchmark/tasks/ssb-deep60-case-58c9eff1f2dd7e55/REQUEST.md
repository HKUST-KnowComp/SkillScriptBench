# User Request

Audit and repair the complete supplied skill package. Inspect SKILL.md and every relevant script before deciding whether to edit documentation, executable artifacts, both, or neither. Preserve public interfaces and unrelated behavior. Leave an already-correct package unchanged.

## Observed problem

Matching and synthesis can silently exchange their execution components.

## Required behavior

Use the embedder for skill matching and the backend for synthesis.
