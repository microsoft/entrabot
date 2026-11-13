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

4. **Start the agent.** Double-click `start-entrabot.cmd` in the Entrabot folder. When setup runs at the PC's own desktop it offers to do this for you as its last question. See [The first start](#the-first-start) for what Claude Code asks.

Running `scripts\setup-windows.cmd` again is how you repair or change a setup. It keeps what already works.

## What you need

- **Windows 10 or 11.** Setup uses the Windows PowerShell 5.1 that ships with Windows. Run it on native Windows, not inside WSL (WSL users run `scripts/setup.sh`).
- **A Microsoft 365 tenant and an account that administers it.** Creating a new agent grants admin consent, which needs a Global Administrator or Privileged Role Administrator. See [Limits](#requirements-and-limits) for what joining an existing agent needs.
- **A licence that includes Teams, free for the agent to use.** Like a person, the agent needs a licence of its own. Setup gives it a free one by itself; when none is free, it walks you through getting one (see [Licences for the agent](#licences-for-the-agent)). It finishes without one if need be, but the agent cannot use Teams until it has one.
- **For Word, Excel and OneDrive files (optional):** a Microsoft 365 Copilot licence for the agent as well.
- **An Azure subscription, only if you choose cloud memory.**
- **Claude Code 2.1.295 or later, if you use Claude Code.** The project's two safety checks (Claude Code hooks) block an action when they fail to start only from that version on; an older Claude Code lets the action through. Setup checks the version. On an older one it finishes "with something to check" (exit code 3) and prints the update command.

## What the wizard asks

| Question | Suggested answer | Notes |
|---|---|---|
| Install the Azure CLI? | Yes | Only asked if it is missing. If your account is a local administrator and `winget` is present, setup installs it with `winget` (the standard install, for the whole PC; Windows may ask for permission). Otherwise, or if that does not work, a private copy goes into Entrabot's data folder. |
| Use this account? | Yes | Shown when you are already signed in and Microsoft still accepts that sign-in (setup checks). Otherwise your browser opens for you to sign in to Microsoft. |
| How do you want to set up your agent? | Create a new agent | Or join an agent that already exists, using its Blueprint App ID. On a PC that is already set up, the suggested answer is to continue with that agent. |
| Short name for your agent | Your Windows user name | The agent's address becomes `entrabot-agent-<name>@<your domain>`. |
| The agent's sponsor | You | The people the agent takes instructions from in Teams. When joining an existing agent, the suggested answer keeps its current sponsors. |
| Memory | On this computer | Cloud memory (Azure Blob Storage) is offered when your account has an Azure subscription in the tenant. |
| Switch on Word, Excel and OneDrive files? | No | Work IQ: the agent reads and edits Word, Excel and OneDrive files. Setup says first that the agent then needs a Microsoft 365 Copilot licence of its own. Most people skip this. They can only be switched on at the PC's own desktop or over Remote Desktop, because one step opens a Microsoft sign-in window; over SSH setup can install their tools but not switch them on. |
| Install them now? | Yes | Only asked when Word, Excel and OneDrive files are switched on and their tools are missing: the Microsoft Agent 365 CLI (and the .NET SDK it runs on, when the PC has none) and PowerShell 7. Installed for your account only, with no administrator rights, in a few minutes; about 1 GB. See [`prereqs-windows.ps1`](../reference/scripts/setup/prereqs-windows-ps1.md). Say no and they stay off; the last screen says what to do. |
| Start setting up? | Yes | The review screen. Nothing has been changed before this point, apart from tools you agreed to install. |
| Install Git for Windows and Claude Code now? | Yes | Only asked if one is missing. |
| Set up the agent's licence now? | Yes | Only asked when the agent has no licence that includes Teams, or no Microsoft 365 Copilot licence while Word, Excel and OneDrive files are on. See [Licences for the agent](#licences-for-the-agent). |
| Give the agent one ... licence? | Yes | When a licence is free. With several, a numbered choice; the suggested one also brings mail and files. |
| Press Enter when that is done ..., or type s to skip | Enter | When no licence is free: after someone adds or frees one, Enter checks again; `s` leaves it for later. |
| Start your agent now? | Yes | Only asked at the PC's own desktop, when everything needed to start is in place. |

Setup signs in through your browser, not through the Windows account sign-in window (the Web Account Manager): on a PC your organization does not manage, that window asks at every sign-in whether Windows should remember the account and manage the device. To use it anyway, set the environment variable `AZURE_CORE_ENABLE_BROKER_ON_WINDOWS` to `true` before running setup. Microsoft's Agent 365 tool, which runs only when you switch on Word, Excel and OneDrive files, still signs in through the Windows window and can ask more than once; setup cannot change that.

In a remote session (SSH, for example) setup signs in with a device code instead of the browser: it prints a web address and a short code to enter there. Word, Excel and OneDrive files are the exception: on Windows their tool signs in through a sign-in window, which failed over SSH in testing, so in a session without a desktop setup leaves them off and says to run it again at the desktop.

Setup then creates or connects the agent, creates this PC's certificate, saves settings, registers the agent with Claude Code and GitHub Copilot CLI, and tests that the agent can sign in and reach Teams. After that test, and only if you switched them on, it sets up Word, Excel and OneDrive files; a failure there never stops setup, and the last screen says why and what to do.

## Licences for the agent

The agent's user account is a user in Microsoft 365, and like a person it needs licences of its own: one that includes Teams (most also bring mail and files), and, for Word, Excel and OneDrive files, Microsoft 365 Copilot. Setup gives it free ones by itself. When it could not, setup offers a walkthrough after the end-to-end test, under **Licences for Microsoft 365**:

1. It shows your organization's licences, what each gives the agent and how many are free. Products are shown by the names the Microsoft 365 admin center uses; one that Microsoft's licensing reference does not list is shown by a readable form of its part number.
2. With a free licence, it asks whether to give it to the agent.
3. With none free, it says who can add a licence or free one, and where in the [Microsoft 365 admin center](https://admin.microsoft.com); or, when nothing includes Teams, which licences do. Then it waits: press Enter once that is done and it checks again, or type `s` to leave it for later. Its last screen then points back to these steps rather than repeating them.

The Microsoft 365 Copilot part comes after the Teams part, and only when Word, Excel and OneDrive files are switched on in that run. If the Teams part is left for later, so is Copilot.

The admin center paths it gives (**Billing > Your products**, **Billing > Licenses**, **Marketplace**) are those of the admin center's **Dashboard view**, which most people see. In the **simplified view**, products are under **Subscriptions** and new ones under **Billing > Add more products**; a button at the top of the admin center switches views. Run unattended, setup asks nothing: it only reads the organization's licences, so the last screen can say what to do. Details are in [Windows troubleshooting](../troubleshooting/windows.md#no-teams-licence-or-graph-returned-403-for-mechats).

## The first start

The first time Claude Code opens the Entrabot folder it asks some questions; `start-entrabot.cmd` and setup's last screen both say so. If this PC has not used Claude Code before, it asks you to sign in to Claude. Then it asks whether to trust this folder, whether to use the `entrabot` MCP server, and whether to load a development channel. Say yes to each: the folder is the one you just set up, the server is your agent, and the channel is how Teams messages reach it. Then say *Who are you?*

Setup's last screen ends with things to try, matched to what the agent has:

- *Send me a Teams message to say hello*, then answer it in Teams. A licence assigned in this run can take 10-15 minutes to reach Teams.
- *Send me an email that says what you can do*. This needs a licence that includes Outlook mail.
- *Create a Word document called Hello and share it with me*, only when Word, Excel and OneDrive files are switched on and the agent has its Microsoft 365 Copilot licence.

When the agent has no licence yet, Teams and email are one item for later: once it has its licence, ask it to message you in Teams and email you.

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
| `3` | Set up, but something needs checking (for example, the agent has no Teams licence, or one given in this run has not reached Teams yet; Claude Code is older than 2.1.295; or Word, Excel and OneDrive files were asked for and do not work yet). |
| `130` | Cancelled. |

See the [`setup-windows.ps1` reference](../reference/scripts/setup/setup-windows-ps1.md) for every parameter.

## What it installs and where

Everything is installed for your account only, with one exception: on an account that is a local administrator and has `winget`, a missing Azure CLI is installed with `winget`, for the whole PC, and Windows may ask for permission. Nothing else is installed machine-wide or asks for administrator approval. PowerShell 7, for Word, Excel and OneDrive files, also comes from `winget`; on the test PC, as a standard user, `winget` installed it for that account.

| What | Where | When |
|---|---|---|
| Python 3.12 (private copy; checksum and signature checked before use) | `%LOCALAPPDATA%\entrabot\python` | Only if the PC has no Python 3.12 or newer. |
| The project's Python environment | `.venv` in the Entrabot folder | Always. |
| Azure CLI (private copy, version 2.92.0 or later and below 3) | `%LOCALAPPDATA%\entrabot\tools\azure-cli` | Only if the Azure CLI is missing and was not installed with `winget` (see above). |
| Git for Windows | `%LOCALAPPDATA%\Programs\Git` | Only if you say yes. Claude Code needs it for this project. |
| Claude Code | `%USERPROFILE%\.local\bin` | Only if you say yes. |
| Microsoft Agent 365 CLI | `%LOCALAPPDATA%\entrabot\tools\a365` | Only for Word, Excel and OneDrive files, if you say yes and it is missing. |
| .NET SDK (Microsoft's install script, checked to be signed by Microsoft; .NET 10) | `%LOCALAPPDATA%\entrabot\tools\dotnet` | Only with the Agent 365 CLI, and only if the PC has no .NET SDK 8 or later. With the CLI about 1 GB. |
| PowerShell 7 | Where `winget` puts it (for a standard user, `%LOCALAPPDATA%\Microsoft\WindowsApps`) | Only for Word, Excel and OneDrive files, if you say yes and it is missing. |
| Two certificates with non-exportable keys | Your Windows certificate store (`Cert:\CurrentUser\My`) | Always. The keys are held by the PC's TPM when it accepts them. |
| Settings | `.env`, `.entrabot-state.json`, `.mcp.json` in the Entrabot folder | Always. |
| Logs, and local memory | `%LOCALAPPDATA%\entrabot` | Always. |

To undo a setup, see [`teardown-windows.ps1`](../reference/scripts/teardown/teardown-windows-ps1.md).

## Requirements and limits

- **Setup needs an Entra administrator.** A person who is not an administrator of the tenant cannot set up an agent by themselves. Joining an existing agent is not an exception: each PC adds its own credential to the setup (Provisioner) app registration and to the agent's Blueprint, and both need directory rights. Setup checks your roles after sign-in and says what to ask for.
- **The agent needs a licence that includes Teams** to use Teams. Without one the agent can still sign in, and setup ends with exit code 3, after offering the [licence walkthrough](#licences-for-the-agent).
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

Licences and Word, Excel and OneDrive files, on the same machine against a real tenant, over SSH:

- The Word, Excel and OneDrive question and the offer to install their tools.
- The install itself with no administrator rights: the .NET SDK 10 and the Microsoft Agent 365 CLI in `%LOCALAPPDATA%\entrabot\tools`, with `a365 --version` running on .NET 10, and PowerShell 7 found.
- The read-only licence report (`create_entra_agent_ids.py --licence-report`, with the setup credential), the walkthrough's screens with every licence that includes Teams in use, and the reminders on the last screen.
- On a repair run, the step that creates the agent saying it has no Teams licence and no Microsoft 365 Copilot licence because none is free.
- `a365 setup requirements` failing to sign in over SSH, which is why setup no longer runs the Word, Excel and OneDrive steps without a desktop.

At the PC's own desktop, through Remote Desktop, on the same machine and tenant, starting from an account with nothing of Entrabot's on it:

- Double-clicking `scripts\setup-windows.cmd`, signing in (through the Windows account sign-in window, which setup used then), and a full run to the last screen.
- The Word, Excel and OneDrive tools installing with no administrator prompt, PowerShell 7 through `winget` included.
- `a365 setup requirements` and `a365 develop add-mcp-servers`, which signed in through the Windows account sign-in window and succeeded. Together with setup's own sign-in that was four sign-in prompts on a PC its organization does not manage.
- The step after them, which grants the Work IQ permissions, failing on a new agent: Microsoft Graph answered 404 when a second permission was added to a grant created moments before. Setup carried on and said so on the last screen, as intended. That step now writes each resource's permissions once and tries again on a 404.
- The licence walkthrough with "check again" and "skip", and "Start your agent now?" opening the agent in its own window.
- `start-entrabot.cmd` starting a signed-in Claude Code session that loaded the agent's tools (on an earlier build).
- Teardown with `-DeleteAgent -PurgeData`, over SSH.
- Setup's sign-in through the browser was run at the desktop and worked. The run still failed at the next step, because the Azure CLI went on sending a day-old Microsoft Graph token that the organization had since revoked; with that token removed from the CLI's cache by hand, the same sign-in carried a full repair run over SSH. Setup now removes the old sign-in itself before asking for a new one; that removal has unit tests only.
- The step that grants the Work IQ permissions, after its fix, against the half-made grant the failed run left: it added the two missing permissions in one write and created the Word grant, and a second run changed nothing.

Not yet tested:

- The rest of the Word, Excel and OneDrive configuration (`a365 setup permissions mcp` and the final check of the tooling manifest): no run on a real machine has reached them yet. So Word, Excel and OneDrive files have not been switched on end to end on Windows.
- `winget` drawing its progress bar in place: on the desktop run it printed a new line for every redraw, and the change that runs it attached to the window has not been seen on Windows.
- Giving the agent a licence through the walkthrough, and the Microsoft 365 Copilot path; unit tests only.
- Changes made after those runs, covered by unit tests: the last screen pointing back to the walkthrough instead of repeating its advice; Microsoft 365 Copilot mentioned only when Word, Excel and OneDrive files are switched on; product names for products Microsoft's reference does not list; the Word, Excel and OneDrive steps running after the end-to-end test, a failure there never stopping setup, and their being skipped in a session without a desktop; and the review screen naming what was installed before it.

- Windows on ARM64, and Windows 10.
- Physical TPM chips (only a virtual TPM was used).
- PCs joined to Entra or managed by Intune, and tenants with Conditional Access.
- Cloud memory set up through the wizard.
- The fixes made after a security review (the Claude Code version check, the hooks' UTF-8 input, the persona-sati token command lookup, revoking an older setup key file, and the teardown and rotation safeguards). They are covered by unit tests, but the end-to-end run above was done before them.
- The rule for taking over an existing Provisioner app, the on-screen warning about an older setup key, teardown's one-write certificate removal and its retry of an older setup key, and the sponsor search by sign-in name. They are covered by unit tests only and have not been run against a real tenant.
- The check that an existing Agent User belongs to the agent's Blueprint before it is reused, the stops when Microsoft Entra could not be read, the wizard's explanations of why it stopped, and the health checks' warning about an older setup key. They are covered by unit tests and have not all been run against a real tenant.

If something goes wrong, see [Windows troubleshooting](../troubleshooting/windows.md).

## Next step

Continue to [Verify Your Agent Identity](verify.md).
