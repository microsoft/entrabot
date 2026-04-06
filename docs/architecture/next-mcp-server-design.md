# Next: Openclaw MCP Server Design

> The core deliverable — an MCP server that gives Copilot CLI agent identity, Teams communication, and audit.

## Overview

Openclaw runs as an **MCP server** that Copilot CLI connects to. It exposes identity, Teams, and audit as MCP tools. When Copilot CLI does agentic work, it calls these tools to authenticate as the agent, communicate through Teams, and record audit events.

## MCP Tools

### Identity Tools

| Tool | Description | When Called |
|------|-------------|------------|
| `openclaw_bootstrap` | Discover human identity (WAM/PRT or device code), register Agent ID, perform OBO exchange. Returns agent token. | Once at session start |
| `openclaw_whoami` | Return current agent identity: Agent ID, human sponsor, token scopes, token expiry | On demand |
| `openclaw_refresh` | Silently refresh the OBO token if nearing expiry | Periodically or before API calls |
| `openclaw_revoke` | Revoke the agent's token and clear cached credentials | When human says "stop" |

### Teams Tools

| Tool | Description | When Called |
|------|-------------|------------|
| `openclaw_teams_connect` | Create or resume a 1:1 Teams chat between the agent and the human | After bootstrap |
| `openclaw_teams_send` | Send a message to the human in Teams (text or Adaptive Card JSON) | Whenever agent has status/results |
| `openclaw_teams_poll` | Check for new messages from the human (delta query) | Every few seconds, or on demand |
| `openclaw_teams_presence` | Set the agent's presence status (Available, Busy, Away, Offline) | On state changes |

### Audit Tools

| Tool | Description | When Called |
|------|-------------|------------|
| `openclaw_audit_log` | Record an audit event (action, resource, outcome) | Before every resource access |
| `openclaw_audit_query` | Query recent audit events for this session | On demand / debugging |

## Architecture

```
┌──────────────────────────────────┐
│ Copilot CLI                      │
│ (MCP Client)                     │
│                                  │
│  User says: "deploy to staging"  │
│  Copilot calls:                  │
│    openclaw_audit_log(...)       │
│    <does the deploy>             │
│    openclaw_teams_send(...)      │
│    openclaw_teams_poll(...)      │
│                                  │
└──────────────┬───────────────────┘
               │ MCP (stdio or HTTP)
               ▼
┌──────────────────────────────────┐
│ Openclaw MCP Server              │
│ (Python process)                 │
│                                  │
│  ┌────────┐ ┌───────┐ ┌───────┐ │
│  │Identity│ │ Teams │ │ Audit │ │
│  │(MSAL)  │ │(Graph)│ │(JSON) │ │
│  └────────┘ └───────┘ └───────┘ │
│                                  │
│  Token cache: OS Credential Mgr  │
│  Audit log: ~/.openclaw/audit/   │
└──────────────────────────────────┘
```

## MCP Server Configuration

The user adds Openclaw to their Copilot CLI MCP config:

```json
// ~/.copilot/mcp-config.json (or .vscode/mcp.json)
{
  "mcpServers": {
    "openclaw": {
      "command": "python",
      "args": ["-m", "openclaw.mcp_server"],
      "env": {
        "OPENCLAW_TENANT_ID": "<entra-tenant-id>",
        "OPENCLAW_CLIENT_ID": "<agent-app-client-id>",
        "OPENCLAW_CLIENT_SECRET": "<agent-app-secret>"
      }
    }
  }
}
```

For the split architecture (production), the client secret moves to a cloud service and the env vars change to point at it.

## Bootstrap Sequence (Detailed)

```python
# Pseudocode for openclaw_bootstrap tool

async def openclaw_bootstrap():
    # 1. Try WAM/PRT (Windows Entra-joined devices)
    human_token = try_wam_acquire()

    # 2. Fallback: check for cached MSAL token
    if not human_token:
        human_token = msal_acquire_silent()

    # 3. Fallback: device code flow
    if not human_token:
        flow = app.initiate_device_flow(scopes=HUMAN_SCOPES)
        print(f"Enter code {flow['user_code']} at {flow['verification_uri']}")
        human_token = app.acquire_token_by_device_flow(flow)

    # 4. Register Agent ID
    agent_id = register_or_get_agent_id(human_token)

    # 5. OBO exchange
    obo_token = confidential_app.acquire_token_on_behalf_of(
        user_assertion=human_token["access_token"],
        scopes=AGENT_SCOPES  # Chat.Create, ChatMessage.Send, etc.
    )

    # 6. Cache everything
    store_in_credential_manager(agent_id, obo_token)

    return {
        "agent_id": agent_id,
        "scopes": AGENT_SCOPES,
        "expires_in": obo_token["expires_in"]
    }
```

## File Structure

```
src/openclaw/
  mcp_server.py        # MCP server entry point
  tools/
    identity.py        # openclaw_bootstrap, whoami, refresh, revoke
    teams.py           # openclaw_teams_connect, send, poll, presence
    audit.py           # openclaw_audit_log, query
  platform/
    windows.py         # WAM/PRT, Credential Manager, Task Scheduler
    mac.py             # Keychain, launchd, osascript consent
    linux.py           # Secret Service, systemd, polkit consent
  models.py            # Pydantic models for tokens, events, identity
  config.py            # Environment-based configuration
```

## What to Build First

1. `mcp_server.py` — bare MCP server with tool registration
2. `tools/identity.py` — `openclaw_bootstrap` with device code flow (skip WAM for first pass)
3. `tools/teams.py` — `openclaw_teams_connect` + `openclaw_teams_send` + `openclaw_teams_poll`
4. `tools/audit.py` — `openclaw_audit_log` writing to JSON file
5. `platform/windows.py` — `keyring` integration for Credential Manager
6. Wire it all together and test with Copilot CLI
