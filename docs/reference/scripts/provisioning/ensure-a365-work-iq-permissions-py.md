# `ensure_a365_work_iq_permissions.py`

Materializes the Microsoft Agent 365 Work IQ MCP resource service principals and
Blueprint-wide OAuth grants, using the Provisioner token, before the Agent 365
`a365` CLI runs its own permission step.

## Purpose

The Agent 365 `a365` CLI can fail to create the first-party resource service
principals that Work IQ depends on, and then still exit successfully after
printing `OAuth2 grants failed`. That leaves the tenant looking configured while
Work IQ tool calls fail. `scripts/ensure_a365_work_iq_permissions.py` closes
that gap: it uses the certificate-backed Provisioner token to create the
required resource service principals and the Blueprint-wide OAuth grants first,
so the permissions exist regardless of whether the CLI's own step succeeds.

## Requirements

- **Platform**: macOS, Linux, or Windows.
- **Provisioner app**: a bootstrapped Provisioner registration whose Graph token
  is minted from its certificate.
- **Blueprint**: the Blueprint service principal must already exist (run
  [`create_entra_agent_ids.py`](create-entra-agent-ids-py.md) first). The script
  refuses to proceed if the Blueprint service principal cannot be found.
- **State or override**: `BLUEPRINT_APP_ID` in `.entrabot-state.json`, or an
  explicit `--blueprint-app-id`.
- **Python environment**: the repository virtual environment with `scripts/`
  and `src/entrabot/` importable.

## Usage

```bash
python scripts/ensure_a365_work_iq_permissions.py
python scripts/ensure_a365_work_iq_permissions.py --blueprint-app-id <BLUEPRINT_APP_ID>
```

### Options

| Option | Description |
| --- | --- |
| `--blueprint-app-id <id>` | The Agent Identity Blueprint application ID. Defaults to `BLUEPRINT_APP_ID` from `.entrabot-state.json`. |

## Effects

Resolves the Blueprint service principal object ID, then for each required Work
IQ resource ensures both a resource service principal and a Blueprint-wide OAuth
grant exist. All calls use the v1.0 Graph endpoint.

- **Resource service principals** — for each required resource,
  `GET /v1.0/servicePrincipals?$filter=appId eq '...'` and, when absent,
  `POST /v1.0/servicePrincipals` with the resource `appId` (retried while the
  object propagates). The required resources are the Agent 365 Tools app (for
  the `McpServers.OneDriveSharepoint.All` and `McpServersMetadata.Read.All`
  scopes) and the Work IQ Word MCP app (for the `Tools.ListInvoke.All` scope).
- **OAuth grants** — an `oauth2PermissionGrant` with
  `clientId` = Blueprint service principal,
  `consentType` = `AllPrincipals`, and `resourceId` = the resource service
  principal. An existing grant that lacks the required scope is `PATCH`ed to
  merge it in; a create that conflicts with an already-existing entry is read
  back and merged rather than treated as an error.

Each stage prints whether it created, updated, or skipped the object, so a
re-run reports the current materialized state without implying a stale history.

## Exit behavior

- **Exit 0** — all resource service principals and grants are present.
- **Exit 1** — the Provisioner token could not be obtained
  (`ProvisionerBootstrapError`), or a permission could not be made ready
  (`A365PermissionError`), including when the Blueprint service principal is not
  found or a created object's ID cannot be read back.
- **Exit 2** — no Blueprint App ID was available from state or `--blueprint-app-id`.

## Common failures

- **Blueprint service principal not found** — run Entrabot provisioning before
  configuring Work IQ so the Blueprint and its service principal exist.
- **Create conflict on a grant** — the grant already exists; the script reads it
  back and merges the scope, so this is handled rather than fatal.

## Related commands

- [Provisioning command index](../index.md#provisioning)
- [`create_entra_agent_ids.py`](create-entra-agent-ids-py.md)
- [`assign_agent_user_licenses.py`](assign-agent-user-licenses-py.md)
  (Copilot / Work IQ licensing)
- Platform docs:
  [Microsoft Agent 365 and Work IQ](../../../platform-docs/microsoft-agent-365.md),
  [Agent Identity Blueprints and Users](../../../platform-docs/agent-id-blueprints-and-users.md)
- Architecture: [Identity and token flow](../../../architecture/identity-and-token-flow.md)
