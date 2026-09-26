# User Request

Audit and repair the complete supplied skill package. Inspect SKILL.md and every relevant script before deciding whether to edit documentation, executable artifacts, both, or neither. Preserve public interfaces and unrelated behavior. Leave an already-correct package unchanged.

## Observed problem

Protocol requests can exchange the endpoint URL and authorization token.

## Required behavior

Forward the endpoint URL as the request destination and the authorization token as the credential at every request boundary.
