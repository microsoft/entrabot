# AGENTS.md — Openclaw Identity Research

> Instructions for AI agents working in this codebase (Copilot, Claude Code, Codex, etc.)

## Non-Negotiables

- **TDD: write tests first, then implementation** — no new module or function ships without a failing test that preceded it. `pytest -v && ruff check .` must pass before every commit
- Security paths fail closed — if audit can't record, the action doesn't proceed
- Every agent resource access must be attributed to an Agent ID, never the human user
- Secrets and tokens never appear in logs — use `__repr__` overrides on sensitive fields
- Never redirect stderr to /dev/null — errors must always be visible for debugging
- Check every token response for `"error"` key before accessing `"access_token"` — Entra returns error dicts, not exceptions
- Never use `az rest` or Azure CLI tokens for Agent Identity APIs — they include `Directory.AccessAsUser.All` which causes hard 403 (Learning #1)
- Always create BlueprintPrincipal explicitly after Blueprint — it is NOT auto-created (Learning #2)
- Agent IDs are service principals, not users — never create fake user accounts with passwords
- Parse `az` CLI output as JSON, not TSV — TSV can be corrupted by warnings (Learning #7)
- Graph API `$filter`/`$orderby` are unreliable for chat messages — always filter client-side (Learning #16)

## Current Runtime Model

- Python 3.12+ research project — no deployed service yet
- Five modules: `platform/` (OS shim) → `auth/` (certificate JWT) → `tools/` (MCP tools) → `audit/` (tracking) → `mcp_server.py` (FastMCP + background channel)
- External dependencies: Microsoft Entra ID (identity), Microsoft Teams (communication via Graph API)
- Auth via three-hop Agent User flow: Blueprint (certificate) → Agent Identity (FIC) → Agent User (`user_fic` grant) — `httpx` direct, no MSAL at runtime
- Certificate auth: private key in OS keystore (Keychain/TPM/Keyring), JWT assertion for Hop 1 (ADR-003)
- Background channel: polls Teams every 5s, pushes via `notifications/claude/channel`
- All structured data uses `dataclasses` or `pydantic` — no raw dicts

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Test + lint (run before every commit)
pytest -v --tb=short && ruff check .

# Test with coverage
pytest -v --cov=openclaw --cov-report=term-missing --cov-fail-under=80

# Single test
pytest tests/tools/test_teams.py::TestAcquireAgentUserToken::test_success -v

# Format
ruff format .

# Run with channel notifications
claude --dangerously-load-development-channels server:openclaw
```

## High-Value Repo Areas

- `src/openclaw/platform/`: OS-specific credential storage — `CredentialStore` protocol with Mac/Linux/Windows implementations
- `src/openclaw/auth/`: Certificate-based JWT assertion builder — `build_client_assertion()`, `compute_cert_thumbprint()`
- `src/openclaw/tools/teams.py`: Three-hop token flow + Teams Graph API (send, read, filter, chat creation)
- `src/openclaw/mcp_server.py`: FastMCP server — 5 tools + background poll + channel push + token refresh
- `docs/decisions/`: ADRs — every significant architectural choice is recorded here
- `docs/runbooks/hard-won-learnings.md`: 27 hard-won learnings — READ THIS before making changes
