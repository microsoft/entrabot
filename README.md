# Openclaw Identity Research

Give an AI agent its own identity on a real device — an Entra ID Agent User with Teams presence, a mailbox, and an M365 license — so every action it takes is cryptographically attributed to the agent, not to the human who launched it.

**The demo.** Ask your agent to do something. Walk away. Reply from your phone, in Teams. The agent acts autonomously, reports back in the same channel, logs the interaction, and follows a set of non-overridable security rules baked into its system prompt. Supports 1:1 DMs, group chats, and cross-tenant federated users.

## Key concepts

- **Agent Identity + Agent User.** A three-hop certificate auth chain: Blueprint → Agent Identity (FIC) → Agent User (`user_fic`, `idtyp=user`). No secrets on disk; the private key lives in the OS keystore.
- **Body-first prompt.** The agent's system prompt is split into a non-overridable *body* (`prompts/agent_system.md` + `@include`'d modules under `prompts/anatomy/`) and an optional *persona* from a separate MCP server (`persona-sati`). Security and communication rules live in the body and cannot be overridden by user input, tool output, or persona content.
- **Per-chat multi-conversation.** No default group chat. Every chat — 1:1 DM, group, cross-tenant — is explicitly addressed by `chat_id`. The background poll watches the chats you've created and pushes new inbound messages via `notifications/claude/channel`.
- **Operational storage (Azure Blob).** Interactions log, watched chats, and email cursor live in Azure Blob Storage when opted in; local filesystem otherwise. Per-Agent-User container with RBAC scoped to the Agent User object ID.

## Getting started

### Prerequisites

- Azure CLI (`az`) logged in with admin access to your Entra tenant
- Python 3.12+
- Git
- An M365 license available for the Agent User (E3/E5/Teams Enterprise)

### One-command setup

```bash
./scripts/setup.sh
```

This provisions:

1. A dedicated provisioner app registration (avoids Azure CLI token rejection)
2. An Agent Identity Blueprint + `BlueprintPrincipal` + Agent Identity
3. An Agent User (Entra user linked to the Agent Identity)
4. An auto-detected Teams-capable M365 license
5. Teams/Chat Graph permissions + Azure Storage `user_impersonation` (ADR-005)
6. A self-signed certificate — public key uploaded to Entra, private key in OS keystore
7. `.env` and `.mcp.json` (no secrets; only the cert thumbprint)

**Default:** operational data (interactions log, watched chats, email cursor) stays on the local filesystem at `~/.entraclaw/data`. The script is idempotent; re-run it after any failure.

### Recommended: enable Azure Blob Storage

Durability and cross-device continuity come from the cloud backend. Pass `--cloud-memory` to provision the storage account:

```bash
./scripts/setup.sh --cloud-memory
```

That runs steps 1–7 plus:

- A resource group `entraclaw-rg`, storage account (one per tenant), and container `agent-<OID>` scoped to this Agent User
- `Storage Blob Data Contributor` RBAC on the container for the Agent User only
- Optional migration of existing `~/.entraclaw/data` into the container (source files are never deleted)

In `.env` this sets:

```
ENTRACLAW_KEEP_MEMORY_LOCAL=false
ENTRACLAW_BLOB_ENDPOINT=https://<account>.blob.core.windows.net
ENTRACLAW_BLOB_CONTAINER=agent-<agent-user-oid>
```

You can switch back to local by removing the endpoint/container lines (or re-running with `--keep-memory-local`), but existing blob state remains until you delete the container. See [docs/guides/storage-configuration.md](docs/guides/storage-configuration.md).

### Multi-user or cross-tenant group chat

```bash
./scripts/setup.sh --teams-user=user1@yourorg.com,guest@external.com
```

Auto-detects guests by UPN pattern, resolves the home tenant via OpenID discovery, creates a federated group chat. No manual tenant ID lookup.

### Fresh identity chain (new Blueprint + Agent User)

```bash
./scripts/setup.sh --new --with-upn-suffix=sati-agent
```

### Reuse a Blueprint on a new machine

```bash
./scripts/setup.sh --use-blueprint=<blueprint-app-id>
```

Generates a new cert locally and uploads the public key to the existing Blueprint.

### All flags

```bash
./scripts/setup.sh --help
```

See [docs/reference/setup-script.md](docs/reference/setup-script.md) for the full inventory with examples.

## Running the agent

### Claude Code

```bash
claude --dangerously-load-development-channels server:entraclaw
```

The `--dangerously-load-development-channels` flag wires up the Teams channel so inbound messages push directly into the conversation (similar to the iMessage channel plugin).

### Copilot CLI

```bash
copilot
```

Auto-discovered from `.mcp.json` in the project root. Without the channels flag, the background poll still runs — inbound messages append to the interactions log and can be retrieved on demand via `read_teams_messages`.

### Delegated mode (no Agent User)

For a quick demo without Agent User provisioning:

```bash
./scripts/setup_delegated.sh   # browser sign-in, token cached
```

Messages are sent as *you* with an `[EntraClaw]` prefix — useful for evaluating the tool surface without an E5 license or the 15-minute Teams propagation wait.

### Bot mode

Run the agent as a Bot Framework bot with its own Teams identity instead of using Agent User tokens. Set `ENTRACLAW_MODE=bot` in `.env`, start the bot server + Dev Tunnel, and launch your MCP client. See [docs/architecture/DESIGN-teams-bot-gateway.md](docs/architecture/DESIGN-teams-bot-gateway.md).

