# Scoring a revised package

The evaluator is separate from method-visible task inputs. It contains
task-specific checks, fixtures, upstream test material, and fixed runtime
bindings. The method receives only the request and input package.

## Setup

Use Linux x86-64, Python 3.12, and Docker. Install the source package, extract
the evaluator asset archive, and load the separate container archive:

```bash
python -m pip install -e '.[evaluation]'
tar -xzf SkillScriptBench_evaluator.tar.gz
python scripts/load_runtimes.py --manifest /path/to/runtime/RUNTIME_PARTS.json --load
skillscriptbench evaluation --bundle /path/to/evaluator verify
skillscriptbench evaluation --bundle /path/to/evaluator doctor
```

Place all numbered runtime parts beside `RUNTIME_PARTS.json`. The loader streams
them without creating another full-size archive. To verify without loading:

```bash
python scripts/load_runtimes.py --manifest /path/to/runtime/RUNTIME_PARTS.json
```

If supplied as one file instead, use `docker image load -i SkillScriptBench_runtime_images.tar.gz`.
The compressed runtime archive is about 13.8 GB; Docker also needs space
for the extracted layers. Evaluator assets and per-task workspaces require
additional disk space.

No images are pulled automatically. The evaluator requires the exact image IDs
recorded in the asset bundle. Keep credentials outside the environment; candidate
code runs with networking disabled and resource limits.

## Evaluate one candidate

Supply a complete package containing `SKILL.md`, scripts, and required auxiliary
files. Use a new output directory for every evaluation:

```bash
skillscriptbench evaluation --bundle /path/to/evaluator prepare TASK_ID \
  --candidate /path/to/revised/package --output /path/to/new/job
skillscriptbench evaluation score --job /path/to/new/job
```

`prepare` snapshots the candidate and materializes its bound evaluator assets.
It rejects changed assets, overlapping paths, and existing output directories.
`score` executes fresh checks and writes `SCORE.json`:

| Result | Meaning |
| --- | --- |
| `pass` | All required components pass. |
| `fail` | Checks complete and at least one required condition fails. |
| `error` | A required check cannot be completed; inspect the component logs. |

The score records task identity, candidate hash, component statuses, and whether
the candidate remained unchanged. Component JSON files and logs retain execution
evidence. Errors are not converted into successes or failures. Saved main-result
outcomes are never inputs to candidate scoring.

## Component inspection

For debugging, prepare a separate job and invoke one component:

```bash
skillscriptbench evaluation behavior --job /path/to/new/job
skillscriptbench evaluation document --job /path/to/another/new/job
```

Component results are not whole-task scores. For Doc tasks, the documentation
adapter also executes old-invocation regression checks using bound reference
documentation and the candidate's scripts. Use `score` for the complete protocol;
do not combine archived component results.

## Asset integrity and validation

`verify` checks object hashes and task bindings. `doctor` checks runtime
availability. Neither executes candidates. The release validation report records
actual executions separately from these checks.

The asset layout preserves versioned checker code, relative command links, and
container-only temporary-cache links. It does not mount historical experiment
directories. Checks and fixtures remain outside the package being scored.

`ASSEMBLY_CHANGES.json` records packaging transformations and a release-only
fixture correction: the rendered deployment service accepts the pinned
boilerplate's sampled `POST /.rum/100` telemetry request. The request is logged
with rendering events; unrelated endpoints still use the strict deployment
router. This leaves task assertions and saved paper results unchanged.
