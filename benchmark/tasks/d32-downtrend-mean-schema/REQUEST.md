# Keep the mean downtrend duration

Compute the packaged summary statistics for a collection of detected downtrends.

Required behavior:
- Return the complete stable statistics schema, including mean_duration_days for non-empty and empty inputs.
- Preserve percentile calculations, rounding, total count, and existing field names and types.
