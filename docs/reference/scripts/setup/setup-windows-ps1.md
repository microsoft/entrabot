# `scripts/setup-windows.ps1`

Platforms: Windows.

## Purpose

`setup-windows.ps1` sets up Entrabot on a Windows PC. It is a bootstrap: it
gets the PC to the point where the setup wizard can run, then hands over to it.
The wizard (`python -m entrabot.winsetup`) is the Windows equivalent of
[`setup.sh`](setup-sh.md): it signs in to Microsoft, creates or joins the agent,
creates this PC's certificate, writes `.env`, registers the MCP server, and
tests that the agent can sign in.

Start it through [`setup-windows.cmd`](setup-windows-cmd.md), which handles the
execution policy. Running it again is how a setup is repaired or changed.

## Requirements

- Native Windows 10 or 11. It refuses to run inside WSL and points WSL users
  to `scripts/setup.sh`.
- Windows PowerShell 5.1 (ships with Windows) or PowerShell 7. Nothing needs
  PowerShell 7.
- No administrator rights, no `winget`, no `git`, and no Python: missing tools
  are installed for the current user only.
- A Microsoft 365 tenant, and an account with the directory rights to create
  the agent. Granting admin consent needs a Global Administrator or Privileged
  Role Administrator. Joining an existing agent also needs directory rights,
  because each PC adds its own credential to the Provisioner app and the
  Blueprint.
- Network access to `nuget.org` (only when Python must be downloaded),
  `pypi.org`, `login.microsoftonline.com` and `graph.microsoft.com`.

## Usage

```bat
rem Interactive: the wizard asks, every question has a suggested answer
scripts\setup-windows.cmd

rem A new agent, unattended
scripts\setup-windows.cmd -NewChain -UpnSuffix workstation -Yes

rem Join an existing agent from another PC
scripts\setup-windows.cmd -UseBlueprint <BLUEPRINT_APP_ID> -Yes

rem Cloud memory
scripts\setup-windows.cmd -UseBlueprint <BLUEPRINT_APP_ID> -UseCloudMemory -WithStorageAccount mycorpstg -WithContainer bob-mem

rem Check an existing setup. Changes nothing.
scripts\setup-windows.cmd -Diagnose
```

## Options

Which agent:

- `-NewChain` — create a brand-new agent (Blueprint, Agent Identity, Agent
  User). Mutually exclusive with `-UseBlueprint`.
- `-UseBlueprint <APP_ID>` — join an existing agent by its Blueprint App ID.
- `-UpnSuffix <NAME>` — short name; the agent becomes
  `entrabot-agent-<NAME>@<domain>`. Required with `-NewChain -Yes`: an
  unattended run never invents a name.
- `-AgentUserUpn <UPN>` — use exactly this Agent User address.
- `-Sponsor <EMAIL>[,<EMAIL>]` — who the agent takes instructions from in
  Teams. Each named sponsor is added on the agent's directory object; nobody is
  removed. Default for a new agent: the account that signs in. Default when
  joining or continuing: the agent's current sponsors, unchanged. A value can
  be an email address, a sign-in name, or an Entra object ID. An object ID is
  looked up directly. An address is looked up in this order, stopping at the
  first step that finds one person: the sign-in name directly; a search for the
  sign-in name (`userPrincipalName eq`), which finds a name the direct lookup
  can miss, such as one with an apostrophe (`o'brien@contoso.com`); `mail`;
  other email addresses; email aliases; and the guest (`#EXT#`) form of the
  sign-in name, so a personal Microsoft account that administers the tenant is
  found. If more than one person matches at a step, setup lists them and asks
  for the exact sign-in name or object ID. With an Azure CLI that can only be
  started through its `az.cmd` batch file, only the first three steps are
  possible, and setup says so when it finds nobody.

Memory:

- `-UseCloudMemory` — keep the agent's memory in Azure Blob Storage. Mutually
  exclusive with `-KeepMemoryLocal`.
- `-KeepMemoryLocal` — keep it on this PC. This is also how a PC on cloud
  memory goes back to local.
