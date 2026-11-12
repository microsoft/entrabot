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

In PowerShell, set it with `$env:ENTRABOT_PROVISIONER_APP_ID_VERIFIED = '<application ID>'`. Setup records the app, so later runs do not need the variable. If setup then says it "could not get the access it needs", read the lines above that message: a value that is not an application ID, that no app in the tenant has, or that differs from the app this PC already has recorded is refused there, and nothing is changed.

## "No Teams licence", or `Graph returned 403 for /me/chats`

**Cause.** The agent has no licence that includes Teams. Setup looks for a free Teams-capable licence in the tenant and assigns it; if none is free, the agent is still created and can sign in, but Teams refuses it. Setup then ends with exit code 3.

**Fix.**

1. Make a licence that includes Teams available in the tenant (Microsoft 365 admin center, <https://admin.microsoft.com>).
2. Run `scripts\setup-windows.cmd` again. It assigns the licence itself.
3. Check with `scripts\setup-windows.cmd -Diagnose`.

A licence that was just assigned takes 10-15 minutes to reach Teams. If setup says a licence was assigned and Teams still returns 403, wait and run `-Diagnose` again.

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
