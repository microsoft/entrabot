# Windows Installation

Setup on Windows is one script. It needs nothing installed first: no PowerShell 7, no Python, no Azure CLI, no `winget`, and no administrator rights on the PC.

## Set up your agent

1. **Get the Entrabot folder onto your PC.** Either download the ZIP from <https://github.com/microsoft/entrabot> (Code > Download ZIP) and extract it, or clone it:

    ```powershell
    git clone https://github.com/microsoft/entrabot.git
    ```

2. **Double-click `scripts\setup-windows.cmd`**, or run it from a terminal in the Entrabot folder:

    ```bat
    scripts\setup-windows.cmd
    ```

3. **Answer the questions.** Every question has a suggested answer; pressing Enter accepts it. Nothing is changed in your tenant until you confirm the review screen.

4. **Start the agent.** Double-click `start-entrabot.cmd` in the Entrabot folder. When setup runs at the PC's own desktop it offers to do this for you as its last question.

Running `scripts\setup-windows.cmd` again is how you repair or change a setup. It keeps what already works.

## What you need

- **Windows 10 or 11.** Setup uses the Windows PowerShell 5.1 that ships with Windows. Run it on native Windows, not inside WSL (WSL users run `scripts/setup.sh`).
- **A Microsoft 365 tenant and an account that administers it.** Creating a new agent grants admin consent, which needs a Global Administrator or Privileged Role Administrator. See [Limits](#requirements-and-limits) for what joining an existing agent needs.
- **A licence that includes Teams, free for the agent to use.** Setup finishes without one and says so, but the agent cannot use Teams until it has one.
- **An Azure subscription, only if you choose cloud memory.**
- **Claude Code 2.1.295 or later, if you use Claude Code.** The project's two safety checks (Claude Code hooks) block an action when they fail to start only from that version on; an older Claude Code lets the action through. Setup checks the version. On an older one it finishes "with something to check" (exit code 3) and prints the update command.

## What the wizard asks

| Question | Suggested answer | Notes |
|---|---|---|
| Install the Azure CLI? | Yes | Only asked if it is missing. A private copy goes into Entrabot's data folder. |
| Use this account? | Yes | Shown when you are already signed in. Otherwise a Microsoft sign-in window opens. |
| How do you want to set up your agent? | Create a new agent | Or join an agent that already exists, using its Blueprint App ID. On a PC that is already set up, the suggested answer is to continue with that agent. |
| Short name for your agent | Your Windows user name | The agent's address becomes `entrabot-agent-<name>@<your domain>`. |
| The agent's sponsor | You | The people the agent takes instructions from in Teams. When joining an existing agent, the suggested answer keeps its current sponsors. |
| Memory | On this computer | Cloud memory (Azure Blob Storage) is offered when your account has an Azure subscription in the tenant. |
| Work IQ? | No | Lets the agent read and edit Word, Excel and OneDrive files. Needs extra tools (the `a365` CLI and PowerShell 7); see [`prereqs-windows.ps1`](../reference/scripts/setup/prereqs-windows-ps1.md). |
| Start setting up? | Yes | The review screen. Nothing has been changed before this point. |
| Install Git for Windows and Claude Code now? | Yes | Only asked if one is missing. |
| Start your agent now? | Yes | Only asked at the PC's own desktop, when everything needed to start is in place. |

In a remote session (SSH, for example) setup signs in with a device code instead of a sign-in window: it prints a web address and a short code to enter there.

Setup then creates or connects the agent, creates this PC's certificate, saves settings, registers the agent with Claude Code and GitHub Copilot CLI, and tests that the agent can sign in and reach Teams.

## Run it unattended

Every question can be answered with a parameter. `-Yes` means "never ask"; if an answer is still missing, setup stops with exit code 2 and names the parameter.

```bat
rem A new agent
scripts\setup-windows.cmd -NewChain -UpnSuffix workstation -Yes

rem Join an existing agent from a second PC
scripts\setup-windows.cmd -UseBlueprint <blueprint-app-id> -Yes

rem Check an existing setup. Changes nothing.
scripts\setup-windows.cmd -Diagnose
```

| Exit code | Meaning |
|---|---|
| `0` | Set up, and the end-to-end test passed. |
| `1` | Failed. The message says what to fix. |
| `2` | Conflicting parameters, or an answer was missing under `-Yes`. |
| `3` | Set up, but something needs checking (for example, the agent has no Teams licence, or Claude Code is older than 2.1.295). |
| `130` | Cancelled. |

See the [`setup-windows.ps1` reference](../reference/scripts/setup/setup-windows-ps1.md) for every parameter.

## What it installs and where

Everything is installed for your account only. Nothing is installed machine-wide and nothing asks for administrator approval.

| What | Where | When |
|---|---|---|
| Python 3.12 (private copy; checksum and signature checked before use) | `%LOCALAPPDATA%\entrabot\python` | Only if the PC has no Python 3.12 or newer. |
| The project's Python environment | `.venv` in the Entrabot folder | Always. |
| Azure CLI (private copy, version 2.92.0 or later and below 3) | `%LOCALAPPDATA%\entrabot\tools\azure-cli` | Only if the Azure CLI is missing. |
| Git for Windows | `%LOCALAPPDATA%\Programs\Git` | Only if you say yes. Claude Code needs it for this project. |
| Claude Code | `%USERPROFILE%\.local\bin` | Only if you say yes. |
| Two certificates with non-exportable keys | Your Windows certificate store (`Cert:\CurrentUser\My`) | Always. The keys are held by the PC's TPM when it accepts them. |
| Settings | `.env`, `.entrabot-state.json`, `.mcp.json` in the Entrabot folder | Always. |
| Logs, and local memory | `%LOCALAPPDATA%\entrabot` | Always. |

To undo a setup, see [`teardown-windows.ps1`](../reference/scripts/teardown/teardown-windows-ps1.md).

## Requirements and limits

- **Setup needs an Entra administrator.** A person who is not an administrator of the tenant cannot set up an agent by themselves. Joining an existing agent is not an exception: each PC adds its own credential to the setup (Provisioner) app registration and to the agent's Blueprint, and both need directory rights. Setup checks your roles after sign-in and says what to ask for.
- **The agent needs a Teams-capable licence** to use Teams. Without one the agent can still sign in, and setup ends with exit code 3 and an explanation.
- **Run one assistant at a time.** Setup registers the agent with Claude Code and GitHub Copilot CLI. Only one Entrabot server runs per PC; a second one exits when it starts.
- **Claude Code needs Git for Windows** for this project. Its safety checks run through Git Bash, and without it Claude Code blocks edits and Teams sends.

### What has been tested

Setup was run end to end on one configuration: Windows 11 Enterprise 25H2 (x64) in an Azure virtual machine with a virtual TPM 2.0, using non-administrator local accounts over SSH. On that machine these were exercised:

- The fresh-PC bootstrap, on a PC with no Python, Git or Azure CLI.
- Creating a new agent, joining an existing Blueprint, and re-running to repair.
- The interactive wizard in a real terminal (six questions, Enter at each), and unattended runs with `-Yes`.
- `-Diagnose`.
- TPM key creation, signing and deletion.
- The agent's sign-in, starting the MCP server, and calling `whoami`.
- `.env` restricted to exactly one account, confirmed by reading the permissions back.
- Sponsors: chosen sponsors added on the agent's directory object; joining with no sponsor named adds nobody and lists the current ones; a personal Microsoft account found by its `#EXT#` sign-in name.
- The Claude Code hooks under Git Bash, and the Git for Windows and Claude Code installs, including the `PATH` entry for Claude Code.
- Status, and certificate rotation.
- Teardown: disconnecting one PC from an agent that other machines use, and `-DeleteAgent`.
- The full test suite on Windows (4208 passed, 226 skipped, 0 failed).

Not yet tested:

- Windows on ARM64, and Windows 10.
- Physical TPM chips (only a virtual TPM was used).
- PCs joined to Entra or managed by Intune, and tenants with Conditional Access.
- A desktop session: double-clicking the scripts, the browser sign-in window, and the "Start your agent now?" window.
- `start-entrabot.cmd` starting a signed-in Claude Code session.
- Cloud memory and Work IQ set up through the wizard.
- Teardown with `-PurgeData`.
- The fixes made after a security review (the Claude Code version check, the hooks' UTF-8 input, the persona-sati token command lookup, revoking an older setup key file, and the teardown and rotation safeguards). They are covered by unit tests, but the end-to-end run above was done before them.

If something goes wrong, see [Windows troubleshooting](../troubleshooting/windows.md).

## Next step

Continue to [Verify Your Agent Identity](verify.md).
