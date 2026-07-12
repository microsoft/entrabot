---
name: implement-agent-id
description: Guide for Microsoft Entra Agent ID and Agent User integration. Covers certificate authentication, stable Graph creation endpoints, sponsors, BlueprintPrincipal creation, permissions, consent, and the three-hop user_fic token flow.
---

# Implementing Microsoft Entra Agent ID

Read these repository sources before changing identity or token code:

- `docs/platform-docs/agent-id-blueprints-and-users.md`
- `docs/platform-docs/entra-agent-users.md`
- `docs/platform-docs/delegated-auth.md`
- `docs/reference/token-flows.md`
- `docs/runbooks/hard-won-learnings.md`

Microsoft Entra Agent ID and Microsoft Agent 365 reached GA on 2026-05-01, but not every related API is on Microsoft Graph v1.0. Use the endpoint version documented for each object rather than treating the whole surface as beta or stable.

## Object model

```text
Agent Identity Blueprint (application)
  └─ AgentIdentityBlueprintPrincipal (service principal; create explicitly)
      ├─ Agent Identity (service principal)
      └─ Agent Identity (service principal)
           └─ Agent User (user; linked through user_fic)
```

An Agent Identity is a service principal, not a user. Do not create a password-backed fake user to represent an agent.

## Non-negotiable constraints

### Use a dedicated provisioner identity

Azure CLI user tokens contain `Directory.AccessAsUser.All`; Agent Identity APIs reject those tokens with a hard 403. Use Azure CLI only to bootstrap the dedicated provisioner app and identify the signed-in sponsor.

The provisioner authenticates with a certificate credential. Keep the private key in the OS credential store and purge legacy password credentials. Entrabot implements this in:

- `scripts/entra_provisioning.py`
- `scripts/create_entra_agent_ids.py`

Do not add a client secret fallback.

### Parse Azure CLI output as JSON

CLI warnings can corrupt TSV output. Request JSON and parse the `id` field:

```python
result = subprocess.run(
    ["az", "ad", "signed-in-user", "show", "-o", "json"],
    check=True,
    capture_output=True,
    text=True,
)
user_id = json.loads(result.stdout)["id"]
```

### Sponsors are user references

Blueprint and Agent Identity creation require at least one sponsor. Bind a Microsoft Graph v1.0 user reference:

```python
sponsors = [f"https://graph.microsoft.com/v1.0/users/{user_id}"]
```

Do not bind a service principal, group, or `/directoryObjects/` reference.

### Create BlueprintPrincipal explicitly

Creating a Blueprint does not create its BlueprintPrincipal. Always create or verify the principal immediately after creating or discovering the Blueprint, including idempotent resume paths.

## Current creation endpoints

### Blueprint

```http
POST https://graph.microsoft.com/v1.0/applications/microsoft.graph.agentIdentityBlueprint
Content-Type: application/json

{
  "displayName": "My Agent Blueprint",
  "description": "Optional description",
  "sponsors@odata.bind": [
    "https://graph.microsoft.com/v1.0/users/{sponsor-object-id}"
  ]
}
```

Persist both returned identifiers:

- `appId`: Blueprint client/application ID used by token requests.
- `id`: Blueprint directory-object ID used by object-specific Graph paths.

### BlueprintPrincipal

```http
POST https://graph.microsoft.com/v1.0/servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal
Content-Type: application/json

{
  "appId": "{blueprint-app-id}"
}
```

### Agent Identity

```http
POST https://graph.microsoft.com/v1.0/servicePrincipals/microsoft.graph.agentIdentity
Content-Type: application/json

{
  "displayName": "my-agent-instance",
  "agentIdentityBlueprintId": "{blueprint-app-id}",
  "sponsors@odata.bind": [
    "https://graph.microsoft.com/v1.0/users/{sponsor-object-id}"
  ]
}
```

The Agent Identity has no backing application object. Do not call application password APIs for it.

### Agent User

Agent User creation remains on Microsoft Graph beta:

```http
POST https://graph.microsoft.com/beta/users
```

Use the current request shape in `scripts/create_entra_agent_ids.py` and `docs/platform-docs/entra-agent-users.md`. License the resulting user before relying on Teams or Outlook.

## Permissions and consent

Discover Agent Identity application roles from the Microsoft Graph service principal rather than copying an old fixed list:

```bash
az ad sp show \
  --id 00000003-0000-0000-c000-000000000000 \
  --query "appRoles[?contains(value, 'AgentIdentity')].{id:id,value:value}" \
  -o json
```

Grant the dedicated provisioner only the roles required by the provisioning operations, then grant admin consent. Expect service-principal and permission propagation delays; retry with bounded backoff and acquire a fresh token for each attempt.

Entrabot writes Agent User delegated consent through:

```http
POST https://graph.microsoft.com/v1.0/oauth2PermissionGrants
```

Some tenants require `startTime` even when a newer Microsoft example omits it. Preserve the compatibility behavior in the repository helper.

## Autonomous three-hop token flow

All requests use:

```text
https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token
```

### Hop 1: Blueprint certificate to T1

```text
client_id={blueprint_app_id}
scope=api://AzureADTokenExchange/.default
fmi_path={agent_identity_app_id}
grant_type=client_credentials
client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
client_assertion={certificate_signed_jwt}
```

### Hop 2: Agent Identity FIC exchange to T2

```text
client_id={agent_identity_app_id}
scope=api://AzureADTokenExchange/.default
grant_type=client_credentials
client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
client_assertion={T1}
```

### Hop 3: Agent User resource token

```text
client_id={agent_identity_app_id}
scope=https://graph.microsoft.com/.default
grant_type=user_fic
client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
client_assertion={T1}
user_id={agent_user_object_id}
user_federated_identity_credential={T2}
requested_token_use=on_behalf_of
```

`user_id` is Entrabot's canonical selector; Microsoft also documents `username={agent_user_upn}` as an alternative. For Azure Blob Storage, keep Hops 1 and 2 and request `https://storage.azure.com/.default` at Hop 3.

Check every token response for an `error` key before reading `access_token`. Never log tokens, assertions, certificate private keys, or full token responses.

## Delegated mode is separate

Delegated auth uses MSAL browser authentication with device-code fallback and represents the signed-in human. It does not provide Agent User attribution. `_init_auth` uses it as the fallback path: when three-hop is skipped (`ENTRABOT_SKIP_PROVISIONING=true` or no Blueprint app ID + tenant ID) or fails, it authenticates with MSAL if `ENTRABOT_CLIENT_ID` is set. `ENTRABOT_MODE` is validated but does not currently select this path. Agent Blueprints are confidential clients and cannot be turned into OAuth public clients for PKCE or device-code flows; use a separate app registration when both patterns are required.

## Implementation checklist

1. Create or recover the certificate-backed provisioner app.
2. Discover and grant required Graph application permissions.
3. Grant admin consent and wait for propagation with bounded retries.
4. Resolve the sponsor from JSON Azure CLI output.
5. Create or discover the Blueprint with the v1.0 subtype endpoint.
6. Create or verify BlueprintPrincipal with the v1.0 subtype endpoint.
7. Create or discover the Agent Identity with the v1.0 subtype endpoint.
8. Create or discover the Agent User on Graph beta.
9. Assign required Microsoft 365 licenses.
10. Create delegated consent records for Graph and optional Storage.
11. Store private key material only in the platform credential store.
12. Verify a three-hop resource token has `idtyp=user` and the Agent User object ID.

Use `scripts/create_entra_agent_ids.py` as the repository reference implementation and add focused endpoint-contract tests before changing provisioning behavior.
