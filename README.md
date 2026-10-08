<div align="center">

# SkillScriptBench

### Benchmarking Self-Evolution of Executable Agent Skill Packages Beyond Markdown

**350 tasks · Markdown + scripts · Repair + preservation**

[Paper](https://arxiv.org/abs/2610.04008) · [Benchmark](benchmark/README.md) · [Quick start](#quick-start) · [Method](docs/method.md) · [Results](docs/results.md) · [Citation](#citation)

</div>

Executable agent skills are packages, not just prompts. **SkillScriptBench** evaluates whether a method can repair a package's documentation and scripts while preserving what already works. **AST-Guided Skill Revision** grounds edits in executable structure and aligns Markdown with the revised implementation.

![SkillScriptBench: construction, evaluation, and AST-guided revision](docs/assets/overview.png)

## Highlights

- **Repository-sourced repair.** 150 In-the-Wild Repair tasks from 100 packages, selected after surveying over 35,000 GitHub-hosted Skill roots.
- **Controlled artifact states.** 50 packages × four states = 200 tasks: Clean, Doc, Script, and Joint. Separate repair from preservation.
- **Joint package revision.** LLM requirement discovery, AST-bound script edits, and Markdown alignment form a single pipeline. Discovery uses an LLM, not regex-based semantic rules.
- **Ready-to-load inputs.** All 350 requests and packages, with a Python loader, workspace materialization, and input-integrity checks.
- **Executable evaluation.** A scoring CLI with task-specific checks, fixtures, offline dependencies, and pinned Docker runtimes supplied as separate assets.
- **Traceable sources.** Upstream attribution for all tasks, with 50 Controlled group mappings and retained source-license notices.
- **Reproducible main results.** Recompute all 300 numerical Table 2 cells from 18,000 released outcomes without model calls.

## Choose a starting point

| Goal | Entry point |
| --- | --- |
| Browse or load tasks | [Data card](benchmark/README.md) and `skillscriptbench list` |
| Create a task workspace | `skillscriptbench materialize TASK_ID --output runs/task` |
| Revise a baseline-generated package | [Running guide](docs/running.md) |
| Understand the model interface | [Method](docs/method.md) and [prompts](code/prompts/) |
| Reproduce Table 2 | `python3 results/reproduce_table2.py` |
| Understand scoring and release coverage | [Evaluation](docs/evaluation.md) |
| Evaluate revised packages | [Evaluator setup and scoring](docs/evaluator-integration.md); requires separate evaluator and runtime assets |

## Benchmark at a glance

| Track / state | Tasks | What it tests |
| --- | ---: | --- |
| In-the-Wild Repair | 150 | Repair script faults in repository-sourced packages |
| Controlled / Clean | 50 | Preserve correct documentation and behavior |
| Controlled / Doc | 50 | Repair documentation while preserving scripts |
| Controlled / Script | 50 | Repair scripts while preserving correct documentation |
| Controlled / Joint | 50 | Coordinate documentation and script repair |
| **Total** | **350** | **Repair and preservation at package level** |

Packages contain `SKILL.md`, scripts, and auxiliary files. `main_results` selects the 300 faulty-package tasks in Table 2; `clean` selects the 50 preservation tasks. These are evaluation subsets, not training/test splits. [Construction and schema →](benchmark/README.md)

## Main results

Four-model mean on the 300 repair tasks in Table 2; all entries are percentages.

| Method | Avg ↑ | Hit³ ↑ |
| --- | ---: | ---: |
| Markdown-only | 4.5 | 3.0 |
| Raw Package | 54.8 | 44.2 |
| **Raw Package + AST** | **76.7** | **65.0** |
| CoEvoSkills | 48.8 | 34.1 |
| **CoEvoSkills + AST** | **76.5** | **65.7** |

Avg averages success over three runs. Hit³ requires success in **all three** runs. P@3, also in the released table, requires at least one success. The full table reports each model and repair state separately. [Metrics and reproduction →](docs/results.md)

## Quick start

From this checkout, use Python 3.12+:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

# Browse inputs without model calls or task execution.
skillscriptbench list --split all
skillscriptbench show d16-matlab-multiroute

# Verify input integrity and reconstruct the published table.
skillscriptbench verify --split all
skillscriptbench verify-sources
python results/reproduce_table2.py

# Create a fresh workspace containing only request + package.
skillscriptbench materialize d16-matlab-multiroute --output runs/example
```

The Python task interface has no third-party runtime dependencies:

```python
from ssbench import Benchmark

benchmark = Benchmark("benchmark")
task = benchmark.task("d16-matlab-multiroute")
request = task.request.read_text()
package_path = task.package
```

### Run AST-Guided Skill Revision

Install the optional revision dependencies. JavaScript parsing additionally uses Node.js 22.18+ in the 22.x line or Node.js 24.11+:

```bash
python -m pip install -e '.[revision]'
npm ci --prefix code/runtime/ASTSkill/skillscriptbench/js_parser

skillscriptbench revise prepare \
  --parent runs/example/package \
  --request runs/example/REQUEST.md \
  --proposal /path/to/baseline-generated/package \
  --model YOUR_EXACT_MODEL_ID \
  --output runs/revision-example
```

Preparation makes no model calls. Execute it using the [provider or callback interface](docs/running.md). An offline example is available as `python code/examples/offline_walkthrough.py`.

## Repository layout

```text
ssbench/       Public task loader, CLI, and revision API
benchmark/     350 inputs, split manifests, metadata, and source inventory
code/          Reviewed runtime, prompt templates, and offline example
results/       Main-result outcomes and Table 2 reconstruction
docs/          Method, running, evaluation, and release documentation
tests/         Public-interface checks
scripts/       Release packaging and split-runtime loading
```

The source repository/archive carries benchmark data and outcomes. Python distributions carry code; use a checkout or source-release archive for the data. [Release contents →](docs/release.md)

## Citation

```bibtex
@misc{liu2026skillscriptbench,
  title = {SkillScriptBench: Benchmarking Self-Evolution of Executable Agent Skill Packages Beyond Markdown},
  author = {Liu, Yuxuan and Li, Haoran and Zhang, Yuhao and Guo, Jiahe and Luo, Hongyu and Hu, Wenbin and Jing, Huihao and Chung, Kawai and Chen, Junle and Fan, Changxuan and Zong, Qing and Xie, Lingyun and Song, Yangqiu},
  year = {2026},
  eprint = {2610.04008},
  archivePrefix = {arXiv},
  url = {https://arxiv.org/abs/2610.04008}
}
```

## Licensing and attribution

A project-level license has not yet been selected. Third-party package files retain their original notices and terms; the [source inventory](benchmark/SOURCES.json) records available repository, revision, and license metadata. See [NOTICE](NOTICE) for scope. Execute task code only in isolated environments without credentials. [Security guidance →](SECURITY.md)
