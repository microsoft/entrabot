# Migrations and upgrades

## Setup is being run again on an existing installation

Setup is intended to be re-runnable. Run it again with no options; it continues
with the agent this computer is set up for rather than creating a new chain:

=== "macOS or Linux"

    ```bash
    ./scripts/setup.sh
    ```

=== "Windows"

    ```bat
    scripts\setup-windows.cmd
    ```

Re-running setup repairs or merges current permissions, consent, certificate
registration, storage configuration, and MCP registration where supported.
Review any prompt before selecting a migration source.

## Cloud migration copied some files and then failed

Local-to-Blob migration continues across per-file errors, but setup treats a
non-empty migration error report as a failure: it says "Copying memory to the
cloud failed." and exits with code 1 (earlier versions of `setup.sh` exited 2).
Successfully copied files remain in Blob and all source files remain local.

Fix the reported storage consent, RBAC, connectivity, or local-file problem,
then run setup again with cloud memory (`./scripts/setup.sh --use-cloud-memory`
on macOS and Linux) and answer yes to the copy. Existing target keys are
skipped, so the retry is idempotent and cloud data remains authoritative.

See [Storage troubleshooting](storage.md).

## A macOS or Linux computer was set up before the setup wizard

Earlier versions of `setup.sh` kept the agent's private key on its own, in one
key store item for the whole account (service `entrabot`, account
`blueprint-private-key`). Such a computer keeps working unchanged after the
upgrade. The next `./scripts/setup.sh` run keeps that key and, when its
certificate is on the agent's Blueprint, stores the two together as the agent's
own item and records `ENTRABOT_BLUEPRINT_CERT_SHA1` in `.env`, creating and
registering nothing new. Run setup once this way before rotating the
certificate or running `./scripts/teardown.sh`; both say so until then. See
[A computer set up before the wizard](macos-linux.md#a-computer-set-up-before-the-wizard).

Scripts that called `setup.sh` or `teardown.sh` need a look too:

- `./scripts/teardown.sh --yes` now disconnects this computer only. Add
  `--delete-agent` to delete the agent, or pass `--tenant-wide` for the older
  teardown.
- An unknown option to `setup.sh` is now an error (exit 2), and its exit codes
  are the wizard's: `0`, `1`, `2`, `3`, `130`.
- `--teams-user` now also adds the person as a sponsor of the agent in the
  directory, and `--with-a365-work-iq` no longer installs the Agent 365 CLI.

See the [`setup.sh` reference](../reference/scripts/setup/setup-sh.md#changes-from-earlier-versions).

## Windows reports legacy and current data directories

The current root is `%LOCALAPPDATA%\entrabot`; the legacy root is
`%USERPROFILE%\.entrabot`.

If only the legacy root has data, run setup. It moves the folder for you:

```bat
scripts\setup-windows.cmd
```

`-Migrate` is accepted and ignored; the move happens on every setup run that needs it.
If both roots have data, setup uses the current root, leaves the legacy one untouched,
and says so. Nothing is merged or deleted. Compare the two, work out which has the
current logs and cursor state, and move what you still need by hand.

## Teams replays self-authored messages after an identity rename

Run the shipped cursor migration from the repository root:

```bash
.venv/bin/python scripts/migrate_cursors_to_upn.py --dry-run
.venv/bin/python scripts/migrate_cursors_to_upn.py
.venv/bin/python scripts/migrate_cursors_to_upn.py --verify
```

Windows:

```powershell
.\.venv\Scripts\python.exe scripts\migrate_cursors_to_upn.py --dry-run
.\.venv\Scripts\python.exe scripts\migrate_cursors_to_upn.py
.\.venv\Scripts\python.exe scripts\migrate_cursors_to_upn.py --verify
```

The migration advances cursor timestamps, seeds recent self-authored message
IDs, and writes the stable marker
`chat_cursors/_migrated_upn_fix.json`. `--dry-run` and `--verify` are read-only.
Once the marker exists, later runs do not rewrite cursors.

## A legacy Provisioner client secret appears in state

Run:

```bash
.venv/bin/python scripts/entra_provisioning.py
```

The Provisioner migration removes `PROVISIONER_CLIENT_SECRET` from local state,
removes legacy password credentials from the Provisioner app when permitted,
and establishes certificate authentication. The Provisioner certificate is
for setup and status diagnostics; it is not the Blueprint certificate used by
the runtime three-hop flow.

Do not preserve the old secret in `.env`, shell history, logs, or backup notes.

## A Blueprint certificate needs rotation

Run:

=== "macOS or Linux"

    ```bash
    ./scripts/setup.sh --rotate-cert
    ```

=== "Windows"

    ```powershell
    powershell -ExecutionPolicy Bypass -File scripts\deploy-windows.ps1
    ```

The old certificate stays in use until the new one is proven: rotation creates a new
certificate, adds it to the Blueprint beside the old one, signs the agent in with it,
switches `.env`, and only then removes the old certificate from the Blueprint and from
this computer. If it fails before the switch, the new certificate is undone and nothing
else changes. Restart the assistant afterwards. See
[macOS and Linux troubleshooting](macos-linux.md#certificate-rotation-stopped-partway)
and [Windows troubleshooting](windows.md#certificate-rotation-stopped-partway).

## Upgrading to shared poll state

This applies when a deployment using cloud memory picks up the release that moved
the watched-chat list and email cursor into the storage backend. Before it, both
were local files even with cloud memory on. Local-only installs have nothing to do:
`watched_chats` stays where it is, and the email cursor picks up from the legacy
file.

On the first boot after upgrading:

- **The email cursor starts at "now".** Mail received before this first boot isn't
  pushed, but stays readable through the `read_email` tool. This is deliberate: the
  `email_cursor.txt` blob in your container is a stale copy from setup's migration,
  and reading it would replay up to 50 old emails as new. It only happens while the
  cursor key is being created. From then on a restart reuses the stored cursor, and
  mail received while an instance is down is pushed once one starts.
- **The watched list comes from the cloud copy plus discovery.** The starting point
  is any `watched_chats` blob already in your container from an earlier setup run; new
  migrations no longer copy it. In an Agent User session,
  the first discovery sweep, about two minutes after boot, adds every other chat the
  Agent User belongs to. A chat found this way surfaces its newest message only if it
  is under 24 hours old.
- **Setup's migration leaves local poll state behind.** It skips `watched_chats`,
  both email cursor files, the legacy `chat_id` key, and everything under
  `chat_cursors/`, because a machine's copies may be stale or belong to another agent.
  A copied per-chat cursor would be used as that chat's cursor and skip the 24-hour
  check above, so two agents sharing a group chat could replay old messages. Earlier
  versions of the migration copied these too, so a container set up that way may
  still hold stale copies. They are not removed automatically.
- **Old local files are ignored.** Startup logs their paths at info level, and you
  can remove them by hand.

On macOS or Linux:

```bash
rm ~/.entrabot/data/watched_chats ~/.entrabot/data/email_cursor.txt
```

On Windows:

```powershell
Remove-Item "$env:LOCALAPPDATA\entrabot\data\watched_chats", "$env:LOCALAPPDATA\entrabot\data\email_cursor.txt"
```

Upgrade every instance on a container together. An instance still on the old code
keeps its own local cursor and takes no part in claiming mail, so it can push the
same email as an upgraded one.

See [Poll state](../architecture/storage-and-memory.md#poll-state).

## Upgrading to a new release

Entrabot is released as tags; the latest release is on
<https://github.com/microsoft/entrabot/releases/latest>. `main` is development and may be
ahead of it. To move an existing install to a release, in the Entrabot folder:

=== "macOS or Linux"

    ```bash
    git fetch --tags
    git checkout v0.3.1
    ./scripts/setup.sh
    ```

=== "Windows"

    ```bat
    git fetch --tags
    git checkout v0.3.1
    scripts\setup-windows.cmd
    ```

Setup continues with the existing agent and reinstalls Entrabot's packages when they changed.
Then do the [post-upgrade verification](#post-upgrade-verification). Git prints a note about a
"detached HEAD" after the checkout: that is normal for a release tag, not an error. The
"Entrabot version" check in `--diagnose` and the status report says which release is running.

Read the section below for the release you are moving to before restarting the agent.

**If the folder has local changes.** Your `.env`, `.mcp.json`, `.entrabot-state.json` and
`ToolingManifest.json` are not tracked by git, so the checkout keeps them. For files you
changed yourself, `git status` lists them, and `git checkout` stops if the release changes one
of them. To keep your edits, put them aside first and reapply them after:

```bash
git stash push -m "my Entrabot changes"
git checkout v0.3.1
git stash pop
```

`git stash pop` stops on a conflict and keeps the stash until you resolve it. To discard your
edits instead, `git restore <file>` before the checkout. If you have commits of your own,
keep them on a branch (`git switch -c my-changes` before the checkout) and merge the release
into it instead of checking the tag out.

**If you installed from a ZIP.** A ZIP has no git history to update. Clone the release into a
new folder as in the getting-started guide, copy `.env` and `.entrabot-state.json` from the old
folder into it, and run setup in the new folder. Start the agent from the new folder from then
on.

## Upgrading to 0.3.1

0.3.1 fixes two bugs in 0.3.0's scripts. There is no new setting and no new question, and
nothing to run again unless one of the bugs affected you:

- **`revoke_consent.py` changes only the grant on one resource**: the one on Microsoft Graph,
  or the one the new `--resource-app-id` names. In 0.3.0 it took the first grant Microsoft
  Graph listed, whichever resource it was on, so `--all` could delete the agent's Azure
  Storage grant instead of its Microsoft Graph one. If you ran it on 0.3.0,
  [`show_permissions.py`](../reference/scripts/operations/show-permissions-py.md) lists the
  grants the agent still holds, and
  [`grant_consent.py`](../reference/scripts/auth-and-certs/grant-consent-py.md) gives back
  one it lost.
- **The scripts in `scripts/` find setup's private Azure CLI.** On a computer whose only Azure
  CLI is the private copy setup installs, 0.3.0's scripts stopped with `FileNotFoundError`
  naming `az`, and the "Agent 365 registry" line of `--diagnose` showed the end of that
  error. In 0.3.1 the scripts that read with the setup app's token need no Azure CLI, and the
  ones that need it find the private copy without it being on your `PATH`. See
  [A script in `scripts/` cannot find `az`](macos-linux.md#a-script-in-scripts-cannot-find-az).

Restart the MCP host after upgrading, as for any release.

## Upgrading to 0.3.0

The changes are in setup and teardown:

- **One question for Agent 365.** Setup asks "Switch on Agent 365 integration?" instead of
  "Switch on Agent 365 telemetry?". Yes switches telemetry on as before and also registers the
  agent in the Agent 365 registry, which gives the setup app the Microsoft Graph permission
  `AgentRegistration.ReadWrite.All`. The flag is `--configure-a365` (`-ConfigureA365` on
  Windows); 0.2.0's `--configure-a365-observability` and `-ConfigureA365Observability` still
  work and now mean the same.
- **One registration per agent.** A computer that joined its agent with 0.2.0's setup has no
  record that it joined, so setup there cannot tell it from the computer that created the
  agent, and a yes registers the agent from it as well. If another computer has registered
  the agent already, record that registration on this one before running setup with a yes:
  `register_agent_instance.py --registration-id <ID> --apply`, with the ID the other
  computer's `--diagnose` shows. Setup then updates that registration instead of making a
  second one. See
  [Telemetry and the Agent 365 registry](../platform-docs/microsoft-agent-365.md#telemetry-and-the-agent-365-registry).
- **Teardown with `--delete-agent`** (`-DeleteAgent`) also deletes the agent's registration
  when this computer recorded one.

Restart the MCP host after upgrading, as for any release.

## Upgrading to 0.2.0

Three behaviour changes take effect with no setting:

- **Containment.** On macOS, and on Linux with `bwrap`, the MCP server now runs in a sandbox.
  Anything it needs outside its repository and state, or any host other than Microsoft's, is
  refused. Check with `./status.sh` ("Server containment") and see
  [Process Containment](../guides/containment.md). `ENTRABOT_CONTAINMENT=off` restores the
  old behaviour.
- **Recipient policy.** `send_email`, `create_chat`, `add_teams_member`, `share_file` and the
  daily summary now refuse addresses outside the agent's sponsors, the requesting chat and the
  home tenant. List outside partners in `ENTRABOT_OUTBOUND_ALLOW`.
- **Audit before sending.** `send_teams_message`, `send_card`, `post_thinking_placeholder` and
  `create_chat` now refuse to act when the audit log can't be written.

Restart the MCP host after upgrading: a server started by an older version is reported as not
contained until it restarts.

## Post-upgrade verification

After pulling an upgrade:

1. Re-run setup for the existing agent. Setup reinstalls Entrabot's packages
   into the repository's own environment when `pyproject.toml` has changed.
2. Run strict health checks.
3. Restart the MCP host.
4. Call `whoami` to verify the runtime session.

=== "macOS or Linux"

    ```bash
    ./scripts/setup.sh
    ./status.sh --health-only --strict
    ```

    Setup does not install the test tools. If you work on Entrabot's code, add
    them with `.venv/bin/python -m pip install -e ".[dev]"`.

=== "Windows"

    ```bat
    scripts\setup-windows.cmd
    scripts\setup-windows.cmd -Diagnose
    ```

Health status validates the Provisioner-visible resource chain. `whoami`
validates the identity and token held by the newly restarted MCP runtime.
