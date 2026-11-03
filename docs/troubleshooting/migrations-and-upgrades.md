# Migrations and upgrades

## Setup is being run again on an existing installation

Setup is intended to be re-runnable. Use the existing Blueprint rather than
creating a new chain:

=== "macOS or Linux"

    ```bash
    ./scripts/setup.sh --use-blueprint=<blueprint-app-id>
    ```

=== "Windows"

    ```powershell
    scripts\setup-windows.cmd -UseBlueprint <blueprint-app-id>
    ```

Re-running setup repairs or merges current permissions, consent, certificate
registration, storage configuration, and MCP registration where supported.
Review any prompt before selecting a migration source.

## Cloud migration copied some files and then failed

Local-to-Blob migration continues across per-file errors, but `setup.sh`
treats a non-empty migration error report as incomplete setup and exits with
code 2. Successfully copied files remain in Blob and all source files remain
local.

Fix the reported storage consent, RBAC, connectivity, or local-file problem,
then re-run the same setup command. Existing target keys are skipped, so the
retry is idempotent and cloud data remains authoritative.

See [Storage troubleshooting](storage.md).

## Windows reports legacy and current data directories

The current root is `%LOCALAPPDATA%\entrabot`; the legacy root is
`%USERPROFILE%\.entrabot`.

If only the legacy root has data:

```powershell
scripts\setup-windows.cmd -UseBlueprint <blueprint-app-id>
```

`-Migrate` is a compatibility-only switch on `setup-windows.ps1`; normal setup
runs the legacy migration every time. If you hit a legacy/current directory
conflict, preserve both, determine which has the current logs and cursor state,
choose one authoritative root, then rerun normal setup.

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

## A Windows Blueprint certificate needs rotation

Run:

```powershell
pwsh -File scripts\deploy-windows.ps1
```

The rotation is transactional: register the new public certificate, update
both thumbprints, smoke-test Agent User authentication, then remove the old
certificate. A failed smoke test restores the old registration and local
values. See [Windows troubleshooting](windows.md).

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

## Post-upgrade verification

After pulling an upgrade:

1. Reinstall the editable package in the repository's own environment.
2. Re-run setup for the existing Blueprint.
3. Run strict health checks.
4. Restart the MCP host.
5. Call `whoami` to verify the runtime session.

=== "macOS or Linux"

    ```bash
    .venv/bin/python -m pip install -e ".[dev]"
    ./scripts/setup.sh --use-blueprint=<blueprint-app-id>
    ./status.sh --health-only --strict
    ```

=== "Windows"

    ```powershell
    .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
    scripts\setup-windows.cmd -UseBlueprint <blueprint-app-id>
    .\status-windows.ps1 -HealthOnly -Strict
    ```

Health status validates the Provisioner-visible resource chain. `whoami`
validates the identity and token held by the newly restarted MCP runtime.
