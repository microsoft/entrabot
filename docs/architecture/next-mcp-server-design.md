# Next: Entraclaw MCP Server Design

> The core deliverable — an MCP server that gives Copilot CLI agent identity, Teams communication, and audit.

## Review Status

**CEO Review:** CLEAR (2026-04-06). HOLD SCOPE mode, B-lite approach.
**Outside Voice:** 8 findings, 2 critical bugs fixed (OBO audience, Agent ID API version).
**Key decisions:**
- Sandbox is co-equal with identity (not deferred)
- God Process accepted for MVP (split architecture for production)
- Tests alongside tools (mocked MSAL + Graph)
- Structured JSON logging for observability
- 2-minute timeout on device code flow
- Error hierarchy designed (see below)
- MSAL error dict handling is critical (returns dicts, not exceptions)

## Overview

Entraclaw runs as an **MCP server** that Copilot CLI connects to. It exposes identity, Teams, and audit as MCP tools. When Copilot CLI does agentic work, it calls these tools to authenticate as the agent, communicate through Teams, and record audit events.

## MCP Tools

### Identity Tools

| Tool | Description | When Called |
|------|-------------|------------|
| `entraclaw_bootstrap` | Discover human identity (WAM/PRT or device code), register Agent ID, perform OBO exchange. Returns agent token. | Once at session start |
| `entraclaw_whoami` | Return current agent identity: Agent ID, human sponsor, token scopes, token expiry | On demand |
| `entraclaw_refresh` | Silently refresh the OBO token if nearing expiry | Periodically or before API calls |
| `entraclaw_revoke` | Revoke the agent's token and clear cached credentials | When human says "stop" |

### Teams Tools

| Tool | Description | When Called |
|------|-------------|------------|
| `entraclaw_teams_connect` | Create or resume a 1:1 Teams chat between the agent and the human | After bootstrap |
| `entraclaw_teams_send` | Send a message to the human in Teams (text or Adaptive Card JSON) | Whenever agent has status/results |
| `entraclaw_teams_poll` | Check for new messages from the human (delta query) | Every few seconds, or on demand |
| `entraclaw_teams_presence` | Set the agent's presence status (Available, Busy, Away, Offline) | On state changes |

### Audit Tools

| Tool | Description | When Called |
|------|-------------|------------|
| `entraclaw_audit_log` | Record an audit event (action, resource, outcome) | Before every resource access |
| `entraclaw_audit_query` | Query recent audit events for this session | On demand / debugging |

## Architecture

```
┌──────────────────────────────────┐
│ Copilot CLI                      │
│ (MCP Client)                     │
│                                  │
│  User says: "deploy to staging"  │
│  Copilot calls:                  │
│    entraclaw_audit_log(...)       │
│    <does the deploy>             │
│    entraclaw_teams_send(...)      │
│    entraclaw_teams_poll(...)      │
│                                  │
└──────────────┬───────────────────┘
               │ MCP (stdio or HTTP)
               ▼
┌──────────────────────────────────┐
│ Entraclaw MCP Server              │
│ (Python process)                 │
│                                  │
│  ┌────────┐ ┌───────┐ ┌───────┐ │
│  │Identity│ │ Teams │ │ Audit │ │
│  │(MSAL)  │ │(Graph)│ │(JSON) │ │
│  └────────┘ └───────┘ └───────┘ │
│                                  │
│  Token cache: OS Credential Mgr  │
│  Audit log: ~/.entraclaw/audit/   │
└──────────────────────────────────┘
```

## MCP Server Configuration

The user adds Entraclaw to their Copilot CLI MCP config:

```json
// ~/.copilot/mcp-config.json (or .vscode/mcp.json)
// ⚠️ MVP ONLY: client secret in env vars is acceptable for dev.
//    Production must use split architecture (secret stays in cloud service).
{
  "mcpServers": {
    "entraclaw": {
      "command": "python",
      "args": ["-m", "entraclaw.mcp_server"],
      "env": {
        "ENTRACLAW_TENANT_ID": "<entra-tenant-id>",
        "ENTRACLAW_CLIENT_ID": "<agent-app-client-id>",
        "ENTRACLAW_CLIENT_SECRET": "<agent-app-secret>"
      }
    }
  }
}
```

### Python Dependencies

Add to `pyproject.toml`:
```toml
dependencies = [
    "msal>=1.28.0",
    "msal-extensions>=1.2.0",  # persistent token cache (OS-native)
    "mcp>=1.0.0",              # MCP server SDK (verify package name)
    "httpx>=0.27.0",           # async HTTP for Graph API calls
    "keyring>=25.0.0",         # OS credential storage abstraction
    "pydantic>=2.0",           # structured models
]
```

> **Note:** The Python MCP SDK landscape is still settling. Check whether `mcp`, `modelcontextprotocol`, or `fastmcp` is the right package before starting.

## Bootstrap Sequence (Detailed)

