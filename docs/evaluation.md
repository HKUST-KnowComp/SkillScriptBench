# Evaluation

A task succeeds when both behavioral and documentation-driven checks pass.
Clean tasks measure preservation; Doc, Script, and Joint tasks measure repair
while preserving correct parts of the package.

## Setup

Use Linux x86-64, Python 3.12, and Docker. Download the evaluator, fixtures, and
pinned runtime images from the [Release](https://github.com/xuansenpa1/skillscriptbench-review/releases/tag/benchmark-v0.1.0).
The private repository requires authenticated access:

```bash
gh release download benchmark-v0.1.0 --repo xuansenpa1/skillscriptbench-review \
  --pattern 'SkillScriptBench_evaluator.tar.gz' --pattern 'RUNTIME_PARTS.json' \
  --pattern 'SkillScriptBench_runtime_images.tar.gz.part*' --dir assets
python -m pip install -e '.[evaluation]'
tar -xzf assets/SkillScriptBench_evaluator.tar.gz
python scripts/load_runtimes.py --manifest assets/RUNTIME_PARTS.json --load
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

## Single-task evaluation

Supply the complete package and a new job directory:

```bash
python evaluate.py --bundle ./evaluator --task TASK_ID \
  --candidate /path/to/revised/package --output runs/evaluation
```

The evaluator snapshots each package and runs fresh checks. Your method receives
only the request and input package; evaluation assets remain separate.

## Batch evaluation

Create `candidates.json`, listing one package for each task and run:

```json
[
  {"task_id": "TASK_ID", "candidate": "outputs/run1/package", "run": 1},
  {"task_id": "TASK_ID", "candidate": "outputs/run2/package", "run": 2},
  {"task_id": "TASK_ID", "candidate": "outputs/run3/package", "run": 3}
]
```

Replace `TASK_ID` with an ID from `skillscriptbench list`. Candidate paths are
relative to the manifest, or absolute. Each run should contain the output of a
separate attempt by your method; omit `run` for a single attempt.

```bash
python evaluate.py --bundle ./evaluator --manifest candidates.json \
  --output runs/batch --workers 2
```

Add `--dry-run` to validate the manifest without executing packages. Up to four
workers are supported; choose concurrency to match available resources.

## Results

`scores.csv` contains task/run outcomes. `summary.json` reports all tasks,
repair tasks, Clean tasks, and individual states. Per-task directories under
`jobs/` contain `SCORE.json`, component results, and execution logs.

| Status | Meaning |
| --- | --- |
| `pass` | All required checks pass |
| `fail` | Checks complete and a required condition fails |
| `error` | A required check cannot complete; inspect the logs |

For Doc tasks, scoring also checks existing invocations using reference
documentation and the candidate's scripts.

For component-level debugging, the lower-level CLI provides `prepare`, `behavior`,
`document`, and `score` subcommands. See `skillscriptbench evaluation --help`.

## Metrics

Across N tasks with three runs each:

- **Avg:** successful runs divided by 3N.
- **P@3:** tasks with at least one success divided by N.
- **Hit³:** tasks with all three runs successful divided by N.

Report percentages, with Clean preservation separate from repair results.
Use the `main_results` subset for the 300 repair tasks and `clean` for the 50 preservation tasks.

Incomplete repeat groups leave Avg, P@3, and Hit³ unset; a single-run evaluation
reports success rate. Execution errors are reported separately and must be
resolved before aggregate rates are reported.
