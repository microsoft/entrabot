# ADR-002: Agent User Over OBO for Device-Local Agents

**Status:** Accepted
**Date:** 2026-04-06
**Supersedes:** [ADR-001](001-obo-flows-for-device-agents.md)

## Context

ADR-001 chose OBO (On-Behalf-Of) token exchange for device-local agent identity. This required:
- A human device-code flow to get an initial user token
- Caching the human's refresh token in the OS keychain
- MSAL runtime dependency for token acquisition
- A custom `access_as_user` scope on the Blueprint for audience matching

This worked but had fundamental problems:
1. **Device-code flow requires a human** — can't run headless, in CI, or on VMs without interaction
2. **Refresh tokens expire and need management** — complex keychain lifecycle
3. **OBO is designed for "act on behalf of a user"** — the agent IS its own entity, not acting on behalf of anyone

Meanwhile, Microsoft shipped **Agent Users** (public preview) — purpose-built Entra user accounts for agents that authenticate autonomously via the three-hop flow.

## Decision

Replace the OBO chain with Agent User authentication:

```
Blueprint (client_credentials) → Agent Identity (FIC) → Agent User (user_fic) → Graph API
```

The Agent User is a real Entra user object (`microsoft.graph.agentUser`) that:
- Gets tokens with `idtyp=user` (accepted by all user-context APIs)
- Can have a mailbox, Teams presence, OneDrive, org chart entry
- Authenticates exclusively through its parent Agent Identity (no passwords/MFA)
- Can be assigned M365 licenses
- Is governed by Conditional Access like any user

## Consequences

### Positive
- No human in the loop — fully autonomous authentication
- No device-code flow — works headless, in CI, on VMs
- No MSAL runtime dependency — raw httpx calls to the token endpoint
- No refresh token management — three-hop flow acquires fresh tokens each time
- Agent has its own Teams identity — messages come FROM the agent
- Cleaner separation: agent IS a digital worker, not impersonating a human

### Negative
- Requires an M365 license per Agent User (~$8-36/month)
- Agent User is a preview feature (may change)
- Three-hop flow is more complex to debug than a single OBO call
- Blueprint needs `AgentIdUser.ReadWrite.IdentityParentedBy` permission

### Neutral
- Provisioner app pattern unchanged (still needed for setup)
- Audit attribution still works (agent identity in sign-in logs)
- Cross-platform credential storage still needed (for Blueprint secret in dev)
