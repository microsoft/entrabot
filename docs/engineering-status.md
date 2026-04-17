# Openclaw Identity Research — Engineering Summary

**Date:** April 17, 2026
**Team:** Brandon Werner
**Status:** Three auth modes working (Agent User / Delegated / Bot Gateway). Progressive identity state machine. **385 tests.** 11 MCP tools + 4 background tasks (Teams 5s / email 60s / chat-discovery 120s / daily summary 5pm PDT). Multi-tenant lightweight chat landed. Phase 1-3 daily-summary stack live (interaction log → email poll → triage email). ADR-005 cloud-memory: Phase 1 (`BlobStore` client) shipped, Phase 2 next.

---

## What's New Since Apr 10

- **Phase 1 (interaction log)** — every Teams/email/terminal in/out appended to `~/.entraclaw/data/interactions/YYYY-MM-DD.jsonl`. Powers the daily summary.
- **Phase 2 (email poll)** — per-minute `/me/messages` poll, filters Teams/M365 noise, detects Purview-encrypted mail via `message.rpmsg` lookup, persists cursor + per-session message-id dedup.
- **Phase 3 (daily summary)** — 5pm PDT scheduler, triages day's interactions into `needs_you / handled / heads_up`, renders HTML, sends via `/me/sendMail`, archives to `<data_dir>/summaries/<day>.html`.
- **Chat auto-discovery (`a75d043`)** — background task hits `GET /me/chats` every 120s; any chat not in `watched_chats` gets auto-registered (in memory + persisted) so chats created via raw Python or by other humans adding the Agent User get polled within ~2 min.
- **Reply detection (`0732b8b`)** — `read_teams_messages` surfaces `reply_to_ids` from the Teams `<attachment id=…>` quote tag. `prompts/agent_system.md` Exception #3 lets the agent continue active 1:1 exchanges in group chats without re-`@`-tagging.
- **Eager MCP init (`d6cc640`)** — `_initialize()` runs as a background task at server boot instead of waiting for the first tool call. Fixed: fresh servers used to sit deaf to inbound DMs/email until someone happened to invoke a tool.
- **Email-push schema fix (`9a71d6c`)** — email push notification meta + content aligned with Teams push (no `<sender@addr>` angle brackets that read as HTML tags; meta carries only `chat_id="email"`/`message_id`/`user`/`ts`). Fixed: silent MCP-stream close after every email push.
- **`prompts/agent_system.md` (`75917a3`)** — system prompt moved out of `mcp_server.py` Python string into editable markdown, loaded at import time. Encodes: channel discipline (reply on the same channel, default-to-Teams when initiating, group chat ≠ N DMs, HTML for structured content), watch-only-in-group-chats with literal "about me ≠ tagged me" caveat + 3 narrow exceptions, internal-framing-stays-internal, no back-to-back pings, IDNA-only chat membership.
- **`setup.sh` hardening** — tenant-wide UPN lookup before Agent User creation (`8541d75`), warn-and-confirm before replacing Blueprint certs (`2338a7a`), cached-cert verification against Entra (`22e81d9`), `redirect_stdout(sys.stderr)` to stop diagnostic spam from corrupting `.env` cert thumbprint (`c99d66a`), `entraclaw-mcp` console script in `.mcp.json` (`5bb3bc4`).
- **ADR-005 Phase 1 (`f900ba1`)** — `BlobStore` async client in `src/entraclaw/storage/blob.py`. 22 tests. Not yet wired into runtime — Phase 2 (`MemoryBackend` protocol + routing) is the next ship.
- **Multi-tenant lightweight chat** — landed to `main` (commit `c8ec521`, 47 commits, +9331/-2484).

---

## What We're Building

A proof-of-concept demonstrating that **device-local AI agents can have their own identity** in Microsoft Entra, separate from the human user. Three identity modes:

1. **Agent User** (production path) — Blueprint → Agent Identity → Agent User via three-hop flow. Agent sends as its own Entra user.
2. **Delegated** (instant start) — MSAL interactive auth with human's token. Messages prefixed `[EntraClaw]`. No provisioning needed.
3. **Bot Gateway** (new) — M365 Agents SDK bot server with Dev Tunnel. Bot has its own identity in Teams by design. No Agent User provisioning, no M365 license.

