# Setup and authentication

## Setup stops before the final summary

Setup is designed to be re-runnable. Fix the first reported failure, then run
setup again. Do not delete Entra resources simply to retry an idempotent step.

Run it again with no options. It recognises the agent this computer is already
set up for, keeps its sponsors and memory setting, and repairs what is broken.
It replaces only its own lines in `.env`; lines you added by hand are kept.

On macOS and Linux:

```bash
./scripts/setup.sh
```

Pass `--sponsor=<email>` to add a sponsor, `--keep-memory-local` to go back to
local storage, or `--use-blueprint=<blueprint-app-id>` to connect this computer
to a different agent.

On Windows:

```bat
scripts\setup-windows.cmd
```

Pass `-Sponsor <email>` to add a sponsor, `-KeepMemoryLocal` to go back to local
storage, or `-UseBlueprint <blueprint-app-id>` to connect this PC to a different agent.

If setup reports that the agent is set up but the end-to-end test failed (exit
code 3), allow time for a newly assigned Teams or Exchange license to
propagate, then run the health check (`./scripts/setup.sh --diagnose`, or
`scripts\setup-windows.cmd -Diagnose` on Windows).

See [macOS and Linux troubleshooting](macos-linux.md), [Windows
troubleshooting](windows.md), [Identity Lifecycle](../guides/identity-lifecycle.md)
and the [setup script references](../reference/scripts/index.md#setup).

## Setup will not take over an existing Provisioner app registration

Setup stops at the step that connects it to your tenant with "An app
registration with the Provisioner's name already exists in this tenant, and
setup would not take it over." Above it, the provisioning script says "Setup did
not take over an existing 'EntraBot Agent ID Provisioner' app registration (see
above), and nothing was changed in Microsoft Entra." and lists each app it found
(application ID, creation date, owners) and why setup did not use it.

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
  sign in again (on macOS and Linux, `./scripts/setup.sh --switch-user`), or
  activate your administrator role.
- **You do not recognise the app:** delete or rename it in the Microsoft Entra
  admin center (App registrations > All applications). With no app left under
  that exact name, setup creates a new one.
- **You have checked that the app is yours** (its owners, its certificates and
  secrets, and its API permissions): set `ENTRABOT_PROVISIONER_APP_ID_VERIFIED`
  to its Application (client) ID for that run.

On macOS or Linux, for one command:

```bash
ENTRABOT_PROVISIONER_APP_ID_VERIFIED=<application-id> ./scripts/setup.sh
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

## Setup stops while creating the agent

Setup stops at its step "Creating your agent" and exits `1`, saying why in its
own words: the Blueprint App ID does not exist in this tenant, Microsoft Entra
could not be read, several applications have the agent's name, or the chosen name
belongs to a different agent (see
[Windows troubleshooting](windows.md#the-blueprint-app-id-does-not-exist-in-this-tenant);
the messages are the same on every system). The lines above come from
`create_entra_agent_ids.py` and are one of the messages below. It also prints an
`ENTRABOT_RESULT=` line naming the outcome, such as
`ENTRABOT_RESULT=blueprint-not-found`; the wizard keeps that line off the
screen, and the setup log has it.

**Cause.** `create_entra_agent_ids.py` stops rather than guess which Blueprint,
Agent Identity or Agent User is this agent's, before it creates the object in
question:

- **"Blueprint ... does not exist in this tenant"**: the Blueprint recorded for
  this machine, where setup also records the one `--use-blueprint` names, is
  not in the tenant you are signed in to. The ID is mistyped, it belongs to
  another tenant, or the Blueprint was deleted; one created in the last few
  minutes may not be listed yet. Nothing was changed, and no other Blueprint was
  looked for by name.
- **"BLUEPRINT_APP_ID in .entrabot-state.json is '...', which is not an ID"**:
  the value given to `--use-blueprint` is not an App ID. Nothing was changed.
- **"Could not check ..."**: Microsoft Graph did not answer a lookup the step
  needs, for example "Microsoft Graph answered HTTP 503", or the request failed.
  "Could not check" is not "there is none", so the step does not create a second
  Blueprint, Agent Identity or Agent User on the strength of it. The message
  says what was not created.
- **"... belongs to a different Blueprint"**: see
  [the next entry](#setup-stops-because-the-agent-user-belongs-to-a-different-blueprint).

**Fix.** For a Blueprint that does not exist, or a value that is not an ID,
check the App ID (it looks like `1a2b3c4d-1234-5678-9abc-def012345678`) and the
tenant setup says you are signed in to, then run setup again with the right App
ID, or create a new agent with `--new --with-upn-suffix=<name>` (`-NewChain
-UpnSuffix <name>` on Windows). For
"Could not check", run setup again in a few minutes; if it keeps failing with
HTTP `401` or `403`, the Provisioner app is missing a permission or its admin
consent, so run setup again as a tenant administrator. If the message says
Graph's answer names no parent Agent Identity for an Agent User, the sign-in
name may be an ordinary account's: give this agent a different name with
`--with-upn-suffix=<name>`. Setup continues from what is recorded. See the
[`create_entra_agent_ids.py` reference](../reference/scripts/provisioning/create-entra-agent-ids-py.md#idempotency-and-resume).

## Setup stops because the Agent User belongs to a different Blueprint

Setup stops at its step "Creating your agent" with "The name chosen for the
agent already belongs to a different agent." and exits `1`. Above it,
`create_entra_agent_ids.py` says "ERROR: The Agent User <upn> belongs to a
different Blueprint from the one this run is for", followed by that Agent User's
Blueprint, this run's Blueprint, and its Agent Identity.

**Cause.** Before it creates an Agent Identity, setup looks for an existing
Agent User with the sign-in name this run wants, and reuses it only when its
Agent Identity belongs to the Blueprint this run is for. Reusing another
Blueprint's would put that other agent on this machine: its consents, app role
and licence granted again, and its IDs recorded next to this Blueprint. The
name is the one `--agent-user-upn` or `--with-upn-suffix` asked for
(`-AgentUserUpn` or `-UpnSuffix` on Windows), or the one an earlier setup left
in this machine's `.env`; with none of those, it is `entrabot-agent@<domain>`.
Nothing was created, granted, or recorded.

**Fix.** To use that agent on this machine, join its Blueprint. The message
prints the command with its App ID:

```bash
./scripts/setup.sh --use-blueprint=<its-blueprint-app-id>
```

To stay with the Blueprint you asked for, give its Agent User a different name,
or name one of its Agent Users exactly:

```bash
./scripts/setup.sh --use-blueprint=<blueprint-app-id> --with-upn-suffix=<name>
./scripts/setup.sh --use-blueprint=<blueprint-app-id> --agent-user-upn=<upn>
```

On Windows, use `-UseBlueprint`, `-UpnSuffix` and `-AgentUserUpn`. A computer
whose switch to another agent was interrupted can still name the previous
agent's Agent User in `.env`: name the new agent's Agent User with
`--agent-user-upn` or `--with-upn-suffix` (`-AgentUserUpn` or `-UpnSuffix` on
Windows).

When this computer switches to another agent, setup runs the agent step
without the Agent User name that `.env` holds for the previous one, so that
name is not looked for under the Blueprint you are joining. Earlier versions of
`setup.sh` printed "Not reusing the Agent User named in .env" when they did
this; the wizard does it without a message.

## Setup stops because a different Blueprint was recorded

Setup stops at its step "Creating your agent" with "You asked to join the agent
with Blueprint <id>, but the agent step (create_entra_agent_ids.py) returned
Blueprint <id>. Setup stopped before this computer's certificate step. Nothing
further was changed." (or "This computer is set up for the agent with Blueprint
<id>, ...") and exits `1`.

**Cause.** After the agent step, setup checks that the Blueprint it came back
with is the one this run is for, because the next steps register this
computer's certificate on it and switch on Word, Excel and OneDrive files for
it. The agent step uses only the Blueprint recorded for the run, so this should
not happen. Setup stopped before the certificate step: no certificate was
registered on either Blueprint.

**Fix.** Check that the App ID is your agent's Blueprint App ID, then run setup
again with it: `./scripts/setup.sh --use-blueprint=<blueprint-app-id>`, or
`scripts\setup-windows.cmd -UseBlueprint <blueprint-app-id>` on Windows.

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
3. The Blueprint private key is present in the platform keystore, and on macOS
   and Linux the keychain or keyring is unlocked in the session the MCP host
   runs in.
4. Both certificate thumbprint variables are present
   (`ENTRABOT_BLUEPRINT_CERT_THUMBPRINT` and `ENTRABOT_BLUEPRINT_CERT_SHA1`). A
   macOS or Linux computer set up before the setup wizard has only the first,
   and signs with the account's older single key until setup is run again.
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

The health check (`./scripts/setup.sh --diagnose` on macOS and Linux) checks
that the certificate `.env` names is in the key store, in date, and that its key
signs. On a macOS or Linux computer set up before the setup wizard, whose key is
on its own in the account's single key store item, find the registered
certificate that matches it with:

```bash
.venv/bin/python scripts/find_local_blueprint_cert.py <BLUEPRINT_OBJECT_ID>
```

If the private key is missing, re-run setup for the existing Blueprint. Do not
copy private-key material into `.env` or logs.

See also [macOS and Linux troubleshooting](macos-linux.md) and
[Windows troubleshooting](windows.md).

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
