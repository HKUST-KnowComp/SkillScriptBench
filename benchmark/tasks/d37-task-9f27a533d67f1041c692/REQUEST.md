# Repair the edge-candidate-agent executable skill

The reusable `edge-candidate-agent` package is producing inconsistent behavior in ordinary documented workflows, especially around CLI status and command outcomes, error propagation and documented behavior, routing and branch selection. Audit the complete SKILL.md and scripts and repair the smallest coherent set of implementation defects.

Required behavior:
- Restore behavior implied by the package's own documentation, names, control flow, and data flow.
- Preserve public interfaces, defaults, output schemas, and unrelated behavior.
- Keep changes bounded to runtime scripts and do not add test-specific branches or new dependencies.
