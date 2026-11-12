# Setup and authentication

## Setup stops before the final summary

Setup is designed to be re-runnable. Fix the first reported failure, then run
the same setup command again. Do not delete Entra resources simply to retry an
idempotent step.

For an existing Blueprint on macOS or Linux:

```bash
./scripts/setup.sh --use-blueprint=<blueprint-app-id>
```

On macOS and Linux, a re-run for the same Agent User keeps the sponsor
(`ENTRABOT_HUMAN_*`) and any cloud-memory settings already in `.env`, so this
command is safe on a configured machine. Pass `--teams-user` or `--switch-user` to
change the sponsor, or `--keep-memory-local` to go back to local storage. Setup writes the
rest of `.env` from scratch, so lines you added by hand are dropped.

On Windows, run setup again with no parameters. It recognises the agent this PC is
already set up for, keeps its sponsors and memory setting, and repairs what is broken:

```bat
scripts\setup-windows.cmd
```

Pass `-Sponsor <email>` to add a sponsor, `-KeepMemoryLocal` to go back to local
storage, or `-UseBlueprint <blueprint-app-id>` to connect this PC to a different agent.

If setup reports that provisioning succeeded but the smoke test failed, allow
time for a newly assigned Teams or Exchange license to propagate, then run:

```bash
./scripts/setup.sh --diagnose
```

See [Identity Lifecycle](../guides/identity-lifecycle.md) and the
[setup script references](../reference/scripts/index.md#setup).

## Setup will not take over an existing Provisioner app registration

Setup stops at the Provisioner step with "Setup did not take over an existing
'EntraBot Agent ID Provisioner' app registration (see above), and nothing was
changed in Microsoft Entra." On Windows the wizard says "An app registration
with the Provisioner's name already exists in this tenant, and setup would not
take it over." The lines above the message list each app it found (application
ID, creation date, owners) and why setup did not use it.

**Cause.** This machine has no Provisioner app recorded in
`.entrabot-state.json`: it is a new machine, or its state file was removed
(teardown removes it). An app registration with the Provisioner's name already
exists, and setup could not show that taking it over grants nobody anything.
The Provisioner app gets tenant-wide Microsoft Graph application permissions
with admin consent, and app names are not unique: anyone allowed to register
applications can create one with this name. So setup takes an existing app over
only when an administrator has already approved it for every permission setup
needs (the usual case on a second machine, or for a second administrator), or
when nobody else can use it: you are its only owner (or it has none), every
credential on it is this machine's, and it asks for no other permissions. It
never picks one of several apps with that name, and anything it could not read
counts against the app. The full rule is in the
[`setup.sh` reference](../reference/scripts/setup/setup-sh.md#which-app-registration-is-the-provisioner).

If the app is yours and the reason given is "not yet approved by an
administrator for N of the permissions setup needs", no administrator has
consented yet to permissions this version of setup asks for. Setup asks for
every Agent ID permission Microsoft Graph offers, so the list grows when a newer
version of setup, or Microsoft, adds one. Run setup once on a machine that
already has the app recorded: it adds those permissions and grants consent, and
this machine can then take the app over. Naming the app, below, also works.

**Fix.** Do one of these, then run the same setup command again:

- **Something could not be read** (for example "its owners could not be read"):
  sign in again with `az login`, or activate your administrator role.
- **You do not recognise the app:** delete or rename it in the Microsoft Entra
  admin center (App registrations > All applications). With no app left under
  that exact name, setup creates a new one.
- **You have checked that the app is yours** (its owners, its certificates and
  secrets, and its API permissions): set `ENTRABOT_PROVISIONER_APP_ID_VERIFIED`
  to its Application (client) ID for that run.

On macOS or Linux, for one command (use the options of the run that stopped):

```bash
ENTRABOT_PROVISIONER_APP_ID_VERIFIED=<application-id> ./scripts/setup.sh --use-blueprint=<blueprint-app-id>
```

On Windows, in `cmd.exe`, then run setup from the same window:

```bat
set "ENTRABOT_PROVISIONER_APP_ID_VERIFIED=<application ID>"
scripts\setup-windows.cmd
```

On Windows, in PowerShell:

```powershell
$env:ENTRABOT_PROVISIONER_APP_ID_VERIFIED = '<application ID>'
scripts\setup-windows.cmd
```

Setup records the app, so later runs do not need the variable. In `cmd.exe`
and PowerShell it stays set until the window is closed. Do not put it in
`.env`: setup does not read it from there. Setup treats the named app as its
own: it adds the permissions setup needs and, when any are not yet consented,
grants admin consent, which covers everything the app asks for. As on every
run, it also deletes any client secret on the app.

**If your tenant was set up with an earlier version.** Earlier versions used
the first app registration whose name began with the Provisioner's name,
without these checks. Look at that app once in the Microsoft Entra admin center
(App registrations > All applications, search for
`EntraBot Agent ID Provisioner`). There should be one. Its **Owners** should be
people you expect. Under **Certificates & secrets** there should be no client
secret and no federated credential, and only certificates you can account for
(each machine that ran setup added one). Under **API permissions** there should
be only Microsoft Graph application permissions. Remove anything you cannot
account for.

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

Use `./status.sh`, or on Windows `scripts\setup-windows.cmd -Diagnose`, for resource-chain health, then
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
.venv/bin/python scripts/verify_blueprint_cert.py <BLUEPRINT_OBJECT_ID> <EXPECTED_THUMBPRINT>
```

On macOS or Linux, if local state lost the registered thumbprint:

```bash
.venv/bin/python scripts/find_local_blueprint_cert.py <BLUEPRINT_OBJECT_ID>
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
