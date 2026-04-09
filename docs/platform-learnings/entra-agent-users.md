# Entra Agent Users

## Overview

An **Agent User** is a specialized user account in Microsoft Entra purpose-built for AI agents. It is NOT the same as the Agent Identity (service principal). It's a second, optional identity paired 1:1 with an Agent Identity, designed for scenarios where the agent needs access to systems that **require a user object** — mailboxes, Teams channels, OneDrive, calendar, org chart presence.

The Agent User receives tokens with `idtyp=user`, meaning it looks like a user to every Microsoft 365 API. But it cannot have passwords, passkeys, or MFA factors — it authenticates exclusively through its parent Agent Identity's credentials.

## Identity Hierarchy

```
Agent Identity Blueprint (application)
  └─ BlueprintPrincipal (service principal)
      └─ Agent Identity (service principal)
          └─ Agent User (user object, optional, 1:1)
```

The Agent User is:
- Created via `POST /beta/users` with `@odata.type: microsoft.graph.agentUser`
- Always linked to exactly one Agent Identity via `identityParentId`
- Immutable parent link — cannot be re-parented to a different Agent Identity
- Deleted automatically if the parent Agent Identity is deleted

## Why Agent Users Exist

Agent Identities are service principals. Service principals cannot:
- Have a mailbox (Exchange Online)
- Join Teams channels or chats as a participant
- Have OneDrive storage
- Appear in the org chart or people cards
- Be @mentioned in Teams, documents, or other M365 apps
- Be assigned M365 licenses

Agent Users solve this. They're real user objects in the directory, but marked as agentic — so Conditional Access, ID Protection, and governance treat them appropriately (no MFA prompts, agent-aware audit, etc.).

## Creating an Agent User

### Permission Required

The Blueprint must be granted `AgentIdUser.ReadWrite.IdentityParentedBy` (application permission) in the tenant. This is NOT granted by default — it must be explicitly requested and admin-consented.

Alternatively, a different client (not the Blueprint) can use `AgentIdUser.ReadWrite.All`.

### API Call

```http
POST https://graph.microsoft.com/beta/users
OData-Version: 4.0
Content-Type: application/json
Authorization: Bearer <token>

{
  "@odata.type": "microsoft.graph.agentUser",
  "displayName": "Openclaw Agent",
  "userPrincipalName": "openclaw-agent@tenant.onmicrosoft.com",
  "identityParentId": "{agent-identity-object-id}",
  "mailNickname": "openclaw-agent",
  "accountEnabled": true
}
```

The token must come from the Blueprint (client_credentials) with the `AgentIdUser.ReadWrite.IdentityParentedBy` permission.

## Licensing

**Agent Users require M365 licenses to access M365 services.** This is explicit in the docs:

> Agentic users require appropriate Microsoft 365 licenses to access services like Teams, Email, Calendar, SharePoint, and OneDrive. Common licenses include Microsoft 365 E5, Teams Enterprise, and Microsoft 365 Copilot.

After assigning a license, resource provisioning (mailbox, OneDrive) typically completes within 10-15 minutes but can take up to 24 hours.

### What This Means for Openclaw

To give the agent its own Teams presence, we need:
1. An Agent User created and linked to the Agent Identity
2. A Teams-capable license assigned to the Agent User (E3, E5, or Teams Enterprise)
3. Wait for mailbox/Teams provisioning to complete

The agent then gets its own UPN (e.g., `openclaw-agent@tenant.onmicrosoft.com`), its own Teams identity, and can be @mentioned, receive messages, and participate in chats.

## Authentication: The Three-Hop Token Flow

Agent Users do NOT use device-code flow, OBO, or any interactive human auth. The flow is entirely machine-to-machine:

### Hop 1: Blueprint Token
```http
POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token
Content-Type: application/x-www-form-urlencoded

client_id={blueprint-app-id}
&scope=https://graph.microsoft.com/.default
&grant_type=client_credentials
&client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
&client_assertion={blueprint-credential}
```

### Hop 2: Agent Identity Token (FIC exchange)
```http
POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token
Content-Type: application/x-www-form-urlencoded

client_id={agent-identity-id}
&scope=api://AzureADTokenExchange/.default
&grant_type=client_credentials
&client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
&client_assertion={blueprint-token}
```

### Hop 3: Agent User Token (user_fic grant)
```http
POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token
Content-Type: application/x-www-form-urlencoded

client_id={agent-identity-id}
&scope=https://graph.microsoft.com/.default
&grant_type=user_fic
&client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
&client_assertion={blueprint-token}
&user_id={agent-user-object-id}
&user_federated_identity_credential={agent-identity-token}
```

The result is a **delegated access token** with `idtyp=user` that can call any Graph API requiring user context — Teams, Exchange, OneDrive, etc.

**No human in the loop. No device-code flow. No OBO. Fully autonomous.**

## Consent for Agent User

Before the Agent Identity can get tokens as the Agent User, an `oAuth2PermissionGrant` must be created:

