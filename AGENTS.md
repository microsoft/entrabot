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
- Eight modules: `platform/` (OS shim) → `auth/` (certificate JWT + MSAL delegated) → `tools/` (MCP tools + interaction log + email poll + daily summary + cards) → `audit/` (tracking) → `bot/` (Bot Gateway) → `identity/` (state machine) → `storage/` (cloud-memory backend: `BlobStore` + `MemoryBackend` protocol + `PersonaBackend` + `migration` helper — ADR-005 Phases 1, 2, 5, 6a shipped) → `mcp_server.py` (FastMCP + background channel)
- External dependencies: Microsoft Entra ID, Microsoft Teams + Outlook mailbox (via Graph API or Bot Framework), Azure Blob Storage (agent memory, provisioned by setup.sh)
- Three auth modes via `ENTRACLAW_MODE`: `agent_user` (three-hop), `delegated` (MSAL), `bot` (M365 Agents SDK). Agent memory has a **parallel third hop** against `https://storage.azure.com/.default` (`acquire_agent_user_storage_token`).
- Certificate auth: private key in OS keystore (Keychain/TPM/Keyring), JWT assertion for Hop 1 (ADR-003)
- Background tasks (eagerly started at MCP server boot in `agent_user` mode):
  - Teams chat poll (5s), email poll (60s), chat auto-discovery via `/me/chats` (120s), daily summary scheduler (5pm PDT)
- System prompt: generic tool-description string. Personality and behavioral rules served by **persona-sati** MCP server (see `.mcp.json.example`)
- All structured data uses `dataclasses` or `pydantic` — no raw dicts

## Mind-Body Architecture

This repo is the **body** (Teams interface). The **mind** (personality, memory, behavioral rules) is served by a separate MCP server: **persona-sati**.

- Both MCPs are listed in `.mcp.json` (see `.mcp.json.example` for dual-server config)
- If persona-sati is not configured, openclaw works standalone as a generic Teams tool
- Memory operations go through persona-sati's tools, not through local blob sync hooks
- The system prompt comes from persona-sati, not from this repo

## Active Work

- **Persona-sati integration (mind-body split)** — personality, system prompt, and memory externalized to persona-sati MCP server. See `docs/architecture/DESIGN-persona-sati-integration.md`.
- **ADR-005: cloud-hosted memory via Azure Blob Storage** — `docs/decisions/005-cloud-hosted-memory.md`. Status: **Accepted, Phases 1, 2, 5, 6a shipped.** Memory sync hooks removed (persona-sati owns memory now).
- Multi-tenant lightweight chat — landed to `main` (commit `c8ec521`).

## Read These First

- `docs/architecture/DESIGN-persona-sati-integration.md` (mind-body split design)
- `docs/decisions/005-cloud-hosted-memory.md` (cloud memory spec)
- `prompts/agent_system.md.archive` (original prompt — archived, personality now in persona-sati)
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
- `src/entraclaw/mcp_server.py`: FastMCP server — Teams tools + 4 background tasks + channel push + token refresh (generic instructions — personality in persona-sati)
- `prompts/agent_system.md.archive`: Original system prompt (archived — personality now in persona-sati)
- `prompts/agent_system.md.example`: Sanitized standalone prompt for open-source users
- `docs/decisions/`: ADRs — every significant architectural choice is recorded here
- `docs/runbooks/hard-won-learnings.md`: hard-won learnings — READ THIS before making changes
