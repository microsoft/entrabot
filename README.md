# Openclaw Identity Research

Research project for securing agentic workflows on local devices (Mac/Linux/Windows) using Microsoft Entra Agent IDs and Agent Users. Agents get their own identity — a real Entra user account with Teams presence, mailbox, and M365 license — so audit logs always distinguish agent actions from human actions.

**The demo:** Tell your AI agent to do something and message you on Teams. Go to a bar. Reply from your phone. The agent acts on your instruction autonomously and reports back. Fully bidirectional, no human at the terminal. Supports multi-user group chats with cross-tenant federated users — add anyone from any org.

## Getting Started

### Prerequisites

- Azure CLI (`az`) logged in with admin access to your Entra tenant
- Python 3.12+
- Git
- An M365 license available for the Agent User (E3/E5/Teams Enterprise)

### One-Command Setup

```bash
./scripts/setup.sh
```

This script will:

1. Create a dedicated provisioner app registration (avoids Azure CLI token rejection)
2. Create an Agent Identity Blueprint + BlueprintPrincipal + Agent Identity
3. Create an Agent User (Entra user account linked to the Agent Identity)
4. Auto-assign a Teams-capable M365 license (scans tenant for E3/E5/Teams Enterprise)
5. Grant consent for Teams/Chat Graph permissions
6. Generate a self-signed certificate, upload public key to Entra, store private key in OS keystore (Keychain/TPM/Keyring) — no secrets on disk
7. Write `.env` with configuration (no secrets — only the cert thumbprint)
8. Write `.mcp.json` for auto-discovery by Claude Code / Copilot CLI

The script is **idempotent** — safe to re-run. State persists in `.entraclaw-state.json`.

#### Multi-user and cross-tenant setup

To start a group chat with multiple users (including B2B guests from other orgs):

```bash
./scripts/setup.sh --teams-user=user1@yourorg.com,guest@external.com
```

The script auto-detects guest users via their UPN pattern, resolves their home tenant via OpenID discovery, and creates a federated group chat (Graph API Example 7). No manual tenant ID lookup needed.

### Run with Claude Code

```bash
claude --dangerously-load-development-channels server:entraclaw
```

The `--dangerously-load-development-channels` flag enables the Teams channel, which pushes inbound Teams messages directly into the conversation (like the iMessage channel plugin).

### Run with Copilot CLI

The `.mcp.json` in the project root auto-discovers the MCP server:

```bash
copilot
```

Note: Without `--dangerously-load-development-channels`, the agent won't receive push notifications for Teams replies. Use `watch_teams_replies` for explicit polling instead.

### MCP Tools (6 total)

| Tool | Purpose |
|------|---------|
| `send_teams_message` | Send a message to the chat via Teams (text or HTML) |
| `add_teams_member` | Add a user to the chat (cross-tenant auto-resolved) |
| `watch_teams_replies` | Poll for new human replies with dedup |
| `read_teams_messages` | Read recent message history |
| `whoami` | Check agent identity and connection status |
| `audit_log` | Record an action before performing it |

Plus a **background channel** that polls Teams every 5 seconds and pushes new messages via `notifications/claude/channel`.

### Delegated Mode (No Agent User Needed)

For a quick demo without Agent User provisioning:

```bash
./scripts/setup_delegated.sh   # Sign in with your browser, caches token
copilot                        # or claude --dangerously-load-development-channels server:entraclaw
```

Messages are sent as you (with `[EntraClaw]` prefix). No E5 license, no 15-minute wait.

### Bot Mode (Separate Bot Identity)

For a demo where the agent has its own identity in Teams via Bot Framework:

1. Set `ENTRACLAW_MODE=bot` in `.env` with bot app credentials
2. Start the bot server + Dev Tunnel
3. Launch Claude Code / Copilot CLI

See `docs/architecture/DESIGN-teams-bot-gateway.md` for the full design.

### Without an Entra Tenant

To run the code and tests locally without a tenant:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -v
```

All Graph API calls are mocked in tests.

### Teardown

```bash
./scripts/teardown.sh
```

Removes the Agent User, Agent Identity, Blueprint, Provisioner app, and all local state.

## Architecture

The agent authenticates via the **three-hop Agent User flow** with certificate auth — fully autonomous, no human in the loop, no secrets on disk:

```
Blueprint (certificate in OS keystore)
  → Agent Identity (FIC exchange)
    → Agent User (user_fic grant, idtyp=user)
      → Graph API: Teams, Mail, OneDrive
```

Five modules handle the agent identity lifecycle:

- **platform/** — OS-specific credential storage (Keychain, Certificate Store, Secret Service)
- **auth/** — Certificate-based JWT assertion builder + MSAL delegated auth (localhost redirect + device code)
- **audit/** — Action tracking — every resource access emits an audit event before executing
- **tools/** — MCP tool implementations (Teams messaging, identity, audit)
- **bot/** — Bot Gateway: M365 Agents SDK server, JSONL IPC, Dev Tunnel manager, conversation reference persistence
- **identity/** — Progressive identity state machine (UNAUTHENTICATED → DELEGATED → AGENT_USER)
- **mcp_server.py** — FastMCP server with three auth modes, background polling + channel notifications

## Build and Test (TDD)

This project uses test-driven development. All new code requires a failing test before implementation.

```bash
# Run all tests
pytest -v

# Run with coverage (80% threshold enforced)
pytest -v --cov=openclaw --cov-report=term-missing --cov-fail-under=80

# Single test
pytest tests/tools/test_teams.py::TestAcquireAgentUserToken::test_success -v

# Lint + format
ruff check . && ruff format .
```

Current status: **299 tests passing**.

## Repository Map

| Directory | Purpose |
|-----------|---------|
| `src/entraclaw/` | Application source code |
| `src/entraclaw/auth/` | Certificate auth + JWT assertion builder + MSAL delegated auth |
| `src/entraclaw/bot/` | Bot Gateway: M365 Agents SDK server, JSONL IPC, tunnel, convo store |
| `src/entraclaw/identity/` | Progressive identity state machine |
| `src/entraclaw/platform/` | OS-specific credential storage |
| `src/entraclaw/tools/` | MCP tool implementations |
| `tests/` | Test suite (mirrors `src/` structure) |
| `scripts/` | Setup, teardown, delegated auth, and Entra provisioning scripts |
| `docs/` | Documentation site (MkDocs Material) |
| `docs/platform-learnings/` | Deep research on integration platforms + MCP ecosystem |
| `docs/decisions/` | Architecture Decision Records (3 ADRs) |
| `docs/runbooks/` | Hard-won learnings (28 entries) |


## Documentation

```bash
pip install mkdocs-material
mkdocs serve
```

Open http://localhost:8000 — or see [docs/index.md](docs/index.md) for a reading guide.
