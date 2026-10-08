# Reproduce the main results

```bash
python results/reproduce_table2.py
```

Expected summary:

```json
{
  "records": 18000,
  "tasks": 300,
  "verified_table_cells": 300,
  "table2_matches": true,
  "model_calls": 0
}
```

The script checks the complete 300 × 4 × 5 × 3 matrix, unique run keys, and agreement with every numerical table cell. No provider account is required.

| File | Content |
| --- | --- |
| `outcomes.csv` | Pass/fail per task, model, method, repeat |
| `tasks.csv` | Main-result task grouping |
| `table2_summary.csv` | Scores by model, method, group |
| `table2.tex` | Numerical reference |
| `SOURCE.json` | Frozen source and export hashes |
| `DATA_DICTIONARY.md` | Column definitions and metrics |

## Method IDs

| ID | Paper method |
| --- | --- |
| `md-only` | Markdown-only |
| `raw-package` | Raw Package |
| `native-ast` | Raw Package + AST |
| `coevoskills` | CoEvoSkills |
| `coevoskills-ast` | CoEvoSkills + AST |

`benchmark/splits/main_results.json` matches the 300-task result set. The 50 added Clean input packages complete input coverage without changing the frozen table.

Store new experiments in separate run directories. The archived experiment outcomes are recorded in `results/SOURCE.json`; the revision runner identifies its implementation in each run's `PIPELINE.json`.
