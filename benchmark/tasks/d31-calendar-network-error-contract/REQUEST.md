# Translate economic-calendar network failures

Fetch the packaged economic calendar when the remote API may reject or fail the request.

Required behavior:
- Preserve the package's actionable payment-tier message, enriched HTTP error, and network-error translation instead of leaking an unprocessed dependency exception.
- Preserve successful decoding, response validation, URL construction, and the public API.
