# Running SkillScriptBench

## Task loading

Use Python 3.12+:

```bash
python -m pip install -e .
skillscriptbench list --split main_results
skillscriptbench show d16-matlab-multiroute
skillscriptbench materialize d16-matlab-multiroute --output runs/example
python results/reproduce_table2.py
```

The benchmark path defaults to `./benchmark`. From elsewhere, use `skillscriptbench --benchmark /path/to/benchmark ...`.

## Revision setup

```bash
python -m pip install -e '.[revision]'
npm ci --prefix code/runtime/ASTSkill/skillscriptbench/js_parser
```

Use Node.js 22.18+ within 22.x or Node.js 24.11+. For a wheel installation, locate the bundled parser with:

```bash
python -c 'from ssbench._runtime import run_revision; print(run_revision.HERE / "runtime/ASTSkill/skillscriptbench/js_parser")'
```

Then run `npm ci --prefix` on that directory. The checkout is the simplest option when using benchmark data.

## Prepare a proposal pair

Raw Package + AST revises Raw Package's generated package; CoEvoSkills + AST revises CoEvoSkills' returned package. Supply the complete proposal, original package, and request:

```bash
skillscriptbench revise prepare \
  --parent runs/example/package \
  --proposal /path/to/baseline-generated/package \
  --request runs/example/REQUEST.md \
  --model YOUR_EXACT_MODEL_ID \
  --output runs/revision
```

Preparation fixes the model and hashes without model calls. The output directory must be new. The task loader does not generate a baseline proposal; baseline instructions are in `code/prompts/`.

The original package is not a substitute for the baseline-generated proposal when running either +AST configuration. For direct script use, replace `skillscriptbench revise` with `python code/run_revision.py` in these commands.

## Execute the revision

Use a Chat Completions-compatible HTTPS endpoint supporting streaming and function tools:

```bash
skillscriptbench revise run \
  --output runs/revision \
  --base-url https://YOUR_PROVIDER_HOST/v1 \
  --credential-fd 3
```

Your credential manager or launcher must supply the API key through already-open file descriptor 3. The runtime reads it in memory. The response model ID must match the prepared model; redirects are refused. Use the [callback API](method.md#public-api) for other provider protocols.

## Outputs

| File | Meaning |
| --- | --- |
| `final/package/` | Complete revised package |
| `FINAL.json` | Package path, hash, revision status |
| `INPUT.json`, `PROTOCOL.json` | Inputs and configuration |
| `DISCOVERY.json`, `branches/` | Discovery and script records |
| `DOCUMENT_REPORT.json` | Markdown review and validated edits |

Evaluate the returned package separately. [Evaluation coverage →](evaluation.md)

## Offline example

```bash
python code/examples/offline_walkthrough.py
```

Fixed responses on a toy package demonstrate the API. This does not call a model or measure benchmark performance.

## Troubleshooting

- **Unknown task:** use an exact ID from `skillscriptbench list`.
- **Hash mismatch:** use a fresh input checkout; keep revisions in a separate workspace.
- **Existing output:** choose a new path.
- **Parser unavailable:** install revision extras and the pinned Node dependencies.
- **Model mismatch:** prepare with the exact ID returned by your provider.
