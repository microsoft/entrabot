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
| **`platform/`** | OS-specific credential storage (Keychain, Credential Manager, Secret Service) | `src/entraclaw/platform/` |
| **`auth/`** | Three-hop token exchange (cert JWT + MSAL delegated) | `src/entraclaw/auth/` |
| **`audit/`** | Action tracking — every resource access emits an audit event before executing | `src/entraclaw/audit/` |
| **`tools/`** | MCP tools (Teams Graph API, interaction log, email poll, daily summary, cards) | `src/entraclaw/tools/` |
| **`bot/`** | Bot Gateway — M365 Agents SDK server, JSONL IPC, Dev Tunnel manager | `src/entraclaw/bot/` |
| **`identity/`** | Progressive identity state machine | `src/entraclaw/identity/` |
| **`storage/`** | LocalBackend / BlobBackend / PersonaBackend + migration helper (ADR-005) | `src/entraclaw/storage/` |
| **`mcp_server.py`** | FastMCP entry — three auth modes + body-first prompt loader + background poll + channel push | `src/entraclaw/mcp_server.py` |

The agent system prompt lives in `prompts/agent_system.md` plus the `@include`-expanded `prompts/anatomy/*.md` modules. When persona-sati is reachable, its mind contract layers on top of the body — never underneath. See `docs/architecture/DESIGN-persona-sati-integration.md`.

## Provisioning

Setup is handled by two Python scripts called from `setup.sh`:

1. **`entra_provisioning.py`** — Creates/manages the dedicated provisioner app (client_credentials, avoids Azure CLI token rejection)
2. **`create_entra_agent_ids.py`** — Creates Blueprint, BlueprintPrincipal, Agent Identity, Agent User, and grants consent

State persists in `.entraclaw-state.json` so re-runs are idempotent and don't reset secrets.

## Testing

TDD is a non-negotiable. All new code requires a failing test before implementation.

- ~790 tests, 80% coverage threshold enforced (see `pyproject.toml`)
- Token flows tested with mocked `httpx` (via `respx`)
- Graph API calls tested with mocked HTTP responses
- Coverage omits: MCP entry point, logging config, OS-specific platform modules
