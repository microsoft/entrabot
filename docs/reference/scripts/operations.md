# Operations scripts

Day-to-day scripts for interacting with the running agent: reading messages, sending DMs, running summaries, checking health.

All use the Agent User three-hop token unless noted otherwise.

> **Note for Copilot CLI / Codex / Cursor users.** On hosts that don't support the `notifications/claude/channel` push extension, the MCP server's background poll still runs and accumulates messages in the interaction log, but the LLM doesn't receive them until it actively reads. `catch_up.py` and `dm.py` are the CLI-side companions for that workflow. Claude Code users get inbound messages as channel-push system reminders and rarely need either script. See [System Overview — Message Delivery](../../architecture/system-overview.md#message-delivery-channel-push-vs-polling) for the full breakdown.

## `catch_up.py`

Pull recent messages from every watched chat and the agent's inbox.

### Usage

```bash
python scripts/catch_up.py
```

### What it does

- Acquires an Agent User token via the three-hop flow.
- Lists watched chats from `<data_dir>/watched_chats.jsonl`.
- For each chat, fetches the latest messages and prints them.
- Pulls recent emails from `/me/messages`.

Useful when the background poll has been running but the LLM didn't see the messages (non-channel-push host), or when the poll itself has been down and you want to see what was missed.

## `dm.py`

Send a Teams message to a chat as the Agent User.

### Usage

```bash
python scripts/dm.py "Your message here" --chat <chat_id>
python scripts/dm.py "Your message here" --chat <alias>
```

Define aliases in the `CHAT_ALIASES` dict at the top of the script for convenience.

### What it does

- Acquires the Agent User token.
- Resolves the chat alias if one is given.
- POSTs the message to `/chats/{chat_id}/messages`.

## `read_email.py`

Fetch and print emails by subject substring match.

### Usage

```bash
python scripts/read_email.py "Project Apollo"        # first 5 matches
python scripts/read_email.py "Project Apollo" 20     # top 20
```

### What it does

- Acquires the Agent User token.
- GETs `/me/messages` with `$filter=contains(subject, '...')`.
- Prints subject, from, received-at, and body of each match.

## `status.sh`

Root-level wrapper for the consolidated Agent Identity status command. This is the canonical status entry point for humans.

### Usage

```bash
./status.sh
./status.sh --json
./status.sh --health-only --strict
./scripts/setup.sh --status --json
```

`./scripts/setup.sh --status` delegates to `./status.sh` and forwards any remaining status arguments.

### What it does

- Ensures the local virtual environment exists.
- Installs the package with the dependencies needed for status checks if the venv is missing.
- Runs `scripts/show_agent_status.py` with the provided arguments.

## `show_agent_status.py`

Consolidated Agent Identity status. Reads local state and queries Graph for live data about the Blueprint, Agent Identity, Agent User, sponsors, permissions, certs, licenses, and storage.

### Usage

```bash
python scripts/show_agent_status.py
python scripts/show_agent_status.py --json
python scripts/show_agent_status.py --health-only
```

### What it does

- Reads `.entraclaw-state.json` and `.env`.
- Queries Graph for the Blueprint app, Agent Identity SP, Agent User.
- Lists sponsors, OAuth grants, cert thumbprints, license assignments.
- With `--health-only`, runs just the green / red checks and exits non-zero on failure (`--strict`).

## `show_permissions.py`

Show delegated `oauth2PermissionGrants` scoped to the Agent Identity.

### Usage

```bash
python scripts/show_permissions.py
python scripts/show_permissions.py --json
```

### What it does

- Mints a Provisioner Graph token.
- Queries `/oauth2PermissionGrants` filtered by `clientId={agent_sp_id}`.
- Prints the grants grouped by resource, with scope diffs against what `setup.sh` would grant.

## `health_check.py`

Compatibility wrapper for `show_agent_status.py --health-only`. Kept for users and scripts that still call `health_check.py`.

### Usage

```bash
python scripts/health_check.py            # health checks
python scripts/health_check.py --json     # machine-readable
```

Forwards to `show_agent_status.main([*args, "--health-only"])`.

## `start_bot.sh`

Launch the EntraClaw bot gateway: Dev Tunnel + bot server (aiohttp on `localhost:PORT`).

### Usage

```bash
./scripts/start_bot.sh             # start tunnel + bot server
./scripts/start_bot.sh --stop      # kill running tunnel + bot server
```

### What it does

- Validates prerequisites (`devtunnel` CLI, `.env`, venv).
- Starts a Dev Tunnel on the configured port.
- Starts the bot server.
- Prints the tunnel URL to register in Azure Bot Service.

The MCP server itself is launched separately by Claude Code via the `entraclaw` MCP entry. Prerequisite: `setup_bot.sh` has been run once.

See `docs/architecture/DESIGN-teams-bot-gateway.md` for the bot mode design.
