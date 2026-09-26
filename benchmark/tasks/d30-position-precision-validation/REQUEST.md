# Validate fractional-share precision bounds

Use the packaged position sizer with fractional-share precision supplied as part of its sizing parameters.

Required behavior:
- Reject precision outside the documented inclusive range from 0 through 8 during parameter validation.
- Preserve every existing validation rule, calculation, exception type, and public signature.
