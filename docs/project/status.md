# Current Status

**Last updated:** November 12, 2026

!!! note "Research implementation"
    Entrabot is a research implementation, not a hosted production service. Run it against an isolated development tenant, and review every permission it requests before granting consent.

## Available today

### Identity and authentication

- Two authenticated session types: certificate-backed Agent User and MSAL delegated.
- Autonomous four-resource provisioning chain: Agent Identity Blueprint → BlueprintPrincipal → Agent Identity → Agent User.
- Blueprint, BlueprintPrincipal, and Agent Identity are created through the dedicated Microsoft Graph v1.0 subtype endpoints; Agent User creation uses Graph beta.
- Certificate private keys are stored in the OS keystore (Keychain, Linux Secret Service, or the Windows certificate store). On Windows both the Blueprint key and setup's Provisioner key are non-exportable CNG keys, created in the TPM when the TPM accepts a key and in the software key store otherwise; no key material is on disk.
- Several machines can share a Blueprint: attaching one adds its certificate and keeps the others. `setup.sh --remove-all-existing-certs` is the only way to replace them all. Windows setup registers its certificate the same way, and re-running it registers a certificate an earlier version left unregistered.
- Windows certificate rotation (`scripts\deploy-windows.ps1`) keeps the current certificate in use until the new one has signed the agent in, then removes the old one from the Blueprint and deletes it locally with its key.
- Setup on every platform takes over an existing app registration with the Provisioner's name only when that grants nobody anything: an administrator has already approved it for every permission setup needs, or nobody else can use it. Otherwise it stops before changing anything in Microsoft Entra and says how to go on, including naming a checked app for one run with `ENTRABOT_PROVISIONER_APP_ID_VERIFIED`. See [Setup and authentication](../troubleshooting/setup-and-authentication.md#setup-will-not-take-over-an-existing-provisioner-app-registration).
- Setup uses only the Blueprint recorded for the machine, or named when joining, and stops if it is not found or cannot be checked. An existing Agent User found by its sign-in name is reused only when its Agent Identity belongs to that Blueprint; one of another Blueprint stops setup before anything is created, granted or recorded. The Agent Identity and Agent User lookups tell "there is none" from "could not check", and stop on "could not check" instead of creating a duplicate. `setup.sh --use-blueprint` does not carry over an Agent User name that `.env` holds for another Blueprint, and stops before the certificate step if the Blueprint recorded is not the one asked for. When the Windows wizard stops at the agent step, or because the app `ENTRABOT_PROVISIONER_APP_ID_VERIFIED` names was refused or Microsoft Entra could not be read, it says which.
- `teardown.sh` deletes only the Provisioner app recorded for this machine, by its application ID. With none recorded it deletes no app and prints the command to delete one by hand.
- On Windows, an older setup key that setup could not switch off in Microsoft Entra is shown on screen at the provisioning step and on the finish screen, and setup finishes with something to check (exit `3`). The health checks (`-Diagnose` and `status-windows.ps1`) report it as a warning that does not change their exit codes. Teardown takes this PC's certificate and a rotation leftover off the Blueprint in one write, and tries to remove such an older setup key before it deletes the state file. The wizard finds a sponsor whose sign-in name contains an apostrophe.

### Microsoft 365

- Microsoft Teams messaging with explicit chat IDs, plus background chat discovery and polling.
- Outlook read and send, background polling, and daily summaries.
- Direct Microsoft Graph Files access alongside Agent 365 Work IQ integrations for Word, Excel, and OneDrive/SharePoint (ODSP).
- Bounded, read-only Excel worksheet and range reads through the Graph Workbook API (`list_workbook_worksheets`, `read_workbook_range`). Run against a live tenant on an `.xlsx` in the Agent User's own OneDrive; not yet exercised on a file shared by someone else or on `.xlsm`.

### Security and runtime

- Audit-first, fail-closed actions: if an action can't be recorded, it doesn't proceed.
- An authoritative external-content boundary wraps untrusted Teams, email, Files, and Work IQ content before it reaches the model.
- Host-gated sponsor reply behavior adapts to the calling client.
- A body-first prompt governs core behavior, with an optional persona layered on top.

