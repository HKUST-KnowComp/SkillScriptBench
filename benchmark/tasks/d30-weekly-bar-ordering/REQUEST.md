# Aggregate weekly bars in chronological daily order

Use the packaged technical-analysis workflow to aggregate unordered daily bar dictionaries into completed ISO-week bars.

Required behavior:
- Daily bars within each week must be ordered by their date field before open, close, high, low, and volume are aggregated.
- Preserve as-of truncation, partial-week handling, output schema, and the public interface.