```http
POST https://graph.microsoft.com/v1.0/oauth2PermissionGrants
Authorization: Bearer {token}
Content-Type: application/json

{
  "clientId": "{agent-identity-object-id}",
  "consentType": "Principal",
  "principalId": "{agent-user-object-id}",
  "resourceId": "{ms-graph-sp-object-id}",
  "scope": "Chat.Create Chat.ReadWrite ChatMessage.Send User.Read"
}
```

This grants the Agent Identity permission to act as the Agent User when calling Graph. This is a one-time admin operation.

## Security Constraints

- **No passwords/passkeys/MFA** — only authenticates through parent Agent Identity
- **No privileged admin roles** — cannot be Global Admin, etc.
- **No role-assignable groups** — cannot be added to groups used for admin role assignment
- **Guest-like default permissions** — can enumerate users/groups but has limited directory access
- **Immutable parent link** — cannot be re-parented once created
- **Auto-deleted with parent** — if Agent Identity is deleted, Agent User is deleted too

## Design Patterns (from Microsoft docs)

### Digital Worker (our use case)
> "A fully autonomous agent acts as a digital employee, provisioned with resources typically reserved for human employees: an Exchange mailbox, OneDrive share, and Teams presence."

Structure: One Blueprint → One Agent Identity → One Agent User

The Agent User gets:
- Its own mailbox
- Its own Teams presence
- Listing in the Global Address List
- Ability to be @mentioned
- A human manager in the org chart (the sponsor)

### When NOT to use Agent Users
- If the agent only needs application-level API access (use Agent Identity alone)
- If the agent only needs to call other agents (use Agent Identity with app roles)
- Scale-out replicas (share one Agent Identity, don't create per-replica)

## Implications for Openclaw

### What Changes
1. **No device-code flow needed** — Agent User authenticates via the three-hop machine-to-machine flow
2. **No OBO needed** — Agent User gets its own delegated tokens without a human token exchange
3. **No refresh token caching in keychain** — no human tokens to cache
4. **Blueprint needs FIC, not a client secret on device** — production auth uses Federated Identity Credentials
5. **Agent needs a license** — E3/E5/Teams Enterprise assigned to the Agent User
6. **Agent has its own Teams identity** — messages come FROM the agent, not "on behalf of" the human

### What Stays
1. Provisioner app pattern (for creating Blueprint + Agent Identity + Agent User)
2. Blueprint + BlueprintPrincipal + Agent Identity hierarchy
3. Sponsor relationship (human accountable for the agent)
4. Audit attribution (agent actions show as agent in sign-in logs)
5. Conditional Access governance (admin can block the agent)

## Directory Scale Constraints (Learning from 2026-04-08 debate)

Agent Users are directory objects. Every Agent User counts against the tenant's directory quota:

| Directory Size | Platform Support | Operational Risk |
|---|---|---|
| ≤300K | default | trivial |
| 300K–500K | common enterprise | low |
| 500K–1M | supported w/ approval | lifecycle needed |
| 1M–2M | achievable | RP propagation cost shows up — requires coordination |

**Without a verified domain:** 50K object limit.
**With 1+ verified domain:** 300K object limit.
**Beyond that:** contact Microsoft support.

### Why This Matters for Agent Users at Scale

If each user creates multiple agent sessions (Ayse reported 7-8 concurrent sessions), a 100K-employee company could need 700K+ Agent User objects — well past default limits.

### Pooling Does NOT Work

A "connection pool" model (pre-provision Agent Users, check out/return) was considered and rejected:

1. **OID recycling is a security risk** — residual permissions/audit from one session would attach to the next user who gets that OID
2. **Soft-deleted objects still count against quota** — deleted objects persist for 30 days at full weight, then another month at partial. Churning through pooled Agent Users worsens the scale problem.
3. **Hard deletes take time** — total lifecycle of a deleted object is ~2 months before quota is freed

### Better Alternatives

1. **IC3/Teams federation** (Henry Placeholder's proposal) — agent identities live in Teams' IC3 backend, not the Entra directory. Session IDs as external OIDs via native federation tokens. IC3 already scales to billions. No directory objects needed for chat-only scenarios.
2. **FMIs (Federated Machine Identities)** — SPIFFE/SPIRE-like identities that don't require directory objects. Could be hosted in IC3 or a separate identity store.
3. **Ephemeral Agent Users with TTL** — auto-deprovision after session end. Acceptable if the 2-month delete lifecycle is fixed.

### Implication

For ad-hoc CLI sessions, Agent Users (directory objects) are overkill. Reserve Agent Users for long-running autonomous agents that need persistent identity, governance, and resource access. For ephemeral sessions, use a lighter identity construct (federation, FMI, or OBO with decorated tokens).

## References

- [Agent Users concept](https://learn.microsoft.com/entra/agent-id/identity-platform/agent-users)
- [Request Agent User tokens](https://learn.microsoft.com/entra/agent-id/identity-platform/autonomous-agent-request-agent-user-tokens)
- [Agent ID design patterns](https://learn.microsoft.com/entra/agent-id/concept-agent-id-design-patterns)
- [Agent ID key concepts](https://learn.microsoft.com/entra/agent-id/identity-platform/key-concepts)
- [Agent 365 Identity](https://learn.microsoft.com/microsoft-agent-365/developer/identity)
