# SkillScriptBench data card

A task pairs a maintenance request with a package containing `SKILL.md`, scripts, and auxiliary files. A method returns a revised **whole package**.

## Construction and coverage

**In-the-Wild Repair** contains 150 cases from 100 repository-sourced packages. Cases introduce script faults and pair the faulty package with a maintenance request. The track evaluates repair in real package structures; “In-the-Wild” describes package sources, not a claim that every defect occurred naturally.

**Controlled Repair** contains 50 package groups with four matched states each. The same request is used across Clean, Doc, Script, and Joint states, separating preservation, documentation repair, script repair, and coordinated repair.

| Selection | Tasks | Purpose |
| --- | ---: | --- |
| `all` | 350 | Complete input collection |
| `main_results` | 300 | Table 2: In-the-Wild, Doc, Script, Joint |
| `clean` | 50 | Clean-state preservation |

These define evaluation subsets, not train/dev/test partitions. Group by `base_id` when comparing Controlled states.

## Files and schema

```text
TASKS.json                  Input paths and expected hashes
STATUS.json                 Coverage and integrity summary
metadata.json               Track, state, partition, and package-group labels
SOURCES.json                Upstream repository/revision/license metadata for 350 tasks
CONTROLLED_GROUPS.json      50 groups, each mapping the four task states
ATTRIBUTION_STATUS.json     Attribution assembly summary
licenses/                  Original Controlled-source license notices
splits/{all,main_results,clean}.json
tasks/<task_id>/REQUEST.md   Public maintenance request
tasks/<task_id>/package/     Complete input package
```

`TASKS.json` paths are relative to this directory. Each row has `task_id`, `request`, `package`, `available`, `expected_request_sha256`, and `expected_package_tree_hash`. The package hash is SHA-256 over a canonical JSON mapping of relative paths to file SHA-256 values; cache directories are excluded as defined in `ssbench/benchmark.py`.

The collection contains 350 package/request pairs. Use `verify --split all` to check input integrity.

## Load a task

```python
from ssbench import Benchmark

bench = Benchmark("benchmark")
for task in bench.tasks("main_results"):
    request = task.request.read_text()
    package = task.package
    # Pass only request + package to your method.
```

```bash
skillscriptbench --benchmark benchmark list --split clean
skillscriptbench --benchmark benchmark verify --split all
skillscriptbench materialize d16-matlab-multiroute --output runs/task
```

Materialization copies only `REQUEST.md` and `package/` to a new directory. It refuses to overwrite an existing workspace, never copies group labels or archived outcomes, and executes no package code.

## Labels and sources

`metadata.json` supports stratified analysis:

| Field | Meaning |
| --- | --- |
| `track` | `in-the-wild` or `controlled` |
| `state` | `source_repair`, `clean`, `doc_fault`, `script_fault`, `joint_fault` |
| `base_id` | Shared Controlled package group, where recorded |
| `partition` | Recorded construction partition, where available |

`SOURCES.json` covers all 350 tasks with upstream repository, commit, and recorded license. Package IDs and languages are included where recorded. Controlled entries link to hash-verified original notices in `licenses/`; `CONTROLLED_GROUPS.json` links the four states of each of the 50 groups. These upstream records identify the sources from which benchmark packages were adapted, not a claim that benchmark inputs are unchanged upstream checkouts. See [source attribution](SOURCES.md).

```bash
skillscriptbench verify-sources
skillscriptbench sources ssb-state-f96c6cadb6c96de8
```

Source notices and task package files are preserved unchanged. Attribution and group metadata are not copied into method workspaces.

## Evaluation

Task success requires behavioral and documentation-driven checks. The scoring CLI uses a separate bundle of task-specific checks, fixtures, and runtime assets. [Setup and scoring →](../docs/evaluation.md)
