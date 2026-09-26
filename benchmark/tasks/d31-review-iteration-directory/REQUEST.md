# Materialize review-loop input directories

Run the packaged edge-pipeline review loop in a fresh output location.

Required behavior:
- Create each iteration's draft-input directory, including missing parents, before writing draft YAML files.
- Preserve iteration limits, review invocation, draft identity, and returned review state.
