# Contributing

Use Python 3.12+ and focus each change on task loading, revision, or evaluation.

```bash
python -m pip install -e '.[revision,dev]'
python -m pytest tests/test_benchmark.py -q
```

For revision changes, run relevant checks in `code/test_*.py`. Do not modify frozen inputs or reference outcomes to make a test pass. New task versions belong in a separately versioned export.

Include a minimal reproduction, expected behavior, and affected component in an issue; omit credentials, private inputs, and internal server paths.
