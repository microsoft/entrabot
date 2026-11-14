# `scripts/setup.sh`

Platforms: macOS, Linux.

## Purpose

`setup.sh` sets up Entrabot on a Mac or a Linux computer, or repairs the setup
that is there. It is a bootstrap: it gets Python and the project's virtual
environment in place, then starts the setup wizard
(`python -m entrabot.winsetup`), the same wizard Windows runs from
[`setup-windows.cmd`](setup-windows-cmd.md). The wizard signs in to Microsoft,
creates or joins the agent, creates this computer's certificate with its key in
the account's Keychain or keyring, writes `.env`, sets up memory, registers the
MCP server, and tests that the agent can sign in.

With no options it asks; every question has a suggested answer. Nothing is
changed in the tenant until the review screen is confirmed. Running it again is
how a setup is repaired or changed. The same script runs the health check
(`--diagnose`), certificate rotation (`--rotate-cert`) and the status view
(`--status`).

The macOS and Linux paths of the wizard have not yet been run against a real
tenant; see
[What has been tested](../../../getting-started/macos-linux.md#what-has-been-tested).

## Requirements

- macOS or Linux, with `bash` (the bash 3.2 that ships with macOS is enough).
- Python 3.12 or newer: `python3.13`, `python3.12`, `python3.14` or `python3`,
  the first that is 3.12 or newer. Without one, setup says how to install it and
  exits `1`. On a Mac, [`prereqs-macos.sh`](prereqs-macos-sh.md) installs it
  through Homebrew. On Debian and Ubuntu, creating the environment also needs
  the `python3-venv` package (`python3.12-venv`).
- A key store the account can use: the login keychain on macOS, or a Secret
  Service keyring (GNOME Keyring, KWallet or libsecret) that is running and
  unlocked on Linux. The wizard checks this first and stops before changing
  anything if there is none. It never falls back to a file. See
  [Linux Platform APIs](../../../platform-docs/platform-linux.md#preparing-a-linux-host)
  for a Linux server with no desktop.
- Network access to `pypi.org` (Python packages, and the private Azure CLI when
  one is installed), `login.microsoftonline.com` and `graph.microsoft.com`.
- A Microsoft 365 tenant, and an account with the directory rights to create the
  agent. Granting admin consent needs a Global Administrator or Privileged Role
  Administrator. Joining an existing agent also needs directory rights, because
  each computer adds its own credential to the Provisioner app and the
  Blueprint.

Not needed beforehand: the Azure CLI or an `az login` (the wizard offers to
install a private copy and signs in itself), Git, Microsoft's Agent 365 CLI, the
.NET SDK, PowerShell 7, or `sudo`.

## Usage

```bash
# Interactive: the wizard asks, every question has a suggested answer
./scripts/setup.sh

# A new agent, unattended
./scripts/setup.sh --new --with-upn-suffix=workstation --yes

# Join an existing agent from another computer
./scripts/setup.sh --use-blueprint=<BLUEPRINT_APP_ID> --yes

# Repair the agent this computer is set up for, unattended
./scripts/setup.sh --yes

# Cloud memory
./scripts/setup.sh --use-blueprint=<BLUEPRINT_APP_ID> --use-cloud-memory --with-storage-account=contosostg --with-container=bob-mem

# Replace EVERY certificate on the Blueprint with a new one for this computer.
# Other computers stop signing in until setup is run on them again.
./scripts/setup.sh --use-blueprint=<BLUEPRINT_APP_ID> --remove-all-existing-certs

# Check an existing setup. Changes nothing.
./scripts/setup.sh --diagnose

# Replace this computer's certificate
./scripts/setup.sh --rotate-cert

# The status view (./status.sh)
./scripts/setup.sh --status --json
```

At a terminal, a command with options still asks the questions the options
leave open, and shows the review screen; add `--yes` for no questions. Without a
terminal the wizard never asks: it uses the options and the suggested answers.
An option that takes a value is written with `=` (`--use-blueprint=APP_ID`).
Values are passed to the wizard as arguments and never evaluated, so a value can
hold any character.

## Options

A value may follow its option after `=` or after a space: `--tenant=contoso.com` and `--tenant contoso.com` are the same. An option left without its value is a usage error (exit `2`).

Which agent:

- `--new` — create a new agent (Blueprint, Agent Identity, Agent User). Give it
  a name with `--with-upn-suffix`, or an exact address with `--agent-user-upn`;
  under `--yes` one of them is required, because an unattended run never invents
  a name. Cannot be combined with `--use-blueprint`.
- `--use-blueprint=APP_ID` — join an agent that already exists, by its Blueprint
  App ID: a second computer, or a different agent for this one. This computer
  gets its own certificate, and every other computer's is kept. Switching this
  computer to another agent backs up its record of the previous one; when this
  computer had a certificate on the previous Blueprint, setup says that it is
  not revoked.
- `--with-upn-suffix=NAME` — the short name: the agent becomes
  `entrabot-agent-NAME@<your domain>`. With `--use-blueprint`, it selects that
  Agent User of the Blueprint.
- `--agent-user-upn=UPN` — use exactly this address for the agent. With
  `--use-blueprint`, an Agent User with that address is reused only when it
  belongs to that Blueprint.
- `--sponsor=EMAIL` — who the agent takes instructions from in Teams. Repeat it,
  or separate addresses with commas. Each one is added as a sponsor on the
  agent's directory object, which is the list the agent obeys, and named in
  `.env`; nobody is removed. Default for a new agent: the account that signs in.
  Default when joining or continuing: the agent's current sponsors, unchanged. A
  value can be an email address, a sign-in name or an Entra object ID; the
  lookup order is described for
  [`setup-windows.ps1 -Sponsor`](setup-windows-ps1.md#options).
- `--teams-user=EMAIL` — another spelling of `--sponsor`.

Signing in:

- `--tenant=NAME_OR_ID` — sign in to this tenant (a domain such as
  `contoso.com`, or a tenant ID). An earlier sign-in to another tenant is not
  used.
- `--device-code` — sign in with a code instead of a browser window. The wizard
  does this by itself over SSH, and on Linux when there is no desktop (neither
  `DISPLAY` nor `WAYLAND_DISPLAY` is set).
- `--switch-user` — ask for a new Microsoft sign-in even when an earlier one
  still works, for example to set the agent up as someone else. It does not
  change the agent's sponsors by itself: add `--sponsor` for that.

Memory:

- `--keep-memory-local` — keep the agent's memory on this computer. This is the
  default on a first run, and on a later run it moves the agent back from Azure.
- `--use-cloud-memory` — keep it in Azure Blob Storage, in your subscription.
- `--with-storage-account=NAME`, `--with-container=NAME`, `--create-new-storage`,
  `--location=REGION`, `--subscription=ID_OR_NAME` — where the cloud memory
  goes. They need `--use-cloud-memory` (otherwise exit `2`).
  `--create-new-storage` and `--with-storage-account` cannot be combined.

With neither memory option, a later run for the same agent keeps the setting it
finds, and a new agent uses local memory.

Word, Excel and OneDrive files:

- `--configure-a365-work-iq` — also switch on Word, Excel and OneDrive files
  (Microsoft Agent 365 Work IQ), as answering yes to that question does. The
  wizard does it itself, with nothing to install: no Agent 365 CLI, no .NET SDK,
  no PowerShell 7, and no `a365.config.json` (see Effects). The agent also needs
  a Microsoft 365 Copilot licence for them.
- `--with-a365-work-iq` — the older name for the same thing. It prints a note.
- `--a365-agent-name=NAME` — accepted and ignored, with a note: Work IQ uses the
  agent's own Blueprint. Not listed in `--help`.

Certificates:

- `--remove-all-existing-certs` — replace **every** certificate on the agent's
  Blueprint, other computers' included, with a new one for this computer. Every
  other computer stops signing in until setup is run on it again. The agent
  itself is not deleted. At a terminal the review screen asks "Replace them
  all?", with No suggested; without a terminal the option is the consent.
  Cannot be combined with `--new` (exit `2`). Without this option setup never
  removes a certificate.
- `--rotate-cert` — replace this computer's certificate only, instead of
  running setup. See [Certificate rotation](#certificate-rotation).

Other:

- `--hosts=LIST` — the assistants to register the agent with: `claude`,
  `copilot`, or `claude,copilot` (the default).
- `--skip-smoke` — skip the end-to-end test at the end.
- `--yes`, `-y` — never ask. An answer that is still missing is an error (exit
  `2`) that names the option to give. With no option that says which agent, it
  continues with the agent this computer is already set up for.
- `--verbose` — show everything the helper scripts print. By default each step
  is a line or two, and the full output goes to the log.
- `--log-file=PATH` — where to write the log. Default:
  `~/.entrabot/logs/setup-<date>-<time>.log`.
- `--diagnose` — the health check of the setup that is here. Changes nothing.
- `--status` — show the agent's status through [`status.sh`](../operations/status-sh.md)
  instead of running setup. Every other option on the line is passed on to it,
  for example `./scripts/setup.sh --status --health-only --strict`.
- `--help`, `-h` — show the help and exit `0`.

`--diagnose` takes only `--log-file`; `--rotate-cert` takes only `--yes`,
`--verbose` and `--log-file`. Other setup options given with them are ignored.

Persona and long-term memory (persona-sati, optional): if your assistant uses
persona-sati, copy
[`docs/clients/persona-sati-host-bootstrap.md`](../../../clients/persona-sati-host-bootstrap.md)
into the assistant's global instruction file (for Claude Code,
`~/.claude/CLAUDE.md`). The help says the same.

## Effects

The bootstrap, in order:

1. With `--status`, hands over to `status.sh` and does nothing else.
2. Reads the options, translating each one for the wizard. An unknown option
   stops it with `ERROR: Unknown option: <option>`, the help, and exit `2`.
3. With `--diagnose` or `--rotate-cert` and no `.venv`, it says
   "This computer has not been set up yet. Run ./scripts/setup.sh first." and
   exits `1`. Those two never build or change `.venv`.
4. For setup, builds `.venv` when it is missing or not ready: it finds Python
   3.12 or newer, creates the environment, updates `pip`, `setuptools` and
   `wheel`, and installs Entrabot with its `provisioning` extra
   (`pip install -e ".[provisioning]"`). It records what it installed from in
   `.venv/.entrabot-setup-stamp` and installs again only when `pyproject.toml`
   has changed or a package setup needs is missing, so a second run starts at
   once. A `.venv` whose Python is older than 3.12 stops it: move that `.venv`
   aside and run it again.
5. Starts the wizard from the Entrabot folder.

The wizard, in order:

1. **Checking this computer.** Checks that the folder is complete and that the
   account's key store (the macOS Keychain or the Linux keyring) can be used.
   Without one it stops here, with exit `1` and nothing changed. It then reads
   whether this computer is already set up, and if it is, asks whether to
   continue with that agent, set up a different one, or only run the health
   check.
2. **Signing in to Microsoft.** If no Azure CLI is on `PATH` (or in
   `~/.entrabot/tools/azure-cli` from an earlier run), it offers to install a
   private copy there with `pip`, as `azure-cli>=2.92.0,<3`; it is used only once
   `az version` answers, and it is not added to your `PATH`. An earlier sign-in
   is offered only after a real request to the directory shows that Microsoft
   still accepts it; one that is refused is removed (`az logout --username`)
   before signing in again. If the directory refuses even a sign-in made that
   minute, the wizard clears the Azure CLI's sign-ins (`az account clear`, which
   signs every account out of that Azure CLI), asks once more, and otherwise
   stops before changing anything. It then checks the account's directory roles
   and warns when they look insufficient.
3. **A few choices.** Sponsor, memory, and Word, Excel and OneDrive files.
4. **Review.** **Nothing in the tenant is changed before this is confirmed**,
   and nothing on the computer apart from an Azure CLI you agreed to install;
   the review screen says so when it did. With `--remove-all-existing-certs` it
   asks here.
5. **Connecting setup to your tenant.** The Provisioner app registration and
   this computer's Provisioner certificate, kept in the key store (service
   `entrabot-provisioner-cert`, one per tenant). A computer with no Provisioner
   app recorded takes over an existing app registration with the Provisioner's
   name only under the rule in
   [Which app registration is the Provisioner](#which-app-registration-is-the-provisioner);
   otherwise it stops here with exit `1`, having changed nothing in Microsoft
   Entra. It also stops here, with nothing changed in Microsoft Entra, when the
   app `ENTRABOT_PROVISIONER_APP_ID_VERIFIED` names is not accepted, or when it
   could not read from Microsoft Entra what it needed to decide. If your
   organization ends the session while this step runs, setup says so in those
   words; running it again signs in again and continues.
6. **Creating your agent.** Creates or connects the agent through
   [`create_entra_agent_ids.py`](../provisioning/create-entra-agent-ids-py.md),
   with the Provisioner's token, never an Azure CLI token. It assigns a licence
   that includes Teams when one is free (preferring one that also brings mail
   and files, and a Microsoft 365 Copilot licence too when Word, Excel and
   OneDrive files are on), and adds the chosen sponsors to the agent's directory
   object. When joining or continuing an agent, an Agent User with the chosen
   name is reused only when it belongs to that agent's Blueprint. It stops here
   with exit `1`, before this computer's certificate step, when the Blueprint
   does not exist in the tenant, the name belongs to a different agent, several
   applications have the agent's name, it could not read what it needed from
   Microsoft Entra, or the step came back with a different Blueprint from the
   one this run is for; it says which.
7. **Securing the agent's sign-in on this computer.** Reuses this computer's
   certificate when the one `.env` names (`ENTRABOT_BLUEPRINT_CERT_SHA1`) is in
   the key store, in date, and signs, and checks with
   [`verify_blueprint_cert.py`](../auth-and-certs/verify-blueprint-cert-py.md)
   that it is on the Blueprint. On a computer set up before the wizard it keeps
   the older key when that key's certificate is on this Blueprint (see
   [Computers set up before the wizard](#computers-set-up-before-the-wizard)).
   Otherwise it creates a new certificate (RSA 2048, SHA-256, valid for one
   year), keeps it with its private key as one key store item, and registers it
   on the Blueprint with
   [`register_windows_blueprint_cert.py`](../auth-and-certs/register-windows-blueprint-cert-py.md),
   beside every other computer's. If the registration is refused, the new
   certificate is deleted with its key. If it cannot be confirmed, the
   certificate is kept and recorded, and setup stops after saving settings; the
   next run checks again.
8. **Saving settings.** Writes `.env`, replacing only its own lines (lines you
   added by hand are kept), and restricts it to your account (`chmod 600`). When
   this run is for the same Agent User, the sponsor and memory settings already
   there are kept unless an option changes them; a run for a different Agent
   User carries none of them over.
9. **Setting up the agent's memory.** Local, or Azure Blob Storage through
   [`provision_blob_storage.py`](../storage/provision-blob-storage-py.md). If
   the storage cannot be created, it says so and asks whether to continue with
   local memory (yes is suggested); an agent already on cloud memory keeps it.
   When cloud memory was set up in this run and this computer already holds
   memory, it offers to copy it (no is suggested; the local copy is kept). It
   then reads the permissions of `.env` back, and says "only your account can
   read or change it" only when that is what the file system says.
10. **Connecting your assistants.** Registers the `entrabot` MCP server in the
    project's `.mcp.json` (Claude Code) and in GitHub Copilot CLI's
    `~/.copilot/mcp-config.json` (or `$COPILOT_HOME`), keeping every other
    entry. Checks that Claude Code is installed and new enough (2.1.295 or
    later), and that the project's Claude Code hooks run under `bash`. Setup does
    not install Claude Code on macOS or Linux.
11. **Testing the agent end to end.** Sign-in, identity, and Teams. When this run
    registered the certificate, a sign-in that Microsoft Entra refuses because it
    does not accept the certificate yet (`AADSTS700027`) is tried again every 10
    seconds, about two minutes in all.

Then, not as numbered steps:

- **Word, Excel and OneDrive files**, when they were switched on. The wizard
  writes the three Work IQ servers (Word, OneDrive/SharePoint, Excel) into the
  project's `ToolingManifest.json`, keeping entries it does not own; gives the
  agent's Blueprint the permission grants and inheritable permissions they need
  through
  [`ensure_a365_work_iq_permissions.py`](../provisioning/ensure-a365-work-iq-permissions-py.md);
  and checks, as the agent, that its tokens carry the scopes, for about two
  minutes. A failure here never stops setup: they stay off,
  `ToolingManifest.json` is put back the way it was, and the last screen says
  why. Earlier versions of this script did this with Microsoft's Agent 365 CLI,
  PowerShell 7 and an `a365.config.json`; none of them is used now.
- **The licence walkthrough**, when the agent could sign in and still has no
  licence that includes Teams, or no Microsoft 365 Copilot licence while Word,
  Excel and OneDrive files are on. It is the same as on Windows; see
  [`setup-windows.ps1`](setup-windows-ps1.md#effects). Unattended it only reads
  the organization's licences.
- **The last screen**: what is set up, what needs attention and why, how to
  start the agent ([`start-entrabot.sh`](start-entrabot-sh.md)), what Claude Code
  asks the first time, and things to try. At a terminal, when everything needed
  is in place, it asks "Start your agent now, in this terminal?" and, on a yes,
  finishes and starts `start-entrabot.sh` in its place.

It does **not** create or watch any Teams chat. There is no default chat; every
Teams tool needs an explicit chat ID at runtime. See
[Messaging and delivery](../../../architecture/messaging-and-delivery.md).

With `--diagnose`, none of the above runs. It checks the state file, the
certificate (in the key store, in date, and its key signs; a warning when 30
days or fewer remain), the three-hop sign-in, the Graph identity, the Teams
scope, the MCP configuration, Word, Excel and OneDrive files when
`ToolingManifest.json` exists, and the optional `persona-sati` entry in
`.mcp.json`. The persona-sati check is advisory: it can report `SKIP` or `WARN`
but never a failure, and it connects only to loopback addresses (see
[MCP connectivity](../../../troubleshooting/mcp-connectivity.md#persona-sati-does-not-connect)).

### Computers set up before the wizard

Before the wizard, `setup.sh` kept the Blueprint's private key on its own, in one
key store item for the whole account (service `entrabot`, account
`blueprint-private-key`), and `.env` named no SHA-1 thumbprint. Such a computer
keeps working unchanged: without `ENTRABOT_BLUEPRINT_CERT_SHA1`, the agent signs
with that key as before.

The next `./scripts/setup.sh` run reads that key and asks Microsoft Entra, with
only its public half, whether its certificate is on this agent's Blueprint. If
it is, setup stores the key and that certificate together as the agent's own
item, records its SHA-1 in `.env`, and says "Kept this computer's existing
sign-in key, now stored as this agent's own item". Nothing new is created or
registered. If it is not, the key may be another agent's: setup leaves it alone
and creates a new certificate for this agent. If Microsoft Entra cannot be
asked, setup stops at the certificate step with nothing generated or changed.

The older item is left in place. Teardown deletes it only when it holds the same
key as the agent's own item. Until setup has run once, `--rotate-cert` and
[`teardown.sh`](../teardown/teardown-sh.md) stop and say to run setup first,
because the settings name no certificate.

[`register_blueprint_cert.py`](../auth-and-certs/register-blueprint-cert-py.md)
and [`find_local_blueprint_cert.py`](../auth-and-certs/find-local-blueprint-cert-py.md),
which worked with that older item, are no longer run by setup.

### Certificate rotation

`./scripts/setup.sh --rotate-cert` runs `python -m entrabot.winsetup rotate-cert`,
the same rotation as [`deploy-windows.ps1`](deploy-windows-ps1.md) on Windows.
The certificate in use stays in use until the new one is proven to work:

1. Creates a new certificate, with its key in the key store.
2. Adds it to the agent's Blueprint, beside the current certificate and every
   other computer's.
3. Signs the agent in with the new certificate, retrying for about two minutes
   while Microsoft Entra takes it up.
4. Switches `.env` to the new certificate.
5. Takes the old certificate off the Blueprint, and deletes it from the key
   store with its private key.

A failure in steps 1-3 undoes the new certificate and changes nothing else. A
failure in step 5 leaves the agent working on the new certificate; the old
thumbprint is kept in `.env` (`ENTRABOT_BLUEPRINT_CERT_RETIRING_SHA1`), and
running the command again finishes the cleanup without asking. It asks "Replace
this computer's certificate now?" first; without a terminal it needs `--yes`. It
uses the Azure CLI sign-in setup made. It needs a certificate named in `.env` and
in the key store: on a computer set up before the wizard, or one whose
certificate is gone, it changes nothing and says to run `./scripts/setup.sh`.
Restart the assistant afterwards: the agent reads its certificate when it
starts. The log is `~/.entrabot/logs/rotate-cert-<date>-<time>.log`.

### Which app registration is the Provisioner

`entra_provisioning.py` uses the Provisioner app that an earlier run on this
machine recorded in `.entrabot-state.json` (`PROVISIONER_CLIENT_ID`). Setup runs
the same script with the same rule on every platform. When no app is recorded,
or the recorded one no longer exists in Microsoft Entra, it looks for app
registrations named exactly `EntraBot Agent ID Provisioner`:

- **None:** it creates one.
- **One:** it takes that app over only if doing so grants nobody anything. That
  is the case when either:
    - an administrator has already granted it admin consent for every Microsoft
      Graph permission setup needs, and anything else the app asks for is a
      Graph application permission that has been granted too. This is the usual
      case on a second machine, or for a second administrator. Setup then grants
      no consent in this run; or
    - nobody else can use it: you are the only owner of the app and of its
      service principal (or they have no owners), the app has no client secret
      and no federated credential, its service principal has no credential of
      its own, every certificate on the app has its private key on this machine,
      and the app asks for no permission besides setup's Graph application
      permissions.
- **More than one, or one that meets neither condition:** setup stops before it
  changes anything in Microsoft Entra. It lists each app (application ID,
  creation date, owners) and why it was not used, and exits `1`. Anything setup
  could not read counts against the app.

The reason: the Provisioner app is granted tenant-wide Microsoft Graph
application permissions with admin consent, and app names are not unique.
Anyone allowed to register applications can create an app with this name, and
taking it over would hand those permissions to whoever controls it.

To use a particular app, set `ENTRABOT_PROVISIONER_APP_ID_VERIFIED` to its
application (client) ID for one run (see
[Setup and authentication](../../../troubleshooting/setup-and-authentication.md#setup-will-not-take-over-an-existing-provisioner-app-registration)).
Setup only checks that an app with that ID exists, then records it and treats it
as its own: it adds the permissions setup needs and, when any are not yet
consented, grants admin consent, which covers everything the app asks for. As on
every run, it also deletes any client secret on the app. Check the app's owners,
credentials and permissions first. A value that is not an
application ID, an ID no app has, or an app other than the one this machine
already has recorded is refused, and nothing is changed. The variable is not
read from `.env`, and `ENTRABOT_PROVISIONER_APP_ID`, which setup writes to
`.env`, does not choose the app.

### Changes from earlier versions

Every option `setup.sh` took before the wizard still works. These behave
differently now:

- With no options, setup asks. Before, it needed `--new` or `--use-blueprint`,
  and stopped otherwise.
- At a terminal, a command with options now also asks the questions they leave
  open and shows the review screen. Add `--yes` to run straight through, as
  before.
- `--teams-user=EMAIL` is another spelling of `--sponsor=EMAIL`, and the person
  is now also added as a sponsor of the agent in the directory. Before, it only
  wrote `.env`.
- `--switch-user` asks for a new sign-in; the agent's sponsors stay as they are
  unless `--sponsor` is given. Before, it ran `az login` and made the new user the
  sponsor in `.env`.
- `--with-a365-work-iq` is the same as `--configure-a365-work-iq`, and neither
  uses the Agent 365 CLI, the .NET SDK, PowerShell 7 or `a365.config.json`.
  `--a365-agent-name=` is ignored.
- An unknown option is an error (exit `2`). Before, it printed the help and
  exited `0`.
- The storage options without `--use-cloud-memory` are an error (exit `2`).
- The exit codes are the wizard's (below). A failed copy of memory to the cloud
  is now `1`; it was `2`.
- No `az login` is needed first, and a missing Azure CLI is offered as a private
  copy.
- `.venv` gets the `provisioning` extra, not `dev`: for the test tools, run
  `.venv/bin/python -m pip install -e ".[dev]"` afterwards.
- `.env` keeps lines you added by hand. Before, it was written from scratch.
- The agent's key is one key store item per certificate, and `.env` names it in
  `ENTRABOT_BLUEPRINT_CERT_SHA1`, as on Windows. See
  [Computers set up before the wizard](#computers-set-up-before-the-wizard).

## Exit behavior

- `0` — set up, and the end-to-end test passed or was skipped; or `--help`.
  With `--diagnose`: no check failed (warnings do not change it).
- `1` — failed. The message says what failed and what to do. This includes no
  Python 3.12, a `.venv` that could not be built, no usable key store, a
  provisioning step that stopped, a certificate that Microsoft Entra did not
  confirm, cloud memory you chose not to continue without, and a copy of memory
  to the cloud that failed. With `--diagnose`: a check failed, or the computer is
  not set up.
- `2` — an unknown option, conflicting options (`--new` with `--use-blueprint`
  or with `--remove-all-existing-certs`, `--create-new-storage` with
  `--with-storage-account`, a storage option without `--use-cloud-memory`, or an
  invalid value), or an answer that was missing under `--yes`.
- `3` — set up, but something needs checking: the end-to-end test failed (for
  example, the agent has no Teams licence, or one given in this run has not
  reached Teams yet; Microsoft Entra has not started accepting the new
  certificate yet; or the key store could not be read in this session), a
  sponsor could not be added or confirmed, Claude Code is older than 2.1.295, an
  older setup key could not be switched off in Microsoft Entra, or Word, Excel
  and OneDrive files were asked for and do not work yet. The last screen says
  which.
- `130` — cancelled, with Ctrl+C or "no" at the review screen.
- `--rotate-cert` — `0` rotated (or an earlier rotation's cleanup finished), `1`
  failed with the current certificate still in use, `2` run without a terminal
  and without `--yes`, `3` rotated with cleanup pending (run it again), `130`
  cancelled.
- `--status` — the exit code of `status.sh`.

## Common failures

See [macOS and Linux troubleshooting](../../../troubleshooting/macos-linux.md).
The most common are a missing Teams licence (exit `3`), an account without the
needed directory rights, a key store that cannot be used in the session (a
locked keychain over SSH, or no keyring on a Linux server), and an option
written without `=` (exit `2`). If an existing Provisioner app is not taken over
(exit `1`), see
[Setup will not take over an existing Provisioner app registration](../../../troubleshooting/setup-and-authentication.md#setup-will-not-take-over-an-existing-provisioner-app-registration).

## Related commands

- [Script reference — Setup](../index.md#setup)
- [macOS and Linux installation](../../../getting-started/macos-linux.md)
- [`start-entrabot.sh`](start-entrabot-sh.md) — starts the agent.
- [`prereqs-macos.sh`](prereqs-macos-sh.md) — installs Python and other tools
  on a Mac through Homebrew.
- [`teardown.sh`](../teardown/teardown-sh.md) — disconnect this computer, or
  delete the agent.
- [`status.sh`](../operations/status-sh.md) /
  [`show_agent_status.py`](../operations/show-agent-status-py.md) — status and
  health checks (reached through `--status`).
- [`setup-windows.ps1`](setup-windows-ps1.md) — the Windows bootstrap for the
  same wizard.
- [`deploy-windows.ps1`](deploy-windows-ps1.md) — the same certificate rotation
  on Windows.
- [`create_entra_agent_ids.py`](../provisioning/create-entra-agent-ids-py.md) —
  the identity-chain step.
- [`register_windows_blueprint_cert.py`](../auth-and-certs/register-windows-blueprint-cert-py.md) —
  registers this computer's certificate on the Blueprint.
- [`provision_blob_storage.py`](../storage/provision-blob-storage-py.md) —
  cloud memory, for `--use-cloud-memory`.
- [`setup_delegated.sh`](setup-delegated-sh.md) — delegated (MSAL) sign-in
  instead of the full Agent User chain.
- [Identity and token flow](../../../architecture/identity-and-token-flow.md).
