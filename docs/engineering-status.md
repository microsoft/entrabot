# Openclaw Identity Research — Engineering Summary

**Date:** April 7, 2026
**Team:** Brandon Werner
**Status:** Full bidirectional Teams channel working — background poll + push notifications. Certificate auth (no secrets on disk). Multi-user group chats with cross-tenant federation. 110 tests. 6 MCP tools + background channel.

---

## What We're Building

A proof-of-concept demonstrating that **device-local AI agents can have their own identity** in Microsoft Entra, separate from the human user. The agent gets an Agent Identity + Agent User, authenticates autonomously via the three-hop token flow, and interacts with Teams as its own digital worker.

**Identity Chain:** Blueprint (certificate auth) → Agent Identity (FIC exchange) → Agent User (user_fic grant) → Graph API with `idtyp=user` token

**Channel:** Background poll every 5s → push via `notifications/claude/channel` → Claude Code receives messages automatically

### The Demo Scenario — WORKING

| Step | What Happens | Status |
|------|-------------|--------|
| 1. `./scripts/setup.sh` | Creates Provisioner, Blueprint, Agent Identity, Agent User, assigns license, grants consent | ✅ Working |
| 2. Copilot CLI with MCP | MCP server starts, three-hop flow acquires Agent User token | ✅ Working |
| 3. `send_teams_message` | Agent sends message to human in Teams as "Openclaw Agent" with AI agent badge | ✅ Working |
| 4. `read_teams_messages` | Agent reads human's replies from Teams | ✅ Working |
| 5. Bidirectional loop | Agent polls for replies, acts on instructions, reports back | ✅ Working |

### MCP Tools (6 total)

| Tool | Purpose | Status |
|------|---------|--------|
| `send_teams_message` | Send message to chat in Teams (text or HTML) | ✅ Live (+ token refresh) |
| `add_teams_member` | Add user to chat (cross-tenant auto-resolved) | ✅ Live |
| `read_teams_messages` | Read human's replies from Teams | ✅ Live (+ token refresh) |
| `watch_teams_replies` | Poll for new human replies with dedup | ✅ Live |
| `whoami` | Show agent identity and connection status | ✅ Live |
| `audit_log` | Record audit event before actions | ✅ Live |

---

## TDD Status

```
110 passed

Name                                Stmts   Miss  Cover
-----------------------------------------------------------------
src/openclaw/auth/__init__.py           2      0   100%
src/openclaw/auth/certificate.py       21      0   100%
src/openclaw/config.py                 43      2    95%
src/openclaw/errors.py                 18      0   100%
src/openclaw/models.py                 47      0   100%
src/openclaw/platform/__init__.py      16     11    31%
src/openclaw/platform/base.py           9      3    67%
src/openclaw/tools/audit.py            26      5    81%
src/openclaw/tools/identity.py          7      0   100%
src/openclaw/tools/teams.py            92      3    97%
-----------------------------------------------------------------
TOTAL                                 281     24    91%
```

---

## Current Milestone: Bidirectional Teams Loop

**Spec:** `docs/superpowers/specs/2026-04-06-bidirectional-teams-loop-design.md`
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
- State persisted in `.openclaw-state.json` (idempotent, no secret reset)
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

### What's Not Started
- Windows VM provisioning and testing
- AppContainer sandbox spike
- Entra sign-in log verification (`idtyp=user` claim)

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

See `docs/runbooks/hard-won-learnings.md` for the full append-only log (29 entries).

---

## Next Steps (Priority Order)

1. ~~Bidirectional Teams loop~~ — ✅ DONE. Background poll + channel push + dedup + token refresh.
2. ~~Token auto-refresh~~ — ✅ DONE. Eager (55-min) + lazy (401 retry).
3. ~~Certificate auth~~ — ✅ DONE. No secrets on disk. Private key in OS keystore (ADR-003).
4. ~~Close the loop~~ — ✅ DONE. `notifications/claude/channel` push via experimental capability.
5. **Multi-tenant lightweight chat** — ACTIVE. Branch: `feature/multi-tenant-lightweight-chat`. Multi-tenant app + device code auth + progressive identity (sponsor → Agent User). See `docs/architecture/NEXT-WhatsApp-lightweight-teams-chat.md` for full spec.
6. **Entra sign-in log verification** — confirm `idtyp=user` and agent attribution
7. **Windows VM provisioning** — verify cross-platform setup.sh (rescheduled to weekend)
8. **AppContainer sandbox spike** — kernel-level agent isolation on Windows (rescheduled to weekend)
9. **Delta query optimization** — replace timestamp polling with `/messages/delta` if needed
10. **Publish as Claude Code marketplace plugin** — move from `--dangerously-load-development-channels` to proper plugin distribution
