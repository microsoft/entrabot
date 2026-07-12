# Agent Users

An **Agent User** is a specialized user account in Microsoft Entra purpose-built
for AI agents. It is distinct from the Agent Identity (a service principal): an
Agent User is a second, optional identity paired 1:1 with an Agent Identity, for
scenarios where the agent needs access to systems that **require a user object** —
mailboxes, Teams chats, OneDrive, calendar, and org-chart presence.

An Agent User receives tokens with `idtyp=user`, so it appears as a user to
every Microsoft 365 API. It cannot have passwords, passkeys, or MFA factors; it
authenticates exclusively through its parent Agent Identity's credentials.

For the full object model and OAuth constraints of the identity chain, see
[Microsoft Entra Agent ID: Blueprints, Identities, and Users](agent-id-blueprints-and-users.md).

## Identity hierarchy

```text
Agent Identity Blueprint (application)
  └─ BlueprintPrincipal (service principal)
      └─ Agent Identity (service principal)
          └─ Agent User (user object, optional, 1:1)
```

The BlueprintPrincipal must be **explicitly created** after the Blueprint via a
separate `POST /v1.0/servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal`
call. It is not auto-created when the Blueprint is created.

An Agent User is:

- Created via `POST /beta/users` with `@odata.type: microsoft.graph.agentUser`.
- Always linked to exactly one Agent Identity via `identityParentId`.
- Bound by an immutable parent link — it cannot be re-parented to a different
  Agent Identity.
- Deleted automatically when its parent Agent Identity is deleted.

## Why Agent Users exist

Agent Identities are service principals. Service principals cannot:

- Have a mailbox (Exchange Online).
- Join Teams chats or channels as a participant.
- Have OneDrive storage.
- Appear in the org chart or people cards.
- Be @mentioned in Teams, documents, or other Microsoft 365 apps.
- Be assigned Microsoft 365 licenses.

Agent Users solve this. They are real user objects in the directory, marked as
agentic, so Conditional Access, ID Protection, and governance treat them
appropriately (no MFA prompts, agent-aware audit).

## Creating an Agent User

### Required permission

The Blueprint must be granted `AgentIdUser.ReadWrite.IdentityParentedBy`
(application permission) in the tenant. This is not granted by default — it must
be explicitly requested and admin-consented.

Alternatively, a different client (not the Blueprint) can use
`AgentIdUser.ReadWrite.All`. `AgentIdUser.ReadWrite.All` is broader — it can
create Agent Users for any Blueprint in the tenant — and correspondingly more
sensitive. For least privilege, prefer `AgentIdUser.ReadWrite.IdentityParentedBy`
granted only to the Blueprint that creates its own Agent Users; this scopes the
permission to the Blueprint's own descendants.

### API call

```http
POST https://graph.microsoft.com/beta/users
OData-Version: 4.0
Content-Type: application/json
Authorization: Bearer <blueprint-client-credentials-token>

{
  "@odata.type": "microsoft.graph.agentUser",
  "displayName": "Entrabot Agent",
  "userPrincipalName": "entrabot-agent@tenant.onmicrosoft.com",
  "identityParentId": "{agent-identity-object-id}",
  "mailNickname": "entrabot-agent",
  "accountEnabled": true
}
```

The token must come from the Blueprint (client_credentials) with the
`AgentIdUser.ReadWrite.IdentityParentedBy` permission.

`POST /beta/users` for Agent User creation remains on Microsoft Graph beta.
Blueprint, BlueprintPrincipal, and Agent Identity creation use dedicated v1.0
subtype endpoints; only Agent User creation in this hierarchy remains on beta.

## Licensing

Agent Users require Microsoft 365 licenses to access Microsoft 365 services
such as Teams, Email, Calendar, SharePoint, and OneDrive. Common licenses
include Microsoft 365 E5, Teams Enterprise, and Microsoft 365 Copilot.

After a license is assigned, resource provisioning (mailbox, OneDrive)
typically completes within 10–15 minutes but can take up to 24 hours.

To give an agent its own Teams presence:

1. Create an Agent User and link it to the Agent Identity.
2. Assign a Teams-capable license (E3, E5, or Teams Enterprise).
3. Wait for mailbox and Teams provisioning to complete.

The agent then has its own UPN (for example,
`entrabot-agent@tenant.onmicrosoft.com`), its own Teams identity, and can be
@mentioned, receive messages, and participate in chats.

## Authentication: the three-hop token flow

Agent Users do not use device-code flow, OBO, or any interactive human auth.
The flow is entirely machine-to-machine:

1. **Hop 1 — Blueprint token.** The Blueprint authenticates with a
   certificate-signed client assertion and binds the exchange token to the
   target Agent Identity via `fmi_path`.
2. **Hop 2 — Agent Identity token.** The Agent Identity exchanges the Hop 1
   token for its own token (FIC exchange).
3. **Hop 3 — Agent User token.** A `grant_type=user_fic` request mints a
   delegated token for the Agent User, using `user_id={agent-user-object-id}`,
   the Hop 1 token as `client_assertion`, and the Hop 2 token as
   `user_federated_identity_credential`.

