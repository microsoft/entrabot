# macOS and Linux

Start with the health check. It changes nothing and names what is wrong:

```bash
./scripts/setup.sh --diagnose
```

Running `./scripts/setup.sh` again is the repair path for most of what follows. It keeps what already works.

The setup wizard is the same on macOS, Linux and Windows, and so are most of its messages. This page covers what is particular to macOS and Linux, then lists the [entries on the Windows page](#messages-explained-on-the-windows-page) that apply here as they are.

## Where the logs are

| Log | Location |
|---|---|
| Setup wizard | `~/.entrabot/logs/setup-<date>-<time>.log` |
| Certificate rotation, teardown, diagnose | `~/.entrabot/logs/rotate-cert-*.log`, `teardown-*.log`, `diagnose-*.log` |
| The agent (MCP server) | `~/.entrabot/logs/entrabot.log` |

The setup log holds everything the helper scripts printed. The screen shows a line or two per step; add `--verbose` to see the full output as it happens, or `--log-file=PATH` to write the log somewhere else. Setup removes tokens and full certificate thumbprints from what it prints and logs. Building `.venv`, before the wizard starts, prints to the terminal only.

## "Entrabot needs Python 3.12 or later, and it was not found on this computer."

**Cause.** `./scripts/setup.sh` looked for `python3.13`, `python3.12`, `python3.14` and `python3` on your `PATH`, and none is 3.12 or newer.

**Fix.** Install one, then run setup again:

- On a Mac: `brew install python@3.12`, or `./scripts/prereqs-macos.sh`, which installs it through Homebrew with the Xcode Command Line Tools, Git and the Azure CLI.
- On Debian and Ubuntu: `sudo apt install python3.12 python3.12-venv`.
- On Fedora and RHEL: `sudo dnf install python3.12`.

If you just installed it, open a new terminal first so it is on your `PATH`.

## "Python could not create a virtual environment."

**Cause.** The Python that setup found cannot create `.venv`. On Debian and Ubuntu the part of Python that does this is a separate package.

**Fix.** On Debian and Ubuntu, install it (`sudo apt install python3-venv`, or `python3.12-venv` for that version), then run `./scripts/setup.sh` again. Elsewhere, the lines above the message say what went wrong.

If setup says instead "The Python in .venv is older than 3.12.", the folder has a `.venv` from an older Python. Move it aside (`mv .venv .venv.old`) and run setup again.

## "Entrabot's packages could not be installed (see above)."

Or "pip could not be updated in .venv (see above)."

**Cause.** Installing Entrabot's Python packages into `.venv` failed, usually because `pypi.org` could not be reached.

**Fix.** Check the network connection, then run `./scripts/setup.sh` again. Behind a company proxy, set `HTTPS_PROXY` in the terminal first; `pip` reads it. Setup has no proxy setting of its own and has not been tested behind a proxy or TLS inspection.

## `Permission denied` when running a script

**Cause.** The copy of the Entrabot folder lost the scripts' executable bits. Some ZIP tools do this.

**Fix.** Make them executable again, from the Entrabot folder:

```bash
chmod +x scripts/*.sh start-entrabot.sh status.sh
```

Or start a script through `bash`, for example `bash scripts/setup.sh`.

## "ERROR: Unknown option: ..."

Setup prints this, then its help, and exits with code 2. Nothing was changed.

**Cause.** An option that `./scripts/setup.sh` does not take, often an option with a value written with a space instead of `=` (`--use-blueprint APP_ID`). Earlier versions printed the help and exited 0 for an unknown option, so a script that passed one may have looked as if it worked.

**Fix.** Write values with `=`, for example `--use-blueprint=APP_ID` and `--with-upn-suffix=NAME`. `./scripts/setup.sh --help` lists every option.

## "this is Entrabot's code from ..., asked to work on ..., which has its own copy of the code"

Setup, the health check, rotation, teardown or status stops at once with exit code 2. Nothing was changed.

**Cause.** The command is running the code of one copy of the project against the folder of another. The agent reads its settings (`.env`) from the folder its code is in, so the two have to be the same folder. This happens when the wizard is started by hand with `--project-root` pointing somewhere else, or when a folder's `.venv` was built from another copy of the project, for example with `pip install -e .` run inside a git worktree.

**Fix.** Run the command from the folder you mean: `cd` there and run `./scripts/setup.sh`. If that folder's own `./scripts/setup.sh` shows the message, its `.venv` points at another copy: rebuild it there with `.venv/bin/python -m pip install -e .`, then run setup again.

## "Setup can't use this account's key store, where the agent's sign-in key is kept"

The message ends "(the macOS Keychain). Nothing was changed." or "(the Linux keyring). Nothing was changed.", and the lines under it give the reason. Setup stops at its first step, "Checking this computer", with exit code 1.

**Cause.** Setup keeps the agent's sign-in key in your account's key store and nowhere else, so it checks that store before anything is created. The usual reasons it cannot use it:

- **On a Mac**, the login keychain is locked. That is usual in an SSH session.
- **On Linux**, there is no Secret Service keyring (GNOME Keyring or KWallet) running in a D-Bus session this terminal can reach, or it is locked. That is usual on a server with no desktop, and in an SSH session.
- `PYTHON_KEYRING_BACKEND`, or a `keyring` configuration file, selects a store that is not the operating system's own, such as a plain-file one. Entrabot refuses those and names the one it found ("insecure backend selected: ...").

Setup never falls back to keeping the key in a file.

**Fix.**

- On a Mac, unlock the login keychain in this session, then run setup again:

    ```bash
    security unlock-keychain
    ```

    It asks for your login password. Or run setup at the Mac's own desktop.

- On Linux with a desktop, sign in to the desktop session (which starts and unlocks its keyring) and run setup in a terminal there. On a server with no desktop, follow [Preparing a Linux host](../platform-docs/platform-linux.md#preparing-a-linux-host), which starts a keyring in a D-Bus session and unlocks it.
- If `PYTHON_KEYRING_BACKEND` is set, remove it (`unset PYTHON_KEYRING_BACKEND`) and run setup again.

## "The test could not read the agent's key from the macOS Keychain in this session."

Or "... from the Linux keyring in this session." It comes at the end-to-end test, and setup finishes "with something to check" (exit code 3).

**Cause.** The agent is set up, but the test, which signs in as the agent, could not read the agent's key in this session. That is usually the session (an SSH session whose key store is locked, or one that locked while setup ran), not the setup.

**Fix.** Unlock the key store in this session (on a Mac, `security unlock-keychain`; on Linux, see [Preparing a Linux host](../platform-docs/platform-linux.md#preparing-a-linux-host)), or sign in at the computer's own desktop, then run:

```bash
./scripts/setup.sh --diagnose
```

## The agent cannot sign in when started over SSH

`whoami` reports no Agent User session, and the agent's log (`~/.entrabot/logs/entrabot.log`) has "Three-hop flow failed" with a reason that names the key store (for example "Could not read this account's key store, the macOS Keychain").

**Cause.** The agent reads its key from the key store each time it signs in, and in this session the key store is locked or cannot be reached.

**Fix.** Unlock the key store in the same session first, then start the agent from it: on a Mac, `security unlock-keychain` and then `./start-entrabot.sh`; on Linux, start the agent from the shell where you unlocked the keyring (see [Preparing a Linux host](../platform-docs/platform-linux.md#preparing-a-linux-host)). A Mac's login keychain can lock again after a while or when the Mac sleeps, depending on its settings.

## "Microsoft Entra has not started accepting the certificate just registered."

At the end-to-end test setup prints "Microsoft Entra has not started accepting the certificate just registered. That can take a minute or two." and waits, trying again every 10 seconds. If it gives up it says "The agent could not sign in yet: this computer's certificate was registered on the agent's Blueprint moments ago, and Microsoft Entra has not started accepting it yet." and exits with code 3.

**Cause.** This run registered a new certificate for this computer, and Microsoft's sign-in service can take a minute or two longer than the directory to start accepting it. Setup waits about two minutes.

**Fix.** Run `./scripts/setup.sh` again in a few minutes. It keeps the certificate and only repeats the test.

If setup instead stops at "Saving settings" with "Microsoft Entra has not confirmed this computer's certificate yet. Your settings were saved." (exit code 1), the registration itself could not be read back. Wait a minute and run setup again: it checks again, and registers the certificate only if it is missing.

## "The earlier sign-in as ... can no longer be used"

The message goes on "so setup asks you to sign in again."

**Cause.** The Azure CLI on this computer still lists an earlier sign-in, but a real request to your directory showed that Microsoft no longer accepts it: your organization ended the session, or it expired. The Azure CLI would go on sending that sign-in's old token, so setup removes it (`az logout --username`) before you sign in again.

**Fix.** None is needed: sign in when setup asks. Related messages:

- "Microsoft did not accept that sign-in for the directory. Setup clears what the Azure CLI remembered and asks you to sign in once more." Setup then runs `az account clear`, which signs every account out of that Azure CLI, not only this one, and asks once more.
- "Microsoft is not accepting this sign-in for requests to your directory." Setup stops, having changed nothing. Wait a few minutes and run setup again. If it keeps happening, your organization may be ending the session (a sign-in risk or Conditional Access policy): ask your administrator.
- "Your Microsoft sign-in is no longer accepted: your organization ended the session while setup was running." Run `./scripts/setup.sh` again. It asks you to sign in again, then continues from where it stopped and keeps what already works.

To sign in as someone else although the earlier sign-in still works, add `--switch-user`.

## A script in `scripts/` cannot find `az`

A script run by hand, such as `scripts/add_agent_sponsor.py`, stops with `FileNotFoundError` naming `az`, or says it could not look up the Provisioner app.

**Cause.** Setup installed its own private copy of the Azure CLI, in `~/.entrabot/tools/azure-cli`, because there was none on your `PATH`, and it does not add that copy to your `PATH`. Setup, `--diagnose`, rotation, both teardowns and `./status.sh` find it by themselves. The other scripts in `scripts/` look for `az` on your `PATH` only, and they need it to reach Microsoft Entra with the setup (Provisioner) credential.

**Fix.** Put the private copy first on `PATH` for that command:

```bash
PATH="$HOME/.entrabot/tools/azure-cli/bin:$PATH" .venv/bin/python scripts/list_sponsors.py
```

Or add that folder to `PATH` in your shell profile, or install the Azure CLI yourself (`brew install azure-cli` on a Mac, or your distribution's package); setup then uses that one too.

## The sign-in page never appears

**Cause.** On a Mac setup opens the Microsoft sign-in page in your browser, unless you are connected over SSH. On Linux it does so only when there is a desktop (`DISPLAY` or `WAYLAND_DISPLAY` is set). The page may have opened in a browser window you are not looking at.

**Fix.** Look for a new tab or window in your browser. Or sign in with a code instead:

```bash
./scripts/setup.sh --device-code
```

Setup prints a web address and a short code; open the address on any device. Add `--tenant=contoso.com` to sign in to a specific tenant. Over SSH, and on Linux with no desktop, setup uses a code by itself. A code lasts about 15 minutes; see ["The sign-in code was not used in time"](windows.md#the-sign-in-code-was-not-used-in-time).

## Claude Code is not installed

Setup's last screen says, as its first step to start the agent, "Install Claude Code:  https://code.claude.com/docs/en/setup", and `./start-entrabot.sh` says "Claude Code is not installed."

**Cause.** Setup does not install Claude Code on macOS or Linux.

**Fix.** Install it from <https://code.claude.com/docs/en/setup>, open a new terminal, and run `./scripts/setup.sh` again to check the version. `./start-entrabot.sh` also finds Claude Code in `~/.local/bin` and `~/.claude/local`, where its installer puts it, when it is not on your `PATH` yet.

## "Claude Code ... is older than 2.1.295"

**Cause.** The project's two safety checks (Claude Code hooks) are set to block an action when the check itself cannot start. Claude Code understands that setting from version 2.1.295 on. An older version ignores it, so a check that fails to start lets the action through. Setup reads the version with `claude --version`; on an older one it finishes "with something to check" and exits with code 3.

**Fix.** Update Claude Code, then run `./scripts/setup.sh` again to confirm:

```bash
claude update
```

If `claude` is not found, use the command setup printed, which names Claude Code by its full path. If setup says the version could not be read, it does not count that as a problem; update anyway to be sure.

## A computer set up before the wizard

Earlier versions of `setup.sh` kept the agent's private key on its own, in one key store item for the whole account (service `entrabot`, account `blueprint-private-key`), and wrote no `ENTRABOT_BLUEPRINT_CERT_SHA1` to `.env`. Such a computer keeps working as it is.

**What the next `./scripts/setup.sh` does.** At the certificate step it asks Microsoft Entra whether that key's certificate is on this agent's Blueprint, sending only the key's public half. If it is, setup prints "Kept this computer's existing sign-in key, now stored as this agent's own item in the macOS Keychain" (or the Linux keyring), records its thumbprint in `.env`, and creates and registers nothing new. If it is not, the key may be another agent's: setup leaves it alone and creates a new certificate for this agent. The older item stays in place either way.

If setup says "This computer has a sign-in key from an earlier setup, and setup could not check whether it belongs to this agent", Microsoft Entra could not be asked. Nothing was generated or changed. Run setup again in a few minutes.

**Before rotating or disconnecting.** Run `./scripts/setup.sh` once first. Until then:

- `./scripts/setup.sh --rotate-cert` says "This computer's settings don't name a certificate to rotate." and changes nothing.
- `./scripts/teardown.sh` says "This computer was set up by an earlier version of Entrabot's setup, which kept the agent's sign-in key as the only one for this account." and changes nothing (exit code 1), because it cannot tell whether the key is this agent's. After setup has run, teardown deletes the older item too when it holds the same key as the agent's own item. `--tenant-wide` still works on such a computer as it always did.

A key that an older `setup.sh` left under `blueprint-private-key.pending` (a registration that did not finish) is not used by the wizard.

## The agent cannot sign in: certificate missing, expired, or unusable

**Cause.** The agent signs in with a certificate whose key is in your key store. Sign-in fails if that certificate has expired (it is valid for one year), its item was deleted from the keychain or keyring, or the key store cannot be read. The agent logs a warning when 30 days or fewer remain, and `./scripts/setup.sh --diagnose` and `./status.sh` warn too.

**Fix.**

- Before it expires, replace it:

    ```bash
    ./scripts/setup.sh --rotate-cert
    ```

- After it has expired or gone missing, run `./scripts/setup.sh` again. Setup creates and registers a new certificate when the current one is missing, expired, or cannot sign.

Restart the assistant afterwards; the agent reads its certificate when it starts.

## Certificate rotation stopped partway

`./scripts/setup.sh --rotate-cert` exits with:

| Code | Meaning | What to do |
|---|---|---|
| `0` | Rotated. | Restart the assistant. |
| `1` | Failed. The old certificate is still in use and the new one was undone. | Fix what the message names, then run it again. |
| `2` | Run without a terminal and without `--yes`. Nothing was changed. | Run it in a terminal, or add `--yes`. |
| `3` | Rotated; the agent is on the new certificate, but removing the old one did not finish. For example, Microsoft Entra did not yet list the new certificate on the Blueprint, so the old one was left there. | Run it again, in a few minutes. It finishes the cleanup. |

Rotation uses the Azure CLI sign-in that setup made. If it says "You are not signed in to Microsoft.", run `./scripts/setup.sh`, which signs you in, then rotate again.

## `./scripts/teardown.sh --yes` did not delete the agent

**Cause.** `./scripts/teardown.sh` now disconnects this computer only, by default and with `--yes`. Before, it deleted the agent's whole chain from the tenant.

**Fix.** To delete the agent as well, add `--delete-agent`. A test wrapper that needs the older tenant-wide teardown can pass `--tenant-wide`. See the [`teardown.sh` reference](../reference/scripts/teardown/teardown-sh.md).

Related messages, both exit code 2 with nothing changed:

- "Teardown needs a yes before it removes anything." It was run without a terminal and without `--yes`.
- "... belong to two different teardowns and can't be combined." An option of the default teardown (`--delete-agent`, `--purge-data`, `--keep-setup-credential`, `--verbose`, `--log-file`) was given with one of the tenant-wide teardown's (`--tenant-wide`, `--agent-user-upn`, `--dry-run`, `--preserve-provisioner`, `--preserve-local-state`, `--delete-cloud-storage`).

## ".env is NOT restricted to your account"

**Cause.** Setup restricts `.env` to your account (`chmod 600`) and then reads the permissions back. This warning means the read-back found that other accounts can open it, or could not read the permissions.

**Fix.** `.env` holds IDs, not passwords or keys, so setup continues. To restrict it yourself, from the Entrabot folder:

```bash
chmod 600 .env
./scripts/setup.sh --diagnose
```

## "Copying memory to the cloud failed."

The message goes on "Cloud memory is not in sync with this computer." The last screen says "Existing memory was NOT copied to the cloud. To retry, run setup again with --use-cloud-memory." and setup exits with code 1 (earlier versions exited 2).

**Cause.** The most common cause is that the agent's access to the new storage has not taken effect yet.

**Fix.** Wait a few minutes, then run `./scripts/setup.sh --use-cloud-memory` at a terminal and answer yes to "Copy it to the cloud storage now?". Setup offers the copy only in a run that sets up cloud memory, which is what `--use-cloud-memory` asks for; an agent already on cloud memory is otherwise left as it is. The copy keeps the local files and skips what is already in the cloud. See [Storage](storage.md#local-to-blob-migration-reports-errors).

## Messages explained on the Windows page

These entries on the [Windows](windows.md) page apply on macOS and Linux as they are:

- ["The sign-in code was not used in time"](windows.md#the-sign-in-code-was-not-used-in-time)
- [Setup says your account lacks admin rights, or consent fails](windows.md#setup-says-your-account-lacks-admin-rights-or-consent-fails)
- ["An app registration with the Provisioner's name already exists ..."](windows.md#an-app-registration-with-the-provisioners-name-already-exists)
- ["The value given in ENTRABOT_PROVISIONER_APP_ID_VERIFIED was not accepted"](windows.md#the-value-given-in-entrabot_provisioner_app_id_verified-was-not-accepted)
- ["Setup could not read what it needed from Microsoft Entra"](windows.md#setup-could-not-read-what-it-needed-from-microsoft-entra)
- ["The Blueprint App ID ... does not exist in this tenant"](windows.md#the-blueprint-app-id-does-not-exist-in-this-tenant)
- ["The name chosen for the agent already belongs to a different agent."](windows.md#the-name-chosen-for-the-agent-already-belongs-to-a-different-agent)
- ["No Teams licence", or `Graph returned 403 for /me/chats`](windows.md#no-teams-licence-or-graph-returned-403-for-mechats)
- The Word, Excel and OneDrive entries, from ["Word, Excel and OneDrive files were asked for, but they are not switched on."](windows.md#word-excel-and-onedrive-files-were-asked-for-but-they-are-not-switched-on) to ["... the agent has no Microsoft 365 Copilot licence"](windows.md#word-excel-and-onedrive-files-are-switched-on-but-the-agent-has-no-microsoft-365-copilot-licence)
- [The agent ignores a person in Teams](windows.md#the-agent-ignores-a-person-in-teams)
- ["another instance is already running"](windows.md#another-instance-is-already-running)

Read them with this system's commands:

| On the Windows page | On macOS and Linux |
|---|---|
| `scripts\setup-windows.cmd` | `./scripts/setup.sh` |
| `-Diagnose`, `-Yes`, `-DeviceCode`, `-SkipSmoke` | `--diagnose`, `--yes`, `--device-code`, `--skip-smoke` |
| `-NewChain -UpnSuffix <name>` | `--new --with-upn-suffix=<name>` |
| `-UseBlueprint <App ID>`, `-AgentUserUpn <address>` | `--use-blueprint=<App ID>`, `--agent-user-upn=<address>` |
| `-Sponsor <email>`, `-Tenant <domain>` | `--sponsor=<email>`, `--tenant=<domain>` |
| `-ConfigureA365WorkIq` | `--configure-a365-work-iq` |
| `.\.venv\Scripts\python.exe scripts\add_agent_sponsor.py` | `.venv/bin/python scripts/add_agent_sponsor.py` |
| `scripts\deploy-windows.ps1` | `./scripts/setup.sh --rotate-cert` |
| `scripts\teardown-windows.ps1`, with `-DeleteAgent`, `-PurgeData`, `-KeepSetupCredential` | `./scripts/teardown.sh`, with `--delete-agent`, `--purge-data`, `--keep-setup-credential` |
| `start-entrabot.cmd` | `./start-entrabot.sh` |
| `%LOCALAPPDATA%\entrabot` | `~/.entrabot` |
| Windows certificate store | macOS Keychain, or Linux keyring |

To set `ENTRABOT_PROVISIONER_APP_ID_VERIFIED` for the next command in a terminal, `export ENTRABOT_PROVISIONER_APP_ID_VERIFIED='<application ID>'`; to clear it, `unset ENTRABOT_PROVISIONER_APP_ID_VERIFIED`.

## I want to remove Entrabot from this computer

```bash
./scripts/teardown.sh
```

By default this disconnects this computer only: its certificate is removed from the agent's Blueprint and deleted from the key store with its key, the `entrabot` entry is removed from the assistants' configuration, `.env` and `.entrabot-state.json` are backed up and removed, and this computer's setup certificate is retired (add `--keep-setup-credential` if you are about to set up again). The agent, its memory, and `~/.entrabot` are kept. `--delete-agent` also deletes the agent from the tenant; `--purge-data` also deletes the data folder. See the [`teardown.sh` reference](../reference/scripts/teardown/teardown-sh.md).

## Reference: the certificate settings in `.env`

| Variable | Format | Purpose |
|---|---|---|
| `ENTRABOT_BLUEPRINT_CERT_THUMBPRINT` | Base64url SHA-256 | JWT assertion header `x5t#S256` |
| `ENTRABOT_BLUEPRINT_CERT_SHA1` | 40-character hexadecimal SHA-1 | Names the key store item that holds the certificate and its key (service `entrabot-blueprint-cert`) |
| `ENTRABOT_BLUEPRINT_KSP` | `keychain` or `keyring` | Where that item is |

Setup writes all three. Do not edit them by hand; run setup again if `.env` and the key store disagree.

See [macOS Platform APIs](../platform-docs/platform-macos.md), [Linux Platform APIs](../platform-docs/platform-linux.md) and [Identity Lifecycle](../guides/identity-lifecycle.md).
