# Report pre-trade workflow failures

Run the packaged pre-trade discipline CLI when an input, evaluation, or report step may fail.

Required behavior:
- Convert a workflow exception into the existing stderr error message and exit code 1 rather than propagating it.
- Preserve successful reports, journal and link finalization, non-GO handling, argument validation, and the public CLI contract.
