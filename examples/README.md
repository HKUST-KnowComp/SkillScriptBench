# Check an evaluation installation

This example scores two unchanged benchmark inputs from the same Controlled
package group. The Clean input should pass, and the Script-fault input should
fail. The package handles DICOM pseudonyms; the fault concerns deriving a token
from a field keyword instead of its configured scope.

| Input | Task ID | Expected status |
| --- | --- | --- |
| Clean | `ssb-deep60-case-18b5b153d97659d7` | `pass` |
| Script fault | `ssb-deep60-case-f8e43276eccd8365` | `fail` |

Run from the repository root after completing the
[Linux, Docker, and evaluator setup](../docs/evaluation.md#setup):

```bash
python scripts/fetch_assets.py --demo --load
python examples/check_installation.py --bundle assets/evaluator --output runs/installation-example
```

Use a new output directory each time. The script verifies the original input
hashes, writes `candidates.json` with absolute paths to the original packages,
and calls the normal evaluator once per task. The evaluator snapshots candidates
before execution; the example does not edit the original packages or call a
model. An expected `fail` means scoring completed and rejected the faulty input;
an `error` does not satisfy this example.

Outputs are under `runs/installation-example/`:

```text
candidates.json
evaluation/scores.csv
evaluation/summary.json
evaluation/jobs/<task_id>/run-1/
```

The script exits successfully only if both outcomes match. To recheck existing
output without rerunning evaluation:

```bash
python examples/check_results.py runs/installation-example/evaluation/scores.csv
```

To inspect the manifest on any supported Python host before installing the
evaluation assets:

```bash
python -m pip install -e .
python examples/check_installation.py --prepare-only --output runs/installation-inputs
```

Preparation verifies the two inputs and creates their manifest. Run the full
example above to check execution and the expected outcomes.