**Identity Chain (Agent User):** Blueprint (certificate auth) → Agent Identity (FIC exchange) → Agent User (user_fic grant) → Graph API with `idtyp=user` token

**Channel:** Background poll every 5s (Graph API) or 2s (bot JSONL) → push via `notifications/claude/channel` → Claude Code receives messages automatically

### The Demo Scenario — WORKING (Three Modes)

| Step | Agent User Mode | Delegated Mode | Bot Mode |
|------|----------------|----------------|----------|
| Setup | `./scripts/setup.sh` (10-15 min) | `./scripts/setup_delegated.sh` (60s) | `./scripts/start_bot.sh` + Dev Tunnel |
| Auth | Three-hop flow (automatic) | MSAL browser sign-in (cached) | Bot app credentials |
| Identity | Agent's own Entra user | Human's identity + `[EntraClaw]` prefix | Bot's app identity |
| Send | Graph API as Agent User | Graph API as human | Bot Framework relay |
| Receive | Graph API poll (5s) | Graph API poll (5s) | Bot activity handler (instant) |

### MCP Tools (6 total)

| Tool | Purpose | Status |
|------|---------|--------|
| `send_teams_message` | Send message to chat in Teams (text or HTML). Bot mode: writes to outbound JSONL. | ✅ Live |
| `add_teams_member` | Add user to chat (cross-tenant auto-resolved) | ✅ Live |
| `read_teams_messages` | Read human's replies from Teams | ✅ Live |
| `watch_teams_replies` | Poll for new human replies with dedup | ✅ Live |
| `whoami` | Show agent identity and connection status | ✅ Live |
| `audit_log` | Record audit event before actions | ✅ Live |

---

## TDD Status

```
299 passed

Key modules:
  src/entraclaw/auth/          — certificate JWT + MSAL delegated auth
  src/entraclaw/bot/           — Bot Gateway (server, handler, tunnel, convo_store)
  src/entraclaw/identity/      — progressive identity state machine
  src/entraclaw/config.py      — ENTRACLAW_MODE + all env config
  src/entraclaw/mcp_server.py  — FastMCP + 3 auth modes + background poll
  src/entraclaw/tools/         — Teams Graph API tools
```

---

## Current Milestone: Bidirectional Teams Loop

**Spec:** `docs/architecture/PLAN-multi-tenant-lightweight-chat.md`
**Research:** `docs/platform-learnings/mcp-messaging-servers.md`

### Scope (scoped down from original)

1. **`watch_teams_replies` tool** — blocking polling tool with server-side cursor, timestamp overlap + message ID dedup
2. **Token auto-refresh** — eager (55-min threshold) + lazy (retry on 401) for three-hop flow

**Out of scope (LLM handles natively):** Conversation state tracking, action dispatch.

### Design Decisions Informed by Platform Research

Researched 12+ MCP messaging servers (Slack, iMessage, Discord, Teams). Key findings that changed the design:

