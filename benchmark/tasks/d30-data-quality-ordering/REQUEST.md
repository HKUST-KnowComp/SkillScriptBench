# Return data-quality findings in domain severity order

Run more than one packaged data-quality check over the same content and return one deterministic findings list.

Required behavior:
- Findings must be ordered by the domain ordering contract already defined by each Finding object, not by attempting to compare Finding instances directly.
- Preserve filtering, date handling, and the public run_checks signature.
