# Rank strategy pivots deterministically

Rank and select packaged strategy-pivot proposals before enforcing archetype diversity.

Required behavior:
- Rank by combined score descending, then novelty descending, then proposal ID ascending as a deterministic tie-break.
- Preserve score calculation, diversity constraints, selection limits, and proposal metadata.
