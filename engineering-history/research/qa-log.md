# QA Log

## Smoke Tests

| Test | Command | Expected |
|------|---------|----------|
| All tests pass | `pytest -v` | 0 failures |
| Lint clean | `ruff check .` | No errors |
| Package installs | `pip install -e ".[dev]"` | No errors |
| Coverage gate | `pytest --cov=entrabot --cov-fail-under=80` | passes |

## Known Issues

See `docs/engineering-status.md` "Known Issues (Open)" for the live list. The hardest-won ones are tracked in `docs/runbooks/hard-won-learnings.md`.
