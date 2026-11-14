# macOS and Linux Installation

Setup on macOS and Linux is one script. It starts the same setup wizard as Windows. It needs Python 3.12 or later and nothing else installed first: no Azure CLI, no `az login`, no Git, and no `sudo`.

!!! warning "Only partly run against a real tenant on macOS"
    The wizard has been run against a real tenant on Ubuntu 24.04: setting up and joining an agent, the health check, certificate rotation, a computer set up before the wizard, and teardown. On macOS a repair run and the health check have been run against a real, older setup; a new agent, rotation and teardown have not. A few paths have not been run on either system. See [What has been tested](#what-has-been-tested).

## Set up your agent

1. **Get the Entrabot folder onto your computer.** Either download the ZIP from <https://github.com/microsoft/entrabot> (Code > Download ZIP) and extract it, or clone it:

    ```bash
    git clone https://github.com/microsoft/entrabot.git
    cd entrabot
    ```

2. **Run setup** from a terminal in the Entrabot folder:

    ```bash
    ./scripts/setup.sh
    ```

    The first run builds the project's Python environment (`.venv`), which takes a minute or two, then starts the wizard.

3. **Answer the questions.** Every question has a suggested answer; pressing Enter accepts it. Nothing is changed in your tenant until you confirm the review screen.

4. **Start the agent.** Run `./start-entrabot.sh` in the Entrabot folder. At a terminal, setup offers to do this for you as its last question, in the same terminal. See [The first start](#the-first-start) for what Claude Code asks.

Running `./scripts/setup.sh` again is how you repair or change a setup. It keeps what already works. On a computer that is already set up it first asks whether to continue with that agent (it checks everything and repairs what is broken), set up a different agent, or only run a health check.

## What you need

- **macOS or Linux, and your own account on it.** Setup installs nothing for the whole computer and never runs `sudo`.
- **Python 3.12 or later.** Setup looks for `python3.13`, `python3.12`, `python3.14` and then `python3`, and uses the first one that is 3.12 or newer. If there is none it says how to get one and stops:
    - On a Mac with Homebrew: `brew install python@3.12`, or [`./scripts/prereqs-macos.sh`](../reference/scripts/setup/prereqs-macos-sh.md), which also installs the Xcode Command Line Tools, Git and the Azure CLI.
    - On Debian and Ubuntu: `sudo apt install python3.12 python3.12-venv`. On Fedora and RHEL: `sudo dnf install python3.12`.
- **A key store for the agent's sign-in key.** On a Mac, your login keychain, unlocked. On Linux, a Secret Service keyring (GNOME Keyring or KWallet) that is running and unlocked; a desktop session normally has one, and a server with no desktop needs one first. Setup checks this before anything else, and stops without changing anything if there is none. See [The key store](#the-key-store).
- **A Microsoft 365 tenant and an account that administers it.** Creating a new agent grants admin consent, which needs a Global Administrator or Privileged Role Administrator. See [Limits](#requirements-and-limits) for what joining an existing agent needs.
- **A licence that includes Teams, free for the agent to use.** Like a person, the agent needs a licence of its own. Setup gives it a free one by itself; when none is free, it walks you through getting one (see [Licences for the agent](#licences-for-the-agent)). It finishes without one if need be, but the agent cannot use Teams until it has one.
- **For Word, Excel and OneDrive files (optional):** a Microsoft 365 Copilot licence for the agent as well.
- **An Azure subscription, only if you choose cloud memory.**
- **Claude Code 2.1.295 or later, if you use Claude Code.** Setup does not install Claude Code on macOS or Linux. If it is missing, the first step on setup's last screen is to install it from <https://code.claude.com/docs/en/setup>. The project's two safety checks (Claude Code hooks) block an action when they fail to start only from version 2.1.295 on; an older Claude Code lets the action through. Setup checks the version. On an older one it finishes "with something to check" (exit code 3) and prints the update command.

You do not need the Azure CLI (setup offers to install a private copy), `az login` (setup signs you in), Git (download the ZIP instead), or Microsoft's Agent 365 CLI, the .NET SDK and PowerShell 7 (setup switches on Word, Excel and OneDrive files itself).

## What the wizard asks

| Question | Suggested answer | Notes |
|---|---|---|
| Install it now? | Yes | Only asked when the Azure CLI is missing. A private copy is installed with `pip` into `~/.entrabot/tools/azure-cli`: no `sudo`, no Homebrew. An Azure CLI already on your `PATH` is used as it is. |
| Use this account? | Yes | Shown when you are already signed in to the Azure CLI and Microsoft still accepts that sign-in (setup checks). Otherwise setup signs you in (see below). |
| What would you like to do? | Continue with your agent | Only on a computer that is already set up. Or set up a different agent on this computer, or only run a health check, which changes nothing. |
| How do you want to set up your agent? | Create a new agent | Or join an agent that already exists, using its Blueprint App ID. When joining, setup also asks for the agent's address; press Enter to let setup find it. |
| Short name for your agent | Your user name | The agent's address becomes `entrabot-agent-<name>@<your domain>`. |
| The agent's sponsor | You | The people the agent takes instructions from in Teams. When joining an existing agent, the suggested answer keeps its current sponsors. |
| Memory | On this computer | Cloud memory (Azure Blob Storage) is offered when your account has an Azure subscription in the tenant. Then it asks which subscription, when there are several, and the Azure region (`eastus`). |
| Switch on Word, Excel and OneDrive files? | No | Work IQ: the agent reads and edits Word, Excel and OneDrive files. Setup says first that the agent then needs a Microsoft 365 Copilot licence of its own. Most people skip this. There is nothing to install for them, and setup switches them on itself, over SSH and unattended too. |
| Start setting up? | Yes | The review screen. Nothing has been changed before this point, apart from an Azure CLI you agreed to install. |
| Set up the agent's licence now? | Yes | Only asked when the agent has no licence that includes Teams, or no Microsoft 365 Copilot licence while Word, Excel and OneDrive files are on. See [Licences for the agent](#licences-for-the-agent). |
| Give the agent one ... licence? | Yes | When a licence is free. With several, a numbered choice; the suggested one also brings mail and files. |
| Press Enter when that is done ..., or type s to skip | Enter | When no licence is free: after someone adds or frees one, Enter checks again; `s` leaves it for later. |
| Start your agent now, in this terminal? | Yes | Only asked at a terminal, when everything needed to start is in place. Setup then finishes and starts `./start-entrabot.sh` in its place. |

A few questions appear only when they apply: "Continue anyway?" (default No) when your account holds none of the administrator roles setup needs; "Try again with a sign-in code instead?" when a browser sign-in did not complete; "Replace them all?" (default No) with `--remove-all-existing-certs`; and, for cloud memory, "Continue with memory on this computer for now?" when the storage could not be created, and "Copy it to the cloud storage now?" (default No; the local copy is kept) when this computer already holds memory.

**Signing in.** On a Mac setup signs in through your browser, unless you are connected over SSH. On Linux it uses the browser when there is a desktop (`DISPLAY` or `WAYLAND_DISPLAY` is set). Otherwise it signs in with a code: it prints a web address and a short code to enter there, on any device. `--device-code` asks for a code anyway.

Before it offers an earlier Azure CLI sign-in, setup checks it with a real request to your directory. One that Microsoft no longer accepts is removed from the Azure CLI (`az logout --username`) before setup asks you to sign in again, because the Azure CLI would otherwise go on sending its old token. `--switch-user` asks for a new sign-in even when the earlier one still works. If your organization ends the session while setup is running, setup says so in plain words and stops; run it again to sign in again and continue.

Setup then creates or connects the agent, creates this computer's certificate with its key in your key store, saves settings, registers the agent with Claude Code and GitHub Copilot CLI, and tests that the agent can sign in and reach Teams. When this run registered a new certificate, the test waits up to about two minutes for Microsoft Entra to start accepting it. After that test, and only if you switched them on, it sets up Word, Excel and OneDrive files: it lists their tools for the agent in `ToolingManifest.json`, gives the agent's Blueprint the permissions they need, and then checks, as the agent, that Microsoft honours them, waiting up to about two minutes. A failure there never stops setup, and the last screen says why and what to do. Nothing is installed for them: no Agent 365 CLI, no .NET SDK, no PowerShell 7, and no `a365.config.json`.

## Licences for the agent

The licence walkthrough is the same as on Windows; see [Licences for the agent](windows.md#licences-for-the-agent) on the Windows page. In short: after the end-to-end test, under **Licences for Microsoft 365**, setup shows your organization's licences and what each gives the agent, gives the agent a free one when you say yes, and otherwise says who can add or free one and where in the [Microsoft 365 admin center](https://admin.microsoft.com), then checks again when you press Enter (type `s` to leave it for later). Run unattended, setup asks nothing: it only reads the organization's licences, so the last screen can say what to do. Details are in [Windows troubleshooting](../troubleshooting/windows.md#no-teams-licence-or-graph-returned-403-for-mechats); the steps there are the same on every system.

## The first start

The first time Claude Code opens the Entrabot folder it asks some questions; `./start-entrabot.sh` and setup's last screen both say so. If this computer has not used Claude Code before, it asks you to sign in to Claude. Then it asks whether to trust this folder, whether to use the `entrabot` MCP server, and whether to load a development channel. Say yes to each: the folder is the one you just set up, the server is your agent, and the channel is how Teams messages reach it. Then say *Who are you?*

`./start-entrabot.sh` runs `claude --dangerously-load-development-channels server:entrabot` in the Entrabot folder, from wherever you start it, and passes anything you add on to Claude Code (for example `./start-entrabot.sh --resume`). See the [`start-entrabot.sh` reference](../reference/scripts/setup/start-entrabot-sh.md).

Setup's last screen ends with things to try, matched to what the agent has:

- *Send me a Teams message to say hello*, then answer it in Teams. A licence assigned in this run can take 10-15 minutes to reach Teams.
- *Send me an email that says what you can do*. This needs a licence that includes Outlook mail.
- *Create a Word document called Hello and share it with me*, only when Word, Excel and OneDrive files are switched on and the agent has its Microsoft 365 Copilot licence.

When the agent has no licence yet, Teams and email are one item for later: once it has its licence, ask it to message you in Teams and email you.

## Run it unattended

Every question can be answered with a flag. At a terminal, a command with flags still asks the questions the flags leave open, and shows the review screen; `--yes` means "never ask". If an answer is still missing under `--yes`, setup stops with exit code 2 and names the flag. Without a terminal (a script, or `ssh host command`), setup never asks: it uses the flags and the suggested answers.

```bash
# A new agent
./scripts/setup.sh --new --with-upn-suffix=workstation --yes

# Join an existing agent from a second computer
./scripts/setup.sh --use-blueprint=<blueprint-app-id> --yes

# Repair the agent this computer is set up for
./scripts/setup.sh --yes

# Check an existing setup. Changes nothing.
./scripts/setup.sh --diagnose
```

A flag that takes a value is written with `=`: `--use-blueprint=APP_ID`, not `--use-blueprint APP_ID`.

| Flag | Answers |
|---|---|
| `--new` and `--with-upn-suffix=NAME` | Create a new agent, `entrabot-agent-NAME@<your domain>` |
| `--use-blueprint=APP_ID` | Join an agent that already exists |
| `--agent-user-upn=UPN` | The agent's exact address |
| `--sponsor=EMAIL` | Who the agent takes instructions from in Teams. Repeat it, or separate addresses with commas |
| `--tenant=NAME_OR_ID`, `--device-code` | How to sign in |
| `--keep-memory-local` or `--use-cloud-memory` | Where the agent keeps its memory |
| `--configure-a365-work-iq` | Switch on Word, Excel and OneDrive files |
| `--hosts=claude,copilot` | Which assistants to register the agent with |
| `--skip-smoke` | Skip the end-to-end test |

| Exit code | Meaning |
|---|---|
| `0` | Set up, and the end-to-end test passed. |
| `1` | Failed. The message says what to fix. |
| `2` | An unknown or conflicting flag, or an answer was missing under `--yes`. |
| `3` | Set up, but something needs checking (for example, the agent has no Teams licence, or one given in this run has not reached Teams yet; Microsoft Entra has not started accepting a new certificate yet; Claude Code is older than 2.1.295; or Word, Excel and OneDrive files were asked for and do not work yet). |
| `130` | Cancelled. |

See the [`setup.sh` reference](../reference/scripts/setup/setup-sh.md) for every flag.

### If you used setup.sh before

Every flag `setup.sh` took still works. These now behave differently:

- **No flags now works.** Setup asks. Before, it needed `--new` or `--use-blueprint`.
- **`--teams-user=EMAIL` is another spelling of `--sponsor=EMAIL`,** and the person is now also added as a sponsor of the agent in the directory, which is the list the agent obeys. Before, it only wrote `.env`.
- **`--with-a365-work-iq` is the same as `--configure-a365-work-iq`** and prints a note. `--a365-agent-name=` is accepted and ignored.
- **An unknown option is an error** (exit code 2). It used to print the help and exit 0.
- **A value may follow its option after a space or after `=`:** `--tenant contoso.com` and `--tenant=contoso.com` are the same.
- **Exit codes are the wizard's** (above). A failed copy of memory to the cloud used to exit 2 and now exits 1.
- **`./scripts/teardown.sh` disconnects this computer only,** with `--yes` too. Before, `./scripts/teardown.sh --yes` deleted the agent from the tenant. To delete the agent now, add `--delete-agent`. See [`teardown.sh`](../reference/scripts/teardown/teardown-sh.md).

## What it installs and where

Everything is for your account only. Nothing is installed for the whole computer, and nothing needs `sudo`. Word, Excel and OneDrive files need nothing installed.

| What | Where | When |
|---|---|---|
| The project's Python environment, with the `provisioning` extra | `.venv` in the Entrabot folder | Always. A later run reinstalls only when `pyproject.toml` has changed (recorded in `.venv/.entrabot-setup-stamp`). |
| Azure CLI (private copy, version 2.92.0 or later and below 3, installed with `pip`) | `~/.entrabot/tools/azure-cli` | Only if no Azure CLI is on your `PATH` and you say yes. It is not added to your `PATH`: setup, rotation, teardown and `./status.sh` find it by themselves, but other scripts you run by hand do not (see [troubleshooting](../troubleshooting/macos-linux.md#a-script-in-scripts-cannot-find-az)). |
| The agent's certificate and its private key, as one item | Your login keychain (macOS) or Secret Service keyring (Linux), service `entrabot-blueprint-cert` | Always. See [The key store](#the-key-store). |
| The setup (Provisioner) certificate and its private key | The same key store, service `entrabot-provisioner-cert`, one per tenant | Always. |
| Settings | `.env` (readable only by your account), `.entrabot-state.json` and `.mcp.json` in the Entrabot folder; GitHub Copilot CLI's `~/.copilot/mcp-config.json` | Always (each assistant's file only when the agent is registered with it). |
| The agent's list of Word, Excel and OneDrive tools | `ToolingManifest.json` in the Entrabot folder | Only when you switch on Word, Excel and OneDrive files. |
| Logs, and local memory | `~/.entrabot` | Always. |

Setup replaces only its own lines in `.env`; lines you added by hand are kept. To undo a setup, see [`teardown.sh`](../reference/scripts/teardown/teardown-sh.md).

## The key store

The agent proves who it is by signing with a private key that stays on this computer. On macOS and Linux setup keeps that key, together with its certificate, in your account's key store: the login keychain on a Mac, a Secret Service keyring on Linux. Each certificate is its own item, named by the certificate's SHA-1 thumbprint, which `.env` records as `ENTRABOT_BLUEPRINT_CERT_SHA1`. Two agents set up under one account are two items, so neither overwrites the other's key.

- **Checked first.** The first step, "Checking this computer", makes sure setup can use the key store, and stops before anything is created if it cannot. Setup never falls back to keeping the key in a file, and the agent does not sign in when its key store cannot be read.
- **On a Mac over SSH** the login keychain is usually locked. Unlock it in that session with `security unlock-keychain` (it asks for your login password) before you run setup, and again before you start the agent there: the agent reads its key each time it signs in. Or run both at the Mac's own desktop.
- **On Linux** the keyring has to be running and unlocked in a D-Bus session that setup and the agent can reach. A desktop session has one. On a server with no desktop, see [Preparing a Linux host](../platform-docs/platform-linux.md#preparing-a-linux-host).
- **It is not hardware-backed.** Anything that can read your keychain or keyring, as you, can read the key. On Windows the key cannot be exported at all. See [Security Boundaries](../architecture/security-boundaries.md).
- **Health checks.** `./scripts/setup.sh --diagnose` checks that the certificate is there, in date, and that its key signs. `./status.sh` and the agent itself check that it is there with its key and in date. All three warn when 30 days or fewer remain. The certificate is valid for one year.
- **Rotation.** `./scripts/setup.sh --rotate-cert` replaces this computer's certificate. The current one stays in use until the new one has signed the agent in. It takes the same steps, and exits with the same codes, as [`deploy-windows.ps1`](../reference/scripts/setup/deploy-windows-ps1.md) on Windows.

**A computer set up before the wizard** keeps working as it is. Its key is on its own in the account's single key store item (service `entrabot`, account `blueprint-private-key`), and its `.env` names no SHA-1 thumbprint. The next `./scripts/setup.sh` keeps that key: when the key's certificate is on the agent's Blueprint, setup stores the two together as the agent's own item, and creates and registers nothing new. Otherwise it creates a new certificate for the agent, and leaves the older item alone, since it may be another agent's. The older item stays where it is either way; teardown deletes it only when it holds the same key as the agent's own item. Run setup once before you rotate the certificate or disconnect such a computer: both need the certificate named in `.env`, and say so.

**Going back to an older copy of the project** after the wizard has run works, as long as the older single item is still there: older code ignores the SHA-1 in `.env` and signs with that item, and the wizard leaves it in place when it takes a key over. Three things remove it, because each of them retires the key it holds: rotating the certificate (`--rotate-cert`), replacing every certificate (`--remove-all-existing-certs`), and teardown. After any of those, an older copy of the project has no key and its own setup has to be run again. Deleting the agent's certificate by hand with `entrabot.platform.keycert.delete_cert` removes the older item too when it holds the same key; to remove only the new item, delete the key store item itself (service `entrabot-blueprint-cert`).

## Requirements and limits

- **Setup needs an Entra administrator.** A person who is not an administrator of the tenant cannot set up an agent by themselves. Joining an existing agent is not an exception: each computer adds its own credential to the setup (Provisioner) app registration and to the agent's Blueprint, and both need directory rights. Setup checks your roles after sign-in and says what to ask for.
- **The agent needs a licence that includes Teams** to use Teams. Without one the agent can still sign in, and setup ends with exit code 3, after offering the [licence walkthrough](#licences-for-the-agent).
- **Run one assistant at a time.** Setup registers the agent with Claude Code and GitHub Copilot CLI. Only one Entrabot server runs per computer; a second one exits when it starts.
- **The key store must be usable in the session** that runs setup and the agent: see [The key store](#the-key-store). Over SSH or in another remote session, sign-in also uses a code instead of the browser.
- **Claude Code is not installed by setup** on macOS or Linux. The hooks run with the `bash` on your `PATH`; Git is not needed for them.

### What has been tested

Run against a real tenant, on Linux: Ubuntu 24.04.5 on a server with no desktop (bash 5.2, Python 3.12.3, GNOME Keyring unlocked inside `dbus-run-session`, no Claude Code installed), signed in as a tenant administrator, with throwaway agents that were deleted afterwards.

- **A new agent.** `./scripts/setup.sh --new --with-upn-suffix=NAME --yes` from a folder with no `.venv` and no setup certificate in the keyring: the environment was built, a setup certificate was created and used in the same run, and all eleven steps ran. Also with `--configure-a365-work-iq`: Word, Excel and OneDrive files were switched on. Each run ended with exit code 3, because the tenant had no free Teams or Microsoft 365 Copilot licence, and the last screen said which licence was missing and what to do.
- **At a terminal**, as a repair run: every question appeared and took its answer, and the licence walkthrough listed the tenant's products by name; "check again" stopped by itself after ten checks.
- **Health check and status.** `./scripts/setup.sh --diagnose` and `./status.sh --health-only`, including the check that the certificate is in the keyring, in date, and that its key signs.
- **Certificate rotation.** `./scripts/setup.sh --rotate-cert --yes`: the new certificate was added beside the old one, signed the agent in, and the old one was removed from the Blueprint and the keyring.
- **Joining an agent from a second folder** on the same account (`--use-blueprint=APP_ID`): each folder got its own certificate and key, and both signed in.
- **Replacing every certificate** (`--remove-all-existing-certs`): the other folder was cut off, its health check said the certificate was not registered, and a plain `./scripts/setup.sh --yes` there registered it again.
- **A computer set up before the wizard.** The `setup.sh` from before the wizard set up an agent (key alone in the account's single slot, no SHA-1 in `.env`); the current `setup.sh` run over that folder kept the key, stored it as the agent's own item, and registered nothing new.
- **Setup's own Azure CLI.** With no Azure CLI on the computer, setup installed its private copy and reused the earlier sign-in; `./status.sh` and the health check found that copy by themselves.
- **Teardown.** Disconnecting one folder (the agent kept signing in from the other, and the setup certificate was kept for it); `--delete-agent --purge-data`; and the tenant-wide teardown targeted at one agent (`--agent-user-upn=`, with and without `--dry-run`). Afterwards no agent was left in the tenant and no key in the keyring.
- **Cloud memory, the failure only.** `--use-cloud-memory` stopped at the role assignment, because the signed-in account may not assign roles in that subscription. Setup showed Azure's error, kept memory on the computer and carried on.

Covered without a real tenant:

- The unit and script tests, with the outside world faked: the wizard's questions, flags and messages on macOS and Linux, the key store check before anything changes, the certificate step, rotation, teardown, and starting the agent. The full test suite passes (`pytest` and `ruff`).
- `scripts/setup.sh`, `scripts/teardown.sh` and `start-entrabot.sh`, run for real under the bash 3.2 that ships with macOS, against a stub interpreter and a stub Claude Code: every older flag translated for the wizard, a value passed on exactly as typed, `.venv` built only when needed, and each teardown flag routed to the right teardown.
- The Keychain store, round-tripped for real on macOS: creating a certificate and its key, finding it, test-signing with it, reading it back, and deleting it.

Run against a real tenant, on macOS (macOS 27, Apple silicon), on a copy of a real setup made by the `setup.sh` from before the wizard, with cloud memory and Word, Excel and OneDrive files already on:

- **The health check** (`diagnose`): the key was found in the account's older single Keychain item, the agent signed in, and Teams and the Word, Excel and OneDrive scopes passed.
- **A repair run** (`./scripts/setup.sh --yes --hosts=claude`): setup recognised the existing agent, kept its key and stored it as the agent's own Keychain item, registered nothing new, left cloud memory as it was, and passed the end-to-end test. `.env` gained the two certificate lines and lost nothing; the other servers in `.mcp.json` were kept. The older Keychain item was untouched, and the older code still passed its own health check afterwards.

Not yet done:

- On macOS: a new agent, anything at a terminal (the questions), certificate rotation, teardown, and the bootstrap reusing an older `.venv` in place.
- Cloud memory set up to the end, and copying existing memory to it.
- Giving the agent a licence (the tenant had no free one).
- A first run that has to wait for a new setup certificate to be accepted: Microsoft accepted it at once on every run, so the wait itself is covered by unit tests only.
- Starting the agent from the last screen, and Claude Code itself: none was installed on the test computer.
- Linux with a desktop, any distribution other than Ubuntu 24.04, and signing in over SSH on a Mac.

If something goes wrong, see [macOS and Linux troubleshooting](../troubleshooting/macos-linux.md).

## Next step

Continue to [Verify Your Agent Identity](verify.md).