- `-WithStorageAccount <NAME>`, `-WithContainer <NAME>`, `-CreateNewStorage`,
  `-Location <REGION>`, `-Subscription <ID_OR_NAME>` — cloud-memory targeting.
  `-CreateNewStorage` and `-WithStorageAccount` are mutually exclusive. All of
  these require `-UseCloudMemory`.

With neither memory switch, a re-run for the same agent keeps its current
setting, and a new PC uses local memory.

Sign-in:

- `-Tenant <DOMAIN_OR_ID>` — sign in to this tenant.
- `-DeviceCode` — sign in with a code instead of a browser window. Used
  automatically in a remote session.

Other:

- `-ConfigureA365WorkIq` — also switch on Word, Excel and OneDrive files
  (Microsoft Agent 365 Work IQ). Needs the `a365` CLI and PowerShell 7, which
  one `a365` step requires; install both, with no administrator rights, with
  [`prereqs-windows.ps1 -WithWorkIq`](prereqs-windows-ps1.md). Unattended, a
  missing tool stops setup with exit `1` before anything is changed;
  interactively, answering yes to the Word, Excel and OneDrive question offers
  to install them. Switching them on needs this PC's own desktop (or Remote
  Desktop), because one `a365` step opens a Microsoft sign-in window; in a
  session without one, such as SSH, setup does not try (see Effects). The
  agent also needs a Microsoft 365 Copilot licence for them.
- `-Hosts <LIST>` — which assistants to register with: `claude`, `copilot`, or
  `claude,copilot` (the default).
- `-SkipSmoke` — skip the end-to-end test.
- `-Yes` — never ask. An answer that is still missing is an error (exit `2`)
  that names the parameter. With no identity parameter, `-Yes` continues the
  agent this PC is already set up for.
- `-Verbose` — show the full package-install output and everything the
  provisioning helpers print. By default each step is a one-line summary and
  the full output goes to the log.
- `-Diagnose` — check an existing setup and explain what is wrong. Never
  installs, repairs or moves anything.
- `-BootstrapOnly` — stop once Python and `.venv` are ready, without starting
  the wizard.
- `-Status`, with `-Json`, `-HealthOnly`, `-Strict` — show the agent's status
  through [`status-windows.ps1`](../operations/status-windows-ps1.md).
- `-A365AgentName`, `-Migrate` — accepted and ignored, with a note.
- `-Help` — show help and exit `0`.

## Effects

Bootstrap, in order:

1. Checks the PC. Refuses to run when the window is elevated as a different
   account than the person logged on, because everything Entrabot creates
   belongs to the account that runs setup.
2. Finds Python 3.12 or newer: a working `.venv` in the project, then the `py`
   launcher, the registry, and `PATH`. The Microsoft Store Python is never
   used. If there is none, it downloads the official Python 3.12.10 package
   from `nuget.org` (x64 or ARM64) and checks it before using any of it:
   first the whole package must match the SHA-512 checksum pinned in the
   script for that package and version, then, once unpacked into a staging
   folder, `python.exe` and its `python3*.dll` files must each carry a valid
   Authenticode signature from the Python Software Foundation. Only then is it
   moved into `%LOCALAPPDATA%\entrabot\python`. A package that fails either
   check is deleted and not used. The checksum covers the files that carry no
   signature (the rest of the DLLs and the standard library). A copy already
   unpacked by an earlier run is reused after its signatures pass again; the
   package checksum cannot be re-checked once unpacked. A processor with no
   pinned package gets no private copy. Later fallbacks are `winget` and the
   python.org installer.
3. Creates `.venv` and installs Entrabot into it with `pip`. A later run skips
   this when `pyproject.toml` has not changed. An unusable `.venv` is renamed
   aside, not deleted.
4. Moves data from the older location (`%USERPROFILE%\.entrabot`) to
   `%LOCALAPPDATA%\entrabot` when only the older one has content.
5. Starts the wizard.

The wizard, in order:

