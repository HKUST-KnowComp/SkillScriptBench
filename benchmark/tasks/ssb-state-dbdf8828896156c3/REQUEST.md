# User Request

Audit the complete skill package and preserve this existing user-facing use case. Keep `SKILL.md` and the bundled implementation consistent. Make no unrelated changes, and leave an already-correct package unchanged.

## Required Use Case

1. Run `scripts/artifact_hints.sh` (JSON on stdout). It sources `skills/jetson-diagnostic/scripts/detect_jetson.sh` and returns `sku`, `generation`, `product_line`, `variant`, `l4t`, a preferred **vLLM** image, `cuda_sm_hint`, and canonical URLs.


## Package Maintenance Contract

Audit all documented local invocations in this package, not only the example above.
With valid user inputs substituted for placeholders, commands, arguments, paths,
and generated artifacts must agree with the supplied implementation and the
stated use case. Preserve existing supported invocations and unrelated behavior.
Do not delete a documented workflow merely to avoid repairing it.
