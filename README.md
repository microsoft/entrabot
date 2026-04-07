# Openclaw Identity Research

Research project for securing agentic workflows on local devices (Mac/Linux/Windows) using Microsoft Entra Agent IDs and Agent Users. Agents get their own identity — a real Entra user account with Teams presence, mailbox, and M365 license — so audit logs always distinguish agent actions from human actions.

**The demo:** Tell your AI agent to do something and message you on Teams. Go to a bar. Reply from your phone. The agent acts on your instruction autonomously and reports back. Fully bidirectional, no human at the terminal.

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
4. Grant consent for Teams/Chat Graph permissions
5. Generate a self-signed certificate, upload public key to Entra, store private key in OS keystore (Keychain/TPM/Keyring) — no secrets on disk
6. Write `.env` with configuration (no secrets — only the cert thumbprint)

The script is **idempotent** — safe to re-run. State persists in `.openclaw-state.json`.

After setup, **assign an M365 license** (E3/E5/Teams Enterprise) to the Agent User in the Entra admin center and wait 10-15 minutes for Teams provisioning.

### Run with Claude Code

```bash
claude --dangerously-load-development-channels server:openclaw
```

The `--dangerously-load-development-channels` flag enables the Teams channel, which pushes inbound Teams messages directly into the conversation (like the iMessage channel plugin).

### Run with Copilot CLI

The `.mcp.json` in the project root auto-discovers the MCP server:

```bash
copilot
```

Note: Without `--dangerously-load-development-channels`, the agent won't receive push notifications for Teams replies. Use `watch_teams_replies` for explicit polling instead.

### MCP Tools (5 total)

| Tool | Purpose |
|------|---------|
| `send_teams_message` | Send a message to the human via Teams |
| `watch_teams_replies` | Poll for new human replies with dedup |
| `read_teams_messages` | Read recent message history |
| `whoami` | Check agent identity and connection status |
| `audit_log` | Record an action before performing it |

Plus a **background channel** that polls Teams every 5 seconds and pushes new messages via `notifications/claude/channel`.

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
- **auth/** — Certificate-based JWT assertion builder + three-hop token exchange
- **audit/** — Action tracking — every resource access emits an audit event before executing
- **tools/** — MCP tool implementations (Teams messaging, identity, audit)
- **mcp_server.py** — FastMCP server with background polling + channel notifications

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

Current status: **89 tests passing, 91% coverage**.

## Repository Map

| Directory | Purpose |
|-----------|---------|
| `src/openclaw/` | Application source code |
| `src/openclaw/auth/` | Certificate auth + JWT assertion builder |
| `src/openclaw/platform/` | OS-specific credential storage |
| `src/openclaw/tools/` | MCP tool implementations |
| `tests/` | Test suite (mirrors `src/` structure) |
| `scripts/` | Setup, teardown, and Entra provisioning scripts |
| `docs/` | Documentation site (MkDocs Material) |
| `docs/platform-learnings/` | Deep research on integration platforms + MCP ecosystem |
| `docs/decisions/` | Architecture Decision Records (3 ADRs) |
| `docs/runbooks/` | Hard-won learnings (27 entries) |
| `docs/superpowers/specs/` | Design specs |
| `docs/superpowers/plans/` | Implementation plans |

## Documentation

```bash
pip install mkdocs-material
mkdocs serve
```

Open http://localhost:8000 — or see [docs/index.md](docs/index.md) for a reading guide.