1. Checks the computer and whether it is already set up.
2. Signs in. If the Azure CLI is missing it installs it: with `winget` when
   the account is a local administrator and `winget` is present, otherwise
   (or if that fails) as a private copy in
   `%LOCALAPPDATA%\entrabot\tools\azure-cli`, installed with `pip` as
   `azure-cli>=2.92.0,<3` (never "whatever is newest", and never a new major
   version unannounced). Either way it is used only once `az version` answers.
   It then checks the account's directory roles and warns if they look
   insufficient.
3. Asks its questions (agent, sponsor, memory, Work IQ). Before the Work IQ
   yes/no it says what that is: Word, Excel and OneDrive files, which need a
   Microsoft 365 Copilot licence for the agent. When the answer is yes and
   the tools are missing, it offers to install them with
   [`prereqs-windows.ps1 -WithWorkIq`](prereqs-windows-ps1.md), for your account
   only and with no administrator rights (Enter means yes). Declined or failed,
   Work IQ stays off, the review screen says so, and the finish screen says
   what to do. In a session without a desktop (SSH, for example) it says first
   that they can only be switched on from the PC's own desktop, then still
   offers the install, so that the run at the desktop is quick; Work IQ stays
   off in this run, and no Copilot licence is assigned.
4. Shows a review screen. **Nothing in the tenant is changed before this is
   confirmed**, and nothing on the PC apart from tools you agreed to install
   (the Azure CLI, the tools for Word, Excel and OneDrive files); the review
   screen says which.
