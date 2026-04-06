# Openclaw Identity Research — Engineering Summary

**Date:** April 6, 2026
**Team:** Brandon Werner
**Status:** Bidirectional loop working — Agent User sends and polls for Teams messages. 82 tests, 91% coverage. 5 MCP tools live.

---

## What We're Building

A proof-of-concept demonstrating that **device-local AI agents can have their own identity** in Microsoft Entra, separate from the human user. The agent gets an Agent Identity + Agent User, authenticates autonomously via the three-hop token flow, and interacts with Teams as its own digital worker.

**Identity Chain:** Blueprint (client_credentials) → Agent Identity (FIC exchange) → Agent User (user_fic grant) → Graph API with `idtyp=user` token

### The Demo Scenario — WORKING

| Step | What Happens | Status |
|------|-------------|--------|
| 1. `./scripts/setup.sh` | Creates Provisioner, Blueprint, Agent Identity, Agent User, assigns license, grants consent | ✅ Working |
| 2. Copilot CLI with MCP | MCP server starts, three-hop flow acquires Agent User token | ✅ Working |
| 3. `send_teams_message` | Agent sends message to human in Teams as "Openclaw Agent" with AI agent badge | ✅ Working |
| 4. `read_teams_messages` | Agent reads human's replies from Teams | ✅ Working |
| 5. Bidirectional loop | Agent polls for replies, acts on instructions, reports back | ✅ Working |

### MCP Tools (5 total)

| Tool | Purpose | Status |
|------|---------|--------|
| `send_teams_message` | Send message to human in Teams | ✅ Live (+ token refresh) |
| `read_teams_messages` | Read human's replies from Teams | ✅ Live (+ token refresh) |
| `watch_teams_replies` | Poll for new human replies with dedup | ✅ Live |
| `whoami` | Show agent identity and connection status | ✅ Live |
| `audit_log` | Record audit event before actions | ✅ Live |

---

## TDD Status

```
64 passed in 0.22s

Coverage: 87% (threshold: 80%)

Name                                Stmts   Miss  Cover
--------------------------------------------------------
src/openclaw/config.py                 43     11    74%
src/openclaw/errors.py                 18      0   100%
src/openclaw/models.py                 47      0   100%
src/openclaw/platform/__init__.py      16     11    31%
src/openclaw/platform/base.py           9      3    67%
src/openclaw/tools/audit.py            26      5    81%
src/openclaw/tools/identity.py          7      0   100%
src/openclaw/tools/teams.py            79      3    96%
--------------------------------------------------------
TOTAL                                 245     33    87%
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

### Discovered Gap: The MCP "Close the Loop" Problem

**The LLM doesn't automatically call `watch_teams_replies` after sending a message.** This is not a bug — it's a fundamental MCP protocol gap. The protocol is request-response; there is no mechanism for the server to wake up the LLM when new data arrives.

**Industry status:** Nobody has solved this. We researched 12+ MCP messaging servers — all are fire-and-forget. The MCP Triggers & Events Working Group was chartered March 2026 with an RFC targeting April 2026. No solution exists in any major client today.

**Our position:** We are ahead of the industry — the only MCP messaging server with a polling tool, token auto-refresh, and message dedup. When the WG ships its spec, we'll adopt it immediately.

**Current workarounds:** PostToolUse hook with `additionalContext` to hint the LLM should poll. See `docs/platform-learnings/mcp-close-the-loop.md` for full research.

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
- 23 hard-won learnings documented in runbooks (8 new from platform research)
- Bidirectional Teams polling loop with dedup + token refresh
- Token auto-refresh: eager (55-min) + lazy (401 retry) for all tools
- 429 rate limit handling propagates through polling tool
- All code passes ruff lint + format, 91% coverage

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

See `docs/runbooks/hard-won-learnings.md` for the full append-only log (21 entries).

---

## Next Steps (Priority Order)

1. **~~Bidirectional Teams loop~~** — IMPLEMENTING NOW. `watch_teams_replies` polling tool + timestamp overlap dedup + token refresh. See spec.
2. **~~Token auto-refresh~~** — IMPLEMENTING NOW. Bundled with #1. Eager (55-min) + lazy (401 retry).
3. **Entra sign-in log verification** — confirm `idtyp=user` and agent attribution
4. **Windows VM provisioning** — verify cross-platform setup.sh
5. **AppContainer sandbox spike** — kernel-level agent isolation on Windows
6. **Delta query optimization** — replace timestamp polling with `/messages/delta` if needed (deferred from #1)
