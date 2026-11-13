# `scripts/teardown-windows.ps1`

Platforms: Windows.

## Purpose

`teardown-windows.ps1` disconnects this PC from an agent. By default it removes
only what belongs to this PC and leaves the agent, its memory, and every other
PC that uses it alone. Two switches widen the scope: `-DeleteAgent` deletes the
agent from the tenant, and `-PurgeData` deletes Entrabot's data folder.

It is a thin wrapper; the work is done by
`python -m entrabot.winsetup teardown`.

The default mode and `-DeleteAgent` have been run on a real PC (Windows 11 25H2
x64, virtual TPM, a non-administrator account, Windows PowerShell 5.1).
`-PurgeData` has not.

## Requirements

- Native Windows, and Windows PowerShell 5.1 (ships with Windows) or
  PowerShell 7.
- `.venv` in the Entrabot folder, from a
  [`setup-windows.ps1`](../setup/setup-windows-ps1.md) run.
- To change anything in Microsoft Entra: a signed-in Azure CLI session with the
  rights setup needed. Without one, the local cleanup still completes and the
  report says what is still listed in Entra.

## Usage

```powershell
# Disconnect this PC (asks first)
powershell -ExecutionPolicy Bypass -File scripts\teardown-windows.ps1

# Also delete the agent from the tenant. Cannot be undone.
powershell -ExecutionPolicy Bypass -File scripts\teardown-windows.ps1 -DeleteAgent

# Also delete Entrabot's data folder on this PC
powershell -ExecutionPolicy Bypass -File scripts\teardown-windows.ps1 -PurgeData

# Disconnect, but keep the setup certificate because you are setting up again
powershell -ExecutionPolicy Bypass -File scripts\teardown-windows.ps1 -KeepSetupCredential
```

## Options

- `-DeleteAgent` — also delete the agent from the tenant (Agent User, Agent
  Identity, Blueprint), for every PC that uses it. Interactively, you must also
  type the agent's address to confirm.
- `-PurgeData` — also delete `%LOCALAPPDATA%\entrabot`.
- `-KeepSetupCredential` — keep this PC's setup (Provisioner) certificate. Use
  it when you are about to set this PC up again. Without it, teardown retires
  that certificate, because its key can manage applications in your tenant and
  should not stay on a PC you are done with. If the setup certificate is kept
  this way and teardown cannot remove an old setup certificate that setup noted
  as still to remove (see Notes), `.entrabot-state.json` is left holding only
  that note, so that setup's next run tries again.
- `-Yes` — do not ask. Only the scopes named by the other parameters are
  removed. In an unattended session, teardown refuses to run without it.
- `-Force` — older name for `-Yes`.
- `-Verbose` — show everything the helper scripts print.
- `-Help` — show help and exit `0`.

## Effects

It first shows what it will remove, and checks whether it can reach Microsoft
Entra, then asks. The suggested answer is No.

| | Default | `-DeleteAgent` | `-PurgeData` |
|---|---|---|---|
| This PC's certificate on the agent's Blueprint | Removed | Gone with the Blueprint | Removed |
| This PC's Blueprint certificate and its private key | Deleted | Deleted | Deleted |
| A certificate `.env` names that is not in this PC's certificate store | Left on the Blueprint (asked first, default No; left with `-Yes`) | Gone with the Blueprint | Left on the Blueprint (as default) |
| The `entrabot` entry in `.mcp.json` and in GitHub Copilot CLI's `mcp-config.json` | Removed; every other entry is kept | Same | Same |
| `.env` and `.entrabot-state.json` | Backed up as `<name>.bak.<stamp>`, then removed. When the setup certificate is kept with `-KeepSetupCredential` and an old setup certificate could not be removed, `.entrabot-state.json` is put back holding only setup's note (see Notes) | Same, once the agent is gone | Same |
| The agent (Agent User, Agent Identity, Blueprint) | Kept | Deleted | Kept |
| An old setup (Provisioner) certificate that setup noted it could not remove (`PROVISIONER_CERT_REVOKE_PENDING` in `.entrabot-state.json`) | Removed from the Provisioner app. If that fails, listed under "Still to do" with where to remove it by hand | Same, once the agent is gone | Same |
| This PC's setup (Provisioner) certificate | Retired: removed from the Provisioner app and deleted from this PC with its private key. Kept if another Entrabot agent on this PC still needs it, or with `-KeepSetupCredential` | Same | Same |
| `%LOCALAPPDATA%\entrabot` (local memory, logs, private Python and Azure CLI, and the .NET SDK and Agent 365 CLI if an earlier version of setup, or `prereqs-windows.ps1 -WithWorkIq`, installed them) | Kept | Kept | Deleted, except the Python this command is running from |
| `start-entrabot.cmd`, your `PATH`, the agent's sponsors, cloud storage | Never touched | Never touched | Never touched |

Notes:

- The certificate to remove is chosen by the thumbprint in `.env`, never by
  its subject, so another agent's certificate is not touched.
