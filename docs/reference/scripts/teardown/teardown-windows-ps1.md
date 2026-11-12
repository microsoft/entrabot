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
  should not stay on a PC you are done with.
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
| `.env` and `.entrabot-state.json` | Backed up as `<name>.bak.<stamp>`, then removed | Same, once the agent is gone | Same |
| The agent (Agent User, Agent Identity, Blueprint) | Kept | Deleted | Kept |
| This PC's setup (Provisioner) certificate | Retired: removed from the Provisioner app and deleted from this PC with its private key. Kept if another Entrabot agent on this PC still needs it, or with `-KeepSetupCredential` | Same | Same |
| `%LOCALAPPDATA%\entrabot` (local memory, logs, private Python and Azure CLI) | Kept | Kept | Deleted, except the Python this command is running from |
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
  kept so that a second attempt can work.

Earlier versions of this script deleted the data folder by default. That now
needs `-PurgeData`.

## Exit behavior

- `0` — done, or there was nothing to do; or `-Help`.
- `1` — finished with something left to do, for example a private key that
  could not be deleted. The report lists it and how to finish.
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

## Related commands

- [Script reference — Teardown](../index.md#teardown)
- [`setup-windows.ps1`](../setup/setup-windows-ps1.md) — sets the PC up again.
- [`deprovision_entra_agent_identity.py`](deprovision-entra-agent-identity-py.md) —
  the script `-DeleteAgent` uses to delete the agent.
- [`deprovision_blob_storage.py`](../storage/deprovision-blob-storage-py.md) —
  removes cloud memory.
- [`teardown.sh`](teardown-sh.md) — the macOS and Linux equivalent.
