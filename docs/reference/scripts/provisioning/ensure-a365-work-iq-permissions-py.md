# `ensure_a365_work_iq_permissions.py`

Gives the agent's Blueprint what Word, Excel and OneDrive files (Microsoft Agent
365 Work IQ) need, using the Provisioner token: the resource service principals,
the Blueprint-wide OAuth grants, and the Blueprint's inheritable permissions. That
is what the Agent 365 CLI's `a365 setup permissions mcp` does.

## Purpose

The Windows setup wizard runs this script instead of the Agent 365 CLI: with
it, setup switches Word, Excel and OneDrive files on with no CLI, no .NET and no
PowerShell 7, over SSH and unattended too. `setup.sh` runs it before the CLI's
own permission step, because the CLI can fail to create the first-party resource
service principals and still exit successfully after printing
`OAuth2 grants failed`.

The grants alone are not enough. Without inheritable permissions on the
Blueprint, the agent's token request for a Work IQ audience fails with
`AADSTS65001` ("has not consented"), even with every grant in place. Microsoft
Learn describes them in
[Configure inheritable permissions for agent identity blueprints](https://learn.microsoft.com/entra/agent-id/configure-inheritable-permissions-blueprints).

The resources and scopes come from the package's catalog
(`src/entrabot/a365/catalog.py`), which is also where setup's
`ToolingManifest.json` entries come from.

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
  the `McpServers.OneDriveSharepoint.All`, `McpServers.Excel.All`, and
  `McpServersMetadata.Read.All` scopes) and the Work IQ Word MCP app (for the
  `Tools.ListInvoke.All` scope).
- **OAuth grants** — an `oauth2PermissionGrant` with
  `clientId` = Blueprint service principal,
  `consentType` = `AllPrincipals`, and `resourceId` = the resource service
  principal. Entra keeps one grant per client and resource, so all of a
  resource's scopes are written in one request: a new grant is created with
  every scope, and an existing grant that lacks any of them is `PATCH`ed once to
  merge them in. A `PATCH` answered with `404` is tried again after 5, 10, 15
  and 20 seconds, because a grant created moments ago can be listed before it
  can be updated. A create that conflicts with an already-existing entry is
  read back and merged rather than treated as an error.

- **Inheritable permissions** — for each resource app, an entry in the
  Blueprint's
  `/v1.0/applications/microsoft.graph.agentIdentityBlueprint/{blueprint}/inheritablePermissions`
  collection that lets its agents inherit the scopes above. A new entry is least
  privilege: only those scopes (`enumeratedScopes`) and no app roles (`noRoles`).
  An entry that already covers them, such as the one Microsoft's tool writes
  (every scope and every role), is left alone. An entry with fewer scopes is
  widened with `PATCH .../inheritablePermissions/{resourceAppId}`, which keeps
  its scopes and does not touch its roles; nothing is ever narrowed. A create
  answered `409` (it exists) is read back and widened.

  Entries are written one at a time, and each is read back before the next is
  written: on a real tenant a read straight after a `201` came back empty, and
  of two creates sent back to back, both answered `201`, only one was kept. The
  read-back waits 2, 3, 5, 10, 15 and 20 seconds between tries. At the end
  every entry is checked again, and one that was lost is written again. Even
  once an entry can be read, Microsoft's token service took 20 to 45 seconds on
  a real tenant to honour it.

Each stage prints whether it created, updated, or skipped the object, so a
re-run reports the current materialized state without implying a stale history.

## Exit behavior

- **Exit 0** — all resource service principals, grants and inheritable
  permissions are present.
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
- **`patch ... OAuth grant failed (404)`** — Microsoft Graph could not find a
  grant it had just listed, even after the retries. Run the script again; it
  picks up from the grants that exist.
- **`The inheritable permissions for ... could not be read back`** — Microsoft
  Graph accepted a write but did not show it within about a minute. Run the
  script again.
- **The agent's Word, Excel or OneDrive token fails with `AADSTS65001`** — the
  inheritable permissions are missing or not honoured yet. Run the script (or
  `scripts\setup-windows.cmd` and answer yes to Word, Excel and OneDrive), wait
  a minute, and check with `scripts\setup-windows.cmd -Diagnose`.

## Related commands

- [Provisioning command index](../index.md#provisioning)
- [`create_entra_agent_ids.py`](create-entra-agent-ids-py.md)
- [`assign_agent_user_licenses.py`](assign-agent-user-licenses-py.md)
  (Copilot / Work IQ licensing)
- Platform docs:
  [Microsoft Agent 365 and Work IQ](../../../platform-docs/microsoft-agent-365.md),
  [Agent Identity Blueprints and Users](../../../platform-docs/agent-id-blueprints-and-users.md)
- Architecture: [Identity and token flow](../../../architecture/identity-and-token-flow.md)
