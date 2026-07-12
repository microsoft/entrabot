# grant_consent.py

Create or update the delegated consent grant that lets the Agent Identity
acquire tokens as the Agent User. This is the generalised CLI form of the
consent logic embedded in
[`create_entra_agent_ids.py`](../provisioning/create-entra-agent-ids-py.md).

Part of the [auth-and-certs command reference](../index.md#auth-and-certs).

## Purpose

Writes an `oauth2PermissionGrant` on Microsoft Graph so the Agent Identity
service principal can mint **delegated** tokens carrying the requested scopes,
acting on behalf of the Agent User. The grant is created with
`consentType: "Principal"`, meaning consent is recorded for the single Agent
User principal only — it is not tenant-wide admin consent.

These are **delegated scopes** (`oauth2PermissionGrants`), not application
permissions. Application permissions are app-role assignments
(`appRoleAssignments`) and behave differently: they grant the service principal
standalone access with no signed-in user. Agent Identity service principals are
created by extending `Microsoft.Graph.AgentIdentity` and have no associated app
registration, so there is no `requiredResourceAccess` manifest and the
admin-consent URL flow does not apply — the grant is written directly.

See [Token flows → Consent records](../../token-flows.md#consent-records) for the
grant shape and how Hop 3 consumes it.

## Requirements

- `.entrabot-state.json` must contain `AGENT_OBJECT_ID` (the Agent Identity
  service-principal object ID) and `AGENT_USER_ID` (the Agent User object ID).
  Run [`setup.sh`](../setup/setup-sh.md) first if either is missing.
- The Provisioner app must already exist with its certificate available for
  cert-auth — the private key lives in the OS keystore on macOS/Linux, or in
  an ACL-locked file under `%LOCALAPPDATA%\entrabot\` on Windows. This command
  uses the existing-only token helper and never bootstraps or repairs the
  Provisioner app.
- The Provisioner app needs `DelegatedPermissionGrant.ReadWrite.All` (to write
  the grant) plus directory read access to resolve the resource service
  principal.

## Usage

```bash
# Grant specific scopes against Microsoft Graph (the default resource)
python scripts/grant_consent.py --scopes "Chat.Create,Mail.Read"

# Grant against a different resource by app ID (e.g. Azure Storage)
python scripts/grant_consent.py \
  --scopes "user_impersonation" \
  --resource-app-id "e406a681-f3d4-42a8-90b6-c2b029497af1"
```

## Options

- `--scopes` (required) — comma-separated delegated scope names, e.g.
  `"Chat.Create,Mail.Read"`. Surrounding whitespace is trimmed and empty entries
  are dropped.
- `--resource-app-id` (optional) — the app ID of the resource to consent
  against. Defaults to Microsoft Graph
  (`00000003-0000-0000-c000-000000000000`).

## Effects

1. Mints a Provisioner Graph token; the certificate private key is read in
   memory only — from the OS keystore on macOS/Linux, or from the Windows
   file-backed store.
2. Resolves the resource app ID to its service-principal object ID via
   `$filter=appId eq '<app-id>'`.
3. Lists existing grants filtered by `clientId` (Agent Identity) and
   `principalId` (Agent User), then selects the grant whose `resourceId` matches
   the resolved resource.
4. Reconciles scopes idempotently:
   - **All requested scopes already present** → prints `[skip]`, makes no change.
   - **Some scopes missing** → `PATCH` the grant with the union of existing and
     requested scopes. Only missing scopes are added; existing scopes are never
     removed.
   - **No grant exists** → `POST` a new grant with `consentType: "Principal"`,
     the sorted requested scopes, and a `startTime` of now (UTC). The `POST`
     retries up to four times (15s, 30s, 45s backoff) when Graph reports the
     principal is not yet propagated.

## Exit behavior

- `0` — grant created, updated, or already satisfied (`[skip]`).
- `1` — Provisioner token failure, missing `AGENT_OBJECT_ID`/`AGENT_USER_ID`,
  resource service principal could not be resolved, no valid scopes provided, or
  a Graph `PATCH`/`POST` failure (including exhausted propagation retries).
  Diagnostics are printed to stdout.
- `2` — argument-parsing error (for example, `--scopes` omitted entirely).

## Security

The Provisioner token and certificate material never touch disk or logs. The
grant only widens delegated access for the specific Agent User principal;
review requested scopes before granting.

## Common failures

- **Missing state** — `AGENT_OBJECT_ID`/`AGENT_USER_ID` not in
  `.entrabot-state.json`; run [`setup.sh`](../setup/setup-sh.md).
- **Propagation** — running immediately after provisioning may hit
  "Principal was not found"; the built-in retries usually cover it.
- **Insufficient Provisioner permissions** — a `403` on the write means the
  Provisioner lacks `DelegatedPermissionGrant.ReadWrite.All`.

## Related commands

- [`grant_files_consent.py`](grant-files-consent-py.md) — ensures the Agent
  User's grant includes the Files/Sites scope set.
- [`revoke_consent.py`](revoke-consent-py.md) — inverse operation.
- [`provisioner-token.py`](provisioner-token-py.md) — mint a Graph token for
  manual inspection.
- [`show_permissions.py`](../operations/show-permissions-py.md) — list the
  current delegated grants.
- [`create_entra_agent_ids.py`](../provisioning/create-entra-agent-ids-py.md) —
  the provisioning script this generalises.
