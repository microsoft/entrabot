# Openclaw Identity Research — Engineering Summary

**Date:** April 21, 2026
**Team:** Brandon Werner
**Status:** v1 released. Three auth modes working (Agent User / Delegated / Bot Gateway). Progressive identity state machine. **~600 tests** across the suite. MCP tools + 4 background tasks (Teams 5s / email 60s / chat-discovery 120s / daily summary 5pm PDT). Multi-tenant lightweight chat shipped. **Mind-body split complete** — body-first prompt architecture loads locally, persona-sati MCP wired for personality/memory when configured. ADR-005 cloud-memory Phases 1, 2, 5, 6a shipped; blob-hosted operational storage is opt-in via `setup.sh --cloud-memory`.

---

## What's New Apr 20–21 (mind-body-tools push)

Twelve PRs merged across two days (#17–#28). Tooling and body-prompt discipline both hardened significantly.

**New MCP tools / body surface**
- **PR #17** — `read_teams_messages` now surfaces attachment metadata (list of `{id, content_type, content_url, name, thumbnail_url}`) so `<attachment id=...>` references resolve without extra Graph calls.
- **PR #18** — Dual-host MCP support. `clientInfo.name` detection drives leader (Claude Code) vs. slave (Copilot CLI and other hosts) mode. Slave-mode adds a disclosure suffix to outbound tool responses. Original implementation gated background tasks on host identity at boot — see PRs #27/#28 below for the subsequent lifecycle fix.
- **PR #19** — `post_thinking_placeholder` + `resolve_placeholder` MCP tools. When the agent decides to answer a Teams chat with real work behind it, it posts a placeholder first and edits/deletes-reposts when the reply is ready. Modes: `edit` (quiet, safer), `delete_repost` (fresh ping), `fallback_new` on Graph failure.
- **PR #20** — `delete_teams_message` MCP tool (standalone soft-delete of the agent's own messages). Also fixes the Graph URL for softDelete: `/me/chats/{chat_id}/messages/{message_id}/softDelete` (the `/me/` prefix is mandatory — v1.0 returns 405 without it).
- **PR #23** — PreToolUse hook at `scripts/hooks/block_local_memory_write.py` blocks `Write`/`Edit`/`NotebookEdit` to `~/.claude/projects/<slug>/memory/**` unless `ENTRACLAW_KEEP_MEMORY_LOCAL=true`. Reuses the existing `setup.sh --keep-memory-local` switch. Paired with a three-way memory-routing tree in `CLAUDE.md`: body/channel rules → `prompts/anatomy/` (PR); mind content → `mcp__persona-sati__write_memory_file`; operational state → openclaw blob.
- **PR #24** — `_push_channel_notification` forwards `reply_to_ids` into the notification `meta` and concurrently fetches each quoted message body via new `fetch_message()` helper, attaching results as `meta.quoted_messages`. Fail-open: individual fetch failures drop from the list; total failure still pushes `reply_to_ids` without `quoted_messages`. The agent now has context for quote-replies without a round-trip.
- **PR #25** — `send_email` MCP tool + new `src/entraclaw/tools/email.py` helper. Supports `reply_to_message_id` for thread-preserving replies via Graph's `/me/messages/{id}/reply`. `daily_summary.py` now routes through the same helper — one send path in the codebase. `Mail.Send` delegation was already granted on the Agent User token chain; no re-provisioning required.
- **PR #26** — `add_promise` / `list_promises` / `resolve_promise` MCP tools backed by `promises.jsonl` in openclaw blob, identity-scoped. Supersedes the session-scoped `TaskCreate` pattern for human-facing commitments — promises now survive MCP server restart, Claude Code restart, and cross-session hand-off (terminal ↔ Teams). ETag-concurrency on writes, compaction at >1000 lines with 30-day resolved retention.

**Body-prompt rules** (`prompts/anatomy/channel-discipline.md`)
- **PR #21** — "No cross-chat context bleed." Outbound messages may only reference work that the specific chat has visible history of. Don't name-drop parallel work from another chat, even with the same human in both.
- **PR #22** — "Promises become tasks." Any time the agent says "I'll report back / post the PR link / confirm when X lands," create a durable entry the same turn. Mark done only after the human-facing follow-up has been posted, not when the internal signal arrives. Superseded by PR #26 (promises become **durable**, backed by blob, not `TaskCreate`).
- PR #19 — "Signal when you're working." When decides to answer a Teams chat and the response will involve real work, post a `post_thinking_placeholder` first and resolve via `resolve_placeholder` when the reply is ready.
- PR #23 body rule — "Spawn sub-agents for side-work" and the memory-routing decision tree in `CLAUDE.md`.
- PR #20 body rule — "Deleting your own messages." Use `delete_teams_message`, not `resolve_placeholder delete_repost` as a hack.

**Lifecycle bug — slave-mode gate, two attempts, third-time-right**
- **PR #27** — First attempt. `_init_poll()` had been gating all background-task startup on `_is_leader_host()`, which reads `clientInfo.name` from the live MCP request context. At boot time no `initialize` request has been processed yet, so `_current_host()` returned `"unknown"` and the entire background stack silently skipped — Teams poll, email poll, daily summary, chat auto-discovery. Fix: remove the boot-time gate; move it to `_push_channel_notification` where the decision is actually needed.
- **PR #28** — Second attempt. PR #27 moved the gate from boot to push-time, but `_push_channel_notification` is called from the background poll task, which runs in an asyncio context **detached from any MCP request** — so `_current_host()` STILL returned `"unknown"` and the push gate silently dropped every inbound message. Fix: cache `clientInfo.name` in `_state["cached_host"]` at every tool invocation; `_is_leader_host()` prefers live context but falls back to the cached value. Background tasks now see the right answer after at least one leader-host tool call.

**The footgun that hid the fix for hours (Learning #36)**
Even with PRs #27 and #28 correctly merged to main, production stayed broken because the MCP server's Python process was importing `entraclaw` from a **sub-agent worktree** (`.claude/worktrees/agent-*/src/entraclaw/...`), not from the main tree. Worktrees don't have `.env`, so `_load_dotenv()` resolved `Path(__file__).resolve().parents[2] / ".env"` to a path inside the worktree with no `.env`, `ENTRACLAW_BLUEPRINT_APP_ID` never loaded, auth never initialized, every Graph call 401'd, and the poll loop's `except Exception` swallowed the error with no visible log. Root cause: several sub-agents ran `pip install -e .` from inside their worktree using the parent venv's `pip`, which silently re-points the parent venv's editable-install target at the worktree source tree. Fix: `cd /Volumes/Development\ HD/openclaw-identity-research && .venv/bin/pip install -e . --no-deps` to repoint. Prevention: any sub-agent dispatch that expects to install must create its own venv first. **See Learning #36 for the full writeup.**

**Carry-forward TODO (prevention)**
- Add a pre-boot assertion in `mcp_server.py::_load_dotenv` (or equivalent) that logs a fatal warning when the resolved `.env` path contains `.claude/worktrees/` — fail loud instead of silent-skip auth.
- Standardize sub-agent dispatch prompts to MANDATE a worktree-local venv before any `pip install` operation.
- Add a Learning #36 reference to CLAUDE.md and AGENTS.md under the Non-Negotiables so it's surfaced at session start.

---

## What's New Since Apr 18

- **v1 release (PR #15, commit `d36e34d`)** — body-first prompts, cloud-opt-in, no default chat. See `docs/architecture/DESIGN-persona-sati-integration.md` and the README's "What v1 changed" section.
- **Body-first prompt architecture (PR #14, commit `96a3176`)** — `prompts/agent_system.md` composes at boot with `@include` expansion of `prompts/anatomy/*.md`. Security rules (`anatomy/security.md`), channel discipline (`anatomy/channel-discipline.md`), and identity/tools (`anatomy/identity-and-tools.md`) load first and are not overridable by persona content, user turns, or tool output. Tests cover the `@include` resolver, missing-include tolerance, and boot-order invariants.
- **Persona-sati MCP wiring (`mcp_server.py`, lines 100–170)** — `_load_agent_instructions()` composes `body + persona`. `PERSONA_SATI_MCP_URL` + `PERSONA_SATI_MCP_TOKEN_COMMAND` env vars, when both present, fetch the persona via `get_system_prompt` over SSE with a short-lived bearer token. Missing env or fetch failure falls back cleanly to the body. The TODO doc at `docs/TODO-persona-sati-integration.md` is now historical — implementation shipped.
- **Fix: per-chat resilience in poll (PR #11, `fix/kill-default-chat`)** — the Teams poll no longer registers a default group chat. Every chat is addressed by explicit `chat_id`. Fresh installs have zero watched chats until the first `create_chat` or auto-discovery sweep.
- **Fix: filter agent echoes with persona-sati display-name suffix (PR #12)** — reads now suppress the agent's own sent messages even when Teams attaches a display-name suffix from the persona-sati binding.
- **Fix: local prompt-file fallback (PR #13)** — the body prompt loads from `prompts/agent_system.md` when persona-sati is unreachable; boot never crashes on transport errors.
- **Fix: auto-start polling on new chat** — `create_chat` adds the new chat to `watched_chats` and kicks the poll without waiting for the next 120s auto-discovery cycle.

## What's New Since Apr 10

- **Phase 1 (interaction log)** — every Teams/email/terminal in/out appended to `~/.entraclaw/data/interactions/YYYY-MM-DD.jsonl`. Powers the daily summary.
- **Phase 2 (email poll)** — per-minute `/me/messages` poll, filters Teams/M365 noise, detects Purview-encrypted mail via `message.rpmsg` lookup, persists cursor + per-session message-id dedup.
- **Phase 3 (daily summary)** — 5pm PDT scheduler, triages day's interactions into `needs_you / handled / heads_up`, renders HTML, sends via `/me/sendMail`, archives to `<data_dir>/summaries/<day>.html`.
- **Chat auto-discovery (`a75d043`)** — background task hits `GET /me/chats` every 120s; any chat not in `watched_chats` gets auto-registered (in memory + persisted) so chats created via raw Python or by other humans adding the Agent User get polled within ~2 min.
- **Reply detection (`0732b8b`)** — `read_teams_messages` surfaces `reply_to_ids` from the Teams `<attachment id=…>` quote tag. The body prompt's Exception #3 lets the agent continue active 1:1 exchanges in group chats without re-`@`-tagging.
- **Eager MCP init (`d6cc640`)** — `_initialize()` runs as a background task at server boot instead of waiting for the first tool call. Fresh servers no longer sit deaf to inbound DMs/email until someone invokes a tool.
- **Email-push schema fix (`9a71d6c`)** — email push notification meta + content aligned with Teams push (no `<sender@addr>` angle brackets that read as HTML tags; meta carries only `chat_id="email"` / `message_id` / `user` / `ts`). Fixed: silent MCP-stream close after every email push.
- **`setup.sh` hardening** — tenant-wide UPN lookup before Agent User creation (`8541d75`), warn-and-confirm before replacing Blueprint certs (`2338a7a`), cached-cert verification against Entra (`22e81d9`), `redirect_stdout(sys.stderr)` to stop diagnostic spam from corrupting `.env` cert thumbprint (`c99d66a`), `entraclaw-mcp` console script in `.mcp.json` (`5bb3bc4`).
- **ADR-005 Phase 1 (`f900ba1`)** — `BlobStore` async client in `src/entraclaw/storage/blob.py` (put/get/list/delete/exists + ETag concurrency + 401→`TokenExpiredError`). 22 tests.
- **ADR-005 Phase 2** — `MemoryBackend` protocol + `LocalBackend` / `BlobBackend` impls + `get_backend()` factory in `src/entraclaw/storage/backend.py`. `interaction_log.py` and `daily_summary.py` route through it. 22 tests.
- **ADR-005 Phase 5** — `acquire_agent_user_storage_token` (storage-scope third hop), `--keep-memory-local` flag in `setup.sh`, `scripts/provision_blob_storage.py` (idempotent Storage Account + container + RBAC), migration helper in `src/entraclaw/storage/migration.py`, blob endpoint/container/keep-memory-local config fields. 23 tests. Setup now exits red + non-zero on migration failure.
- **ADR-005 Phase 6a** — Claude Code persona-memory sync. `PersonaBackend` + `claude_code_memory_dir()` in `src/entraclaw/storage/persona.py`. `scripts/claude_memory_sync.py` CLI with `pull` / `push` / `push-one` subcommands. Memory sync hooks removed from `.claude/settings.json` — persona-sati owns memory sync via its own MCP tools (`write_memory_file`, `read_memory_file`, `refresh_persona`). `claude_memory_sync.py` retained as a manual migration tool.
- **Multi-tenant lightweight chat** — landed to `main` (commit `c8ec521`, 47 commits, +9,331 / −2,484).

---

## What We're Building

A proof-of-concept demonstrating that **device-local AI agents can have their own identity** in Microsoft Entra, separate from the human user. Three identity modes:

1. **Agent User** (production path) — Blueprint → Agent Identity → Agent User via three-hop flow. Agent sends as its own Entra user.
2. **Delegated** (instant start) — MSAL interactive auth with human's token. Messages prefixed `[EntraClaw]`. No provisioning needed.
3. **Bot Gateway** — M365 Agents SDK bot server with Dev Tunnel. Bot has its own identity in Teams by design. No Agent User provisioning, no M365 license.

**Identity Chain (Agent User):** Blueprint (certificate auth) → Agent Identity (FIC exchange) → Agent User (`user_fic` grant, `idtyp=user`) → Graph API (Teams, Mail, OneDrive)

**Channel:** Background poll every 5s (Graph) or 2s (bot JSONL) → push via `notifications/claude/channel` → Claude Code receives messages automatically.

### The Demo Scenario — WORKING (Three Modes)

| Step | Agent User Mode | Delegated Mode | Bot Mode |
|------|----------------|----------------|----------|
| Setup | `./scripts/setup.sh` (10–15 min) | `./scripts/setup_delegated.sh` (60s) | `./scripts/start_bot.sh` + Dev Tunnel |
| Auth | Three-hop flow (automatic) | MSAL browser sign-in (cached) | Bot app credentials |
| Identity | Agent's own Entra user | Human's identity + `[EntraClaw]` prefix | Bot's app identity |
| Send | Graph API as Agent User | Graph API as human | Bot Framework relay |
| Receive | Graph API poll (5s) | Graph API poll (5s) | Bot activity handler (instant) |

### MCP Tools

| Tool | Purpose | Status |
|------|---------|--------|
| `send_teams_message` | Send text/HTML to a chat (requires `chat_id`); supports `@mentions` | ✅ Live |
| `send_card` | Adaptive Card (tool_activity / task_status / build_result) | ✅ Live |
| `create_chat` | Open a 1:1 DM by email; auto-registers for polling | ✅ Live |
| `read_teams_messages` | Read recent messages from a chat | ✅ Live |
| `list_chat_members` | Resolve display names to Entra GUIDs for `@mentions` | ✅ Live |
| `add_teams_member` | Add user to chat (cross-tenant auto-resolved) | ✅ Live |
| `watch_teams_replies` | Blocking poll with dedup — fallback when channel push is unavailable | ✅ Live |
| `whoami` | Show agent identity, mode, and connection status | ✅ Live |
| `audit_log` | Record an audit event before performing a security-sensitive action | ✅ Live |
| `run_daily_summary` | Generate and email the day's interaction digest on demand | ✅ Live |
| `view_image` | Read an image file and return as base64 for the LLM | ✅ Live |

Full reference: `docs/reference/mcp-tools.md`.

---

## TDD Status

```
484 tests collected
```

Key modules:

```
src/entraclaw/auth/          — certificate JWT + MSAL delegated auth
src/entraclaw/bot/           — Bot Gateway (server, handler, tunnel, convo_store)
src/entraclaw/identity/      — progressive identity state machine
src/entraclaw/storage/       — LocalBackend / BlobBackend / PersonaBackend + migration
src/entraclaw/tools/         — Teams Graph API tools + interaction log + email + daily summary + cards
src/entraclaw/config.py      — ENTRACLAW_MODE + all env config
src/entraclaw/mcp_server.py  — FastMCP + 3 auth modes + body-first prompt loader + persona-sati fetch + background poll + channel push
prompts/                     — body prompt + anatomy/ modules
```

Invariant: `pytest -v && ruff check .` passes before every commit.

---

## Close the Loop (Channel Push Architecture)

**Problem:** LLM doesn't automatically check for replies after sending a Teams message. The MCP protocol is request-response — no mechanism for the server to wake up the LLM when new data arrives.

**Solution:** Background polling + `notifications/claude/channel` push. The MCP server declares `experimental: {"claude/channel": {}}` capability and pushes inbound Teams messages directly into the Claude Code conversation — same mechanism as the iMessage channel plugin.

**Requirements:** Start Claude Code with `--dangerously-load-development-channels server:entraclaw` to enable channel notifications for development servers. Without the flag, the background poll still runs and appends to the interaction log; `read_teams_messages` retrieves them on demand.

**Fallback:** `watch_teams_replies` tool still available for explicit polling. Background poll uses separate dedup state so both can detect the same message independently (Learning #27).

**Research:** `docs/platform-learnings/mcp-close-the-loop.md` — analysis of 12+ MCP messaging servers, the MCP Triggers & Events Working Group, and the three problems we solved (capability declaration, startup flag, separate state).

---

## What Works (Shipped)

- End-to-end: `setup.sh` → MCP server → Teams message delivery
- Three-hop Agent User token flow (Blueprint → Agent Identity → Agent User)
- Agent User creation via Graph beta API (`microsoft.graph.agentUser`)
- Agent User license assignment (auto-detects Teams-capable SKUs)
- Consent grant (`oAuth2PermissionGrant`) for Teams/Chat permissions
- Dedicated provisioner app (avoids Azure CLI token rejection)
- State persisted in `.entraclaw-state.json` (idempotent, no secret reset)
- MCP server auto-discovered via `.mcp.json`
- `--teams-user` flag to set Teams recipient separately from admin
- `read_teams_messages` with null-from handling (system messages)
- 29 hard-won learnings documented in runbooks
- Bidirectional Teams channel — background polling + push notifications
- Certificate auth for Blueprint — private key in OS keystore, no secrets on disk (ADR-003)
- Token auto-refresh: eager (55-min) + lazy (401 retry) for all tools
- `notifications/claude/channel` push — same mechanism as iMessage channel plugin
- Message dedup: 2s overlap window + bounded seen-set (imessage-kit pattern)
- 429 rate limit handling propagates through polling tool with `Retry-After`
- Autonomous agent behaviour — acts on Teams messages without terminal prompting
- Multi-user group chat support (`setup.sh --teams-user=user1,user2`)
- Cross-tenant federated chats for B2B guests — auto-detects guest UPN, resolves home tenant via OpenID discovery
- `add_teams_member` — add users to chat at runtime without restart
- Chat ID persistence across restarts — no duplicate group chats
- All code passes `ruff` lint + format
- Progressive identity state machine — `UNAUTHENTICATED → DELEGATED → PROVISIONING → AGENT_USER` with `asyncio.Lock`-protected transitions
- MSAL delegated auth — localhost redirect + device code fallback, OS-encrypted token cache via `msal-extensions`
- Delegated setup script (`scripts/setup_delegated.sh`) — sign in once, cache token, launch MCP server
- Bot Gateway — M365 Agents SDK bot server + JSONL IPC (inbound/outbound with `fcntl.flock`) + Dev Tunnel manager + conversation reference persistence. Coexists via `ENTRACLAW_MODE=bot`
- Identity-aware user ID — `_effective_user_id()` returns the correct ID for the current mode (agent user OID vs signed-in human OID)
- Body-first prompt architecture with `@include` expansion — security, channel discipline, identity/tools layered under non-overridable body
- Persona-sati MCP integration — body composes `body + persona` when `PERSONA_SATI_MCP_URL` + `PERSONA_SATI_MCP_TOKEN_COMMAND` env vars are set; clean fallback when not
- Adaptive Cards: `send_card` tool with `tool_activity`, `task_status`, `build_result` templates
- Azure Blob Storage backend — opt-in via `./scripts/setup.sh --cloud-memory` (ADR-005 Phases 1, 2, 5, 6a)

### What's Not Started / Deferred

- Azure Bot resource registration on werner.ac (needed for live bot test)
- Windows VM provisioning and testing (rescheduled)
- AppContainer sandbox spike — kernel-level agent isolation on Windows
- Delta query optimization — replace timestamp polling with `/messages/delta` if rate-limit becomes an issue
- Dynamic precision weighting for the polling cadence (still static per source)
- Purview-protected email decryption — `Mail.Read` can't decrypt `.rpmsg` attachments; needs separate IRM scope (low priority, see `docs/platform-learnings/teams-graph-api.md`)

---

## Architecture

```
Blueprint (client_credentials)
  → Agent Identity (FIC exchange)
    → Agent User (user_fic grant, idtyp=user)
      → Graph API: Teams, Mail, OneDrive
      → Azure Blob Storage (parallel third hop, ADR-005 Phase 5)

┌─────────────────────────────────────────────────────────┐
│  Local Device (Mac / Windows / Linux)                   │
│                                                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │ Claude Code / Copilot CLI (MCP Client)           │   │
│  │   └── stdio + channels ────┐                     │   │
│  └────────────────────────────┼─────────────────────┘   │
│                               │                         │
│                               ▼                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │ Entraclaw MCP Server (Python)                    │   │
│  │                                                  │   │
│  │  Body prompt: agent_system.md + anatomy/*.md     │   │
│  │    + Persona (optional): persona-sati /sse       │   │
│  │                                                  │   │
│  │  send_teams_message ───▶ Graph API (Agent User)  │   │
│  │  read_teams_messages ──▶ Graph API (Agent User)  │   │
│  │  whoami ───────────────▶ cached state            │   │
│  │  audit_log ────────────▶ interaction log         │   │
│  │                                                  │   │
│  │  Background: Teams 5s, email 60s, discovery 120s,│   │
│  │  daily summary 5pm PDT                           │   │
│  │                                                  │   │
│  │  Tokens: Agent User (three-hop, idtyp=user)      │   │
│  └──────────────────────────────────────────────────┘   │
└───────────┬──────────────────────────┬──────────────────┘
            │                          │
            ▼                          ▼
    ┌───────────────┐          ┌──────────────┐
    │ Entra ID      │          │ Graph API    │
    │ Agent IDs     │          │ Teams Chat   │
    │ Agent Users   │          │ Mail / Drive │
    └───────────────┘          └──────────────┘
```

---

## Next Steps (Priority Order)

1. ~~Bidirectional Teams loop~~ — ✅ DONE. Background poll + channel push + dedup + token refresh.
2. ~~Token auto-refresh~~ — ✅ DONE. Eager (55-min) + lazy (401 retry).
3. ~~Certificate auth~~ — ✅ DONE. No secrets on disk. Private key in OS keystore (ADR-003).
4. ~~Close the loop~~ — ✅ DONE. `notifications/claude/channel` push via experimental capability.
5. ~~Multi-tenant lightweight chat~~ — ✅ DONE. Progressive identity state machine + MSAL delegated auth (PR #1).
6. ~~Bot Gateway~~ — ✅ DONE. M365 Agents SDK bot server + JSONL IPC + tunnel manager. Coexists via `ENTRACLAW_MODE=bot`.
7. ~~Body-first prompt architecture~~ — ✅ DONE. `@include` expansion, non-overridable body rules, persona layered on top (PRs #14, #15).
8. ~~Persona-sati MCP wiring~~ — ✅ DONE. `PERSONA_SATI_MCP_URL` + `PERSONA_SATI_MCP_TOKEN_COMMAND` consumed at boot.
9. **Bot Gateway live test** — Register Azure Bot on werner.ac, sideload Teams app, verify end-to-end with Dev Tunnel.
10. **Entra sign-in log verification** — confirm `idtyp=user` and agent attribution in tenant audit logs.
11. **Windows VM provisioning** — verify cross-platform `setup.sh`.
12. **AppContainer sandbox spike** — kernel-level agent isolation on Windows.
13. **Delta query optimization** — replace timestamp polling with `/messages/delta` if rate-limit becomes an issue.

---

## Bugs Encountered & Resolved (Selected)

| # | Bug | Impact | Fix |
|---|-----|--------|-----|
| 1 | Provisioner secret reset on every re-run | High | Cache in state file, use `--append` |
| 2 | Agent User UPN used tenant ID as domain | Blocking | Extract domain from signed-in user's UPN |
| 3 | `oAuth2PermissionGrant` missing `startTime` | Blocking | Add `startTime: now()` to request body |
| 4 | Provisioner lacked `DelegatedPermissionGrant` permission | Blocking | Added to `BASE_PERMISSION_VALUES` |
| 5 | Three-hop flow missing `fmi_path` parameter | Blocking | Added `fmi_path={agent-id}` to hop 1 |
| 6 | Consent grant used beta API instead of v1.0 | Blocking | Use v1.0 URL directly, not `graph_request()` |
| 7 | Chat creation `/me` doesn't work for Agent Users | Blocking | Use explicit user IDs for both members |
| 8 | `read_teams_messages` crashed on null `from` field | Crash | `(m.get("from") or {})` pattern |
| 9 | Non-Teams licenses triggered skip | Wrong | Check `TEAMS_CAPABLE_SKUS`, not any license |
| 10 | MCP tool names not discoverable by LLM | UX | Renamed to verb-first, added trigger phrases |
| 11 | No `httpx` timeout on token flow | Hang | Added 15s timeout to all hops |
| 12 | `teardown.sh` silent exit on missing `.env` | Silent | Guard with `[ -f .env ]` check |
| 13 | `stderr` swallowed throughout scripts | Hidden errors | Removed all `2>/dev/null` |
| 14 | Admin and Teams user conflated | Wrong recipient | Added `--teams-user` flag |
| 15 | Default group chat registered at install | Wrong | No default chat — explicit `create_chat` only (v1) |
| 16 | Agent reading its own messages back as new input | Loop | Filter agent echoes including persona-display-name suffix (v1) |
| 17 | Body prompt not loading from file | Wrong | Fall back to `prompts/agent_system.md` when persona-sati unreachable (v1) |

Full append-only log: `docs/runbooks/hard-won-learnings.md` (29 entries).
