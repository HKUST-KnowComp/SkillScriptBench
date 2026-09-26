# Normalize fetched daily price series

Fetch daily rows through the packaged technical-analysis price-source fallback chain.

Required behavior:
- Convert successful provider rows through the package's canonical sorted-daily-series policy before as-of filtering and weekly use.
- Preserve fallback attempts, proxy metadata, error reporting, and the public result schema.
