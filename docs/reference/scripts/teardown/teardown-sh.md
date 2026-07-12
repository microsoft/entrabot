# `teardown.sh`

macOS / Linux teardown entry point. Reverses everything `setup.sh` creates and,
optionally, deprovisions individual Agent User chains by UPN.

## Purpose

`teardown.sh` removes the full Agent Identity chain and local state that setup
creates: the Agent User, the Agent Identity service principal, the Blueprint app
registration (and its cascaded `BlueprintPrincipal`), the Provisioner app
registration, and the local `.env`, `.entrabot-state.json`, and OS keystore
entries.

It operates in one of two modes:

- **State-based full teardown** (default) — reads the resource IDs from `.env`
  and `.entrabot-state.json` and deletes the whole chain those IDs describe.
- **Targeted teardown** — when one or more `--agent-user-upn` values are given,
  it delegates each UPN to
  [`deprovision_entra_agent_identity.py`](deprovision-entra-agent-identity-py.md)
  and skips the state-based deletes.

Cloud (Azure Blob) storage is never deleted by this script. See
[Effects](#effects) and [Safety](#safety) below.

## Requirements

- macOS or Linux with `bash`.
- Azure CLI (`az`) signed in (`az login`). It is used to discover and delete the
  Provisioner app and to delete the Agent User, which are ordinary directory
  objects.
- The project virtual environment at `.venv` with provisioning dependencies
  installed (`pip install -e ".[dev]"`). The script uses `.venv/bin/python3` to
  mint a Provisioner certificate token and to call the Microsoft Graph beta
  endpoints that delete the Agent Identity and Blueprint.
- `python3` and `keyring` for reading state and clearing keystore entries.

If the Provisioner certificate token cannot be acquired, the script continues
but cannot delete the Agent Identity or Blueprint (see
[Common failures](#common-failures)).

## Usage

```bash
# Full teardown of the chain described by local state
./scripts/teardown.sh

# Preview only — resolve and print, delete nothing
./scripts/teardown.sh --dry-run

# Skip the interactive confirmation prompt
./scripts/teardown.sh --yes

# Targeted teardown of one or more Agent User chains by UPN
./scripts/teardown.sh --agent-user-upn=agent-sample@contoso.onmicrosoft.com
./scripts/teardown.sh --agent-user-upn=agent-a@contoso.onmicrosoft.com,agent-b@contoso.onmicrosoft.com

# Preserve the shared Provisioner app or local state
./scripts/teardown.sh --preserve-provisioner
./scripts/teardown.sh --preserve-local-state
```

## Options

| Option | Default | Effect |
| --- | --- | --- |
| `--agent-user-upn=UPN` | none | Target a specific Agent User UPN for deprovisioning instead of the state-based chain. May be repeated or comma-separated. Switches the run to targeted mode. |
| `--dry-run` | off | Resolve and print what would be deleted without deleting anything in the tenant or on disk. In targeted mode the flag is passed through to the deprovision helper. |
| `--yes`, `-y` | off | Skip the interactive `Are you sure?` confirmation prompt. |
| `--delete-cloud-storage` | off | **Reserved and intentionally not implemented.** Supplying it exits with an error (see [Safety](#safety)). |
| `--preserve-provisioner` | off | Do not delete the shared Provisioner app. Intended for targeted, smoke-test teardowns that reuse the Provisioner. |
| `--preserve-local-state` | off | Do not remove `.env`, `.entrabot-state.json`, or local keystore entries. Intended for wrapper-managed test runs. |
| `--help`, `-h` | — | Print usage and exit. |

An unrecognized argument prints an error to stderr followed by the usage text.

## Effects

When there is nothing to remove — no Entra resources referenced by local state,
no Provisioner app discoverable by name, and no local state files — the script
prints `Nothing to clean up.` and exits without prompting.

Otherwise it prints the resources it is about to delete and, unless `--dry-run`
or `--yes` is set, prompts for confirmation before proceeding.

**Deletion order (state-based full teardown).** Children are removed before
parents:

1. **Agent User** — deleted with `az ad user delete` (an Agent User is an
   ordinary directory user, so the Azure CLI token is accepted here).
2. **Agent Identity service principal** — deleted via `DELETE
   /beta/servicePrincipals/{id}` using the Provisioner certificate token.
3. **Blueprint application** — deleted via `DELETE /beta/applications/{id}`
   using the Provisioner certificate token. This cascade also removes the
   `BlueprintPrincipal`.
4. **Provisioner app** — deleted last (it is needed for steps 2–3), by display
   name, with `az ad app delete`, unless `--preserve-provisioner` is set.
5. **Local state** — unless `--preserve-local-state` is set: clears the
   Blueprint private key and legacy secrets from the OS keystore (service
   `entrabot`), then removes `.env` and `.entrabot-state.json`.

**Targeted teardown.** Each `--agent-user-upn` is handed to
[`deprovision_entra_agent_identity.py`](deprovision-entra-agent-identity-py.md),
which removes the Agent User's directly-assigned licenses, the Agent User, the
Agent Identity, and the Blueprint for that UPN. After a targeted run the
state-based deletes in steps 1–3 are skipped; step 4 (Provisioner) and step 5
(local state) still run unless the corresponding `--preserve-*` flag is set.

**Cloud storage.** Any Azure Blob container, storage account, or resource group
used for cloud-hosted memory is left untouched. Remove it separately with
[`deprovision_blob_storage.py`](../storage/deprovision-blob-storage-py.md).

## Safety

!!! danger "This permanently deletes Entra identities"
    A full teardown deletes the Agent User, Agent Identity, and Blueprint from
    the tenant. There is no undo. Run `--dry-run` first and confirm the printed
    resource IDs are the ones you intend to remove.

- **`--delete-cloud-storage` is refused.** The switch is reserved but not
  implemented: passing it prints an error explaining that cloud storage is not
  deleted here and exits non-zero, so no partial storage teardown can occur. Back
  up any cloud memory, then delete containers or accounts manually or with
  [`deprovision_blob_storage.py`](../storage/deprovision-blob-storage-py.md).
- **The state-based full teardown does not check for shared Blueprints.** Steps
  2–3 delete the Agent Identity and Blueprint named in local state
  unconditionally. If several Agent Identities share one Blueprint, a full
  teardown removes that Blueprint out from under the others. To delete a single
  chain while leaving a shared Blueprint intact, use targeted mode
  (`--agent-user-upn`), which delegates to
  [`deprovision_entra_agent_identity.py`](deprovision-entra-agent-identity-py.md)
  and refuses to delete a Blueprint still referenced by other Agent Identities.
- The confirmation prompt defaults to **No**; any answer other than `y`/`Y`
  aborts without changes.

## Common failures

- **No Provisioner token — Agent Identity / Blueprint not deleted.** If the
  `.venv` is missing or the Provisioner certificate is unavailable, the script
  warns and cannot delete the Agent Identity or Blueprint: the Agent Identity beta
  APIs reject Azure CLI tokens (which carry the `Directory.AccessAsUser.All`
  scope) with a hard `403`. The Agent Identity and Blueprint then survive as
  orphans. Delete them afterward with
  [`cleanup-orphans.sh`](cleanup-orphans-sh.md) or in the Entra admin portal.
- **`Not logged in`.** Provisioner discovery and Agent User deletion need
  `az login`.
- **Resource already deleted.** A `404` from a delete is treated as
  already-gone and reported as a warning, not a failure.

## Exit behavior

- `0` — success; also returned for `--help`, `--dry-run`, a `Nothing to clean
  up` no-op, an aborted confirmation, and a `--preserve-local-state` early exit.
- `1` — a targeted `--agent-user-upn` teardown failed.
- `2` — `--delete-cloud-storage` was supplied (refused).

Because the script runs under `set -euo pipefail`, an unexpected error in a
non-tolerated step also aborts with a non-zero status.

## Related commands

- [`deprovision_entra_agent_identity.py`](deprovision-entra-agent-identity-py.md) — targeted, shared-Blueprint-safe teardown of one UPN.
- [`cleanup-orphans.sh`](cleanup-orphans-sh.md) — delete Agent Identity / Blueprint orphans left after a token failure.
- [`teardown-windows.ps1`](teardown-windows-ps1.md) — local-only teardown on Windows.
- [`deprovision_blob_storage.py`](../storage/deprovision-blob-storage-py.md) — remove cloud-memory storage.
- [`setup.sh`](../setup/setup-sh.md) — the provisioning entry point this reverses.
- [Teardown reference index](../index.md#teardown)
- [Identity Lifecycle and Deprovisioning](../../../guides/identity-lifecycle.md)
- [Troubleshooting: Migrations and Upgrades](../../../troubleshooting/migrations-and-upgrades.md)
