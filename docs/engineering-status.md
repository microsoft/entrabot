# Openclaw Identity Research — Engineering Summary

**Date:** April 6, 2026
**Team:** Brandon Werner
**Status:** End-to-end working — Agent User sends Teams messages from Copilot CLI. 64 tests, 87% coverage. Next: bidirectional Teams loop.

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
| 5. Bidirectional loop | Agent polls for replies, acts on instructions, reports back | 🔄 Next milestone |

### MCP Tools (4 total)

| Tool | Purpose | Status |
|------|---------|--------|
| `send_teams_message` | Send message to human in Teams | ✅ Live |
| `read_teams_messages` | Read human's replies from Teams | ✅ Live |
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

The agent can send messages and read replies, but there's no automated loop. The human replies in Teams, but the agent doesn't know to check unless explicitly told.

### What Needs Building

1. **`watch_teams_replies` tool** — polls Teams every N seconds, returns when the human replies. Needs message deduplication (track last-seen message ID so old messages aren't reprocessed).

2. **Conversation state** — the agent needs to know what it was working on when the human replies (e.g., "I was editing hello.py when the human asked me to add Brandon to the string").

3. **Action dispatch** — when the human says "add Brandon to the Hello World string", the agent parses that as an instruction and executes it, then reports back via Teams.

4. **Token refresh** — the three-hop tokens expire after ~60 min. Long-running polling needs automatic re-acquisition.

### Design Options

| Approach | Pros | Cons |
|----------|------|------|
| **Polling tool** (`watch_teams_replies`) | Simple, LLM calls it explicitly | LLM must remember to call it; burns context |
| **Background polling thread** in MCP server | Automatic, no LLM involvement | Needs a way to notify the LLM of new messages |
| **CronCreate** in Claude Code | Uses the built-in scheduler | Tight coupling to Claude Code runtime |

Recommended: **Polling tool first** (simplest, works today), then add background polling as a follow-up.

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
- 16 hard-won learnings documented in runbooks
- All code passes ruff lint + format, 87% coverage

### What's Not Started
- Bidirectional Teams polling loop (NEXT)
- Token auto-refresh for long-running sessions
- Windows VM provisioning and testing
- AppContainer sandbox spike
- Graph API rate limit handler
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

See `docs/runbooks/hard-won-learnings.md` for the full append-only log (16 entries).

---

## Next Steps (Priority Order)

1. **Bidirectional Teams loop** — `watch_teams_replies` polling tool + message dedup + action dispatch
2. **Token auto-refresh** — re-acquire three-hop tokens before 60-min expiry
3. **Entra sign-in log verification** — confirm `idtyp=user` and agent attribution
4. **Windows VM provisioning** — verify cross-platform setup.sh
5. **AppContainer sandbox spike** — kernel-level agent isolation on Windows
