# Release versions

| Artifact | Version and provenance |
| --- | --- |
| Portable revision runner | Release `2026-09-26.2`; LLM requirement discovery, parser-bound script edits, and Markdown alignment. |
| Discovery and script revision | `semantic-discovery-native-runner-v1110`; `semantic-discovery-native-bridge-v1109-preservation-r1`. |
| Markdown alignment | `public-invocation-document-audit-v5`. |
| Published instructions | `code/prompts/`; aligned with the manuscript instructions on 2026-09-26. |
| Main-result outcomes | Frozen `unified_experiments_20260916_v1/RESULTS.json` export. `results/SOURCE.json` records the source and export hashes. |

The portable runner is not an exact replay of the historical experiment harness.
Table 2 is reconstructed from the archived per-run outcomes in `results/`.
New revision runs produce their own packages and run records.

Release `2026-09-26.2` removes unused experiment and release tools, separates
document markers from those tools, and makes the CLI model selection and API
endpoint explicit. It preserves the published prompt text, benchmark inputs,
and archived outcomes.
