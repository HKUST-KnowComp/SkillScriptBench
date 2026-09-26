# SkillScriptBench

- `code/`: LLM requirement discovery, AST-guided script revision, Markdown alignment, and prompt templates aligned with the paper.
- `benchmark/`: 300 task inputs used in Table 2. Each task includes its maintenance request and complete skill package. `TASKS.json` provides paths and input hashes.
- `results/`: 18,000 per-run outcomes across four models, five methods, and three runs, with a script that reproduces Table 2.

The main-results task set contains 150 In-the-Wild Repair tasks and 50 tasks each for Doc, Script, and Joint repair. Task inputs and result labels are stored separately.

## Run the method

Use Python 3.12 and Node.js 20 or newer. Install the dependencies:

```sh
python3 -m pip install -r code/requirements.txt
npm ci --prefix code/runtime/ASTSkill/skillscriptbench/js_parser
```

AST-Guided Skill Revision takes the original package, a maintenance request, and an initial package revision from Raw Package or CoEvoSkills. It uses LLM-based requirement discovery, parser-bound script edits, and Markdown alignment to produce the final package.

See [the usage guide](code/USAGE.md) for the input layout, CLI commands, and model callback interface. To walk through the API without model calls:

```sh
python3 code/examples/offline_walkthrough.py
```

## Reproduce the reported table

```sh
python3 results/reproduce_table2.py
```

The script recomputes Avg, P@3, and Hit³ from the released per-run outcomes and checks every Table 2 value. See [field definitions](results/DATA_DICTIONARY.md) and [release versions](VERSIONS.md).

Optional offline code checks:

```sh
python3 -m pytest code/test_entrypoint.py code/test_prompt_alignment.py -q
```

Run benchmark packages in an isolated environment without credentials. Third-party files retain their original notices and license terms.
