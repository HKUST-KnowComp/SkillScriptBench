# SkillScriptBench

- `code/`: LLM requirement discovery, AST-guided script revision, Markdown alignment, and prompt templates aligned with the paper.
- `benchmark/`: 300 task inputs used in Table 2. Each task includes its maintenance request and complete skill package. `TASKS.json` provides paths and input hashes.
- `results/`: 18,000 frozen outcomes across four models, five methods, and three runs, with a script that reproduces Table 2.

The benchmark contains 150 In-the-Wild tasks and 50 each of Doc, Script, and Joint tasks. Clean-state tasks and supplementary ablations are outside this main-results export. Input packages retain their original notices and auxiliary files; outcome labels are separate from method inputs.

## Run the method

Use Python 3.12 and Node.js 20 or newer:

```sh
python3 -m pip install -r code/requirements.txt
npm ci --prefix code/runtime/ASTSkill/skillscriptbench/js_parser
python3 code/run_revision.py prepare --parent /path/original-package --proposal /path/initial-revision --request /path/REQUEST.md --output /path/new-run
```

`prepare` builds the revision inputs without model calls. Use `run_prepared(output, callback)` from `code/run_revision.py` with a model callback `(prompt, tool_schema, stage) -> tool_arguments`. The bundled CLI transport also supports `run --output /path/new-run --model gpt-5.6-sol --credential-fd 3`, with credentials supplied through an already-open file descriptor. The revised package is written to `final/package`.

Semantic discovery uses an LLM; parser-based checks bind locations and constrain edits. The script-revision instruction includes the manuscript update of 2026-09-26. Frozen experiment outcomes remain unchanged.

## Reproduce the reported table

```sh
python3 results/reproduce_table2.py
```

This recomputes Avg, P@3 and Hit³ from all recorded outcomes and checks every Table 2 value. It does not execute or rescore candidate packages. Field definitions are in `results/DATA_DICTIONARY.md`.

Optional offline code checks:

```sh
python3 -m pytest code/test_entrypoint.py code/test_prompt_alignment.py -q
```

Run benchmark packages in an isolated environment without credentials. Third-party notices are retained; this packaging step assigns no new license.
