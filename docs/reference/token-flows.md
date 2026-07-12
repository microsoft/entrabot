# Token flows

Entrabot has two authenticated session types. `agent_user` uses the autonomous three-hop Agent User flow described below. `delegated` uses MSAL interactive authentication (localhost redirect with device-code fallback) and is intended for demos or environments without a provisioned Agent User. `_init_auth` selects between them by credential presence and `ENTRABOT_SKIP_PROVISIONING`, not by `ENTRABOT_MODE`.

## Autonomous Agent User flow

All three requests use the tenant v2 token endpoint:

```text
https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token
```

The implementation is `acquire_agent_user_token(config, resource_scope=...)` in `src/entrabot/tools/teams.py`. Every token response is checked for an `error` key before `access_token` is read.

### Hop 1: Blueprint certificate → Agent Identity exchange token (T1)

The Blueprint authenticates with a certificate-signed client assertion and binds the exchange token to the target Agent Identity through `fmi_path`.

```http
POST /{tenant_id}/oauth2/v2.0/token
Content-Type: application/x-www-form-urlencoded

client_id={blueprint_app_id}
&scope=api://AzureADTokenExchange/.default
&fmi_path={agent_identity_app_id}
&grant_type=client_credentials
&client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
&client_assertion={certificate_signed_jwt}
```

The private key is loaded from the platform credential store; it is not read from a PEM path in `.env`.

### Hop 2: Agent Identity FIC exchange → Agent Identity token (T2)

The Agent Identity presents T1 as its client assertion:

```http
POST /{tenant_id}/oauth2/v2.0/token
Content-Type: application/x-www-form-urlencoded

client_id={agent_identity_app_id}
&scope=api://AzureADTokenExchange/.default
&grant_type=client_credentials
&client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
&client_assertion={T1}
```

### Hop 3: Agent User `user_fic` grant → resource token

Entrabot requests a delegated token for the selected resource:

```http
POST /{tenant_id}/oauth2/v2.0/token
Content-Type: application/x-www-form-urlencoded

client_id={agent_identity_app_id}
&scope=https://graph.microsoft.com/.default
&grant_type=user_fic
&client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
&client_assertion={T1}
&user_id={agent_user_object_id}
&user_federated_identity_credential={T2}
&requested_token_use=on_behalf_of
```

Microsoft's current examples use `user_id` as the canonical object-ID selector and also document `username={agent_user_upn}` as an alternative. Entrabot uses `user_id`. The implementation includes `requested_token_use=on_behalf_of` for compatibility even though newer canonical examples may omit it.

The resulting token has:

- `idtyp=user`
- `oid={agent_user_object_id}`
- `aud=https://graph.microsoft.com` for the default Graph scope
- delegated scopes consented to the Agent Identity/Agent User relationship

For Azure Blob Storage, Hops 1 and 2 are unchanged and Hop 3 uses `scope=https://storage.azure.com/.default` through `acquire_agent_user_storage_token()`.

## Consent records

Entrabot grants delegated scopes with `POST /v1.0/oauth2PermissionGrants`:

```json
{
  "clientId": "{agent-identity-service-principal-object-id}",
  "consentType": "Principal",
  "principalId": "{agent-user-object-id}",
  "resourceId": "{resource-service-principal-object-id}",
  "scope": "Chat.ReadWrite Mail.ReadWrite Files.ReadWrite.All User.Read",
  "startTime": "2026-07-10T00:00:00Z"
}
```

Microsoft's current request example may omit `startTime`, but Entrabot has observed tenants that reject the grant without it. The provisioning helper therefore includes it as a compatibility requirement.

## Token refresh

The MCP server uses two layers:

1. `_ensure_valid_token()` refreshes eagerly before expiry.
2. `_with_token_retry()` retries once after a Graph 401.

Tokens and assertions must never be logged. Failures surface as typed errors that identify the failed hop without exposing token material.

## Delegated mode

The delegated path uses the regular Entrabot app registration (`ENTRABOT_CLIENT_ID`) and MSAL. `_init_auth` takes it when the three-hop fast path is skipped or fails; `ENTRABOT_MODE` is validated but does not currently select it. The primary flow is localhost browser authentication; device code is a fallback for headless environments. Delegated tokens represent the signed-in human and therefore do not provide Agent User attribution. This path is separate from the autonomous three-hop flow and does not turn a Blueprint into an OAuth public client. See [Delegated Auth](../platform-docs/delegated-auth.md) for detail.

## Related

- [Agent Identity platform constraints](../platform-docs/agent-id-blueprints-and-users.md)
- [Agent Users](../platform-docs/entra-agent-users.md)
- [Identity and Token Flow](../architecture/identity-and-token-flow.md)
- [Auth API reference](api/auth.md)
