# User Request

Audit the complete skill package and preserve this existing user-facing use case. Keep `SKILL.md` and the bundled implementation consistent. Make no unrelated changes, and leave an already-correct package unchanged.

## Required Use Case

**A `.plain.html` pass is NOT a layout pass — add one computed-style assertion (#the silent-failure guard).** The text-level asserts above are all satisfied while the page renders as a single stacked column, because a block-CSS scoping mistake (a selector keyed to a wrapper class the target runtime doesn't emit — see `blockWrapperClass` in the runtime contract) makes every grid fall back to `display: block` *with the typography still correct*. This shipped green on a real e2e site. So the contract's final gate is a **headless computed-style check on the delivered live URL** (not `.plain.html`): load the page in a headless browser and assert, for the first page of each template, that every block whose CSS declares a grid/flex layout **computes `display: grid`/`flex` (not `block`)**, `main .section` count > 0, blocks are decorated (`data-block-name` present), zero `pageerror`, zero broken images. A block that should grid but computes `block` fails the page — do not flip it to `deployed`. This is the assertion `blockWrapperClass` in the runtime contract calls for; the atomic contract is where it must actually run, once per template. Two field-decodes worth pinning: a **burst of `PUT` 400s is a malformed path, not rate limiting** — lowercase every segment, never a double slash (`content//…` 400s the PUT while preview/live still 200), no trailing `-`/`_` on a segment; and **write long loops to a bash script file with absolute binary paths** (`/usr/bin/curl`, the full `node` path) — zsh drops PATH inside `while`/`for` in some contexts, and the resulting `command not found` burst mimics a transport failure.


## Package Maintenance Contract

Audit all documented local invocations in this package, not only the example above.
With valid user inputs substituted for placeholders, commands, arguments, paths,
and generated artifacts must agree with the supplied implementation and the
stated use case. Preserve existing supported invocations and unrelated behavior.
Do not delete a documented workflow merely to avoid repairing it.
