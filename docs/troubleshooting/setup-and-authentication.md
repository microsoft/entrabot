# Setup and authentication

## Setup stops before the final summary

Setup is designed to be re-runnable. Fix the first reported failure, then run
the same setup command again. Do not delete Entra resources simply to retry an
idempotent step.

For an existing Blueprint on macOS or Linux:

```bash
./scripts/setup.sh --use-blueprint=<blueprint-app-id>
```

For Windows, use the native Windows launcher:

```powershell
scripts\setup-windows.cmd -UseBlueprint <blueprint-app-id>
```

If setup reports that provisioning succeeded but the smoke test failed, allow
time for a newly assigned Teams or Exchange license to propagate, then run:

```bash
./scripts/setup.sh --diagnose
```

See [Identity Lifecycle](../guides/identity-lifecycle.md) and the
[setup script references](../reference/scripts/index.md#setup).

## The runtime selected an unexpected authentication type

`ENTRABOT_MODE` is validated, but `_init_auth` does not currently use it as the
selector. Startup uses credential presence and this sequence:

1. When `ENTRABOT_SKIP_PROVISIONING` is false and both
   `ENTRABOT_BLUEPRINT_APP_ID` and `ENTRABOT_TENANT_ID` exist, Entrabot tries
   the Agent User three-hop flow.
2. If that attempt is skipped or fails, Entrabot tries delegated MSAL when
   `ENTRABOT_CLIENT_ID` exists.
3. If neither path succeeds, the runtime remains `UNAUTHENTICATED`.

To deliberately use delegated authentication, set
`ENTRABOT_SKIP_PROVISIONING=true` or omit the Blueprint configuration while
keeping `ENTRABOT_CLIENT_ID`.

Call `whoami` to inspect the runtime result. Do not infer runtime mode only from
`ENTRABOT_MODE` or from a successful status command.

## Status is healthy but `whoami` is unauthenticated

The status commands mint a Graph token with the **Provisioner certificate**.
The MCP runtime normally authenticates with the **Blueprint certificate**, then
exchanges through the Agent Identity and Agent User. These are separate
credentials and token paths.

Check:

1. The MCP executable is using the expected repository and `.venv`.
2. `.env` contains the Blueprint, Agent Identity, and Agent User identifiers.
3. The Blueprint private key is present in the platform keystore.
4. On Windows, both certificate thumbprint variables are present.
5. The MCP host was restarted after configuration changed.

Use `./status.sh` or `.\status-windows.ps1` for resource-chain health, then
`whoami` for the active runtime session.

## Three-hop authentication fails

The error names the failed exchange:

| Hop | Identity and request | Common checks |
|---|---|---|
| `hop1:blueprint` | Blueprint certificate assertion requests the token-exchange scope and binds it to the Agent Identity with `fmi_path` | Blueprint app ID, tenant ID, registered public certificate, local private key, `x5t#S256`, certificate dates |
| `hop2:agent_identity` | Agent Identity exchanges the Blueprint token through its federated credential | Agent Identity client ID, parent Blueprint relationship, propagation |
| `hop3:agent_user` | Agent Identity uses `grant_type=user_fic` to mint the Agent User token | Agent User object ID, per-principal delegated consent, requested resource scopes |

The resource chain must include an **explicitly created
BlueprintPrincipal**. Creating a Blueprint application does not automatically
create its service principal. Re-run setup if the Blueprint exists but the
BlueprintPrincipal or child Agent Identity is missing.

Never substitute a human Azure CLI token for the certificate-based Agent
Identity token exchanges.

## Hop 1 reports an invalid client or certificate

Verify the local and registered certificate metadata:

```bash
.venv/bin/python scripts/verify_blueprint_cert.py
```

On macOS or Linux, if local state lost the registered thumbprint:

```bash
.venv/bin/python scripts/find_local_blueprint_cert.py
```

If the private key is missing, re-run setup for the existing Blueprint. Do not
copy private-key material into `.env` or logs.

On Windows, use the checks in [Windows troubleshooting](windows.md).

## Hop 3 reports consent or scope errors

The Agent Identity needs a per-principal `oAuth2PermissionGrant` for the Agent
User and the target resource. Re-run setup to merge the current delegated
scopes and consent. Also confirm the Agent User has the license required by the
Microsoft 365 workload.

Graph and Azure Storage are different resources. A Graph-scoped Agent User
token does not satisfy Blob Storage, and storage consent does not grant Teams
or mail access. See [Storage troubleshooting](storage.md).

## Delegated sign-in does not complete

Delegated authentication requires a separate ordinary public-client app
registration. An Agent Identity Blueprint cannot be configured as an OAuth
public client.

The delegated flow attempts:

1. Silent acquisition from the encrypted MSAL cache.
2. Interactive localhost redirect on port **8400**, with a 120-second window.
3. Device-code fallback when the browser cannot open, the port is unavailable,
   or the redirect does not complete.

If port 8400 is occupied, close the conflicting listener or complete the
device-code flow printed to stderr. Conditional Access applies to the signed-in
human and may require tenant-admin review.

See [Delegated Authentication](../platform-docs/delegated-auth.md) and
[Microsoft Entra Agent ID](../platform-docs/agent-id-blueprints-and-users.md).

## Authentication expires during a tool call

Entrabot refreshes near-expiry tokens and retries a tool once after a 401:

- Agent User sessions re-run the three-hop flow.
- Delegated sessions attempt silent MSAL refresh.

If delegated silent refresh requires interaction, restart through a user-facing
startup path so localhost or device-code sign-in can run. Repeated 401s after
one retry usually indicate revoked consent, certificate drift, the wrong
resource scope, or a different runtime configuration.
