# Windows

Start with the health check. It changes nothing and names what is wrong:

```bat
scripts\setup-windows.cmd -Diagnose
```

Running `scripts\setup-windows.cmd` again is the repair path for most of what follows. It keeps what already works.

## Where the logs are

| Log | Location |
|---|---|
| Setup wizard | `%LOCALAPPDATA%\entrabot\logs\setup-<date>-<time>.log` |
| Package install | `%LOCALAPPDATA%\entrabot\logs\entrabot-bootstrap-pip-<date>.log` (left in `%TEMP%` if the install failed) |
| Certificate rotation, teardown, diagnose | `%LOCALAPPDATA%\entrabot\logs\rotate-cert-*.log`, `teardown-*.log`, `diagnose-*.log` |

The setup log holds everything the helper scripts printed. The screen shows a one-line summary per step; add `-Verbose` to see the full output as it happens. Setup removes tokens and full certificate thumbprints from what it prints and logs.

## "Entrabot setup cannot start on this PC"

**Cause.** Your organization has turned off PowerShell scripts with a Group Policy (the `MachinePolicy` or `UserPolicy` execution policy). `setup-windows.cmd` cannot override that.

**Fix.** Ask IT to allow locally created scripts for your account (execution policy `RemoteSigned`). The message on screen has wording you can send them.

## "This window is running as a different account"

**Cause.** The terminal was opened as an administrator account that is not the person logged on to the PC. Entrabot keeps its certificate and settings for the account that runs setup, so the agent would belong to that administrator account.

**Fix.** Close the window and run `scripts\setup-windows.cmd` normally. Setup does not need administrator rights.

## "Python could not be set up automatically"

**Cause.** The PC has no Python 3.12 or newer, and setup could not download its private copy (from `nuget.org`), nor install one another way.

**Fix.** Install Python 3.12 from <https://www.python.org/downloads/> ("Install Now"; administrator rights are not needed), then run setup again. Do not use the Python from the Microsoft Store.

## The sign-in window never appears

**Cause.** The Microsoft sign-in window opened behind another window, or the PC has no desktop session to show it on.

**Fix.** Look behind the setup window and on the taskbar. Or sign in with a code instead:

```bat
scripts\setup-windows.cmd -DeviceCode
```

Setup prints a web address and a short code; open the address on any device. Add `-Tenant <domain>` to sign in to a specific tenant. In a remote session (SSH) setup uses a code automatically.

If your organization only allows sign-in from managed devices, sign-in from an unmanaged PC is refused by Microsoft Entra; setup cannot work around that.

## "The sign-in code was not used in time"

With `-DeviceCode` (or in a remote session, where setup uses a code automatically) setup shows a code and waits for you to enter it at the page it names. Setup says "The sign-in code was not used in time. Codes last about 15 minutes."

**Cause.** Nobody finished the sign-in before the code expired.

**Fix.** In the wizard, answer yes to "Get a new code and try again?". With `-Yes` setup exits with code 1: run `scripts\setup-windows.cmd` again to get a new code.

## Setup says your account lacks admin rights, or consent fails

**Cause.** Creating an agent grants admin consent to the setup (Provisioner) app, which needs a **Global Administrator** or **Privileged Role Administrator**. Joining an existing agent also needs directory rights, because each PC adds its own credential to the Provisioner app and to the agent's Blueprint.

**Fix.** Sign in with an account that holds one of those roles, or ask an administrator to run setup. If you hold the role through Privileged Identity Management, activate it first. Setup checks your active roles after sign-in and warns before it changes anything; you can continue anyway if you know you have the rights another way.

## "An app registration with the Provisioner's name already exists ..."

The message ends "and setup would not take it over". Setup exits with code 1.

