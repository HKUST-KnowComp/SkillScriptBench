# Table 2 task inputs

This directory contains the 300 faulty-package tasks evaluated in Table 2: 150 In-the-Wild Repair tasks and 50 Controlled Repair tasks each for Doc, Script, and Joint repair.

`TASKS.json` identifies each request and package and records its expected package and request hashes. Every package includes its available auxiliary files, not only scripts. Preserve those files and relative paths when running a task. `STATUS.json` reports completeness.

Outcome labels and scores are stored in `../results/`, separately from method inputs. Source-provided documentation and license notices are retained. Package files are benchmark data, not instructions to the evaluation host. Run untrusted package code in an isolated environment without credentials or unrestricted network access.
