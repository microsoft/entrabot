# Contributing

This project welcomes contributions and suggestions. Before opening a pull
request, please read the repository guidance in [`AGENTS.md`](AGENTS.md) and
[`CLAUDE.md`](CLAUDE.md), then run the relevant validation commands for the
files you changed.

## Contributor License Agreement

Most contributions require you to agree to a Contributor License Agreement
(CLA) declaring that you have the right to, and actually do, grant us the rights
to use your contribution. For details, visit
[https://cla.opensource.microsoft.com](https://cla.opensource.microsoft.com).

When you submit a pull request, the CLA bot will determine whether you need to
provide a CLA and decorate the pull request appropriately. Follow the bot's
instructions when prompted.

## Development workflow

1. Fork the repository and create a topic branch.
2. Keep changes focused and avoid committing generated build output, local
   deployment state, credentials, or environment-specific identifiers
   (`.env`, `.entrabot-state.json`, certificates, tokens).
3. Update documentation when behavior, setup, deployment, or public APIs change.
4. Test discipline is the contract: TDD — failing test first, implementation
   second. New modules or public functions ship with tests that preceded them.

Useful validation commands:

```bash
pytest -v --tb=short
ruff check .
ruff format --check .
mkdocs build --strict
```

Coverage threshold is 80%. The full test suite is fast (~17s) — run it before
every commit, not just the targeted file. For anything touching auth, Teams,
or the body prompt, read
[`engineering-history/research/hard-won-learnings.md`](engineering-history/research/hard-won-learnings.md)
first. That file is append-only; new gotchas get numbered entries, never
deletions.

## Security issues

Do not report security vulnerabilities through public GitHub issues. Follow the
instructions in [`SECURITY.md`](SECURITY.md).
