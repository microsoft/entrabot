# Copilot Instructions — entraclaw-identity-research

## Project Overview

Entraclaw is a research project for securing agentic workflows on local devices (Mac/Linux/Windows) using Microsoft Entra Agent IDs and Agent Users. The goal: agents get their own identity — a real Entra user account with Teams presence — so audit logs distinguish agent actions from human actions.

Key concepts:
- **Agent ID**: An identity issued to an autonomous agent that distinguishes it from the human user
- **Three-hop flow**: Blueprint (certificate) → Agent Identity (FIC) → Agent User (`user_fic` grant) — produces `idtyp=user` token for Graph API
- **Certificate auth**: Private key in OS keystore (Keychain/TPM/Keyring), JWT assertion replaces client secrets (ADR-003)
- **Platform abstraction**: OS-level credential storage (macOS Keychain, Windows Certificate Store, Linux Secret Service) via `CredentialStore` protocol
- **Teams channel**: Background polling + `notifications/claude/channel` push — inbound Teams messages appear in Claude Code automatically
- **Digital worker**: The agent's Teams identity with AI agent badge — sends and receives messages as itself

## Tech Stack

- **Language**: Python 3.12+
- **HTTP**: `httpx` (async + sync) for Graph and Work IQ calls; MSAL only for delegated-mode auth
- **Crypto**: `cryptography` + `PyJWT` for certificate-based JWT assertions
- **MCP**: `mcp` SDK with `FastMCP` for tool registration
- **Credential storage**: `keyring` (cross-platform OS keystore)
- **Testing**: `pytest`, `pytest-asyncio`, `respx` (httpx mocking)
- **Linting**: `ruff`

## Commands

```bash
# Install dependencies
pip install -e ".[dev]"

# Run all tests (1,237 tests)
pytest -v --tb=short && ruff check .

# Run with channel notifications
claude --dangerously-load-development-channels server:entraclaw

# Single test
pytest tests/tools/test_teams.py::TestAcquireAgentUserToken::test_success -v

# Format
ruff format .
```

## Architecture

```
src/entraclaw/
  a365/           # Agent 365 Work IQ MCP provider + Word adapter
  platform/       # OS-specific credential storage (CredentialStore protocol)
  auth/           # Certificate JWT builder (build_client_assertion)
  tools/          # MCP tools (Teams, Files, email, cards, promises, A365)
  audit/          # Action tracking / audit log
  bot/            # Bot Gateway
  identity/       # Progressive identity state machine
  storage/        # Local/Blob/Persona backends
  mcp_server.py   # FastMCP server + background poll + channel push
tests/            # Mirrors src/ structure (1,237 tests)
docs/             # Research, ADRs, learnings, specs
scripts/          # setup.sh, teardown.sh, Entra provisioning
```

### Key patterns

- **CredentialStore protocol**: `platform/` modules expose `store()`, `retrieve()`, `delete()` backed by OS keystore via `keyring`. Certificate private keys live here.
- **Token flow in teams.py**: Three-hop flow is a single function (`acquire_agent_user_token`). Hop 1 uses JWT assertion from certificate. All hops use `httpx.Client` with 15s timeout.
- **Token refresh**: `_ensure_valid_token()` (eager, 55-min threshold) + `_with_token_retry()` (lazy, catches 401). Both in `mcp_server.py`.
- **Background channel**: `_background_poll()` runs every 5s, pushes new human messages via `notifications/claude/channel`. Uses separate dedup state from `watch_teams_replies` (Learning #27).
- **Audit-first design**: Every agent action that touches a resource must emit an audit event before returning.
- **Graph API**: `$filter`/`$orderby` unreliable for chat messages (Learning #16) — always filter client-side.

## Conventions

- Use `dataclasses` or `pydantic` models for all structured data — no raw dicts
- Type-annotate all function signatures
- Test files mirror source structure
- Secrets and tokens never appear in logs — use `repr` overrides on sensitive fields
- Read `docs/runbooks/hard-won-learnings.md` (66 entries) before making auth/Teams changes
- ADRs in `docs/decisions/` for all significant architectural choices
- **Sponsor DM wait pattern (host-gated).** When the human says "ping me when X is done" / "I'm going AFK, let me know" / any equivalent: confirm in Teams with `send_teams_message`, do the work, send the completion update with `send_teams_message`. Claude Code receives replies through channel-push next-turn input. Copilot CLI, Codex, Cursor, and other non-channel-push hosts receive the sponsor reply inline from `send_teams_message` as `sponsor_reply`. Only call `wait_for_sponsor_dm` when the operator explicitly says "block until they reply." NEVER poll in a loop. NEVER spawn `copilot -p` / headless subprocesses. NEVER use `watch_teams_replies` for this pattern. Full protocol: `prompts/anatomy/channel-discipline.md`; see Learning #54.

## Persona-Sati Bootstrap

When persona-sati MCP server is available, call `bootstrap_session()` before first substantive answer or external tool call. FastMCP instructions are not enough — Claude Code and Copilot CLI do not reliably inject FastMCP instructions into the LLM system prompt; they only surface them in MCP debug UI. The bootstrap protocol must reach you via tool results.

**Decision tree:**
1. If `bootstrap_session()` succeeds and `mind_contract_available` is true, proceed with returned mind contract, cognition protocol, context, and memory catalog.
2. If `bootstrap_session()` unavailable but older tools exist, fall back to `get_system_prompt()`, `context()`, `list_memory_files()`.
3. If `mind_contract_available` is false, say persona-sati is degraded and do not impersonate persona.
4. If persona-sati entirely unreachable, say body-only mode.

**Per-turn cognition:** Use exact tool names `observe(tool_name, args)` before/after external tools, `reflect(observation, kind=...)` for user feedback/durable context, `recall(query)` when bootstrap/observe indicates relevant memory but excerpt insufficient. Efferent-copy may mechanically cover body-tool observe but not bootstrap/reflect/recall.

See `docs/clients/persona-sati-host-bootstrap.md` for full protocol.
