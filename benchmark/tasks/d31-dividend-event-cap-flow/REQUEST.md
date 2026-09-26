# Apply event risk caps to dividend entry rows

Build packaged dividend entry rows when an event scan is present or has to be synthesized as skipped.

Required behavior:
- Every row must pass the event scan and trigger state through the package event-cap policy before verdict and order gates are populated.
- Preserve scan serialization, payout checks, blocker accumulation, and the public row schema.