### Storage and platforms

- Local operational storage by default, with Azure Blob Storage available as an opt-in.
- With Blob Storage, all shared operational state, including the watched-chat list and email cursor, lives in the container. Instances on one container share it, and exactly one instance pushes a given email.
- Setup and teardown flows for macOS, Linux, and Windows.
- Windows setup is one step: `scripts\setup-windows.cmd` runs on the Windows PowerShell 5.1 that ships with Windows, needs no administrator rights, PowerShell 7, `winget` or pre-installed Python, and starts an interactive wizard that can also run unattended (`-Yes` plus parameters). `-Diagnose` checks an existing setup without changing it. `start-entrabot.cmd` starts the agent, and both it and setup's last screen say what Claude Code asks the first time; the last screen also lists things to try, matched to the agent's licences.
- Agent licences: setup gives the agent a licence that includes Teams (read from each product's service plans, preferring one with mail and files) and, for Word, Excel and OneDrive files, Microsoft 365 Copilot. When none is free, an interactive run walks the person through it: what the organization has, a free licence given on a yes, or who can add or free one and where in the Microsoft 365 admin center, then checking again. Unattended runs read what the organization has and say what to do on the last screen. Reading and assigning use the setup (Provisioner) credential, through `create_entra_agent_ids.py --licence-report` and `--assign-licence`.
- Word, Excel and OneDrive files (Work IQ) on Windows: setup explains them before asking, offers to install their tools with no administrator rights (the Agent 365 CLI, and the .NET SDK under `%LOCALAPPDATA%\entrabot\tools` when the PC has none; PowerShell 7 with `winget`), and switches them on after the end-to-end test. A failure there never stops setup. They can only be switched on at the PC's own desktop, not over SSH.
- Windows runtime: storage files are UTF-8 and byte-identical across platforms, only one Entrabot server runs per PC, `.env` tolerates a byte-order mark, the persona-sati token command may be a `.ps1`, `.py` or `.cmd` file with arguments (found by absolute path or on `PATH`, never in the current folder), and the Claude Code hooks run through one cross-platform launcher, read their input as UTF-8, and have gates that block when a hook cannot run (on Claude Code 2.1.295 or later, which setup checks for).
- **What the Windows work was verified on.** One configuration: Windows 11 Enterprise 25H2 (x64) in an Azure virtual machine with a virtual TPM 2.0, non-administrator local accounts, driven over SSH. Exercised there: fresh-PC bootstrap; new-agent setup; joining an existing Blueprint; re-run and repair; the interactive wizard in a real terminal; `-Diagnose`; TPM key creation, signing and deletion; the three-hop sign-in; starting the MCP server and calling `whoami`; the single-entry `.env` permission, read back; directory sponsors, including lookup of a personal Microsoft account by its `#EXT#` sign-in name; the Claude Code hooks under Git Bash; the Git for Windows and Claude Code installs and the `PATH` entry for Claude Code; status; certificate rotation; teardown in its default mode and with `-DeleteAgent`; and the full test suite on Windows (4208 passed, 226 skipped, 0 failed). Later, against a real tenant over SSH: the Word, Excel and OneDrive question and install offer; the install of the .NET SDK 10 and the Agent 365 CLI with no administrator rights; the read-only licence report and the walkthrough's screens with every Teams licence in use; and the last screen's reminders. An earlier version of Windows setup was acceptance-tested on Windows 11 ARM64; the current setup has not been run there.

### Documentation and operations

- Public site with nine task-oriented sections and source-audited references for all registered MCP tools and all 46 supported operator commands.
- Legacy URLs redirect to current pages; plans, specs, investigations, and research are retained only under non-published `engineering-history/`.

## Active development

- MXC sandbox integration remains under review and is not part of the current `main` runtime.

## Known limitations

- Entrabot is a research implementation, not a production-ready tenant service.
- Agent User provisioning still depends on Microsoft Graph beta.
- Live smoke tests provision real Entra, Azure, and Microsoft 365 resources and send a real Teams message, so they run manually rather than in CI until an isolated tenant and non-human CI identity are available.
- Windows end-to-end coverage is narrower than macOS. Not yet tested with the current setup: Windows on ARM64; Windows 10; physical TPM chips (only a virtual TPM was used); PCs joined to Entra or managed by Intune, and tenants with Conditional Access; cloud memory set up through the Windows wizard; and Word, Excel and OneDrive files (Work IQ) switched on end to end through it. Broader acceptance coverage is tracked in [#105](https://github.com/microsoft/entrabot/issues/105).
- The Windows fixes made after a security review (the Claude Code version check, UTF-8 hook input and gates that block on any error, the persona-sati token command lookup, revoking an older setup key file on the Provisioner app, teardown leaving a certificate that is not in this PC's store, rotation waiting for the new certificate to be listed, and the pinned checksum for the private Python download) are covered by unit tests but have NOT been re-run on a real Windows machine. The end-to-end verification described above predates them.
- The Provisioner take-over rule, the on-screen warning about an older setup key, teardown's one-write Blueprint removal and its retry of an older setup key, and the sponsor search by sign-in name are covered only by unit tests, which the Windows CI job also runs. They have NOT been run against a real tenant. In particular the take-over rule relies on the real output shapes of `az ad app owner list`, `az ad sp show`, and Graph `appRoleAssignments`, which could not be verified offline. If a shape differs, the rule fails closed: setup refuses the take-over and names the override.
- The rule that setup uses only the recorded or named Blueprint and never searches by display name, the check that a reused Agent User belongs to that Blueprint, the "could not check" stops in the Agent Identity and Agent User lookups, `teardown.sh` deleting only the recorded Provisioner app, and the health checks' warning about an older setup key are covered by unit tests. They have not all been run against a real tenant.
- Open follow-ups, not fixed: `scripts/teardown.sh` runs `.env` as shell code (`source .env`) and passes the Provisioner token to Python on a `python -c` command line, where the process list can show it; `scripts/setup.sh` puts the `--use-blueprint` value into Python source without checking that it is an App ID; the status report (`show_agent_status.py`, which `status.sh` and `status-windows.ps1` run) prints the full thumbprints of the current Provisioner and Blueprint certificates; the Windows wizard clears a stale agent sign-in name only when the state file shows a switch, so a switch that was interrupted is caught by the Agent User check, which stops setup, rather than avoided; and setup still needs an Entra administrator (below).
- Setting up an agent needs an Entra administrator on every platform. Granting admin consent needs a Global Administrator or Privileged Role Administrator, and joining an existing agent from another machine still needs directory rights, because each machine adds its own credential to the Provisioner app and the Blueprint. A person without those rights cannot set up an agent by themselves; there is no "administrator prepares, user joins" flow yet.
- An agent needs a licence that includes Teams to use Teams. Setup completes without one, says so, and exits with a "something to check" code. On Windows it first offers the licence walkthrough.
- Windows: Word, Excel and OneDrive files cannot be switched on over SSH or in any session without a desktop, because `a365 setup requirements` signs in through a sign-in window (it failed with `MsalAuthenticationFailedException` over SSH). Setup installs their tools there if asked, and says to run it again at the desktop.
- Windows: Word, Excel and OneDrive files have not been switched on end to end. At the desktop, `a365 setup requirements` and `a365 develop add-mcp-servers` succeeded, and the next step, granting the Work IQ permissions, failed on a new agent with a 404 from Microsoft Graph (a second permission added to a grant created moments before). That step now writes each resource's permissions once and tries again on a 404, with unit tests only; `a365 setup permissions mcp` and the manifest check after it have never run on a real machine.
- Windows: covered by unit tests only, not yet run against a real tenant: giving the agent a licence through the walkthrough, the Microsoft 365 Copilot path, setup's sign-in through the browser at a desktop, `winget` drawing its progress bar in place, and the changes made after the last run on the test machine (the last screen pointing back to the walkthrough, Copilot only when Word, Excel and OneDrive files are on, readable names for products Microsoft's reference does not list, the configuration running after the end-to-end test and never stopping setup, and the review screen naming what was installed before it).
- When Word, Excel and OneDrive files are switched on, the Microsoft 365 Copilot licence is assigned at the step that creates the agent, before they are configured; if their configuration then fails, the licence stays assigned for the next run.
- On Windows, Claude Code needs Git for Windows to run this repository's hooks. Without it the hook gates block edits and Teams sends.
- In a Windows remote session (SSH), Windows Credential Manager and MSI-based installers are unavailable. Setup does not depend on either; this is why it uses private copies of Python and the Azure CLI when they are missing.
- Local storage files are read and written as UTF-8 ([#60](https://github.com/microsoft/entrabot/issues/60)) and the single-instance lock now also holds on Windows, with its file descriptor closed on every error path ([#59](https://github.com/microsoft/entrabot/issues/59)). Both issues are still open pending review of these changes.
- Legacy Graph file comments are not the same as Word UI comments; use the Work IQ Word tools for document comments.
- Work IQ Excel (`mcp_ExcelServer`) is in the live Agent 365 catalog but not on Microsoft Learn, and the CLI warns of a "legacy ATG audience", so its tools may change. It requires `McpServers.Excel.All` on the Blueprint.
- `ENTRABOT_FILES_DENIED_SITES` is checked against Graph's reported site only for the workbook and Work IQ Excel tools. The Work IQ Word and ODSP tools don't apply it, and `read_file` and `add_file_comment` check the `site_id` the caller supplies.
- Excel writes, table reads, and workbook sessions aren't implemented ([#117](https://github.com/microsoft/entrabot/issues/117)).
- External-content wrapping is defense in depth. It reduces instruction-injection risk but is not proof against every possible model jailbreak.
- On a cloud backend, the email cursor starts at "now" when its key is first created (the first boot on a container, or after the key is deleted), so mail received before then isn't pushed; it stays readable through `read_email`. Once the key exists, a restart reuses it and mail received during downtime is pushed as normal. See [Upgrading to shared poll state](../troubleshooting/migrations-and-upgrades.md#upgrading-to-shared-poll-state).
- An instance on a host that ignores channel notifications can still claim Teams and email deliveries, so with mixed hosts on one container an item can go to the host that can't show it.
- Two emails with the same timestamp, where the second arrives after the cursor has moved past it, won't be pushed. Both stay readable through `read_email`.
- The shutdown save stops waiting after 10 seconds, but a storage call already in flight can still delay process exit until the storage client's own timeout.
- A batch of email held because its claim couldn't be confirmed lives only in memory, so restarting before the claim settles loses it. The mail stays readable through `read_email`.
- Chats are polled one at a time and each read sees the latest 10 messages, so a very busy chat can have messages pushed past that window between visits. They stay readable through `read_teams_messages`.
- Delegated sessions have no discovery sweep, so with a cloud backend their chat list only grows through `create_chat`.
- A wait keeps the sponsor gate it started with, so a gate built during a brief storage or Graph failure can miss a federated sponsor until the next wait.
- Failed watched-list writes retry on the next discovery sweep and at shutdown, so in a delegated session a failed write can wait until shutdown. Discovery re-adds the chat in an Agent User session.
- If the sponsor lookup fails, a sponsor's Teams push arrives in the XPIA envelope like anyone else's until a later lookup succeeds, so the agent may ask them to restate an instruction. Someone removed as a sponsor can keep speaking outside the envelope for up to 15 minutes, until the cached list refreshes.
- Adding a Blueprint certificate reads the list and then writes it, and those two steps aren't atomic. Two machines attaching in the same moment can lose one of the new certificates; the affected machine's setup reports it, and re-running setup there adds it again. A per-user lock keeps two setups on the same machine from overlapping, but it can't cover two machines.
- `ENTRABOT_MODE` is validated but does not currently select the auth path in `_init_auth`; credential presence and `ENTRABOT_SKIP_PROVISIONING` determine which path (three-hop Agent User or MSAL delegated) is attempted.

## Project links

- Changelog: [`changelog.md`](changelog.md)
- GitHub issues: <https://github.com/microsoft/entrabot/issues>
- GitHub pull requests: <https://github.com/microsoft/entrabot/pulls>
- [Security policy](https://github.com/microsoft/entrabot/blob/main/SECURITY.md)