```python
# Pseudocode for entraclaw_bootstrap tool
#
# CRITICAL: The device code flow must request YOUR APP's custom scope,
# not Graph scopes directly. The OBO exchange requires the incoming token's
# `aud` claim to match the app's client ID.
#
# Device code flow scope: api://<client-id>/access_as_user → aud=<client-id> ✓
# NOT: User.Read → aud=https://graph.microsoft.com → OBO fails with invalid_grant

HUMAN_SCOPES = ["api://{client_id}/access_as_user"]
AGENT_SCOPES = ["https://graph.microsoft.com/Chat.Create",
                "https://graph.microsoft.com/ChatMessage.Send",
                "https://graph.microsoft.com/Chat.ReadWrite"]

async def entraclaw_bootstrap():
    # 1. Try WAM/PRT (Windows Entra-joined devices)
    human_token = try_wam_acquire(scopes=HUMAN_SCOPES)

    # 2. Fallback: check for cached MSAL token
    if not human_token:
        human_token = msal_acquire_silent(scopes=HUMAN_SCOPES)

    # 3. Fallback: device code flow (2-minute timeout)
    if not human_token:
        flow = public_app.initiate_device_flow(scopes=HUMAN_SCOPES)
        print(f"Enter code {flow['user_code']} at {flow['verification_uri']}")
        human_token = public_app.acquire_token_by_device_flow(flow, timeout=120)

    # CRITICAL: Check for MSAL error dict (MSAL returns errors as dicts, not exceptions)
    if "error" in human_token:
        raise MSALError(human_token["error"], human_token.get("error_description", ""))

    # 4. Register Agent ID (beta API — may not be available in all tenants)
    try:
        agent_id = register_or_get_agent_id(human_token)
    except AgentIDNotAvailable:
        agent_id = None  # Fallback: use app registration's azp claim for attribution

    # 5. OBO exchange (requires ConfidentialClientApplication with client secret)
    obo_result = confidential_app.acquire_token_on_behalf_of(
        user_assertion=human_token["access_token"],
        scopes=AGENT_SCOPES
    )
    if "error" in obo_result:
        raise OBOExchangeError(obo_result["error"], obo_result.get("error_description", ""))

    # 6. Cache everything in OS credential store
    store_in_credential_manager(agent_id, obo_result)

    # 7. Verify: Check Entra sign-in logs to confirm agent attribution
    # (manual step for MVP — log the azp and oid claims for inspection)
    logger.info("OBO token acquired",
                azp=obo_result.get("id_token_claims", {}).get("azp"),
                oid=obo_result.get("id_token_claims", {}).get("oid"),
                agent_id=agent_id)

    return {
        "agent_id": agent_id,
        "scopes": AGENT_SCOPES,
        "expires_in": obo_result["expires_in"]
    }
```

### Error Hierarchy

```python
# src/entraclaw/errors.py

class EntraclawError(Exception):
    """Base class for all Entraclaw errors."""

class AuthError(EntraclawError):
    """Authentication/identity errors."""

class MSALError(AuthError):
    """MSAL returned an error dict instead of a token."""
    def __init__(self, error: str, description: str):
        self.error = error
        self.description = description
        super().__init__(f"{error}: {description}")

class DeviceCodeTimeout(AuthError): ...
class ConsentDenied(AuthError): ...
class OBOExchangeError(AuthError): ...
class AgentIDNotAvailable(AuthError): ...

class TeamsError(EntraclawError):
    """Teams Graph API errors."""

class TeamsNotLicensed(TeamsError): ...
class ChatNotFound(TeamsError): ...
class MessageTooLong(TeamsError): ...

class TokenExpiredError(AuthError): ...
class RateLimitError(EntraclawError):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Rate limited. Retry after {retry_after}s")
```

## File Structure

```
src/entraclaw/
  mcp_server.py        # MCP server entry point
  tools/
    identity.py        # entraclaw_bootstrap, whoami, refresh, revoke
    teams.py           # entraclaw_teams_connect, send, poll, presence
    audit.py           # entraclaw_audit_log, query
  platform/
    windows.py         # WAM/PRT, Credential Manager, Task Scheduler
    mac.py             # Keychain, launchd, osascript consent
    linux.py           # Secret Service, systemd, polkit consent
  models.py            # Pydantic models for tokens, events, identity
  config.py            # Environment-based configuration
```

## What to Build First

### Tonight's Goal (MVP of the MVP)

> "Run Copilot CLI on the Windows VM, type something, and see a message appear in Teams from the agent."

Three tools. That's it:
1. `entraclaw_bootstrap` — get an agent-attributed token
2. `entraclaw_teams_connect` — create a 1:1 chat
3. `entraclaw_teams_send` — send a message

Everything else (`audit_log`, `refresh`, `revoke`, `whoami`, `teams_poll`, `teams_presence`) is iteration.

### Build Order

1. `config.py` — environment-based configuration (tenant ID, client ID, secret from env vars)
2. `models.py` — Pydantic models for tokens, identity, audit events
3. `platform/windows.py` — `keyring` integration for Credential Manager (identity needs this)
4. `mcp_server.py` — bare MCP server with tool registration
5. `tools/identity.py` — `entraclaw_bootstrap` with device code flow (stores to credential store)
6. `tools/teams.py` — `entraclaw_teams_connect` + `entraclaw_teams_send` (needs identity working first)
7. `tools/audit.py` — `entraclaw_audit_log` writing to JSON file (add after Teams works)
