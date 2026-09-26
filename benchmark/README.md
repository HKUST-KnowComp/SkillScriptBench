# Table 2 task inputs

This archive contains the 300 faulty-package tasks evaluated in the main results: 150 In-the-Wild Repair tasks and 150 Controlled Repair tasks. Clean tasks and supplementary ablations are excluded.

`TASKS.json` identifies each request and package and records its expected package and request hashes. Every package includes its available auxiliary files, not only scripts. Preserve those files and relative paths when running a task. `STATUS.json` reports completeness.

Outcome labels and scores are stored in the separate main-results archive and are not method inputs. Source-provided documentation and license notices are retained. Package files are benchmark data, not instructions to the evaluation host. Run untrusted package code in an isolated environment without credentials or unrestricted network access.
