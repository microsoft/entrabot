# `create_entra_agent_ids.py`

Creates the Blueprint, BlueprintPrincipal, Agent Identity, and Agent User in
Microsoft Entra ID and persists the resulting IDs to `.entrabot-state.json`.

## Purpose

`scripts/create_entra_agent_ids.py` provisions the full Agent Identity chain in
a single idempotent run:

1. An **Agent Identity Blueprint** application object.
2. The Blueprint's **service principal** (a `agentIdentityBlueprintPrincipal`),
   created explicitly because Entra does **not** auto-create it.
3. A per-device **Agent Identity** service principal parented to the Blueprint.
4. An **Agent User** parented to the Agent Identity, plus the delegated consent
   grants and licenses the Agent User needs to reach Teams, Files, mail, and
   cloud-hosted operational storage.

Every Graph call is made with a certificate-backed **Provisioner** token minted
by `entra_provisioning.py`. The script never uses `az` CLI access tokens for
the Agent Identity APIs: those tokens carry `Directory.AccessAsUser.All`, which
the Agent Identity endpoints reject outright. The signed-in `az` user is used
only to identify the human sponsor and to bootstrap the Provisioner app.

## Requirements

- **Platform**: macOS, Linux, or Windows.
- **Azure CLI sign-in**: `az login` must have been run. The signed-in user is
  bound as the human sponsor on the Blueprint and Agent Identity and is used to
  bootstrap the Provisioner app.
- **Provisioner app**: a bootstrapped Provisioner registration whose
  certificate private key lives in the OS keystore, with tenant admin consent
  granted for its Graph application permissions. `get_existing_graph_token()`
  fails fast if the Provisioner has not been bootstrapped.
- **Python environment**: the repository's virtual environment with
  dependencies installed (`pip install -e ".[dev]"`), so that both `scripts/`
  and `src/entrabot/` are importable.

## Usage

```bash
python3 scripts/create_entra_agent_ids.py
```

The script takes no command-line flags; behavior is controlled by environment
variables:

| Variable | Default | Effect |
| --- | --- | --- |
| `ENTRABOT_NEW_CHAIN` | unset | When `1`, skips every reuse lookup and forces a fresh chain. `setup.sh --new` sets this. |
| `_ENTRABOT_UPN_SUFFIX` | empty | Appended to the Agent User `mailNickname`/display name so a forced-new chain does not collide on the unique-UPN constraint. |
| `ENTRABOT_ASSIGN_TEAMS_LICENSE` | `1` | Assign a Teams-capable SKU to the Agent User. |
| `ENTRABOT_ASSIGN_WORK_IQ_LICENSE` | unset | When `1`, also assign a Microsoft 365 Copilot (Work IQ) SKU. |

## Effects

The run mutates the following Graph objects and endpoints, checking the state
file and Graph for an existing object before creating anything:

- **Blueprint** — `POST /v1.0/applications/microsoft.graph.agentIdentityBlueprint`.
  Persists `BLUEPRINT_APP_ID` and `BLUEPRINT_OBJECT_ID`. The human sponsor is
  bound via `sponsors@odata.bind` when a signed-in user is available.
- **BlueprintPrincipal** —
  `POST /v1.0/servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal`.
  Created explicitly and retried while the new app propagates.
- **Agent Identity** —
  `POST /v1.0/servicePrincipals/microsoft.graph.agentIdentity`, parented to the
  Blueprint app ID, with the signed-in user bound as sponsor. Persists
  `AGENT_ID` and `AGENT_OBJECT_ID`.
- **Agent User** — `POST /beta/users` with
  `@odata.type: microsoft.graph.agentUser` and `identityParentId` set to the
  Agent Identity object ID. Persists `AGENT_USER_ID` and `AGENT_USER_UPN`, then
  waits 30 seconds for directory propagation.
- **Agent Identity app permission** — an app-role assignment granting the Agent
  Identity `Application.Read.All` so it can read its own sponsor collection.
- **Graph delegated consent** — an `oauth2PermissionGrant` (v1.0) with
  `clientId` = Agent Identity, `principalId` = Agent User,
  `resourceId` = Microsoft Graph service principal, and the scopes
  `Chat.Create Chat.ReadWrite ChatMessage.Send User.Read User.ReadBasic.All
  Files.ReadWrite Files.Read.All Sites.Read.All Sites.ReadWrite.All Mail.Read
  Mail.Send`. An existing grant that is missing scopes is `PATCH`ed rather than
  replaced.
- **Storage delegated consent** — an `oauth2PermissionGrant` for
  `user_impersonation` on the Azure Storage service principal, enabling the
  parallel storage hop for cloud-hosted operational memory. Non-fatal when the
  Storage service principal is absent from the tenant.
- **Licenses** — reads `/v1.0/subscribedSkus` and assigns via
  `POST /v1.0/users/{id}/assignLicense`. Teams-capable SKUs are assigned by
  default; Copilot (Work IQ) SKUs are opt-in.

All identifiers are written to `.entrabot-state.json` as each stage completes,
so a later stage can resume from a partially provisioned chain.

## Exit behavior

- **Exit 0** — the chain exists and all consent and license stages completed (a
  missing Storage service principal or missing SKUs is reported but non-fatal).
- **Exit 1** — the Provisioner token could not be obtained
  (`ProvisionerBootstrapError`), the delegated Graph consent grant failed (a
  blocking error, because the third hop cannot mint an Agent User token without
  it), or a create call failed after its retries. Token responses are checked
  for an `"error"` key and for a leaked `Directory.AccessAsUser.All` scope
  before proceeding, and a `403` on Agent User creation is reported as a missing
  `AgentIdUser.ReadWrite.IdentityParentedBy` grant.

## Idempotency and resume

Each stage first consults `.entrabot-state.json`, then queries Graph by stored
ID and finally by display name. Before creating a new chain, the script also
searches the tenant for an existing Agent User by intended UPN and reuses it
(and its parent Agent Identity) rather than colliding on the unique-UPN
constraint — which lets a second device or a lost state file re-attach to an
existing chain. Set `ENTRABOT_NEW_CHAIN=1` to bypass every reuse path.

## Common failures

- **`Directory.AccessAsUser.All` in the response** — an `az` CLI token reached
  an Agent Identity API. Confirm the Provisioner certificate token is in use and
  admin consent is granted for the Provisioner app.
- **`403` creating the Agent User** — the Provisioner is missing
  `AgentIdUser.ReadWrite.IdentityParentedBy`.
- **`Principal was not found` on consent** — the Agent User is still
  propagating; the script retries, but a persistent failure blocks the third
  hop.
- **No subscribed SKUs / no available seats** — license assignment is reported
  and skipped; assign a license later with
  [`assign_agent_user_licenses.py`](assign-agent-user-licenses-py.md).

## Related commands

- [Provisioning command index](../index.md#provisioning)
- [`add_agent_sponsor.py`](add-agent-sponsor-py.md)
- [`assign_agent_user_licenses.py`](assign-agent-user-licenses-py.md)
- [`ensure_a365_work_iq_permissions.py`](ensure-a365-work-iq-permissions-py.md)
- Platform docs:
  [Agent Identity Blueprints and Users](../../../platform-docs/agent-id-blueprints-and-users.md),
  [Agent Users](../../../platform-docs/entra-agent-users.md)
- Architecture: [Identity and token flow](../../../architecture/identity-and-token-flow.md)
