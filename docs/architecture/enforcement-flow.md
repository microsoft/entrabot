# Enforcement Flow

## Overview

Every agent action that touches a resource must pass through the enforcement pipeline. This is not optional — the audit layer wraps resource access, not the other way around.

## Flow

```
Agent wants to access a resource
        │
        ▼
┌─────────────────┐
│ 1. Token check  │  Does the agent have a valid Agent User token?
│    (auth/)      │  If expired → eager refresh (55-min) or lazy 401 retry
└────────┬────────┘
         │ valid token
         ▼
┌─────────────────┐
│ 2. Audit emit   │  Log the intent BEFORE the action
│    (audit/)     │  Event: agent_id, resource, action, timestamp
└────────┬────────┘
         │ event recorded
         ▼
┌─────────────────┐
│ 3. Execute      │  Perform the actual resource access
│    (caller)     │  Using the Agent User token
└────────┬────────┘
         │ result
         ▼
┌─────────────────┐
│ 4. Audit result │  Log success/failure of the action
│    (audit/)     │  Append outcome to the audit event
└─────────────────┘
```

The token check uses the three-hop Agent User token (ADR-002) for `agent_user` mode; in `delegated` mode the human's MSAL-cached token; in `bot` mode the bot's app credentials.

## Key Invariant

**Audit before execute.** If the audit emit fails, the action does not proceed. This ensures there is no "dark" agent activity — every attempted access is recorded, even if it ultimately fails.
