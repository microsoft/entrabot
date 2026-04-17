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

- Python 3.12+ research project — no deployed service yet (385 tests as of 2026-04-17)
- Eight modules: `platform/` (OS shim) → `auth/` (certificate JWT + MSAL delegated) → `tools/` (MCP tools + interaction log + email poll + daily summary + cards) → `audit/` (tracking) → `bot/` (Bot Gateway) → `identity/` (state machine) → `storage/` (cloud-memory backend, ADR-005 Phase 1) → `mcp_server.py` (FastMCP + background channel)
- External dependencies: Microsoft Entra ID, Microsoft Teams + Outlook mailbox (via Graph API or Bot Framework), Azure Blob Storage (planned for memory, ADR-005)
- Three auth modes via `ENTRACLAW_MODE`: `agent_user` (three-hop), `delegated` (MSAL), `bot` (M365 Agents SDK)
- Certificate auth: private key in OS keystore (Keychain/TPM/Keyring), JWT assertion for Hop 1 (ADR-003)
- Background tasks (eagerly started at MCP server boot in `agent_user` mode):
  - Teams chat poll (5s), email poll (60s), chat auto-discovery via `/me/chats` (120s), daily summary scheduler (5pm PDT)
- Agent system prompt: `prompts/agent_system.md` (markdown, loaded by mcp_server at import time)
- All structured data uses `dataclasses` or `pydantic` — no raw dicts

## Active Work

- **ADR-005: cloud-hosted memory via Azure Blob Storage** — `docs/decisions/005-cloud-hosted-memory.md`. Status: **Accepted, Phase 1 shipped, Phase 2 next.** Phase 1 (`f900ba1`) = `BlobStore` async client in `src/entraclaw/storage/blob.py`. Phase 2 = `MemoryBackend` protocol + `Local`/`Blob` impls + route `interaction_log.py` / `daily_summary.py` / memory access through it.
- Multi-tenant lightweight chat — landed to `main` (commit `c8ec521`).

## Read These First

- `docs/decisions/005-cloud-hosted-memory.md` (current active spec)
- `prompts/agent_system.md` (agent behavioral rules — channel discipline, watch-only, reply detection)
- `docs/engineering-status.md` (current state)
- `docs/runbooks/hard-won-learnings.md` (read before making changes)

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Test + lint (run before every commit)
pytest -v --tb=short && ruff check .

# Test with coverage
pytest -v --cov=entraclaw --cov-report=term-missing --cov-fail-under=80

# Single test
pytest tests/tools/test_teams.py::TestAcquireAgentUserToken::test_success -v

# Format
ruff format .

# Run with channel notifications (entraclaw MCP auto-loads via .mcp.json)
claude --dangerously-load-development-channels server:entraclaw
```

## High-Value Repo Areas

- `src/entraclaw/platform/`: OS-specific credential storage — `CredentialStore` protocol with Mac/Linux/Windows implementations
- `src/entraclaw/auth/`: Certificate-based JWT assertion builder + MSAL delegated auth
- `src/entraclaw/identity/`: Progressive identity state machine (UNAUTHENTICATED → DELEGATED → PROVISIONING → AGENT_USER)
- `src/entraclaw/bot/`: Bot Gateway — M365 Agents SDK server, JSONL IPC, Dev Tunnel
- `src/entraclaw/tools/`: Teams Graph API + interaction log (Phase 1) + email poll (Phase 2) + daily summary (Phase 3) + Adaptive Cards
- `src/entraclaw/storage/`: Cloud-memory backend (ADR-005 Phase 1: `BlobStore` client only; Phase 2 wires it in)
- `src/entraclaw/mcp_server.py`: FastMCP server — 11 MCP tools + 4 background tasks + channel push + token refresh
- `prompts/agent_system.md`: System prompt loaded into every MCP session (channel-discipline rules, watch-only, falsehood-correction, reply-detection)
- `docs/decisions/`: ADRs — every significant architectural choice is recorded here
- `docs/runbooks/hard-won-learnings.md`: hard-won learnings — READ THIS before making changes