**Cause.** This PC has no Provisioner app recorded (a new PC, or one that was torn down), and an app registration named 'EntraBot Agent ID Provisioner' already exists in the tenant. That app gets tenant-wide Microsoft Graph permissions with admin consent, and anyone allowed to register applications can give an app that name. So setup takes an existing app over only when an administrator has already approved it for every permission setup needs (the usual case on a second PC), or when nobody else can use it. The lines above the message, and the setup log, list each app setup found and why it did not use it. Nothing was changed in Microsoft Entra. The full rule is in [Setup and authentication](setup-and-authentication.md#setup-will-not-take-over-an-existing-provisioner-app-registration).

**Fix.** Delete or rename the app you don't recognise (Entra admin center > App registrations > All applications). Or, once you have checked that the app is yours, set `ENTRABOT_PROVISIONER_APP_ID_VERIFIED` to its application ID and run setup again from the same window:

```bat
set "ENTRABOT_PROVISIONER_APP_ID_VERIFIED=<application ID>"
scripts\setup-windows.cmd
```

In PowerShell, set it with `$env:ENTRABOT_PROVISIONER_APP_ID_VERIFIED = '<application ID>'`. Setup records the app, so later runs do not need the variable. If setup then says the value was not accepted, see the next entry.

## "The value given in ENTRABOT_PROVISIONER_APP_ID_VERIFIED was not accepted"

The message goes on "Nothing was changed in Microsoft Entra." Setup exits with code 1, at the step that connects setup to your tenant.

**Cause.** `ENTRABOT_PROVISIONER_APP_ID_VERIFIED` is set in this window, and setup refused the app it names: the value is not an application ID, no app registration in the tenant has that application ID (or Microsoft Entra returned more than one), or this PC already uses a different Provisioner app, recorded by an earlier setup. The lines above the message, and the setup log, give the reason.

**Fix.** Set the variable to the application ID of the Provisioner app you have checked is yours, or clear it, then run setup again from the same window. If this PC already has a Provisioner app recorded, clear the variable: setup keeps using the recorded app, and naming another one is not a way to move off it.

To clear it in `cmd.exe`:

```bat
set "ENTRABOT_PROVISIONER_APP_ID_VERIFIED="
scripts\setup-windows.cmd
```

To set it, use `set "ENTRABOT_PROVISIONER_APP_ID_VERIFIED=<application ID>"` instead. In PowerShell, `$env:ENTRABOT_PROVISIONER_APP_ID_VERIFIED = '<application ID>'` sets it and `Remove-Item Env:ENTRABOT_PROVISIONER_APP_ID_VERIFIED` clears it.

## "Setup could not read what it needed from Microsoft Entra"

Setup exits with code 1. At the step that connects setup to your tenant the message goes on "Nothing was changed in Microsoft Entra." At the step that creates your agent it goes on "Nothing was changed." when the lookup was for the Blueprint, and "The lines above say what was and was not created." when it was for the Agent Identity or Agent User, because by then the step may already have created something.

**Cause.** A lookup setup needs got no answer it could use: Microsoft Entra throttled it, the network failed, the sign-in expired, or your account lacks an active admin role. At the step that connects setup to your tenant, it is a lookup of the Provisioner app (the one this PC has recorded, the one `ENTRABOT_PROVISIONER_APP_ID_VERIFIED` names, or one with the Provisioner's name) or a second read of its admin consent. At the agent step it is a lookup of the agent's Blueprint, Agent Identity or Agent User. Setup never reads "could not check" as "there is none", so it does not create, take over or reuse anything on the strength of it. What Microsoft Entra said, and at the agent step what was not created, is in the lines above the message and in the setup log.

**Fix.** Run `scripts\setup-windows.cmd` again in a few minutes; it continues from what is recorded. If you activate admin roles on demand (Privileged Identity Management), activate them first. If it keeps failing with HTTP 401 or 403, run setup as an administrator of the tenant.

## "The Blueprint App ID ... does not exist in this tenant"

The message is "The Blueprint App ID you gave (<App ID>) does not exist in this tenant (<tenant>)." for an agent you named with `-UseBlueprint` that this PC is not set up for yet, or "The Blueprint App ID recorded on this computer (<App ID>) ..." for the agent it is set up for. Setup exits with code 1, at the step that creates your agent.

**Cause.** The tenant you signed in to has no Blueprint with that App ID: it is mistyped, it belongs to another tenant, or the Blueprint was deleted. One created in the last few minutes may not be listed yet. Setup uses only that Blueprint and never looks for another one by name, so the agent step stopped before creating or changing anything.

**Fix.** Check the Blueprint App ID, and that you signed in to the agent's tenant (`-Tenant <domain>` chooses one). Then run setup again with the right one (`-UseBlueprint <App ID>`), or create a new agent instead (`-NewChain -UpnSuffix <name>`).

Setup does not keep an App ID that does not exist: it prints "Setup did not keep that Blueprint App ID: this computer's record of its agent is as it was before this run.", so the next run starts from where this PC was before.

If the message is instead "Several applications in this tenant have the agent's name, and none is recorded on this computer, so setup can't tell which one is your agent.", more than one application in the tenant is named 'EntraBot Code Agent', the name setup gives a new agent's Blueprint, and display names are not unique. The lines above list each one's App ID and creation date. Run setup again with the App ID of yours: `scripts\setup-windows.cmd -UseBlueprint <App ID>`.

## "The name chosen for the agent already belongs to a different agent."

Setup exits with code 1, at the step that creates your agent.

**Cause.** When you join or continue an agent, setup looks for an existing Agent User with the sign-in name this run uses, and reuses it only when it belongs to that agent's Blueprint. This one belongs to another agent's Blueprint, so the agent step stopped before creating, granting or recording anything. The name is the one `-AgentUserUpn` or `-UpnSuffix` gave, or the one this PC's `.env` names from an earlier setup; with neither, it is `entrabot-agent@<domain>`. A PC whose switch to another agent was interrupted can still name the previous agent's Agent User in `.env`. The lines above the message list both Blueprints and the commands to go on.

**Fix.** Join that agent instead (`scripts\setup-windows.cmd -UseBlueprint <its Blueprint App ID>`), or give this agent's Agent User another name (`-UpnSuffix <name>`), or name one of its Agent Users exactly (`-AgentUserUpn <address>`). See [Setup and authentication](setup-and-authentication.md#setup-stops-because-the-agent-user-belongs-to-a-different-blueprint).

## "No Teams licence", or `Graph returned 403 for /me/chats`

**Cause.** The agent has no licence that includes Teams. Like a person, the agent's user account needs a licence of its own. Setup gives it a free one by itself, preferring one that also brings mail and files. When none is free, the agent is still created and can sign in, but Teams refuses it, and setup ends with exit code 3.

A licence counts as including Teams when it carries the Microsoft Teams service plan. The "(no Teams)" suites sold since April 2024 do not, even though some of their plans are named after Teams, and neither do licences for meeting rooms and shared devices.

**Fix.** Run `scripts\setup-windows.cmd` again, answering its questions yourself (not with `-Yes`; an SSH session is fine). After the end-to-end test it offers to walk you through it, under **Licences for Microsoft 365**:

- It shows your organization's licences, what each gives the agent (Teams, mail, files) and how many are free, then gives the agent a free one when you say yes. With several, it suggests the one with mail and files, and you can pick another.
- When every licence that includes Teams is in use, it says the two ways out:
    - **Add a licence.** A billing administrator does this in the Microsoft 365 admin center (<https://admin.microsoft.com>): **Billing > Your products**, the product, then **Buy licenses**. If your organization buys through a partner, ask them instead.
    - **Free one** from someone who no longer needs it. A licence or user administrator does this under **Billing > Licenses**: the product, the person, then **Unassign licenses**. When that licence includes their mailbox, their mail is kept for 30 days and then deleted.
- When nothing in the organization includes Teams, it lists what does (Microsoft 365 Business Basic, Standard or Premium; Microsoft Teams Essentials; Microsoft Teams Enterprise or, in the EEA and Switzerland, Microsoft Teams EEA, with a "(no Teams)" suite) and says a billing administrator buys one in the admin center under **Marketplace**.
- Press Enter when that is done and setup checks again; type `s` to leave it for later. Its last screen then points back to these steps instead of repeating them.

These menu names are the admin center's **Dashboard view**, which most people see. In the **simplified view**, the products are under **Subscriptions** and new ones under **Billing > Add more products**; a button at the top of the admin center switches between the views. The roles are as Microsoft Learn names them: buying needs at least a Billing Administrator (with a Microsoft Customer Agreement, a billing account or billing profile owner or contributor); assigning and unassigning need at least a License Administrator or User Administrator.

Run unattended (`-Yes`), setup asks nothing; it reads what the organization has, changes nothing, and the last screen says what to do about it. Then run setup again, which assigns the licence by itself, and check with `scripts\setup-windows.cmd -Diagnose`.

A licence that was just assigned takes 10-15 minutes to reach Teams. If setup says a licence was assigned and Teams still returns 403, wait and run `-Diagnose` again. A bought licence can be assigned at once.

## "Word, Excel and OneDrive files were asked for, but they are not switched on: ... the tools they need"

Setup ends with exit code 3.

**Cause.** Word, Excel and OneDrive files (Microsoft Agent 365 Work IQ) need two tools on this PC: the Microsoft Agent 365 CLI (`a365`), which runs on the .NET SDK, and PowerShell 7, which one `a365` step uses. They were missing, and the install was declined or did not finish.

**Fix.** Install them for your account, with no administrator rights:

```bat
powershell -ExecutionPolicy Bypass -File scripts\prereqs-windows.ps1 -WithWorkIq
```

then run `scripts\setup-windows.cmd` again and answer yes to the Word, Excel and OneDrive question. When you answer yes and the tools are missing, setup offers to run that command for you. The Agent 365 CLI, and the .NET SDK when the PC has none, go in `%LOCALAPPDATA%\entrabot\tools` (about 1 GB; a few minutes); PowerShell 7 comes from `winget`. If the helper reports a failure, its messages say which tool and where to get it. See [`prereqs-windows.ps1`](../reference/scripts/setup/prereqs-windows-ps1.md).

Run unattended with `-ConfigureA365WorkIq`, setup stops with exit code 1 before changing anything when the tools are missing, and prints the same command.

To run Entrabot's own copy of `a365` by hand, first set `DOTNET_ROOT=%LOCALAPPDATA%\entrabot\tools\dotnet`; without it, `a365.exe` cannot find its .NET.

## "... they can only be switched on from this PC's own desktop"

Setup ends with exit code 3. The rest of setup is done.

**Cause.** One of the steps that switch on Word, Excel and OneDrive files (`a365 setup requirements`) signs in to Microsoft through a sign-in window (on Windows, the Web Account Manager). A session without a desktop, such as SSH, cannot show that window, so setup does not try. On a real run over SSH, `a365` failed with `MsalAuthenticationFailedException` there. Setup says so when you answer yes to the Word, Excel and OneDrive question, before it offers to install their tools; it still installs them if you agree, and it gives the agent no Microsoft 365 Copilot licence in that run.

**Fix.** Sign in at the PC's own desktop, or with Remote Desktop, and run `scripts\setup-windows.cmd` again there, answering yes to the Word, Excel and OneDrive question. With the tools already installed, that run is quick. Word, Excel and OneDrive files cannot be switched on over SSH or in any other session without a desktop.

## "Word, Excel and OneDrive files were asked for, but they are not switched on."

Setup ends with exit code 3. The rest of setup is done.

**Cause.** A step that switches them on failed. These steps run after the end-to-end test, and a failure there never stops setup. The reason under the message says what happened:

- *The Agent 365 tool could not sign in to Microsoft.* `a365` reported a failed sign-in.
- *The Agent 365 tool's first step, which signs in and checks the tenant, did not finish.* `a365 setup requirements` runs in your terminal, so its own messages are on the screen above, where it ran, rather than in the log.
- *The 'Agent 365 CLI' app registration was not found in this tenant.* Setup points the Agent 365 tooling at your tenant's own app registration named **Agent 365 CLI**, and there is none.
- *One of the steps that switch them on did not finish*, or *... could not grant all of their permissions*: see the setup log, whose path the message gives.

Setup puts `ToolingManifest.json` and `a365.config.json` back the way they were, so neither setup's next run nor the agent takes Word, Excel and OneDrive files to be on.

**Fix.** Run `scripts\setup-windows.cmd` again at the PC's own desktop and answer yes to the Word, Excel and OneDrive question; sign in when the Microsoft window opens, and answer `a365`'s own questions. For the missing app registration, run `a365 setup requirements` once, choose **C** to create it, then run setup again (for Entrabot's own copy of `a365`, set `DOTNET_ROOT` first, as above). Switching them on needs a Global Administrator, because `a365 setup permissions mcp` grants permissions.

A Microsoft 365 Copilot licence that setup gave the agent earlier in the same run (at the step that creates the agent) stays assigned when a later step fails; the next run uses it.

## "Word, Excel and OneDrive files are switched on, but the agent has no Microsoft 365 Copilot licence"

Setup ends with exit code 3.

**Cause.** The agent's Work IQ tools need a Microsoft 365 Copilot licence of its own. Setup gives it a free one by itself, at the step that creates the agent, in every run that switches Word, Excel and OneDrive files on; none was free.

**Fix.** As for a missing Teams licence above: setup's licence walkthrough shows your Microsoft 365 Copilot licences and gives the agent a free one, or says how to add or free one. It comes after the Teams licence, and only when Word, Excel and OneDrive files are switched on in that run. Then run setup again and answer yes to the Word, Excel and OneDrive question; it gives the agent the licence by itself. A new Copilot licence can take 10-15 minutes before the files tools work.

## The agent ignores a person in Teams

**Cause.** The agent takes instructions from its **sponsors**, and the list it obeys is the one on the agent's directory object in Microsoft Entra, not the names in `.env`. A new agent's first sponsor is the account that created it.

**Fix.** Add the person as a sponsor. Either run setup again and name them:

```bat
scripts\setup-windows.cmd -Sponsor person@example.com
```

or use the script directly:

```powershell
.\.venv\Scripts\python.exe scripts\add_agent_sponsor.py person@example.com
.\.venv\Scripts\python.exe scripts\list_sponsors.py
```

Setup only adds sponsors; it never removes one. To remove one, use `scripts\remove_agent_sponsor.py`. Restart the assistant afterwards.

Setup looks an address up as a sign-in name, first directly and then with a search (which finds a sign-in name with an apostrophe, such as `o'brien@contoso.com`), then by email address, email alias, and guest sign-in name. The message lists what it tried. If setup says it could not find the person, give their Entra object ID instead of an address (Microsoft Entra admin center > Users > the person > Object ID). That always works.

## Claude Code blocks every edit or Teams message

**Cause.** This project's safety checks (Claude Code hooks) run through Git Bash, and they are set to block when they cannot run. Without Git for Windows they cannot run.

**Fix.** Install Git for Windows for your account. No administrator rights are needed:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\prereqs-windows.ps1 -WithGit
```

Then start the agent with `start-entrabot.cmd`, which points Claude Code at a Git Bash installed for your account.

A block that mentions "body prompt not loaded this session" is different and expected: read `prompts/agent_system.md` and the files it includes, then retry.

## "Claude Code ... is older than 2.1.295"

**Cause.** The project's two safety checks (Claude Code hooks) are set to block an action when the check itself cannot start. Claude Code understands that setting from version 2.1.295 on. An older version ignores it, so a check that fails to start lets the action through. Setup reads the version with `claude --version`; on an older one it finishes "with something to check" and exits with code 3.

**Fix.** Update Claude Code, then run `scripts\setup-windows.cmd` again to confirm:

```bat
claude update
```

If `claude` is not recognized, use the command setup printed, which names Claude Code by its full path. If setup says the version could not be read, it does not count that as a problem; update anyway to be sure.

## `claude` is not recognized

**Cause.** Claude Code's installer puts it in `%USERPROFILE%\.local\bin`, which may not be on your `PATH`.

**Fix.** Start the agent by double-clicking `start-entrabot.cmd`; it finds Claude Code there. To install Claude Code and add that folder to your `PATH`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\prereqs-windows.ps1 -WithClaudeCode
```

Open a new terminal afterwards.

## "another instance is already running"

**Cause.** Only one Entrabot server runs per PC. Setup registers the agent with both Claude Code and GitHub Copilot CLI; whichever assistant starts second finds the first one's server running, and its own exits.

**Fix.** Use one assistant at a time. Close the other one first.

## "could not mint persona-sati token ... token command not found"

**Cause.** On Windows, the program in `PERSONA_SATI_MCP_TOKEN_COMMAND` must be an absolute path, or a bare name found in a folder on `PATH`. A relative path (`scripts\get-token.cmd`, `.\get-token.ps1`) is refused, and so is a program found only in the folder the assistant was started from: that folder can be one nobody has checked, and Windows would otherwise look there first. `PATH` entries that are relative, or that point at the current folder, are skipped. The agent then starts with the body prompt alone, without its persona.

**Fix.** Put the full path in the `env` block of the `entrabot` entry in `.mcp.json`, for example `C:\Users\you\persona-sati\get-token.cmd --profile work`, then restart the assistant. A path with spaces needs no quotes. A `.ps1` script runs under the Windows PowerShell that ships with Windows (`powershell.exe`, version 5.1), not PowerShell 7; for a script that needs PowerShell 7, set the command to `pwsh -File <full path to the script>`. A `.py` script runs with Entrabot's own Python.

## "The agent could not sign in yet"

At the end-to-end test setup prints "Microsoft Entra has not started accepting the certificate just registered. That can take a minute or two." and waits, trying again every 10 seconds. If it gives up it says "The agent could not sign in yet: this computer's certificate was registered on the agent's Blueprint moments ago, and Microsoft Entra has not started accepting it yet." and exits with code 3.

**Cause.** This run registered a new certificate for this PC, and Microsoft's sign-in service can take a minute or two longer than the directory to start accepting it. Setup waits about two minutes.

**Fix.** Run `scripts\setup-windows.cmd` again in a few minutes. It keeps the certificate and only repeats the test.

## The agent cannot sign in: certificate missing, expired, or unusable

**Cause.** The agent signs in with a certificate in your Windows certificate store. Sign-in fails if that certificate has expired (it is valid for one year), was deleted, or its private key is not on this PC, for example after the PC was reset or the profile was copied from another PC. The agent logs a warning when 30 days or fewer remain.

**Fix.**

- Before it expires, replace it:

    ```powershell
    powershell -ExecutionPolicy Bypass -File scripts\deploy-windows.ps1
    ```

- After it has expired or gone missing, run `scripts\setup-windows.cmd` again. Setup creates and registers a new certificate when the current one is missing, expired, or cannot sign.

Restart the assistant afterwards; the agent reads its certificate when it starts.

## Certificate rotation stopped partway

`deploy-windows.ps1` exits with:

| Code | Meaning | What to do |
|---|---|---|
| `0` | Rotated. | Restart the assistant. |
| `1` | Failed. The old certificate is still in use and the new one was undone. | Fix what the message names, then run it again. |
| `2` | Run without a terminal and without `-Yes`. Nothing was changed. | Run it in a terminal, or add `-Yes`. |
| `3` | Rotated; the agent is on the new certificate, but removing the old one did not finish. For example, Microsoft Entra did not yet list the new certificate on the Blueprint, so the old one was left there. | Run it again, in a few minutes. It finishes the cleanup. |

## "An older setup key for this computer could not be switched off in Microsoft Entra"

**Cause.** Older versions of setup kept the setup (Provisioner) private key in a plain file under `%LOCALAPPDATA%\entrabot`. Setup now replaces it with a non-exportable key in the certificate store. Once Microsoft Entra accepts the new key, setup removes the old file's certificate from the Provisioner app, so that a copy of the file stops working, and then deletes the file. If the removal fails, setup still deletes the file and carries on, but a copy of the file can get tokens for the Provisioner app until its certificate is removed. Setup records the certificate (`PROVISIONER_CERT_REVOKE_PENDING` in `.entrabot-state.json`) and tries again on every run.

The warning is on screen in every mode, with or without `-Verbose`: at the end of the step that connects setup to your tenant, and again on the finish screen. It names the certificate by the last four characters of its thumbprint and its expiry date, gives the admin-center path, and says setup will try again. Setup then finishes "with something to check" (exit code 3). If a later step in the same run removes the certificate, the finish screen says it is switched off now instead. The reason the removal failed is in the setup log: search it for "The old Provisioner certificate".

If setup cannot read its record (an entry that is not a thumbprint, or a state file it cannot read), the warning says so and asks you to check the app's certificates by hand. It names the certificate this computer uses now, when it knows it, so you keep that one.

The health checks report the same key while it is waiting, as a warning named "Older setup keys" that fails no check and does not change their exit codes. `scripts\setup-windows.cmd -Diagnose` lists it after the state file check and then ends "No check failed, but an older setup key still has to be switched off in Microsoft Entra (see above)." instead of "All checks passed.". `status-windows.ps1` lists it among its health checks, with or without `-Strict`. In a folder that `teardown-windows.ps1 -KeepSetupCredential` left holding only setup's note about the key, `status-windows.ps1` says the folder is not connected to an agent, names the key on a second line, and exits 1.

**Fix.** Run `scripts\setup-windows.cmd` again. If the warning comes back, remove the certificate by hand: in the Microsoft Entra admin center (<https://entra.microsoft.com>), open App registrations > 'EntraBot Agent ID Provisioner' (the warning gives its application ID) > Certificates & secrets > Certificates, and remove the certificate whose thumbprint ends in the four characters the warning shows. Then run setup again; it finds the certificate gone and stops retrying.

Teardown also tries to remove the certificate before it deletes `.entrabot-state.json`, and says what is left if it cannot; see the [`teardown-windows.ps1` reference](../reference/scripts/teardown/teardown-windows-ps1.md).

## ".env is NOT restricted to your account"

**Cause.** Setup restricts `.env` to your account and then reads the permissions back. This warning means the read-back found other accounts with access, or could not read the permissions.

**Fix.** `.env` holds IDs, not passwords or keys, so setup continues. To restrict the file yourself, open its Properties > Security in File Explorer and remove every entry except your own account. Then run `scripts\setup-windows.cmd -Diagnose`.

## The end-to-end test says it could not use Windows Credential Manager

**Cause.** Setup was run in a remote session (SSH). Windows does not make Credential Manager available there. This is a limit of the session, not of the setup.

**Fix.** Sign in at the PC's own desktop and run `scripts\setup-windows.cmd -Diagnose`.

## Two Entrabot data folders exist

**Cause.** Older versions kept data in `%USERPROFILE%\.entrabot`. Current versions use `%LOCALAPPDATA%\entrabot`. Setup moves the old folder automatically when the new one is empty. When both have content it leaves both alone, uses the new one, and says so.

**Fix.** If the old folder holds data you still want, compare the two and move what you need by hand. Do not merge cursor files blindly; replay protection depends on their timestamps.

## Downloads fail behind a proxy

Setup has no proxy or TLS-inspection setting of its own, and it has not been tested behind either. It downloads with Windows PowerShell (Python from `nuget.org`, Git for Windows from `github.com`) and with `pip` (Python packages and the Azure CLI from `pypi.org`), then talks to `login.microsoftonline.com` and `graph.microsoft.com`. If a download fails, ask IT to allow those addresses, or install the missing tool yourself (Python 3.12, the Azure CLI, Git for Windows) and run setup again: setup uses tools that are already installed.

## I want to remove Entrabot from this PC

```powershell
powershell -ExecutionPolicy Bypass -File scripts\teardown-windows.ps1
```

By default this disconnects this PC only: its certificate is removed from the agent's Blueprint and from the PC, the `entrabot` entry is removed from the assistants' configuration, `.env` and `.entrabot-state.json` are backed up and removed, and this PC's setup certificate is retired (add `-KeepSetupCredential` if you are about to set up again). The agent, its memory, and `%LOCALAPPDATA%\entrabot` are kept. `-DeleteAgent` also deletes the agent from the tenant; `-PurgeData` also deletes the data folder. If setup left an older setup key it could not switch off, teardown tries to remove it as well. When that fails it says where to remove it by hand and exits 1; when it keeps the setup certificate (`-KeepSetupCredential`) it also leaves `.entrabot-state.json` holding only setup's note about the key, so setup's next run tries again. See the [`teardown-windows.ps1` reference](../reference/scripts/teardown/teardown-windows-ps1.md).

## Reference: the two certificate thumbprints

| Variable | Format | Purpose |
|---|---|---|
| `ENTRABOT_BLUEPRINT_CERT_THUMBPRINT` | Base64url SHA-256 | JWT assertion header `x5t#S256` |
| `ENTRABOT_BLUEPRINT_CERT_SHA1` | 40-character hexadecimal SHA-1 | Locates the certificate and its key in `Cert:\CurrentUser\My` |

Setup writes both. Do not edit them by hand; run setup again if `.env` and the certificate store disagree.

See [Windows Platform APIs](../platform-docs/platform-windows.md) and [Identity Lifecycle](../guides/identity-lifecycle.md).
