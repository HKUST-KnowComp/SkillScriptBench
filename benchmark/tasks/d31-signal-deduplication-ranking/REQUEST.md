# Keep the strongest signal during deduplication

Use the packaged signal aggregator to deduplicate overlapping signals.

Required behavior:
- Deduplication must consider higher raw-score signals first so the strongest representative remains primary.
- Preserve merge rules, logs, empty-input behavior, configuration, and the public function interface.
