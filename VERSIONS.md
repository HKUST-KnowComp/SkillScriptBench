# Release versions

## Evaluator integration: 2026-10-08

Adds task preparation, behavioral and documentation checks, and whole-package
scoring through `skillscriptbench evaluation`. Evaluator assets bind all 350
task IDs to versioned checks, fixtures, offline dependencies, and 22 pinned
container images. The separate runtime archive can be loaded from hash-verified
numbered parts. See [setup and scoring](docs/evaluator-integration.md).

Portable adapters relocate paths into isolated job directories, preserve runtime
command links, and run fresh checks instead of loading historical verdicts.
Input calibration is reported separately from Table 2 reconstruction. The
release-only rendered-delivery fixture serves the pinned boilerplate's sampled
telemetry endpoint; `ASSEMBLY_CHANGES.json` records the original and updated
hashes. Deployment assertions, method prompts, benchmark inputs, and archived
main-result outcomes are unchanged.

## Public release preparation: 2026-10-07

Controlled-source attribution now covers 200 tasks in 50 matched groups, with
19 original license notices verified against construction records. The CLI adds
`sources` and `verify-sources`. Evaluation dependencies are being recovered in
a separate maintainer staging area; they are not yet part of this portable
release. Input packages, prompts, and published outcomes are unchanged.

Adds the ssbench task API, skillscriptbench CLI, Python distribution, user
documentation, and 50 Clean inputs. All 350 package/request hashes match the
recorded inputs. The 300-task Table 2 selection and its 18,000 outcomes are
unchanged. The reviewed 2026-09-27.1 runtime and prompts are retained. No model
experiments were rerun.

## Revision and result lineage

| Artifact | Version and provenance |
| --- | --- |
| Portable revision runner | Release `2026-09-27.1`; LLM requirement discovery, parser-bound script edits, and Markdown alignment. |
| Discovery and script revision | `semantic-discovery-native-runner-v1110`; `semantic-discovery-native-bridge-v1109-preservation-r1`. |
| Markdown alignment | `public-invocation-document-audit-v5`. |
| Published instructions | `code/prompts/`; runtime-checked discovery, script-revision, and Markdown-alignment instructions. The 2026-09-27 release adds explicit document-edit span constraints. |
| Main-result outcomes | Frozen `unified_experiments_20260916_v1/RESULTS.json` export. `results/SOURCE.json` records the source and export hashes. |

The portable runner is not an exact replay of the historical experiment harness.
Table 2 is reconstructed from the archived per-run outcomes in `results/`.
New revision runs produce their own packages and run records.

Release `2026-09-26.2` removes unused experiment and release tools, separates
document markers from those tools, and makes the CLI model selection and API
endpoint explicit. It preserves the published prompt text, benchmark inputs,
and archived outcomes.

Release `2026-09-27.1` binds document edits to their cited Markdown blocks,
prevents ambiguous cross-snapshot script patch placement, rejects API redirects,
and handles malformed provider responses through the existing fallback path.
It updates the Node.js prerequisite to match the pinned parser and expands
offline regression checks. Benchmark inputs and archived outcomes are unchanged.
