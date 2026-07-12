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