- `.env` says which certificate the folder was set up with, not that it is on
  this PC: a copied or shared `.env` names another PC's. So the certificate is
  taken off the Blueprint only when it is in this PC's certificate store. One
  that is not may be another PC's, and removing it would cut that PC off, so by
  default it is left on the Blueprint. With `-Yes` it is left, and the report
  lists it under "Kept" with where to remove it if no PC uses it. Interactively
  you are asked whether to remove it anyway; the suggested answer is No. If the
  certificate store cannot be opened at all, the certificate is also left on the
  Blueprint, and the report lists it as something left to do.
- A certificate rotation that could not finish leaves its old certificate's
  thumbprint in `.env` (`ENTRABOT_BLUEPRINT_CERT_RETIRING_SHA1`). Teardown
  cleans that certificate up too: when it is in this PC's store, it is taken off
  the Blueprint and deleted with its private key. When it is not, it is left on
  the Blueprint and reported; you are not asked about it.
- The certificates that come off the Blueprint go in one write. Graph replaces
  the whole list on every write, and a read can land on a copy that has not
  caught up yet, so a second removal could put back the certificate the first
  one took off.
- An older version of setup kept the setup key in a plaintext file. When setup
  moved this PC off that file but could not remove the file's certificate from
  the Provisioner app, it noted the certificate in `.entrabot-state.json`
  (`PROVISIONER_CERT_REVOKE_PENDING`) to try again. Teardown removes that file,
  so it tries the removal first, signed in through the Azure CLI as setup's
  retry is, and the plan says so before you are asked. If the removal fails, or
  Microsoft Entra cannot be reached, the rest of teardown still runs. The report
  lists the certificate under "Still to do" by the last four characters of its
  thumbprint and its expiry date, with where to remove it (App registrations >
  'EntraBot Agent ID Provisioner' > Certificates & secrets > Certificates), and
  teardown exits `1`. This PC's current setup certificate is never treated as an
  old one.
- When the setup certificate is kept with `-KeepSetupCredential` and that
  removal failed, teardown leaves `.entrabot-state.json` on purpose. It holds
  only the tenant ID (`TENANT_ID`), the Provisioner app's application ID
  (`PROVISIONER_CLIENT_ID`), and setup's note about the certificates still to
  remove (`PROVISIONER_CERT_REVOKE_PENDING` and
  `PROVISIONER_CERT_REVOKE_PENDING_EXPIRES`); the backup keeps the whole file.
  Setup's next run reads the note and tries again. While that file is all there
  is, `status-windows.ps1` says the folder is not connected to an agent, names
  the key that is still waiting on a second line, and exits `1`.
- If a private key cannot be deleted, the report says so, with how to remove it
  by hand, and teardown exits `1`. A certificate is reported as deleted with its
  key only when both are gone.
- Removing the certificate from the Blueprint means this PC stops being trusted
  now, not when the certificate expires. Other PCs' certificates on the same
  Blueprint are kept and keep working.
- The setup certificate is retired last, because removing this PC's certificate
  from the Blueprint needs it to sign in with. After it is retired, setting this
  PC up again starts by creating a new one, which needs the same administrator
  rights as a first setup.
- If Microsoft Entra cannot be reached, the certificate is still deleted from
  this PC with its key. It then cannot be used by anyone, and the report says
  where it is still listed and how to remove it.
- Cloud storage is never deleted. The report prints the
  [`deprovision_blob_storage.py`](../storage/deprovision-blob-storage-py.md)
  command to run if you want that.
- After `-PurgeData`, the project's `.venv` may no longer work, because the
  private Python lived in the data folder. `scripts\setup-windows.cmd` rebuilds
  it.
- If `-DeleteAgent` fails, `.env`, the state file and the setup certificate are
  kept so that a second attempt can work. An old setup certificate in setup's
  note is then not tried; it stays in the note for the next run.

Earlier versions of this script deleted the data folder by default. That now
needs `-PurgeData`.

## Exit behavior

- `0` — done, or there was nothing to do; or `-Help`.
- `1` — finished with something left to do, for example a private key that
  could not be deleted, or an old setup certificate that could not be removed
  from the Provisioner app. The report lists it and how to finish.
- `2` — usage: run unattended without `-Yes`.
- `130` — cancelled.
- `1` — `.venv` is missing.

## Common failures

- **"Could not reach Microsoft Entra"** — not signed in, or no rights. Local
  cleanup completed. The report names the Blueprint and the last four
  characters of the certificate so an administrator can remove it in the Entra
  admin center.
- **The agent was NOT deleted** — `-DeleteAgent` could not finish. Fix what the
  report names and run it again.
- **An old setup (Provisioner) certificate may still be listed on the
  Provisioner app** — teardown could not remove a certificate setup had noted.
  Remove it where the report says. If you kept the setup certificate
  (`-KeepSetupCredential`), running setup again also retries it.

## Related commands

- [Script reference — Teardown](../index.md#teardown)
- [`setup-windows.ps1`](../setup/setup-windows-ps1.md) — sets the PC up again.
- [`deprovision_entra_agent_identity.py`](deprovision-entra-agent-identity-py.md) —
  the script `-DeleteAgent` uses to delete the agent.
- [`deprovision_blob_storage.py`](../storage/deprovision-blob-storage-py.md) —
  removes cloud memory.
- [`teardown.sh`](teardown-sh.md) — the macOS and Linux equivalent.
