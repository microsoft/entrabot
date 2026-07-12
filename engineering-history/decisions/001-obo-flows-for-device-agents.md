# ADR-001: Use OBO Flows for Device-Local Agent Identity

**Status:** Superseded by [ADR-002](002-agent-user-over-obo.md)
**Date:** 2026-04-05

## Context

Autonomous agents running on a user's device (Mac/Linux/Windows) need to access resources on behalf of the user. Without a distinct agent identity, all actions appear as the human user in audit logs — making it impossible to distinguish human activity from agent activity.

Microsoft Entra supports Agent IDs and on-behalf-of (OBO) token flows in cloud scenarios. We need to decide whether to extend this pattern to device-local agents or use a different identity mechanism.

## Decision

Use Microsoft Entra Agent IDs with OBO token exchange for device-local agents, matching the cloud pattern.

## Rationale

- **Audit attribution**: OBO tokens attribute actions to the agent's identity while preserving the link to the consenting human. Sign-in logs show the agent, not the user.
- **Consistency**: The same identity model used for cloud agents works on devices. One audit story across cloud and device.
- **Consent model**: OBO requires explicit human consent, which aligns with the security requirement that agents never silently use human credentials.
- **Existing infrastructure**: Agent IDs and OBO are already built in Entra. We extend rather than reinvent.

## Consequences

- Requires MSAL integration on all three OSes (macOS, Linux, Windows)
- Depends on Entra Agent ID availability in the tenant
- Token refresh and revocation must be handled gracefully on devices that may go offline
- The platform abstraction layer must handle OS-specific credential storage for agent secrets

## Alternatives Considered

### Managed Identity (system-assigned)
Managed identities are tied to Azure resources, not user devices. No mechanism to issue a managed identity to a process on a personal laptop.

### Service Principal with client credentials
Would give the agent its own identity, but loses the "on behalf of" chain — audit logs wouldn't show which human consented. Also requires distributing client secrets to devices.

### Impersonation (using the human's token directly)
Simple but defeats the purpose — all actions look like the human. No way to distinguish agent activity in logs.