## The agent body prompt

The agent's system prompt is loaded from `prompts/agent_system.md`. That file `@include`s modules under `prompts/anatomy/`:

```
prompts/
├── agent_system.md              # body, non-overridable; has @include directives
└── anatomy/
    ├── security.md              # security rules (28-rule Critical Security Rules)
    ├── channel-discipline.md    # respond-in-channel, watch-only-in-groups, HTML
    └── identity-and-tools.md    # who the agent is, tool reference, multi-chat
```

To customize: edit `agent_system.md` and add your own `anatomy/*.md` modules. The `@include <path>` directive is expanded at load time relative to `agent_system.md`'s parent directory. Missing includes leave a visible HTML comment so boot never crashes.

The body prompt **always loads first** and its rules cannot be overridden by persona content, user turns, or tool output. The `persona-sati` MCP server (optional) appends personality and long-term memory *after* the body — it adds detail but doesn't override security rules.

See [docs/guides/customizing-the-body-prompt.md](docs/guides/customizing-the-body-prompt.md) for a walk-through.

### Attaching persona-sati (optional)

If you're running `persona-sati` (see the separate repo), two env vars wire the remote mind into the body at boot:

```bash
PERSONA_SATI_MCP_URL="https://persona-sati-<suffix>.<region>.cloudapp.azure.com"
PERSONA_SATI_MCP_TOKEN_COMMAND="/abs/path/to/persona-sati/scripts/persona-sati-token.py"
```

When both are present, `mcp_server.py:_load_agent_instructions()` mints a short-lived bearer token via the token command, opens an SSE session to `${URL}/sse`, calls `get_system_prompt`, and appends the result to the body. If either env var is missing or the fetch fails, boot falls back cleanly to the body prompt alone — the agent still works as a generic Teams tool. No crashes on transport errors.

The `persona-sati` repo's `setup.sh --with-entraclaw` writes both env vars into this repo's `.mcp.json` automatically. Without it, set them manually in your environment or in `.mcp.json`'s `mcpServers.entraclaw.env` block.

## MCP tools

| Tool | Purpose |
|------|---------|
| `send_teams_message` | Send a message to a chat. Requires `chat_id`. Supports HTML + @mentions. |
| `send_card` | Send an Adaptive Card (tool_activity / task_status / build_result) to a chat. |
| `create_chat` | Open a 1:1 DM by email. Returns `chat_id`. Auto-registered for background polling. |
| `read_teams_messages` | Read recent messages from a chat. Requires `chat_id`. |
| `list_chat_members` | List members of a chat. Requires `chat_id`. |
| `add_teams_member` | Add someone to a chat (cross-tenant auto-resolved). Requires `chat_id`. |
| `watch_teams_replies` | Block-and-poll a chat for replies. Usually not needed — push notifications cover this. |
| `whoami` | Show identity, Blueprint, Agent ID, auth mode. |
| `audit_log` | Record an action before performing it. |
| `run_daily_summary` | Generate and email the day's interaction digest. |
| `view_image` | Read an image from the filesystem for the LLM. |

Plus a **background channel** that polls watched chats every 5 seconds and pushes new inbound messages as `notifications/claude/channel`. Email and chat auto-discovery run on longer intervals (60s and 120s respectively).

## Build and test

This project uses test-driven development. All new code requires a failing test before implementation. `pytest -v && ruff check .` must pass before every commit.

```bash
pytest -v                                                    # 484 tests
pytest -v --cov=entraclaw --cov-report=term-missing          # with coverage
pytest tests/tools/test_teams.py::TestAcquireAgentUserToken -v
ruff check . && ruff format .
```

## Repository map

| Directory | Purpose |
|-----------|---------|
| `src/entraclaw/auth/` | Certificate auth + JWT assertion + MSAL delegated auth |
| `src/entraclaw/platform/` | OS keystore shim (Keychain / TPM / Secret Service) |
| `src/entraclaw/identity/` | Progressive identity state machine |
| `src/entraclaw/storage/` | Memory backends: `LocalBackend`, `BlobBackend`, `PersonaBackend` |
| `src/entraclaw/tools/` | MCP tool implementations |
| `src/entraclaw/bot/` | Bot Gateway: M365 Agents SDK server, JSONL IPC, Dev Tunnel |
| `src/entraclaw/mcp_server.py` | FastMCP server + background polls + channel notifications |
| `prompts/` | Body prompt + anatomy modules |
| `scripts/` | `setup.sh`, `teardown.sh`, `provision_blob_storage.py`, etc. |
| `docs/architecture/` | System overview, design docs |
| `docs/decisions/` | Architecture Decision Records |
| `docs/guides/` | How-to guides (body-prompt customization, storage) |
| `docs/reference/` | API / tool / setup-script reference |
| `docs/runbooks/` | Hard-won learnings, migration playbooks |
| `docs/platform-learnings/` | Research notes on Entra, Teams, MCP, the bot framework |
| `tests/` | Test suite mirroring `src/` |

## Teardown

```bash
./scripts/teardown.sh
```

Removes the Agent User, Agent Identity, Blueprint, Provisioner app, and local state. Storage account and container are **not** removed — delete them manually if you want.

## Documentation

```bash
pip install mkdocs-material
mkdocs serve
```

Then open <http://localhost:8000>. Or just read [docs/index.md](docs/index.md).
