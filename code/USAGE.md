# Running AST-Guided Skill Revision

## Installation

Use Python 3.12 and Node.js 22.18+ within the 22.x series, or Node.js 24.11+.
From the extracted repository or code-only archive root:

```sh
python3 -m pip install -r code/requirements.txt
npm ci --prefix code/runtime/ASTSkill/skillscriptbench/js_parser
python3 code/examples/offline_walkthrough.py
```

The code-only archive includes the revision stage and example. The complete
release additionally includes `benchmark/` and `results/` used below.

## Inputs

The revision stage takes three inputs:

| Argument | Content |
| --- | --- |
| `--parent` | The original task package, containing `SKILL.md` and scripts. |
| `--request` | The task's `REQUEST.md`. |
| `--proposal` | The complete initial revision produced by a baseline generator. |

Use the paths in `benchmark/TASKS.json` to select a task. For Raw Package + AST,
the proposal is Raw Package's generated package; for CoEvoSkills + AST, it is
the package returned by CoEvoSkills. The original input is not a substitute
for that proposal when reproducing either configuration. Baseline instructions
are provided in `prompts/raw_package.txt` and `prompts/coevo_*.txt`.

## CLI

From the repository root, prepare a new run and select its model:

```sh
python3 code/run_revision.py prepare \
  --parent /path/to/task/package \
  --proposal /path/to/baseline/package \
  --request /path/to/task/REQUEST.md \
  --model gpt-5.6-sol \
  --output /path/to/new-run
```

The output directory must not exist. Preparation records the model and input
hashes in `PROTOCOL.json` and makes no model calls.

Run the prepared inputs through an API supporting Chat Completions, streaming,
and function tools:

```sh
python3 code/run_revision.py run \
  --output /path/to/new-run \
  --base-url https://YOUR_PROVIDER_HOST/v1 \
  --credential-fd 3
```

Replace the API base URL with your provider's HTTPS endpoint. File descriptor 3
must already supply the API key through your credential manager or launcher.
The key is read in memory; do not place it in the command or repository.
`run` uses the model fixed during `prepare`. The transport checks that the
response model ID exactly matches that identifier. For other provider
protocols, use the callback interface below.
HTTP redirects are refused to prevent forwarding credentials to another endpoint.
Configure the provider's final API URL directly.

## Model callback

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path("code").resolve()))
from run_revision import prepare, run_prepared

run = Path("/path/to/new-run")
prepare("/path/to/task/package", "/path/to/baseline/package",
        Path("/path/to/task/REQUEST.md").read_text(), run,
        model="YOUR_EXACT_MODEL_ID")

def call_model(prompt, tool_schema, stage):
    # Send prompt and tool_schema to your model client.
    # Return the parsed JSON arguments of its single function-tool call.
    return your_model_client(prompt, tool_schema, stage)

result = run_prepared(run, call_model)
print(result["package"])
```

Stages are `discovery`, zero to two script-revision stages (`parent-signal`
and `raw-residual`), and `document-alignment`. The callback returns tool
arguments as a Python dictionary, not an API response envelope. The complete
offline example in `examples/offline_walkthrough.py` demonstrates this contract
with fixed responses on a toy package.

## Outputs

- `final/package/`: the revised skill package.
- `INPUT.json` and `PROTOCOL.json`: prepared inputs and run configuration.
- `DISCOVERY.json`, `branches/`, and `DOCUMENT_REPORT.json`: revision records.
- `FINAL.json`: final package path, hash, and revision status.

The revision stage produces a package; task success is determined by subsequent
evaluation. To reconstruct the published metrics from the archived outcomes,
run `python3 results/reproduce_table2.py`.
