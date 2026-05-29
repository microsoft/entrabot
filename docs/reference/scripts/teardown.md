# Teardown scripts

Scripts that remove what setup created. Run in the opposite order: Agent User → Agent Identity → Blueprint → Provisioner app → local state.

Cloud storage is not deleted by default — see `deprovision_blob_storage.py` in the storage reference for that.

## `teardown.sh`

End-to-end teardown for macOS / Linux. Reverses `setup.sh`.

### Usage

```bash
./scripts/teardown.sh                                     # delete everything
./scripts/teardown.sh --agent-user-upn=agent@example.com  # target one chain
./scripts/teardown.sh --dry-run                           # show what would happen
./scripts/teardown.sh --yes                               # skip confirmation
./scripts/teardown.sh --preserve-provisioner              # leave Provisioner app
./scripts/teardown.sh --preserve-local-state              # leave .env, state, keychain
```

### What it deletes

In order (children before parents):

1. Agent User (must go first — it is a child of Agent Identity).
2. Agent Identity service principal.
3. Blueprint app registration (also deletes `BlueprintPrincipal`).
4. Provisioner app registration (unless `--preserve-provisioner`).
5. Local state: `.env`, `.entrabot-state.json`, legacy Keychain entries (unless `--preserve-local-state`).

### Cloud storage

`--delete-cloud-storage` is reserved as an explicit switch but the current script refuses it. Run `deprovision_blob_storage.py` after a backup to delete the container, account, or resource group.

## `teardown-windows.ps1`

Windows teardown. Reverse of `setup-windows.ps1`.

### Usage

```powershell
.\scripts\teardown-windows.ps1
.\scripts\teardown-windows.ps1 -Force          # skip confirmation
```

### What it deletes

- Blueprint cert(s) from `Cert:\CurrentUser\My` matching subject `CN=entrabot-blueprint`.
- `%LOCALAPPDATA%\entrabot\` data directory.
- `BLUEPRINT_CERT_*` lines from `.env` (preserves the rest).
- MSAL cache.
- MCP registration entries from `.mcp.json` and Copilot's `mcp-config.json`.

### What it does NOT delete

The Entra app registrations (Blueprint, Agent Identity, Agent User) persist in the tenant. Use `deprovision_entra_agent_identity.py` from a Mac / Linux host, or the Entra admin portal, to clean those up.

## `deprovision_entra_agent_identity.py`

Targeted teardown of a single Agent User chain. Safer than `teardown.sh` when you have multiple chains and only want to delete one.

### Usage

```bash
python scripts/deprovision_entra_agent_identity.py --agent-user-upn=<UPN>
```

### What it deletes

In safe order:

1. Remove assigned licenses from the Agent User.
2. Delete the Agent User.
3. Delete the parent Agent Identity service principal.
4. Delete the parent Blueprint application.

Azure Blob Storage is intentionally out of scope — use `deprovision_blob_storage.py` for that.

## `cleanup-orphans.sh`

Delete orphaned Blueprint / Agent Identity resources left behind when `teardown.sh` could not delete them.

### Why orphans happen

`teardown.sh` falls back to `az` CLI tokens in some paths. Those tokens include `Directory.AccessAsUser.All`, which Agent Identity APIs reject with a hard 403 (Learning #1, #4). The result: Blueprint and Agent Identity remain in the tenant after teardown.

### Usage

```bash
./scripts/cleanup-orphans.sh <blueprint-object-id> [agent-identity-object-id]
```

### What it does

- Uses the EntraBot Provisioner app (cert-auth, re-created by `ensure_app_registration` if teardown wiped it) to mint a clean Graph token.
- Deletes the named Blueprint and (optionally) Agent Identity.
- No client secrets on disk or in the shell environment.

Run after `teardown.sh` when `show_agent_status.py` reports orphans.
