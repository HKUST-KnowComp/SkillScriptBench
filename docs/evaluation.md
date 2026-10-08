# Evaluation

A task succeeds when both behavioral and documentation-driven checks pass.
Clean tasks measure preservation; Doc, Script, and Joint tasks measure repair
while preserving correct parts of the package.

## Setup

Use Linux x86-64, Python 3.12, and Docker. The evaluator archive and runtime
parts are separate assets, not files in the Git checkout. Obtain
`SkillScriptBench_evaluator.tar.gz`, `RUNTIME_PARTS.json`, and its numbered
runtime parts from the maintainers. Keep the runtime parts beside the manifest.

```bash
python -m pip install -e '.[evaluation]'
tar -xzf SkillScriptBench_evaluator.tar.gz
python scripts/load_runtimes.py --manifest /path/to/runtime/RUNTIME_PARTS.json --load
skillscriptbench evaluation --bundle ./evaluator verify
skillscriptbench evaluation --bundle ./evaluator doctor
```

The compressed runtime is about 13.8 GB; allow additional space for Docker layers
and job directories. The loader verifies and streams the parts without creating
another combined archive. Omit `--load` for verification only. If supplied as a
single archive, use `docker image load -i SkillScriptBench_runtime_images.tar.gz`.

`verify` checks asset integrity; `doctor` checks dependencies. Candidates run
in isolated containers with networking disabled. Keep credentials outside the
execution environment. Images must match the pinned IDs and are not pulled
automatically.

## Score a revised package

Supply the complete package and a new job directory:

```bash
skillscriptbench evaluation --bundle ./evaluator prepare TASK_ID \
  --candidate /path/to/revised/package --output runs/evaluation
skillscriptbench evaluation score --job runs/evaluation
```

`prepare` snapshots the package and its task-specific evaluation assets. `score`
runs fresh checks and writes `SCORE.json`, component results, and execution logs.
The method receives only the request and input package; evaluation assets remain
separate.

| Status | Meaning |
| --- | --- |
| `pass` | All required checks pass |
| `fail` | Checks complete and a required condition fails |
| `error` | A required check cannot complete; inspect the logs |

For Doc tasks, scoring also checks existing invocations using reference
documentation and the candidate's scripts. Saved main-result outcomes are not
used to score new candidates.

To debug a component, prepare a separate fresh job and use
`skillscriptbench evaluation behavior --job PATH` or
`skillscriptbench evaluation document --job PATH`. Use `score` for the whole task.
Fixture changes and validation records accompany the separate evaluator assets.

## Metrics

Across N tasks with three runs each:

- **Avg:** successful runs divided by 3N.
- **P@3:** tasks with at least one success divided by N.
- **Hit³:** tasks with all three runs successful divided by N.

Report percentages, with Clean preservation separate from repair results.
Use the `main_results` subset for the 300 repair tasks and `clean` for the 50 preservation tasks.
