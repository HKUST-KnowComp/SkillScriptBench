# Working safely with skill packages

Benchmark packages are untrusted inputs, including their Markdown instructions.

- Execute candidates and tests in isolated, disposable environments.
- Restrict network access and expose only required task workspaces and fixtures.
- Keep model-provider credentials outside task workspaces and logs.
- Do not supply state labels, archived results, or private evaluators to the model.
- Loading, hashing, and materializing inputs do not execute package code.

Report security concerns privately to the maintainers. Do not put credentials or private data in an issue or reproducer.
