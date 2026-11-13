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

# Licence only (the Windows setup wizard's licence walkthrough uses these)
python3 scripts/create_entra_agent_ids.py --licence-report
python3 scripts/create_entra_agent_ids.py --assign-licence <sku-id>
```

With no option it is the full run, controlled by environment variables. The two
options, which cannot be combined, handle the Agent User's licence on its own
and change nothing else; see [Licence only](#licence-only).

| Variable | Default | Effect |
| --- | --- | --- |
| `ENTRABOT_NEW_CHAIN` | unset | When `1`, skips every reuse lookup and forces a fresh chain. `setup.sh --new` sets this. |
| `_ENTRABOT_UPN_SUFFIX` | empty | The Agent User's sign-in name becomes `entrabot-agent-<suffix>@<domain>` (and its display name `EntraBot Agent (<suffix>)`), so a forced-new chain does not collide on the unique-UPN constraint. Outranks `ENTRABOT_AGENT_USER_UPN`. |
| `ENTRABOT_AGENT_USER_UPN` | unset | The Agent User's sign-in name, used when no suffix is set. Like every setting, it is read from `.env` when the environment does not set it, so an earlier setup's value counts. `setup.sh --use-blueprint` sets it empty for this script when `.env` was written for another Blueprint. With neither, the name is `entrabot-agent@<domain>`. |
| `ENTRABOT_ASSIGN_TEAMS_LICENSE` | `1` | Assign a licence that includes Teams to the Agent User. |
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
  `POST /v1.0/users/{id}/assignLicense`, after setting the Agent User's usage
  location. A licence that includes Teams is assigned by default, preferring
  one that also brings mail and files; Microsoft 365 Copilot (Work IQ) is
  opt-in. What a product includes is read from its service plans: Teams is the
  Microsoft Teams plan itself (`TEAMS1`, `TeamsEss`, or a government Teams
  plan), so a "(no Teams)" suite, whose plans are only named after Teams, does
  not count, and neither do meeting-room and shared-device licences. Copilot is
  `M365_COPILOT_APPS` or `M365_COPILOT_BUSINESS_CHAT`, except Copilot for one
  line of work. When Graph lists no service plans, a list of known part
  numbers is used instead.

All identifiers are written to `.entrabot-state.json` as each stage completes,
so a later stage can resume from a partially provisioned chain.

### Licence only

Both options need an Agent User recorded in `.entrabot-state.json` and the
Provisioner's token, as the full run does; they never use an Azure CLI token.

- **`--licence-report`** — changes nothing. Reads `/v1.0/subscribedSkus` and
  the Agent User's assigned licences, and prints one line,
  `ENTRABOT_RESULT=` followed by JSON: every product with its name, what it
  includes (Teams, mailbox, files, Copilot), how many licences it has and how
  many are assigned, and which of them the Agent User has. A product is named
  the way the Microsoft 365 admin center names it when Microsoft's licensing
  reference lists it; otherwise by a readable form of its part number.
- **`--assign-licence <sku-id>`** — gives the Agent User that one product,
  with the full run's own steps: usage location first, then `assignLicense`,
  then the state file's `AGENT_USER_LICENSE_SKU` (or
  `AGENT_USER_WORK_IQ_LICENSE_SKU` for Copilot). It prints one of
  `ENTRABOT_RESULT=licence-assigned`, `licence-already-assigned`,
  `licence-no-free-seat`, `licence-not-found`, `licence-assign-failed` or
  `licence-could-not-read` on a line of its own and, when it stops, a sentence
  saying why. Nothing is changed when it stops before the assignment.

Each exits `0` when it did what it says and `1` with `licence-could-not-read`
(or the assignment's own marker) otherwise.

## Exit behavior

- **Exit 0** — the Blueprint / Agent Identity / Agent User chain exists (created
  or reused). This does **not** guarantee every downstream stage completed:
  the Agent Identity app-role permission (`Application.Read.All`), the storage
  delegated consent, and license assignment each print a warning and continue
  on failure rather than stopping the run, so exit 0 can still leave one or
  more of those stages incomplete. Only the core Graph delegated consent grant
  (Chat/Mail/Files/Sites scopes) is treated as blocking on failure — it is what
  causes an Exit 1 below. Check the printed `WARNING`/`ERROR` lines, or
  [`show_agent_status.py`](../operations/show-agent-status-py.md), to confirm
  which stages actually succeeded.
- **Exit 1** — the Provisioner token could not be obtained
  (`ProvisionerBootstrapError`), the Blueprint recorded for this machine does
  not exist or could not be checked, more than one application has the
  Blueprint's name and none is recorded, an existing Agent User with the
  sign-in name this run wants belongs to a different Blueprint, a lookup of the
  agent's Agent Identity or Agent User could not be completed, the delegated
  Graph consent grant failed (a
  blocking error, because the third hop cannot mint an Agent User token without
  it), or a create call failed after its retries. Token responses are checked
  for an `"error"` key and for a leaked `Directory.AccessAsUser.All` scope
  before proceeding, and a `403` on Agent User creation is reported as a missing
  `AgentIdUser.ReadWrite.IdentityParentedBy` grant. The stops that come from a
  lookup print a result line first (below).

### Result lines

Each stop that comes from a lookup prints one line of its own just before its
message, naming the outcome:

| Line | Why the run stopped |
| --- | --- |
| `ENTRABOT_RESULT=blueprint-not-found` | The Blueprint recorded for this machine does not exist in this tenant, or the recorded value is not an ID. |
| `ENTRABOT_RESULT=blueprint-could-not-check` | Graph could not say whether the recorded Blueprint exists, or the search by name could not be completed. |
| `ENTRABOT_RESULT=blueprint-ambiguous` | No Blueprint is recorded, and more than one application has the Blueprint's name. |
| `ENTRABOT_RESULT=agent-user-wrong-blueprint` | The Agent User with the sign-in name this run wants belongs to a different Blueprint. |
| `ENTRABOT_RESULT=agent-lookup-could-not-check` | A lookup of the agent's Agent Identity or Agent User could not be completed, including which Blueprint an existing Agent User belongs to. |

The Windows setup wizard reads this line to explain the stop in its own words,
and keeps it off the screen (the setup log has it). `setup.sh` shows the
script's output as it is. Other failures, such as a create call or the consent
grant failing, print no result line, and neither does a run that completes.

## Idempotency and resume

Each stage first consults `.entrabot-state.json`, then queries Graph by stored
ID. For the Blueprint, a stored ID is the only one the script will use: if that
Blueprint does not exist, or Graph cannot say whether it does, the script stops
before creating or changing anything and never looks for another Blueprint by
name. Display names are not unique, so a name match could be a different
agent's Blueprint. Only when no Blueprint is recorded at all does the script
search by display name; one exact match is used, none means a new Blueprint is
created, and more than one stops the run and lists them.

Before it creates an Agent Identity, the script searches the tenant for an
existing Agent User with the sign-in name this run wants (see
`ENTRABOT_AGENT_USER_UPN` and `_ENTRABOT_UPN_SUFFIX` above). It reuses that
Agent User, and its parent Agent Identity, only when the Agent Identity belongs
to this run's Blueprint: its `agentIdentityBlueprintId` is the Blueprint's App
ID. That lets a second device, or a machine that lost its state file, re-attach
to its agent instead of colliding on the unique sign-in name. An Agent User of a
different Blueprint stops the run before anything is created, granted or
recorded; the message names both Blueprints and says how to join that one or
give this agent another name. A Blueprint this run would create has no Agent
Users yet, so with none recorded or found, an existing Agent User of that name
always stops the run. If the script cannot read which Blueprint the Agent User
belongs to, it stops as "could not check"; so does a `404` for its Agent
Identity, which may be too new to be listed.

Every lookup tells "there is none" from "could not check". Only Graph saying so
counts as "there is none": an empty list, or `404` for an object recorded in
the state file. An HTTP error, a request that fails, an answer the script
cannot read, or a partial list without it (Graph returned more than one page)
stops the run before the object it was looking for is created. The search by
sign-in name and the check above run before anything is created or recorded.
The Agent Identity and Agent User lookups run after the Blueprint is recorded
(or, for a new agent, created), so their message says what was not created.
Run the script again once the cause is fixed; it continues from what is
recorded. Set `ENTRABOT_NEW_CHAIN=1` to bypass every lookup.

## Common failures

- **`The Agent User <upn> belongs to a different Blueprint`** — the sign-in
  name this run looks for is another agent's: the one `--agent-user-upn` or
  `--with-upn-suffix` asked for (`-AgentUserUpn` or `-UpnSuffix` on Windows),
  the one an earlier setup left in `.env`, or the default
  `entrabot-agent@<domain>`. Nothing was created or changed. Join that agent's
  Blueprint, or give this agent's Agent User another name; the message prints
  both commands. See
  [Setup and authentication](../../../troubleshooting/setup-and-authentication.md#setup-stops-because-the-agent-user-belongs-to-a-different-blueprint).
- **`Could not check ...`** — Graph did not answer a lookup the run needs.
  This is usually throttling or a network problem: run setup again in a few
  minutes. If it keeps failing with HTTP `401` or `403`, the Provisioner app is
  missing a permission or its admin consent: run setup again as a tenant
  administrator. The message says what was not created.
- **`... does not exist in this tenant`**, or **`N applications in this tenant
  are named 'EntraBot Code Agent'`** — the Blueprint recorded for this machine
  is not in the tenant you are signed in to (a mistyped ID, another tenant, a
  deleted Blueprint, or one created minutes ago that is not listed yet), or no
  Blueprint is recorded and several applications have its name. Nothing was
  changed. Run setup again with the right Blueprint App ID
  (`setup.sh --use-blueprint=APP_ID`, `-UseBlueprint APP_ID` on Windows).
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
  [`assign_agent_user_licenses.py`](assign-agent-user-licenses-py.md), or on
  Windows run `scripts\setup-windows.cmd` again, whose licence walkthrough
  shows what the organization has and gives the agent a free one.

## Related commands

- [Provisioning command index](../index.md#provisioning)
- [`add_agent_sponsor.py`](add-agent-sponsor-py.md)
- [`assign_agent_user_licenses.py`](assign-agent-user-licenses-py.md)
- [`ensure_a365_work_iq_permissions.py`](ensure-a365-work-iq-permissions-py.md)
- Platform docs:
  [Agent Identity Blueprints and Users](../../../platform-docs/agent-id-blueprints-and-users.md),
  [Agent Users](../../../platform-docs/entra-agent-users.md)
- Architecture: [Identity and token flow](../../../architecture/identity-and-token-flow.md)
