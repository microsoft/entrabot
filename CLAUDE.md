# CLAUDE.md — Openclaw Identity Research

> Root working context. Durable architecture lives in `docs/`.

## Non-Negotiables

- **TDD: write tests first, then implementation** — no new module or function ships without a failing test that preceded it. `pytest -v && ruff check .` must pass before every commit
- Security paths fail closed — if audit can't record, the action doesn't proceed
- Every agent resource access must be attributed to an Agent ID, never the human user
- Secrets and tokens never appear in logs — use `__repr__` overrides on sensitive fields
- Never redirect stderr to /dev/null — errors must always be visible for debugging
- Check every token response for `"error"` key before accessing `"access_token"` — Entra returns error dicts, not exceptions
- Never use `az rest` or Azure CLI tokens for Agent Identity APIs — they include `Directory.AccessAsUser.All` which causes hard 403
- Always create BlueprintPrincipal explicitly after Blueprint — it is NOT auto-created
- Agent IDs are service principals, not users — never create fake user accounts with passwords
- Parse `az` CLI output as JSON, not TSV — TSV can be corrupted by warnings

## Current Runtime Model

- Python 3.12+ research project — no deployed service yet
- Eight modules: `platform/` (OS shim) → `auth/` (certificate JWT + MSAL delegated) → `tools/` (MCP tools + interaction log + email poll + daily summary + cards) → `audit/` (tracking) → `bot/` (Bot Gateway) → `identity/` (state machine) → `storage/` (cloud-memory backend, ADR-005 Phase 1) → `mcp_server.py` (FastMCP + background channel)
- External dependencies: Microsoft Entra ID (identity), Microsoft Teams + Outlook mailbox (communication via Graph API or Bot Framework), Azure Blob Storage (planned for memory, ADR-005)
- Three auth modes via `ENTRACLAW_MODE` config switch:
  - `agent_user` — three-hop Agent User flow (Blueprint cert → Agent Identity FIC → Agent User `user_fic`)
  - `delegated` — MSAL interactive auth with human's token, messages prefixed `[EntraClaw]`
  - `bot` — M365 Agents SDK bot server with JSONL IPC, bot has its own Teams identity
- Certificate auth: private key in OS keystore (Keychain/TPM/Keyring), JWT assertion for Hop 1 (ADR-003)
- Background tasks (all started eagerly at MCP server boot in `agent_user` mode):
  - Teams chat poll (5s) — pushes inbound DMs / group-chat messages via `notifications/claude/channel`
  - Email poll (60s) — `/me/messages`, filters Teams/M365 noise, detects Purview-encrypted mail
  - Chat auto-discovery (120s) — `GET /me/chats`, registers any chat not in `watched_chats`
  - Daily summary scheduler — 5pm PDT triage email of the day's interactions
- Agent system prompt: `prompts/agent_system.md` (markdown, loaded by mcp_server at import time, includes channel-discipline rules)
- All structured data uses `dataclasses` or `pydantic` — no raw dicts

## Active Work

- **ADR-005: cloud-hosted memory via Azure Blob Storage** — `docs/decisions/005-cloud-hosted-memory.md`. Status: **Accepted, Phase 1 shipped, Phase 2 next.**
  - Phase 1 (shipped, commit `f900ba1`): `BlobStore` async client in `src/entraclaw/storage/blob.py` (put/get/list/delete/exists + ETag concurrency + 401→TokenExpiredError). 22 tests in `tests/storage/test_blob.py`.
  - Phase 2 (next): `MemoryBackend` protocol in `src/entraclaw/storage/backend.py` with `Local` and `Blob` implementations. Route `interaction_log.py`, `daily_summary.py`, and memory-file access through it. ~150 LOC. See ADR-005 §"Implementation phases" for the full 6-phase plan.
- Multi-tenant lightweight chat — **landed to main** (commit `c8ec521`, PR #23369 abandoned-as-merged-externally). Spec: `docs/architecture/NEXT-WhatsApp-lightweight-teams-chat.md`.

## Read These First

- `docs/decisions/005-cloud-hosted-memory.md` (current active spec — phase plan + open TODOs)
- `prompts/agent_system.md` (agent behavioral rules — channel discipline, watch-only, reply detection)
- `docs/architecture/DESIGN-teams-bot-gateway.md` (Bot Gateway design)
- `docs/architecture/NEXT-WhatsApp-lightweight-teams-chat.md` (delegated mode spec — multi-tenant chat, now landed)
- `docs/engineering-status.md` (current state: 385 tests, 3 auth modes, Phase 1-3 daily-summary stack live)
- `docs/index.md`
- `docs/runbooks/hard-won-learnings.md` (read before making changes — covers stdout-capture-into-env, lazy-init dead poll, schema-divergence killing MCP stream, et al.)
- `docs/decisions/001-obo-flows-for-device-agents.md`
- `docs/decisions/003-certificate-auth-over-client-secrets.md`
- `docs/platform-learnings/mcp-close-the-loop.md`

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

# Docs preview
pip install mkdocs-material && mkdocs serve
```

## High-Value Repo Areas

- `src/entraclaw/platform/`: OS-specific credential storage — `CredentialStore` protocol with Mac/Linux/Windows implementations
- `src/entraclaw/auth/`: Certificate-based JWT assertion builder + MSAL delegated auth (localhost redirect + device code fallback)
- `src/entraclaw/bot/`: Bot Gateway — M365 Agents SDK server, JSONL IPC handler, Dev Tunnel manager, conversation reference persistence
- `src/entraclaw/identity/`: Progressive identity state machine (UNAUTHENTICATED → DELEGATED → PROVISIONING → AGENT_USER)
- `src/entraclaw/tools/teams.py`: Three-hop token flow + Teams Graph API (send, read, filter, chat creation, add members cross-tenant)
- `src/entraclaw/mcp_server.py`: FastMCP server — 6 tools + 3 auth modes + background poll + channel push + token refresh
- `src/entraclaw/config.py`: `ENTRACLAW_MODE` switch (auto/bot/delegated/agent_user) + all env config
- `docs/decisions/`: ADRs — every significant architectural choice is recorded here
- `docs/runbooks/hard-won-learnings.md`: 29 hard-won learnings — READ THIS before making changes

## gstack

This project uses gstack for enhanced AI workflows. **Use `/browse` for all web browsing — never use `mcp__claude-in-chrome__*` tools.**

### Available skills

`/office-hours`, `/plan-ceo-review`, `/plan-eng-review`, `/plan-design-review`, `/design-consultation`, `/design-shotgun`, `/design-html`, `/review`, `/ship`, `/land-and-deploy`, `/canary`, `/benchmark`, `/browse`, `/connect-chrome`, `/qa`, `/qa-only`, `/design-review`, `/setup-browser-cookies`, `/setup-deploy`, `/retro`, `/investigate`, `/document-release`, `/codex`, `/cso`, `/autoplan`, `/plan-devex-review`, `/devex-review`, `/careful`, `/freeze`, `/guard`, `/unfreeze`, `/gstack-upgrade`, `/learn`

### Troubleshooting

If gstack skills aren't working, rebuild:

```bash
cd .claude/skills/gstack && ./setup
```

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
- Save progress, checkpoint, resume → invoke checkpoint
- Code quality, health check → invoke health
