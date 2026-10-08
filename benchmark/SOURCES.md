# Source attribution

`SOURCES.json` is the task-level upstream inventory. All 350 entries identify the source repository, commit, and recorded license. Original notices already inside task packages remain in place.

## Controlled Repair

The 200 Controlled tasks form 50 four-state groups. Their upstream records are recovered from the original Core, Breadth, and DeepFlow construction registries. Each record was checked against the registry's source-record and license SHA-256 values.

- `CONTROLLED_GROUPS.json` maps each base package to its Clean, Doc, Script, and Joint task IDs.
- Each Controlled entry in `SOURCES.json` includes its upstream repository and commit, a recorded license, and a relative `license_file` with its `license_sha256`.
- `licenses/` retains the 19 distinct original notice files, deduplicated by content hash. A notice may apply to multiple packages or states.
- `construction_registry` identifies the source registry. `source_record_sha256` records the verified attribution document identity.

These are benchmark-adapted packages. Reference repairs and state-specific changes are represented by the released task inputs and their own hashes in `TASKS.json`; upstream attribution does not replace those input hashes.

## Separation from method inputs

The materialization command copies only the maintenance request and package. It does not copy state labels, source records, evaluators, fixtures, or archived outcomes. Evaluation assets belong outside the workspace supplied to a revision method.

Run `skillscriptbench verify-sources` to validate source coverage, Controlled group membership, and license-notice hashes. This is an integrity check, not an additional license grant. No project-level license has been selected.
