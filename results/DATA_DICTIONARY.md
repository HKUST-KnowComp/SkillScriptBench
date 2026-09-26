# Main Results data

`outcomes.csv` contains the complete Table 2 matrix: 300 tasks × 4 models × 5 methods × 3 runs = 18,000 records. A row is uniquely identified by `(model, method, task_id, repeat)`. There is no selection by outcome. `tasks.csv` gives one row per task; labels are for analysis and are not method inputs.

Fields: `track` is SourceRepair150 (In-the-Wild) or ArtifactState200 (Controlled Repair); `state` is source_repair, doc_fault, script_fault, or joint_fault; `partition` and `base_id` retain the task's grouping. `task_version` and `score_version` retain the frozen record's version labels. `status` is the final pass/fail outcome. `selected_tree_hash` identifies a selected package when present; an empty hash means that field was absent from the unified record, not that the outcome failed.

Methods: `md-only` = Markdown-only; `raw-package` = Raw Package; `native-ast` = Raw Package + AST; `coevoskills` = CoEvoSkills; `coevoskills-ast` = CoEvoSkills + AST. Internal identifiers are retained to join records unambiguously.

For N tasks with binary outcomes x[t,r], Avg = 100 × sum(x)/(3N); P@3 = 100 × count(tasks with any success)/N; Hit³ = 100 × count(tasks with three successes)/N. Overall pools all 300 repair tasks; it is not an unweighted average of the four displayed subgroups. Clean tasks are excluded, as in Table 2.

`table2.tex` is the manuscript's numerical table source. `table2_summary.csv` contains its recomputed values. `SOURCE.json` records the SHA-256 of the immutable 350-task source and the state-based export rule. Original result logs remain unchanged. This archive contains outcomes, not all generated candidate packages or a new evaluation of them.
