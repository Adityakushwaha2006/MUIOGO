# Tests

Standard `pytest`. Run everything from the repo root with your virtualenv active
(the project's `pyproject.toml` sets `pythonpath`, so no `PYTHONPATH` or
`sys.path` setup is needed).

## Running

| Command | What it runs | Time |
|---|---|---|
| `pytest` | Default fast suite (slow + gold excluded) | seconds |
| `pytest -m slow` | Reduced-dimension OG-Core equivalence + production solves | minutes |
| `pytest -m gold` | Full-dimension match against the official expected CSV | hours |
| `pytest -m "slow or gold"` | Both heavy tiers together | hours |

The default run excludes the heavy OG-Core model solves via
`addopts = "-m 'not slow and not gold'"`, so plain `pytest` stays fast for normal
development and CI. The `slow` and `gold` markers are opt-in.

Useful selectors (all standard pytest):

```bash
pytest tests/ogcore_standalone            # only the OG-Core suite
pytest -k tax_params                       # tests matching a substring
pytest tests/ogcore_standalone/test_ogcore_routes.py   # a single file
```

## Layout

```
tests/
  conftest.py                 # shared fixtures: app, client
  test_*.py                   # CLEWS / OSeMOSYS tests
  ogcore_standalone/          # OG-Core standalone suite (Stage 1)
    conftest.py               # OG-Core fixtures: isolated_storage, ogc_client
    _ogcore_specs.py          # shared baseline/reform param specs
    ogcore_fixtures/          # pinned param JSON + expected output CSV
    test_ogcore_*.py
```

The OG-Core tests live under `ogcore_standalone/` so the suite stays grouped as
the coupled (Stage 2) and converging (Stage 3) test sets are added alongside it.
CLEWS tests stay at the top level, untouched.

> The folder is named `ogcore_standalone`, not `ogcore`, on purpose: `tests/` is
> on `pythonpath`, and a folder literally named `ogcore` would shadow the
> installed `ogcore` library and break every `import ogcore`.
