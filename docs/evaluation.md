# Evaluation

A task succeeds when both behavioral and documentation-driven checks pass.
Clean tasks measure preservation; Doc, Script, and Joint tasks measure repair
while preserving correct parts of the package.

## Setup

Use Linux x86-64, Python 3.12, Docker, and the GitHub CLI (`gh`). The private
repository requires authenticated access. Start with the two-task example:

```bash
python -m pip install -e '.[evaluation]'
python scripts/fetch_assets.py --demo --plan
python scripts/fetch_assets.py --demo --load
python examples/check_installation.py --bundle assets/evaluator \
  --output runs/installation-example
```

The example uses two independently packaged images. To install other task
environments, repeat `--task`, or use `--all` for the full benchmark:

```bash
python scripts/fetch_assets.py --task d16-matlab-multiroute --plan
python scripts/fetch_assets.py --all --load
```

`--plan` lists selected tasks, missing images, and maximum download size without
writing files or downloading. Already installed exact image IDs are reused.
When an independent asset is unavailable, the plan explicitly selects the
complete 13.8 GB runtime archive. Allow additional space for Docker layers and
jobs. Downloads are checksum-verified; existing cache files are preserved.
Use a new `--dest` directory if a later release reports a conflicting cached file.

The evaluator is installed at `assets/evaluator`. Check only the environments
for the tasks you intend to run:

```bash
skillscriptbench evaluation --bundle assets/evaluator doctor \
  --task ssb-deep60-case-18b5b153d97659d7 \
  --task ssb-deep60-case-f8e43276eccd8365
```

Omit `--task` to check all environments. `verify` checks asset integrity;
`doctor` checks dependencies. Candidates run
in isolated containers with networking disabled. Keep credentials outside the
execution environment. Images must match the pinned IDs and are not pulled
automatically.

## Single-task evaluation

Supply the complete package and a new job directory:

```bash
python evaluate.py --bundle assets/evaluator --task TASK_ID \
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
python evaluate.py --bundle assets/evaluator --manifest candidates.json \
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

## Inspect a task's checks

```bash
skillscriptbench evaluation --bundle assets/evaluator inspect \
  ssb-deep60-case-18b5b153d97659d7 --format text
```

The view lists behavioral and documentation components, the selected task keys,
checker files, and required images. Add `--files` for the complete file index.
Read any listed logical file with `inspect TASK_ID --read-file LOGICAL_PATH`;
the command verifies its checksum before printing it. The view never executes
task code or copies checkers into candidate packages.

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