- **Every MCP messaging server uses stateless request-response** — no background polling. Our blocking poll tool aligns with ecosystem patterns.
- **Client-side filtering mandatory** — Graph API `$filter`/`$orderby` unreliable for chat messages (Learning #16)
- **Timestamp overlap + seen-set dedup** — proven by imessage-kit (2s overlap, Map dedup). Prevents boundary message loss (Learning #17)
- **Token refresh is universal #1 pain point** — official Slack MCP had 18 re-auths in 5 days. Our three-hop flow is the most complex token lifecycle of any MCP messaging server studied (Learning #18)
- **Delta queries deferred** — too much complexity upfront (`@removed` entries, change types). Start simple (Learning #21)

### Known Unknowns (Will Discover During Testing)

1. **Overlap window size** — 2s borrowed from iMessage/SQLite; Graph API latency may need 3-5s
2. **Three-hop refresh behavior** — nobody else has refreshed a chained OBO flow mid-session
3. **Agent User token + `$orderby`** — floriscornel uses MSAL delegated tokens; our `user_fic` grant may behave differently
4. **Rate limiting thresholds** — undocumented for our endpoint + token type combination

### Solved: The MCP "Close the Loop" Problem

**Problem:** LLM doesn't automatically check for replies after sending a Teams message. The MCP protocol is request-response — no mechanism for the server to wake up the LLM when new data arrives.

**Solution:** Background polling + `notifications/claude/channel` push notifications. The MCP server declares `experimental: {"claude/channel": {}}` capability and pushes inbound Teams messages directly into the Claude Code conversation — the same mechanism used by the iMessage channel plugin.

**Requirements:** Start Claude Code with `--dangerously-load-development-channels server:openclaw` to enable channel notifications for development servers.

**Fallback:** `watch_teams_replies` tool still available for explicit polling. Background poll uses separate dedup state so both can detect the same message independently (Learning #27).

**Research:** See `docs/platform-learnings/mcp-close-the-loop.md` for full analysis of 12+ MCP messaging servers, the MCP Triggers & Events Working Group, and the three problems we solved (capability declaration, startup flag, separate state).

---

## What Works (Shipped)

- End-to-end: setup.sh → MCP server → Teams message delivery ✅
- Three-hop Agent User token flow (Blueprint → Agent Identity → Agent User)
- Agent User creation via Graph beta API (`microsoft.graph.agentUser`)
- Agent User license assignment (auto-detects Teams-capable SKUs)
- Consent grant (`oAuth2PermissionGrant`) for Teams/Chat permissions
- Dedicated provisioner app (avoids Azure CLI token rejection)
- State persisted in `.entraclaw-state.json` (idempotent, no secret reset)
- MCP server auto-discovered via `.mcp.json`
- `--teams-user` flag to set Teams recipient separately from admin
- Teams read with null-from handling (system messages)
- 27 hard-won learnings documented in runbooks
- Bidirectional Teams channel with background polling + push notifications
- Certificate auth for Blueprint (private key in OS keystore, no secrets on disk, ADR-003)
- Token auto-refresh: eager (55-min) + lazy (401 retry) for all tools
- `notifications/claude/channel` push — same mechanism as iMessage channel plugin
- Message dedup: 2s overlap window + bounded seen-set (imessage-kit pattern)
- 429 rate limit handling propagates through polling tool
- Autonomous agent instructions — acts on Teams messages without terminal prompting
- Multi-user group chat support (setup.sh `--teams-user=user1,user2`)
- Cross-tenant federated chats for B2B guests (auto-detects guest UPN, resolves home tenant via OpenID discovery)
- `add_teams_member` tool — add users to chat at runtime without restart (auto-resolves tenant from email domain)
- Chat ID persistence across restarts — no duplicate group chats
- 429 rate limit handling with Retry-After propagation
- All code passes ruff lint + format
- **Progressive identity state machine** — UNAUTHENTICATED → DELEGATED → PROVISIONING → AGENT_USER with asyncio.Lock-protected transitions
- **MSAL delegated auth** — localhost redirect + device code fallback, OS-encrypted token cache via msal-extensions
- **Setup script** for delegated mode (`scripts/setup_delegated.sh`) — sign in once, cache token, launch MCP server
- **Bot Gateway** — M365 Agents SDK bot server + JSONL IPC (inbound/outbound with fcntl.flock) + Dev Tunnel manager + conversation reference persistence. Coexists via `ENTRACLAW_MODE=bot` config switch
- **Identity-aware user ID** — `_effective_user_id()` returns the correct user ID for the current mode (agent user OID vs signed-in human OID)

### What's Not Started
- Azure Bot resource registration on werner.ac (needed for live bot test)
- Adaptive Cards for bot mode (Phase 2)
- Windows VM provisioning and testing

---

## Architecture

```
Blueprint (client_credentials)
  → Agent Identity (FIC exchange)
    → Agent User (user_fic grant, idtyp=user)
      → Graph API: Teams, Mail, OneDrive

┌─────────────────────────────────────────────────────────┐
│  Local Device (Mac / Windows)                           │
│                                                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │ Copilot CLI (MCP Client)                         │   │
│  │   └── connects via stdio ──┐                     │   │
│  └────────────────────────────┼─────────────────────┘   │
│                               │                         │
│                               ▼                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │ Openclaw MCP Server (Python)                     │   │
│  │                                                  │   │
│  │  send_teams_message ───▶ Graph API (Agent User)  │   │
│  │  read_teams_messages ──▶ Graph API (Agent User)  │   │
│  │  whoami ───────────────▶ cached state            │   │
│  │  audit_log ────────────▶ ~/.openclaw/audit/      │   │
│  │                                                  │   │
│  │  Token: Agent User (three-hop, idtyp=user)       │   │
│  └──────────────────────────────────────────────────┘   │
└───────────┬──────────────────────────┬──────────────────┘
            │                          │
            ▼                          ▼
    ┌───────────────┐          ┌──────────────┐
    │ Entra ID      │          │ Graph API    │
    │ Agent IDs     │          │ Teams Chat   │
    │ Agent Users   │          │ Messaging    │
    └───────────────┘          └──────────────┘
```

---

## Bugs Encountered & Resolved (This Session)

| # | Bug | Impact | Fix |
|---|-----|--------|-----|
| 1 | Provisioner secret reset on every re-run | High | Cache in state file, use `--append` |
| 2 | Agent User UPN used tenant ID as domain | Blocking | Extract domain from signed-in user's UPN |
| 3 | oAuth2PermissionGrant missing startTime | Blocking | Add `startTime: now()` to request body |
| 4 | Provisioner lacked DelegatedPermissionGrant permission | Blocking | Added to BASE_PERMISSION_VALUES |
| 5 | Three-hop flow missing fmi_path parameter | Blocking | Added `fmi_path={agent-id}` to hop 1 |
| 6 | Consent grant used beta API instead of v1.0 | Blocking | Use v1.0 URL directly, not graph_request() |
| 7 | Chat creation /me doesn't work for Agent Users | Blocking | Use explicit user IDs for both members |
| 8 | read_teams_messages crashed on null from field | Crash | `(m.get("from") or {})` pattern |
| 9 | Non-Teams licenses triggered skip | Wrong | Check TEAMS_CAPABLE_SKUS, not any license |
| 10 | MCP tool names not discoverable by LLM | UX | Renamed to verb-first, added trigger phrases |
| 11 | No httpx timeout on token flow | Hang | Added 15s timeout to all hops |
| 12 | teardown.sh silent exit on missing .env | Silent | Guard with `[ -f .env ]` check |
| 13 | stderr swallowed throughout scripts | Hidden errors | Removed all `2>/dev/null` |
| 14 | Admin and Teams user conflated | Wrong recipient | Added `--teams-user` flag |

See `docs/runbooks/hard-won-learnings.md` for the full append-only log (28 entries).

---

## Next Steps (Priority Order)

1. ~~Bidirectional Teams loop~~ — ✅ DONE. Background poll + channel push + dedup + token refresh.
2. ~~Token auto-refresh~~ — ✅ DONE. Eager (55-min) + lazy (401 retry).
3. ~~Certificate auth~~ — ✅ DONE. No secrets on disk. Private key in OS keystore (ADR-003).
4. ~~Close the loop~~ — ✅ DONE. `notifications/claude/channel` push via experimental capability.
5. ~~Multi-tenant lightweight chat~~ — ✅ DONE (PR #1). Progressive identity state machine + MSAL delegated auth. Branch: `feature/multi-tenant-lightweight-chat`.
6. ~~Bot Gateway~~ — ✅ DONE. M365 Agents SDK bot server + JSONL IPC + tunnel manager. Coexists via `ENTRACLAW_MODE=bot` switch. See `docs/architecture/DESIGN-teams-bot-gateway.md`.
7. **Bot Gateway live test** — NEXT. Register Azure Bot on werner.ac, sideload Teams app, verify end-to-end with Dev Tunnel.
8. **Adaptive Cards** — Rich status cards (build results, PR links, action buttons) for bot mode.
9. **Entra sign-in log verification** — confirm `idtyp=user` and agent attribution
10. **Windows VM provisioning** — verify cross-platform setup.sh (rescheduled to weekend)
11. **AppContainer sandbox spike** — kernel-level agent isolation on Windows
12. **Delta query optimization** — replace timestamp polling with `/messages/delta` if needed
