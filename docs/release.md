# Public release preparation

## Prepared

- Python task API and unified CLI.
- All 350 inputs, including the 50 Clean cases.
- Source attribution for all 350 tasks; Controlled group mappings and 19 hash-verified original license notices.
- Whole-package scoring CLI, versioned evaluation assets, and a separate 22-image runtime archive.
- Offline runtime dependencies, fixture files, and a loader for hash-verified runtime archive parts.
- Separate `all`, `main_results`, and `clean` manifests.
- LLM discovery, AST-bound script edits, and Markdown alignment.
- Reviewed fixes to document-edit scope, script placement, and provider handling.
- Unchanged main-result outcomes and Table 2 reproduction.
- Benchmark, method, running, and evaluation documentation.

## Verified release checks

- All 350 original inputs were scored: 50 Clean packages passed and all 300
  faulty inputs failed the required checks, with no unresolved execution errors.
- The full run was followed by a fixed 13-task revalidation covering dependency
  and deployment-adapter fixes. Candidate files remained unchanged.
- A fresh Python 3.12 installation verified all 350 inputs and source mappings,
  loaded both CLI entrypoints, and reproduced all 300 numerical Table 2 cells
  from the 18,000 released outcomes.
- All 22 image identities, 263 content-addressed archive blobs, and all 13 runtime
  parts were verified.

The [machine-readable report](release-validation.json) records per-task calibration
verdicts and provenance. Input calibration is not a new model-performance result;
these checks made no model calls and did not change the archived paper outcomes.

## Remaining before public distribution

1. Select a license for project-authored code/documentation.
2. Review the assembled third-party attribution and notices for distribution.
3. Add the final public repository URL and publish the approved source and data archives. The paper is available at https://arxiv.org/abs/2610.04008.

This snapshot does not change GitHub visibility or publish to a package registry. Wheels carry code and prompts; source-release archives additionally carry data and outcomes.

## Distribution files

| File | Contents |
| --- | --- |
| `SkillScriptBench_source.zip` | Code, prompts, all 350 inputs, attribution, main-result outcomes, and documentation |
| `SkillScriptBench_benchmark350.zip` | The benchmark directory alone, for use with an existing installation |
| `SkillScriptBench_evaluator.tar.gz` | Task-specific checks, fixtures, offline dependencies, and runtime bindings |
| `RUNTIME_PARTS.json` and numbered runtime parts | The 22 container images, with per-part and combined archive hashes |
| `CHECKSUMS.json` | Checksums for the source, benchmark, evaluator, and runtime manifest |

Extract the evaluator archive to obtain an `evaluator/` directory. Keep all
runtime parts next to their manifest and follow the [evaluation guide](evaluator-integration.md).
Runtime binaries belong in downloadable release assets, not Git history.

To assemble these files from a release checkout:

```bash
python scripts/package_release.py --output /path/to/new/release \
  --evaluator /path/to/evaluator \
  --runtime-manifest /path/to/runtime/RUNTIME_PARTS.json
```

This command includes the runtime manifest, not the multi-gigabyte image parts.
Distribute the referenced parts alongside it. It neither publishes files nor
selects a license.
