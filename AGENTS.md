# CLAUDE.md — Openclaw Identity Research

> Root working context. Durable architecture lives in `docs/`.

## Non-Negotiables

- Security paths fail closed — if audit can't record, the action doesn't proceed
- Every agent resource access must be attributed to an Agent ID, never the human user
- Secrets and tokens never appear in logs — use `__repr__` overrides on sensitive fields
- Test before committing — `pytest -v && ruff check .`
- Token flows are separated by type — never mix OBO, device-code, and client-credentials logic

## Current Runtime Model

- Python 3.12+ research project — no deployed service yet
- Four modules: `platform/` (OS shim) → `auth/` (OBO/Agent ID) → `audit/` (tracking) → `teams/` (Agent User)
- External dependencies: Microsoft Entra ID (identity), Microsoft Teams (communication via Graph API)
- Auth via `msal` library — OBO token exchange is the core flow
- All structured data uses `dataclasses` or `pydantic` — no raw dicts

## Read These First

- `docs/index.md`
- `docs/getting-started/quickstart.md`
- `docs/architecture/system-overview.md`
- `docs/reference/obo-flows.md`
- `docs/decisions/001-obo-flows-for-device-agents.md`

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Test + lint
pytest -v && ruff check .

# Single test
pytest tests/auth/test_obo.py::test_token_exchange -v

# Format
ruff format .

# Docs preview
pip install mkdocs-material && mkdocs serve
```

## High-Value Repo Areas

- `src/openclaw/platform/`: OS-specific agent identity — `AgentIdentityProvider` protocol with Mac/Linux/Windows implementations
- `src/openclaw/auth/`: OBO token exchange, Agent ID registration, consent — one module per flow type
- `src/openclaw/audit/`: Audit-first enforcement — events emitted before actions execute
- `src/openclaw/teams/`: Bidirectional Teams communication via Graph API
- `docs/decisions/`: ADRs — every significant architectural choice is recorded here
