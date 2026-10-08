# AST-Guided Skill Revision

The method takes a maintenance request **q**, original package **S**, and an initial revision produced by Raw Package or CoEvoSkills. It returns coordinated script and Markdown edits in a complete package.

## Pipeline

1. **LLM requirement discovery.** Read the request, Markdown, and complete supported scripts. Produce evidence-linked requirements, preservation constraints, and source-location hypotheses.
2. **Structural binding.** Resolve locations against parsed AST/CST nodes, gather caller/helper context, and admit supported edit targets.
3. **Script revision.** Request replacements for admitted nodes while preserving interfaces and unrelated behavior. Validate edit scope and syntax before assembling candidates.
4. **Markdown alignment.** Review documented invocations against the assembled scripts and apply validated localized edits.

Discovery is semantic model inference. Parser binding and edit validation are deterministic implementation steps; discovery is not replaced by a regex-based rule miner.

## Public API

```python
from pathlib import Path
from ssbench.revision import prepare, run_prepared

prepare(
    parent="inputs/original/package",
    proposal="inputs/baseline/package",
    request=Path("inputs/REQUEST.md").read_text(),
    output="runs/revision",
    model="YOUR_EXACT_MODEL_ID",
)

def call_model(prompt, tool_schema, stage):
    return client_returning_tool_arguments(prompt, tool_schema, stage)

result = run_prepared("runs/revision", call_model)
print(result["package"])
```

The callback returns parsed JSON **tool arguments**, not a provider response envelope. Stages are `discovery`, zero to two script stages (`parent-signal`, `raw-residual`), and `document-alignment`. The [offline example](../code/examples/offline_walkthrough.py) demonstrates this contract.

## Instructions

| Component | Template |
| --- | --- |
| Requirement discovery | [requirement_discovery.txt](../code/prompts/requirement_discovery.txt) |
| Script revision | [script_revision.txt](../code/prompts/script_revision.txt) |
| Markdown alignment | [markdown_alignment.txt](../code/prompts/markdown_alignment.txt) |
| Raw proposal | [raw_package.txt](../code/prompts/raw_package.txt) |
| CoEvoSkills proposal | [revision](../code/prompts/coevo_revision.txt), [verifier](../code/prompts/coevo_verifier.txt) |

`FINAL.json` points to `final/package/`. Discovery, script branches, and document review records are retained with the run. Task checks determine success separately from package generation.
