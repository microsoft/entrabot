# System Overview

## Goal

Give device-local AI agents their own identity in Microsoft Entra so that every action is attributed to the agent, not the human. The agent authenticates autonomously as an **Agent User** — a purpose-built Entra user account with its own Teams presence, mailbox, and M365 license.

## Identity Hierarchy

```
Agent Identity Blueprint (application — one per project)
  └─ BlueprintPrincipal (service principal — must be created explicitly)
      └─ Agent Identity (service principal — one per device)
          └─ Agent User (user object — optional, 1:1, has Teams/mailbox)
```

## Authentication: Three-Hop Flow

No human in the loop. No device-code flow. No OBO. Fully autonomous:

```
Hop 1: Blueprint authenticates with a certificate JWT assertion
       (private key in OS keystore — Keychain / TPM / Keyring; see ADR-003)
       → Blueprint token (client_credentials grant)

Hop 2: Agent Identity authenticates with Blueprint token as assertion
       → Agent Identity token (FIC exchange, client_credentials grant)

Hop 3: Agent User token via user_fic grant
       → Delegated token with idtyp=user
       → Can call Teams, Exchange, OneDrive, plus a parallel storage-scope
         hop for Azure Blob (ADR-005 Phase 5)
```

The Blueprint's underlying app type post-GA cannot be flipped to fallback-public-client mode and cannot host browser-based PKCE flows. For MCP servers that need both machine flows (this three-hop) and browser-based delegation, see `docs/platform-learnings/agent-id-blueprints-and-users.md`.

## System Topology

```
┌─────────────────────────────────────────────────────────┐
│  Local Device (Mac / Windows / Linux)                   │
│                                                         │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐          │
│  │ Platform │───▶│   Auth   │───▶│  Audit   │          │
│  │ (OS shim)│    │(3-hop)   │    │(log/emit)│          │
│  └──────────┘    └────┬─────┘    └──────────┘          │
│                       │                                 │
│                       ▼                                 │
│                 ┌──────────┐                            │
│                 │  Teams   │                            │
│                 │(Agent UX)│                            │
│                 └──────────┘                            │
└────────────┬────────────────────────────────────────────┘
             │
             ▼
     ┌───────────────┐       ┌──────────────┐
     │ Microsoft     │       │ Microsoft    │
     │ Entra ID      │       │ Teams        │
     │ (Agent IDs,   │       │ (Graph API,  │
     │  Agent Users) │       │  Agent User) │
     └───────────────┘       └──────────────┘
```

## Core Modules

| Module | Purpose | Location |
|--------|---------|----------|
| **`platform/`** | OS-specific credential storage (Keychain, Credential Manager, Secret Service) | `src/entrabot/platform/` |
| **`auth/`** | Three-hop token exchange (cert JWT + MSAL delegated) | `src/entrabot/auth/` |
| **`audit/`** | Action tracking — every resource access emits an audit event before executing | `src/entrabot/audit/` |
| **`tools/`** | MCP tools (Teams Graph API, interaction log, email poll, daily summary, cards) | `src/entrabot/tools/` |
| **`bot/`** | Bot Gateway — M365 Agents SDK server, JSONL IPC, Dev Tunnel manager | `src/entrabot/bot/` |
| **`identity/`** | Progressive identity state machine | `src/entrabot/identity/` |
| **`storage/`** | LocalBackend / BlobBackend / PersonaBackend + migration helper (ADR-005) | `src/entrabot/storage/` |
| **`mcp_server.py`** | FastMCP entry — three auth modes + body-first prompt loader + background poll + channel push | `src/entrabot/mcp_server.py` |

The agent system prompt lives in `prompts/agent_system.md` plus the `@include`-expanded `prompts/anatomy/*.md` modules. When persona-sati is reachable, its mind contract layers on top of the body — never underneath. See `docs/architecture/DESIGN-persona-sati-integration.md`.

## Message Delivery — Channel Push vs Polling

The MCP server runs four background tasks: Teams chat poll (5s), email poll (60s), chat auto-discovery (120s), and a daily-summary scheduler at 5pm PDT. All four are server-side and always running in `agent_user` mode. What differs between hosts is how those messages reach the LLM.

**Claude Code (channel push).** Claude Code implements the `notifications/claude/channel` extension. When the background poll detects an inbound Teams message or email, the server emits a channel notification and the LLM receives it as a next-turn `<channel source="entrabot">` system reminder — no tool call, no human prompt. The agent sees DMs the moment they land; the Teams conversation IS the conversation with the agent. Start Claude Code with `--dangerously-load-development-channels server:entrabot` to enable the extension.

**Copilot CLI / Codex / Cursor / other MCP hosts (polling fallback).** Hosts without the channel-push extension still get the background poll running — messages accumulate in the interaction log (`~/.entrabot/data/interactions/<day>.jsonl` or the equivalent blob path), but they don't stream into the LLM. The agent reads them on demand:

- `read_teams_messages(chat_id)` — current state of a chat
- `send_teams_message(...)` — on non-Claude-Code hosts, auto-blocks after sending until the sponsor's reply arrives, then returns it inline as `sponsor_reply`. This is the deterministic, host-detected wait pattern; no parameter the model can disable.
- `scripts/catch_up.py` — prints every watched chat's recent activity. Useful when a human wants to see what landed while the host wasn't subscribed.
- `scripts/dm.py "message" --chat <id>` — send-only CLI shortcut for when the agent isn't running.

Channel push is the better UX. The polling fallback is a working second-class path for hosts that haven't shipped the extension. See `docs/platform-learnings/mcp-close-the-loop.md` for the spec analysis and the three problems channel-push solves.

## Provisioning

Setup is handled by two Python scripts called from `setup.sh`:

1. **`entra_provisioning.py`** — Creates/manages the dedicated provisioner app (client_credentials, avoids Azure CLI token rejection)
2. **`create_entra_agent_ids.py`** — Creates Blueprint, BlueprintPrincipal, Agent Identity, Agent User, and grants consent

State persists in `.entrabot-state.json` so re-runs are idempotent and don't reset secrets.

## Testing

TDD is a non-negotiable. All new code requires a failing test before implementation.

- ~790 tests, 80% coverage threshold enforced (see `pyproject.toml`)
- Token flows tested with mocked `httpx` (via `respx`)
- Graph API calls tested with mocked HTTP responses
- Coverage omits: MCP entry point, logging config, OS-specific platform modules
