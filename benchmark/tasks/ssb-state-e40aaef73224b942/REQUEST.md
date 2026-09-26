# User Request

Audit the complete skill package and preserve this existing user-facing use case. Keep `SKILL.md` and the bundled implementation consistent. Make no unrelated changes, and leave an already-correct package unchanged.

## Required Use Case

**`dig_url_root` is the one exception — NO silent default.** First-time (no memory entry), MUST elicit via `AskUserQuestion` before any submit / `osmo data upload` / `preflight_urls.sh`. `s3://osmo-workflows/dig` is a *suggestion to confirm*, never auto-picked (~80 GB+ lands there). Later runs may reuse the remembered value silently. See Step 0 + memory rules (§4).


## Package Maintenance Contract

Audit all documented local invocations in this package, not only the example above.
With valid user inputs substituted for placeholders, commands, arguments, paths,
and generated artifacts must agree with the supplied implementation and the
stated use case. Preserve existing supported invocations and unrelated behavior.
Do not delete a documented workflow merely to avoid repairing it.
