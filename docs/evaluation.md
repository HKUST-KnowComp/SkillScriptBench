# Evaluation

## Task success

The paper evaluates the whole package. Success requires both:

1. **Behavioral checks:** scripts satisfy requested behavior and preserve required existing behavior.
2. **Documentation-driven checks:** documented usage agrees with executable behavior and satisfies documentation requirements.

Controlled Clean tasks measure preservation; Doc, Script, and Joint measure repair while retaining correct parts.

## Release coverage

| Capability | Included |
| --- | --- |
| Load all 350 task requests and packages | Yes |
| Verify package and request integrity | Yes |
| Run AST-Guided Skill Revision with a provider | Yes |
| Reconstruct Table 2 from 18,000 outcomes | Yes |
| Run task-specific behavioral/documentation checks | Scoring CLI included; evaluator assets and fixed containers are separate |
| Reconstruct Clean-state scores from per-run outcome exports | Not yet bundled |

The outcome snapshot reconstructs the published table; it does not score newly generated candidates.

## Evaluator packaging

Follow the [evaluator setup guide](evaluator-integration.md) to prepare and score
a new candidate. The separate asset bundle binds checks, fixtures, dependencies,
and entrypoints to all 350 task IDs. Containers are a separate archive rather
than large binary files in the source repository.

Execution validation is recorded in the release report. Asset-integrity checks
and saved outcome reconstruction are separate from fresh candidate evaluation.

## Metrics

For N tasks with three binary outcomes each:

- **Avg:** successful runs divided by 3N.
- **P@3:** tasks with at least one success divided by N.
- **Hit³:** tasks with three successes divided by N.

Multiply by 100 for percentages. Pool task/run counts within each reported group. Clean is separate from the main repair table.