5. Connects setup to the tenant: the Provisioner app registration and this PC's
   Provisioner certificate. A PC with no Provisioner app recorded takes over an
   existing app registration with the Provisioner's name only under the rule
   described for [`setup.sh`](setup-sh.md#which-app-registration-is-the-provisioner);
   otherwise setup stops here with exit `1`, having changed nothing in Microsoft
   Entra. It also stops here, with nothing changed in Microsoft Entra, when the
   app `ENTRABOT_PROVISIONER_APP_ID_VERIFIED` names is not accepted, or when it
   could not read from Microsoft Entra what it needed to decide. If an older
   setup key could not be switched off on the Provisioner
   app, setup says so here, on screen, and again on the finish screen.
6. Creates or connects the agent, assigns a licence that includes Teams when
   one is free (preferring one that also brings mail and files, and a
   Microsoft 365 Copilot licence too when Work IQ is on), and adds the chosen
   sponsors. When joining or continuing an agent, an
   existing Agent User with the chosen name is reused only when it belongs to
   that agent's Blueprint. Setup stops here with exit `1`, before this PC's
   certificate step, when the Blueprint does not exist in the tenant, the name
   belongs to a different agent, or it could not read from Microsoft Entra what
   it needed, and says which.
7. Creates this PC's Blueprint certificate, or reuses the existing one if it is
   in date and can sign, and registers it on the Blueprint beside other
   machines' certificates. The key is non-exportable and is held by the TPM
   when the TPM accepts it.
8. Writes `.env`, restricted to the current user.
9. Sets up memory.
10. Registers the `entrabot` MCP server in the project's `.mcp.json` and in
    GitHub Copilot CLI's `mcp-config.json`, and checks that Claude Code and Git
    for Windows are present and that the project's Claude Code hooks run.
    Interactively it offers to install what is missing. It also reads Claude
    Code's version: the hooks' gates block when they fail to start only on
    Claude Code 2.1.295 or later, so an older version is reported, with the
    update command, as something to check.
11. Tests the agent end to end: sign-in, identity, and Teams.

Then, not as numbered steps:

**Word, Excel and OneDrive files**, when they were switched on in step 3 at a
desktop: setup points the Agent 365 tooling at the agent's Blueprint and grants
it Work IQ, with `a365 setup requirements` (in your terminal: it may open a
Microsoft sign-in window and ask before changing the Agent 365 CLI app
registration), `a365 develop add-mcp-servers`, the Work IQ permission grant,
`a365 setup permissions mcp`, and a check of the tooling manifest. It comes
after the end-to-end test, so nothing optional sits in front of the steps the
agent needs. **A failure here never stops setup**: Word, Excel and OneDrive
files stay off, `ToolingManifest.json` and `a365.config.json` are put back the
way they were, and the finish screen says why in plain words (for example,
that the Agent 365 tool could not sign in to Microsoft) and where the setup log
is. A Microsoft 365 Copilot licence given to the agent in step 6 stays with it.

**The licence walkthrough**,
("Licences for Microsoft 365"). It runs when the agent could sign in and still
has no licence that includes Teams, or no Microsoft 365 Copilot licence while
Work IQ is switched on in this run. Interactively (or with its answer key) it:

- offers to walk you through it, then shows the organization's licences and
  what each gives the agent (Teams, mail, files), with how many are free;
- gives the agent a free one when you say yes, suggesting the one with mail and
  files and letting you pick another;
- otherwise says who can add a licence or free one and where in the
  Microsoft 365 admin center, or what to get when nothing includes Teams, then
  checks again each time you press Enter, until you type `s` or ten checks;
- when Teams is left for later, stops there (the Copilot part waits for it).

Unattended it only reads the organization's licences, so the finish screen can
say what to do about what the organization has. Reading and assigning run in
[`create_entra_agent_ids.py`](../provisioning/create-entra-agent-ids-py.md)'s
licence-only mode, with the setup (Provisioner) credential; your own sign-in
never changes the agent's account. Every product is shown by the name the admin
center uses, or, for a product Microsoft's reference does not list, a readable
form of its part number; part numbers go to the log.

It writes a log to `%LOCALAPPDATA%\entrabot\logs\setup-<date>-<time>.log`.

With `-Diagnose`, none of the above runs. The same end-to-end checks are
reported and nothing is changed. An older setup key that is still waiting to be
switched off in Microsoft Entra is listed after the state file check, as an
"Older setup keys" warning, which fails no check. The closing line is then "No
check failed, but an older setup key still has to be switched off in Microsoft
Entra (see above)." instead of "All checks passed.".

## Exit behavior

- `0` — set up and the end-to-end test passed; or `-Help`; or `-BootstrapOnly`
  finished. With `-Diagnose`: no check failed. Warnings, such as an older setup
  key still waiting, do not change it.
- `1` — failed. The message says what failed and what to do. With `-Diagnose`:
  a check failed, or the folder is not set up.
- `2` — conflicting parameters (reported before anything is installed), or an
  answer was missing under `-Yes`.
- `3` — set up, but something needs checking: the end-to-end test failed (for
  example, the agent has no Teams licence, or one given in this run has not
  reached Teams yet), a sponsor could not be added, Claude Code is older than
  2.1.295, an older setup key could not be switched off in Microsoft Entra, or
  Word, Excel and OneDrive files were asked for and do not work yet (their tools
  are not installed, the session has no desktop to switch them on from, a step
  that switches them on failed, or the agent has no Microsoft 365 Copilot
  licence).
- `130` — cancelled, with Ctrl+C or "no" at the review screen.
- `-Status` — the exit code of `status-windows.ps1`.

## Common failures

See [Windows troubleshooting](../../../troubleshooting/windows.md). The most
common are a missing Teams licence (exit `3`), an account without the needed
directory rights, and a sign-in window hidden behind another window (use
`-DeviceCode`). If an existing Provisioner app is not taken over (exit `1`), see
[Setup will not take over an existing Provisioner app registration](../../../troubleshooting/setup-and-authentication.md#setup-will-not-take-over-an-existing-provisioner-app-registration).

## Related commands

- [Script reference — Setup](../index.md#setup)
- [`setup-windows.cmd`](setup-windows-cmd.md) — the launcher for this script.
- [`start-entrabot.cmd`](start-entrabot-cmd.md) — starts the agent.
- [`prereqs-windows.ps1`](prereqs-windows-ps1.md) — optional extras.
- [`deploy-windows.ps1`](deploy-windows-ps1.md) — certificate rotation.
- [`teardown-windows.ps1`](../teardown/teardown-windows-ps1.md) — disconnect
  this PC.
- [`status-windows.ps1`](../operations/status-windows-ps1.md) — status.
- [`setup.sh`](setup-sh.md) — the macOS and Linux equivalent.
- [Windows installation](../../../getting-started/windows.md).
