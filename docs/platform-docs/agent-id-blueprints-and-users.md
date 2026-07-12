# Microsoft Entra Agent ID: Blueprints, Identities, and Users

Microsoft Entra Agent ID gives autonomous agents their own first-class
directory identities, distinct from both the humans who operate them and the
applications that host them. This page is the platform reference for the four
object types, their creation endpoints, the OAuth constraints they enforce, and
the provisioning identity model. For Entrabot's concrete token acquisition, see
[Identity and Token Flow](../architecture/identity-and-token-flow.md) and the
[Token Flows reference](../reference/token-flows.md).

## Object model

The Agent ID platform has four distinct first-class object types, each with a
distinct Graph resource and `@odata.type`:

| Object | Graph resource | `@odata.type` | Underlying type | Created with |
|---|---|---|---|---|
| **Agent Identity Blueprint** | [`agentIdentityBlueprint`](https://learn.microsoft.com/en-us/graph/api/resources/agentidentityblueprint?view=graph-rest-1.0) | `#microsoft.graph.agentIdentityBlueprint` | application (subtype) | `POST /v1.0/applications/microsoft.graph.agentIdentityBlueprint` |
| **Agent Identity Blueprint Principal** | [`agentIdentityBlueprintPrincipal`](https://learn.microsoft.com/en-us/graph/api/resources/agentidentityblueprintprincipal?view=graph-rest-1.0) | `#microsoft.graph.agentIdentityBlueprintPrincipal` | servicePrincipal (subtype) | `POST /v1.0/servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal` |
| **Agent Identity** | [`agentIdentity`](https://learn.microsoft.com/en-us/graph/api/resources/agentidentity?view=graph-rest-1.0) | `#microsoft.graph.agentIdentity` | servicePrincipal (subtype) | `POST /v1.0/servicePrincipals/microsoft.graph.agentIdentity` |
| **Agent User** | `agentUser` | `#microsoft.graph.agentUser` | user (subtype) | `POST /beta/users` |

Lifecycle hierarchy:

```text
Agent Identity Blueprint (application object)
  └─ Agent Identity Blueprint Principal (service principal — runtime presence in a tenant)
      └─ Agent Identity (service principal, ServiceIdentity subtype)
          └─ Agent User (user object, optional, 1:1 with the Agent Identity)
```

Blueprint, BlueprintPrincipal, and Agent Identity creation use dedicated
Microsoft Graph v1.0 subtype endpoints. Agent User creation uses `POST /beta/users`
with the subtype discriminator.

From [Fundamental concepts in Microsoft Entra Agent ID](https://learn.microsoft.com/en-us/entra/agent-id/key-concepts):

> *"An agent identity is the primary identity an AI agent uses to authenticate
> to systems and access resources. Unlike user accounts, agent identities don't
> have credentials of their own. They authenticate using tokens issued by their
> agent identity blueprint."*

> *"An agent identity blueprint is an object in Microsoft Entra ID that serves
> as the template and authentication foundation for one or more agent
> identities. The blueprint holds credentials and uses them to acquire tokens on
> behalf of all agent identities created from it."*

### Agent Identity Blueprint

Created via:

```http
POST https://graph.microsoft.com/v1.0/applications/microsoft.graph.agentIdentityBlueprint
Content-Type: application/json

{
  "displayName": "...",
  "sponsors@odata.bind": ["https://graph.microsoft.com/v1.0/users/<id>"],
  "owners@odata.bind": ["https://graph.microsoft.com/v1.0/users/<id>"]
}
```

The Blueprint inherits from `application` but Microsoft excludes several
application properties:

> *"While this resource inherits from **application**, some properties are not
> applicable and return `null` or default values."*

Supported properties per the v1.0 reference include:

- `api` (apiApplication) — supports `oauth2PermissionScopes` for the
  `access_agent` scope used by interactive agents.
- `appId`, `appRoles`, `displayName`, `description`, `identifierUris`,
  `signInAudience`, `tags`, `info`, `requiredResourceAccess`, `optionalClaims`,
  `keyCredentials`, `passwordCredentials`, `verifiedPublisher`,
  `groupMembershipClaims`, `tokenEncryptionKeyId`, `serviceManagementReference`.
- `web` (webApplication) — supports `redirectUris`, `implicitGrantSettings`,
  `homePageUrl`, `logoutUrl`, used **only** for the consent-recording redirect
  for interactive agents.
- `managerApplications` — up to 10 first-party Microsoft apps that can manage
  the Blueprint without `AgentIdentityBlueprintPrincipal.ReadWrite.All`.
- `inheritablePermissions` (relationship) — scopes that auto-inherit to child
  Agent Identities.

Explicitly **absent** from the supported-properties list, and rejected at the
API surface:

- `publicClient` — no `redirectUris` for native/desktop/CLI apps.
- `spa` — no SPA redirect URIs.
- `isFallbackPublicClient` — cannot be flipped to fallback-public-client mode.

`az ad app update` returns *"incompatible with Agent Blueprints"* and Graph
PATCH rejects setting these.

`signInAudience` for Blueprints supports the standard four values
(`AzureADMyOrg` (default), `AzureADMultipleOrgs`,
`AzureADandPersonalMicrosoftAccount`, `PersonalMicrosoftAccount`), but Agent
Identities themselves are always single-tenant:

> *"Agent identities are always single-tenant regardless of their parent Agent
> identity blueprint's tenancy model. Each agent identity operates within one
> tenant's security and policy boundaries."*

### Agent Identity Blueprint Principal

Created explicitly after the Blueprint application:

```http
POST https://graph.microsoft.com/v1.0/servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal
{ "appId": "<blueprint-appId>" }
```

This is **not** auto-created by Graph when the Blueprint is created — you must
call this explicitly after the Blueprint creation step, including on idempotent
resume paths.

When the Blueprint acquires tokens, the token's `oid` claim references the
Blueprint Principal's object ID, not the Blueprint application's `appId`. Audit
logs attribute Blueprint actions to this principal.

### Agent Identity

Created from a Blueprint, single-tenant by definition:

```json
{
  "@odata.type": "#microsoft.graph.agentIdentity",
  "id": "<oid>",
  "accountEnabled": true,
  "agentIdentityBlueprintId": "<blueprint-appId>",
  "createdByAppId": "<blueprint-appId>",
  "displayName": "...",
  "servicePrincipalType": "ServiceIdentity",
  "tags": []
}
```

`servicePrincipalType` is fixed at `"ServiceIdentity"`. There is no
redirect-URI field, no credential field, and no `web`/`spa`/`publicClient`. The
Agent Identity holds **permissions and audit identity** only; the Blueprint
holds the credentials and impersonates it.

### Agent User

Optional, 1:1 with an Agent Identity:

```http
POST https://graph.microsoft.com/beta/users
{
  "@odata.type": "microsoft.graph.agentUser",
  "displayName": "...",
  "userPrincipalName": "...@tenant.onmicrosoft.com",
  "identityParentId": "<agent-identity-oid>",
  "accountEnabled": true
}
```

The Agent User's tokens carry `idtyp=user`, so it appears as a user to every
Microsoft 365 API. It cannot have passwords, passkeys, or MFA factors:

> *"The agent's user account doesn't have regular credentials like passwords.
> Instead, it's restricted to using the credentials provided through its parent
> relationship... The only credential type it supports is the agent identity
> reference to its parent. So even if the agent's user account behaves as a
> user, its credentials are confidential client credentials."*

> *"Once established, this relationship is immutable... The relationship is a
> one-to-one (1:1) mapping. Each agent identity can have at most one associated
> agent's user account, and each agent's user account is linked to exactly one
> parent agent identity, itself linked to exactly one agent identity blueprint
> application."*

See [Agent Users](entra-agent-users.md) for the full Agent User object model,
consent, licensing, and lifecycle.

## Sponsors

Blueprint and Agent Identity creation require at least one sponsor — a human who
is accountable for the agent. Bind a Microsoft Graph v1.0 user reference:

```json
"sponsors@odata.bind": ["https://graph.microsoft.com/v1.0/users/{sponsor-object-id}"]
```

Do not bind a service principal, group, or `/directoryObjects/` reference where
a user reference is expected.

Group-type sponsors are restricted: only **dynamic-membership groups** and
**Microsoft 365 groups** are accepted. Role-assignable groups and
fixed-membership security groups are not accepted as group-type sponsors.
Individual users are always supported.

## Load-bearing OAuth and platform constraints

These constraints are enforced by the platform above the OAuth protocol layer,
in the Graph API and `az` CLI. They are not visible from RFC 8414 / RFC 7591 /
RFC 9728 alone. Design any auth flow starting from these facts.

1. **Agent Identity Blueprints cannot be OAuth public clients.**
   > *"Public client capabilities aren't available, requiring all agents to
   > operate as confidential clients. Redirect URLs aren't supported."*

   The Blueprint application object inherits from `application`, but the
   `publicClient`, `spa`, and `isFallbackPublicClient` surfaces are excluded —
   they return `null` or are rejected on PATCH. **A Blueprint cannot be the
   `client_id` of a browser-based PKCE authorization-code flow.**
2. **Agent Identities are confidential service principals**
   (`servicePrincipalType=ServiceIdentity`). They cannot hold credentials of
   their own; the Blueprint impersonates them via FIC. They cannot be public
   clients.
3. **OBO for agents does not use `/authorize` directly for the Blueprint.**
   > *"OBO flows using the `/authorize` endpoint aren't supported for any agent
   > entity, ensuring all authentication occurs programmatically."*

   The user-facing `/authorize` step is run by a separate client app (an
   ordinary public-client or web app registration) that obtains a token whose
   `aud` is the Blueprint, then sends it to the agent backend, which performs
   OBO.
4. **Microsoft Entra does not implement RFC 7591 Dynamic Client Registration.**
   There is no `registration_endpoint` in Entra v2.0's OIDC discovery document.
   App registrations are created via the admin center or Microsoft Graph
   (`POST /v1.0/applications`).
5. **Entra v2.0's OIDC discovery document does not include
   `code_challenge_methods_supported`.** PKCE works against Entra, but the
   metadata is missing. MCP clients that strictly validate authorization-server
   metadata refuse to proceed without this field; a metadata shim is required.
6. **Entra v2.0 silently ignores the RFC 8707 `resource` parameter on
   `/authorize`.** Entra is scope-centric: audience binding uses
   `scope={resource}/.default`. Sending `resource=https://...` is dropped on
   v2 and returns `AADSTS901002` on the v1 endpoint.
7. **A broad set of high-risk Graph permissions is blocked for agents** —
   `AgentIdentity.Create`, `AgentIdentityBlueprint.Create`,
   `Application.ReadWrite.All`, `Application.ReadWrite.OwnedBy`,
   `Directory.ReadWrite.All`, `RoleManagement.ReadWrite.Directory`,
   `User.ReadWrite.All`, and more. Including any of these in an Agent Identity's
   `requiredResourceAccess` returns HTTP 400. See the
   [blocked-permissions table](https://learn.microsoft.com/en-us/graph/api/resources/agentid-platform-overview?view=graph-rest-beta).

There is exactly one narrow OAuth/redirect-URI exception for Blueprints: a
Blueprint configured as an **interactive agent** (acting on behalf of users via
OBO) gets a `web.redirectUris` entry, but that URI is where Entra sends the user
after consent recording, not where an auth code lands for the Blueprint itself.
The actual OAuth client in interactive flows is a separate client app
registration, and the auth-code request uses that client app's `client_id` — not
the Blueprint's.

## Capabilities and constraints by object

### Agent Identity Blueprint

**Supported flows:** `client_credentials` (autonomous, app-only token
acquisition; the Blueprint impersonates the Agent Identity via FIC),
`urn:ietf:params:oauth:grant-type:jwt-bearer` (OBO), and `refresh_token`. The
Blueprint can be the *audience* of an auth-code flow run by a separate client
app, and its `web.redirectUris` can record consent, but it cannot itself be the
`client_id` of a browser-based PKCE flow.

**Blocked flows:** `authorization_code` with the Blueprint as `client_id` and a
public-client redirect URI; `device_code`; implicit grant; ROPC; OBO via
`/authorize`.

**Permissions to create:** `AgentIdentityBlueprint.Create`,
`AgentIdentityBlueprint.AddRemoveCreds.All`,
`AgentIdentityBlueprint.UpdateAuthProperties.All`, and
`AgentIdentityBlueprintPrincipal.Create`. Directory roles such as `Privileged
Role Administrator` (to grant Graph application permissions), `Application
Administrator` or `Cloud Application Administrator` (for delegated
permissions), and `Agent ID Developer` or `Agent ID Administrator` apply.

### Agent Identity

**Supported flows:** `client_credentials` (the Blueprint mints a token whose
subject is the Agent Identity via FIC), `urn:ietf:params:oauth:grant-type:jwt-bearer`
(client-credential extension and OBO), and `refresh_token`.

**Blocked flows:** all interactive flows (authorization_code, device_code,
implicit, ROPC); public-client capabilities of any kind; independent credential
management (the Blueprint is the credential holder).

**Permissions to create:** `AgentIdentity.Create`.

**Tenant policies:** direct Conditional Access targeting (by object ID, custom
security attribute, or Blueprint), Identity Protection (`agentRiskDetection`,
`riskyAgent`), and ID Governance (access reviews, entitlement management). The
blocked-permissions table applies to `requiredResourceAccess`.

### Agent User

**Supported flows:** the three-hop `user_fic` flow that the Agent Identity runs
to mint tokens **as** the Agent User. The third hop uses `grant_type=user_fic`
(not `urn:ietf:params:oauth:grant-type:jwt-bearer`), `user_id={agent-user-object-id}`
(or `username={agent-user-upn}`), the Blueprint token as `client_assertion`, and
the Agent Identity token as `user_federated_identity_credential`.

**Blocked flows:** all human-interactive flows (no passwords, passkeys, MFA,
device code, or authorization_code with the user as subject). The Agent User
cannot sign in directly, be assigned privileged admin roles, or join
role-assignable groups.

**Permissions to create:** `AgentIdUser.ReadWrite.IdentityParentedBy` (when the
Blueprint creates its own Agent User) or `AgentIdUser.ReadWrite.All` (when a
separate client creates Agent Users across Blueprints).

**Licensing:** a Microsoft 365 license (E5, Teams Enterprise, Microsoft 365
Copilot, etc.) is required for mailbox / Teams / OneDrive provisioning. Resource
provisioning typically completes in 10–15 minutes and can take up to 24 hours.
See [Agent Users](entra-agent-users.md) for directory-quota and lifecycle
detail.

## Provisioning identity

Agent Identity APIs must be called with a **dedicated client-credentials
provisioner**, not an interactive user token.

Azure CLI user tokens contain `Directory.AccessAsUser.All`, and Agent Identity
APIs reject those tokens with a hard HTTP 403. Use the Azure CLI only to
bootstrap the dedicated provisioner app and to identify the signed-in sponsor —
never as the credential for Agent Identity API calls, and never via `az rest`
for these operations.

The provisioner authenticates with a certificate credential. Keep the private
key in the OS credential store and do not add a client-secret fallback.

When reading identity from the Azure CLI, request JSON and parse the `id`
field — CLI warnings can corrupt TSV output:

```python
result = subprocess.run(
    ["az", "ad", "signed-in-user", "show", "-o", "json"],
    check=True, capture_output=True, text=True,
)
user_id = json.loads(result.stdout)["id"]
```

Discover Agent Identity application roles from the Microsoft Graph service
principal rather than copying a fixed list, grant the provisioner only the roles
its operations require, then grant admin consent. Expect service-principal and
permission propagation delays; retry with bounded backoff and a fresh token per
attempt. Entrabot's reference provisioner is `scripts/create_entra_agent_ids.py`.

## Pattern: certificate machine flow plus browser PKCE

When a single service needs both certificate-based machine flows (Blueprint →
Agent Identity → resource token) **and** browser-based PKCE for human sign-in,
use **two separate Entra app registrations**. A Blueprint cannot serve both
roles because it cannot be an OAuth public client.

```text
App registration #1: <Service> Blueprint
  - @odata.type: agentIdentityBlueprint
  - signInAudience: AzureADMyOrg
  - api.oauth2PermissionScopes: [{ value: "access_agent", type: "User" }]
  - identifierUris: ["api://<blueprint-appId>"]
  - keyCredentials: [<cert>] OR FIC against a managed identity
  - web.redirectUris: ["https://service.example/authorize"]  (interactive OBO only)
  - publicClient / spa: NOT SUPPORTED
  Used for: autonomous client_credentials flows, and the resource-server
  side of OBO (validates incoming user tokens whose aud is this Blueprint).

App registration #2: <Service> Client
  - Standard application object (NOT agentIdentityBlueprint)
  - publicClient.redirectUris OR spa.redirectUris:
      ["http://localhost", "http://127.0.0.1"]
  - isFallbackPublicClient: true (if no platform configured)
  - requiredResourceAccess: [api://<blueprint-appId>/access_agent]
  - No credentials (public client)
  Used for: OAuth 2.1 PKCE authorization-code flow; the client_id in the
  /authorize call; holding the human's refresh token.
```

The client app registration is an ordinary `application` object, created via
`POST /v1.0/applications` **without** `@odata.type=agentIdentityBlueprint`, so it
is not subject to the agent platform's public-client restrictions. It requests
the Blueprint's `access_agent` scope; the user consents; the resulting access
token has `aud = <blueprint-appId>`, which the resource server validates.

From [Authenticate users and acquire tokens for interactive agents](https://learn.microsoft.com/en-us/entra/agent-id/identity-platform/interactive-agent-authentication-authorization-flow):

> *"After authorization is configured, the client app (such as a frontend or
> mobile app) initiates an OAuth 2.0 authorization code request to obtain a
> token where the audience is the agent identity blueprint. **In this step,
> `client_id` refers to the client app's own registered application ID, not the
> agent identity or agent identity blueprint ID.**"*

## Working around Entra OAuth metadata gaps

For MCP servers and other spec-compliant OAuth clients running against Entra:

- **Dynamic Client Registration (RFC 7591).** Entra has no
  `registration_endpoint`. Options: a DCR shim (a `/register` endpoint that
  returns a pre-registered public-client `appId`), OAuth Client ID Metadata
  Documents (not yet supported by Entra), or manual pre-registration distributed
  via configuration. DCR is a MAY in the MCP authorization spec, not a MUST.
- **PKCE metadata.** Entra's `/.well-known/openid-configuration` omits
  `code_challenge_methods_supported`. Host an OIDC discovery shim that fetches
  Entra's document and injects `code_challenge_methods_supported: ["S256"]`
  without changing the issuer or endpoint URLs, so token signatures still
  validate against Entra's JWKS.
- **Resource indicators (RFC 8707).** Entra ignores the `resource` parameter and
  binds audience through scope. Request `scope=api://<blueprint-appId>/.default`
  (or `/access_agent`) and validate that the token's `aud` matches your
  Blueprint's `appId`. Clients may still send `resource` per the MCP spec; Entra
  ignores it harmlessly.

## Entrabot implementation

Entrabot implements the autonomous three-hop `user_fic` flow directly with
`httpx` (no MSAL): Blueprint certificate → Agent Identity FIC exchange → Agent
User `user_fic` grant, producing a token with `idtyp=user`. For the wire-level
requests and Entrabot's certificate handling, token lifecycle, and error model,
see:

- [Identity and Token Flow](../architecture/identity-and-token-flow.md)
- [Token Flows reference](../reference/token-flows.md)
- [Agent Users](entra-agent-users.md)

## References

- [What is Microsoft Entra Agent ID?](https://learn.microsoft.com/en-us/entra/agent-id/what-is-microsoft-entra-agent-id)
- [Fundamental concepts in Microsoft Entra Agent ID](https://learn.microsoft.com/en-us/entra/agent-id/key-concepts)
- [Authentication protocols in agents](https://learn.microsoft.com/en-us/entra/agent-id/agent-oauth-protocols)
- [Agent OBO OAuth flow](https://learn.microsoft.com/en-us/entra/agent-id/agent-on-behalf-of-oauth-flow)
- [Agent autonomous app OAuth flow](https://learn.microsoft.com/en-us/entra/agent-id/agent-autonomous-app-oauth-flow)
- [Agent's user account impersonation protocol](https://learn.microsoft.com/en-us/entra/agent-id/agent-user-oauth-flow)
- [Authenticate users and acquire tokens for interactive agents](https://learn.microsoft.com/en-us/entra/agent-id/identity-platform/interactive-agent-authentication-authorization-flow)
- [Agent identities, service principals, and applications](https://learn.microsoft.com/en-us/entra/agent-id/agent-service-principals)
- [Agent identity blueprints](https://learn.microsoft.com/en-us/entra/agent-id/agent-blueprint)
- [Agent's user account](https://learn.microsoft.com/en-us/entra/agent-id/agent-users)
- [Create an agent identity blueprint](https://learn.microsoft.com/en-us/entra/agent-id/create-blueprint)
- [Grant agents access to Microsoft 365 resources](https://learn.microsoft.com/en-us/entra/agent-id/grant-agent-access-microsoft-365)
- [Microsoft Entra Agent ID APIs in Microsoft Graph (blocked permissions)](https://learn.microsoft.com/en-us/graph/api/resources/agentid-platform-overview?view=graph-rest-beta)
- [agentIdentityBlueprint v1.0 resource](https://learn.microsoft.com/en-us/graph/api/resources/agentidentityblueprint?view=graph-rest-1.0)
- [agentIdentity v1.0 resource](https://learn.microsoft.com/en-us/graph/api/resources/agentidentity?view=graph-rest-1.0)
- [agentIdentityBlueprintPrincipal v1.0 resource](https://learn.microsoft.com/en-us/graph/api/resources/agentidentityblueprintprincipal?view=graph-rest-1.0)
- [OpenID Connect on the Microsoft identity platform](https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc)
