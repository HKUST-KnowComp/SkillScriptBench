# SkillScriptBench

**Benchmarking Self-Evolution of Executable Agent Skill Packages Beyond Markdown**

[Paper](https://arxiv.org/abs/2610.04008) · [Dataset](benchmark/README.md) · [Evaluation](docs/evaluation.md) · [Download assets](https://github.com/xuansenpa1/skillscriptbench-review/releases/tag/benchmark-v0.1.0)

SkillScriptBench evaluates repair and preservation of executable agent skill packages containing Markdown instructions and scripts. This repository contains the benchmark and evaluation tools.

## Dataset

| Track | Tasks | Purpose |
| --- | ---: | --- |
| In-the-Wild Repair | 150 | Script repair in 100 repository-sourced packages |
| Controlled Repair | 200 | 50 packages in Clean, Doc, Script, and Joint states |

Each task provides a `REQUEST.md` and a complete input `package/`. Your method returns a revised package for evaluation.

## Load a task

Use Python 3.12+ from the repository root:

```bash
python -m pip install -e .
skillscriptbench list --split all
skillscriptbench materialize d16-matlab-multiroute --output runs/example
```

The workspace contains only the request and package. The `main_results` subset selects 300 repair tasks; `clean` selects 50 preservation tasks. See the [dataset guide](benchmark/README.md) for the Python API and metadata.

## Evaluate a package

Evaluation requires Linux x86-64, Docker, and the separate evaluator and runtime assets. Follow the [setup guide](docs/evaluation.md) to obtain and load them, then run:

```bash
python -m pip install -e '.[evaluation]'
python evaluate.py --bundle /path/to/evaluator --task TASK_ID \
  --candidate /path/to/revised/package --output runs/evaluation
```

A task succeeds when both behavioral and documentation-driven checks pass. The command produces `scores.csv`, `summary.json`, and per-task logs. For multiple packages, supply a [candidate manifest](docs/evaluation.md#batch-evaluation):

```bash
python evaluate.py --bundle /path/to/evaluator --manifest candidates.json \
  --output runs/batch --workers 2
```

Three runs per task produce Avg, P@3, and Hit³, with Clean preservation reported separately from repair.

## Contents

```text
benchmark/   350 task inputs, metadata, source attribution, and license notices
ssbench/     Task loader and scoring CLI
evaluate.py  Single-task and batch evaluation
docs/        Evaluation setup and usage
scripts/     Runtime asset loader
tests/       Loader and evaluator tests
```

The large evaluator and runtime assets are distributed separately from Git. They contain the task-specific checks, fixtures, and pinned environments required for scoring.

## Citation and attribution

```bibtex
@misc{liu2026skillscriptbench,
  title = {SkillScriptBench: Benchmarking Self-Evolution of Executable Agent Skill Packages Beyond Markdown},
  author = {Liu, Yuxuan and Li, Haoran and Zhang, Yuhao and Guo, Jiahe and Luo, Hongyu and Hu, Wenbin and Jing, Huihao and Chung, Kawai and Chen, Junle and Fan, Changxuan and Zong, Qing and Xie, Lingyun and Song, Yangqiu},
  year = {2026},
  eprint = {2610.04008},
  archivePrefix = {arXiv}
}
```

Third-party files retain their original terms; see [NOTICE](NOTICE) and [source attribution](benchmark/SOURCES.md). A project-level license has not yet been selected. Execute task code in isolated environments without credentials; see [security guidance](SECURITY.md).