The result is a delegated access token with `idtyp=user` that can call any
Graph API requiring user context — Teams, Exchange, OneDrive. There is no human
in the loop, no device-code flow, and no OBO.

Microsoft examples use `user_id={agent-user-object-id}` as the canonical
selector and document `username={agent-user-upn}` as an alternative. For the
wire-level request and response shape of every hop, see the
[Token Flows reference](../reference/token-flows.md); for how Entrabot
implements it, see [Identity and Token Flow](../architecture/identity-and-token-flow.md).

## Consent for an Agent User

Before the Agent Identity can mint tokens as the Agent User, an
`oAuth2PermissionGrant` must be created:

```http
POST https://graph.microsoft.com/v1.0/oauth2PermissionGrants
Authorization: Bearer <token>
Content-Type: application/json

{
  "clientId": "{agent-identity-object-id}",
  "consentType": "Principal",
  "principalId": "{agent-user-object-id}",
  "resourceId": "{ms-graph-sp-object-id}",
  "scope": "Chat.Create Chat.ReadWrite ChatMessage.Send User.Read"
}
```

This grants the Agent Identity permission to act as the Agent User when calling
Graph. It is a **one-time admin operation per Agent User** — not per session,
per token acquisition, or per scope superset. Once granted, the Agent Identity
can mint Agent User tokens with the granted scopes for that Agent User until the
grant is revoked or the Agent User is deleted.

The `consentType: "Principal"` + `principalId: {agent-user-object-id}`
combination is per-principal consent, distinct from tenant-wide admin consent
(`consentType: "AllPrincipals"`). Tenant-wide consent for Agent User scopes
would grant the Agent Identity the right to mint Agent User tokens for *every*
Agent User in the tenant — almost always broader than intended.

## Security constraints

- **No passwords, passkeys, or MFA** — authenticates only through the parent
  Agent Identity.
- **No privileged admin roles** — cannot be Global Administrator, etc.
- **No role-assignable groups** — cannot join groups used for admin role
  assignment.
- **Guest-like default permissions** — can enumerate users and groups but has
  limited directory access.
- **Immutable parent link** — cannot be re-parented once created.
- **Auto-deleted with parent** — deleting the Agent Identity deletes the Agent
  User.

## Design patterns

### Digital worker

A fully autonomous agent acts as a digital employee, provisioned with resources
typically reserved for human employees: an Exchange mailbox, OneDrive share, and
Teams presence. Structure: one Blueprint → one Agent Identity → one Agent User.

The Agent User gets:

- Its own mailbox.
- Its own Teams presence.
- Listing in the Global Address List.
- The ability to be @mentioned.
- A human manager in the org chart (the sponsor).

### When not to use an Agent User

- If the agent only needs application-level API access, use the Agent Identity
  alone.
- If the agent only needs to call other agents, use the Agent Identity with app
  roles.
- For scale-out replicas, share one Agent Identity rather than creating one
  Agent User per replica.

## Directory scale

Agent Users are directory objects, and each counts against the tenant's
directory quota:

| Directory size | Support tier | Operational note |
|---|---|---|
| ≤ 300K | Default | Trivial. |
| 300K–500K | Common enterprise | Low risk. |
| 500K–1M | Supported with approval | Lifecycle management needed. |
| 1M–2M | Achievable | Resource-provisioning propagation cost requires coordination. |

Quota limits:

- **Without a verified domain:** 50K objects.
- **With one or more verified domains:** 300K objects.
- **Beyond that:** contact Microsoft support.

### Pooling is an anti-pattern

Pre-provisioning Agent Users and checking them out and back in does not work:

1. **Object-ID recycling is a security risk** — residual permissions and audit
   from one session can attach to the next holder of that object ID.
2. **Soft-deleted objects still count against quota** — a deleted object
   persists roughly 30 days at full weight and about another month at partial
   weight, so churning pooled Agent Users worsens the quota problem.
3. **Hard deletes take time** — the total lifecycle of a deleted object is about
   two months before quota is freed.

Reserve Agent Users for long-running autonomous agents that need persistent
identity, governance, and resource access. For ephemeral sessions, a lighter
identity construct (federation, federated machine identity, or delegated auth)
is a better fit.

## References

- [Agent's user account](https://learn.microsoft.com/en-us/entra/agent-id/agent-users)
- [Agent's user account impersonation protocol](https://learn.microsoft.com/en-us/entra/agent-id/agent-user-oauth-flow)
- [Agent ID design patterns](https://learn.microsoft.com/en-us/entra/agent-id/concept-agent-id-design-patterns)
- [Fundamental concepts in Microsoft Entra Agent ID](https://learn.microsoft.com/en-us/entra/agent-id/key-concepts)
- [Microsoft Entra Agent ID: Blueprints, Identities, and Users](agent-id-blueprints-and-users.md)
- [Identity and Token Flow](../architecture/identity-and-token-flow.md)
