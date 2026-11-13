# `teardown.sh`

macOS / Linux teardown entry point. Reverses everything `setup.sh` creates and,
optionally, deprovisions individual Agent User chains by UPN.

## Purpose

`teardown.sh` removes the full Agent Identity chain and local state that setup
creates: the Agent User, the Agent Identity service principal, the Blueprint app
registration (and its cascaded `BlueprintPrincipal`), the Provisioner app
registration recorded for this machine, and the local `.env`,
`.entrabot-state.json`, and OS keystore entries.

It operates in one of two modes:

- **State-based full teardown** (default) — reads the resource IDs from `.env`
  and `.entrabot-state.json` and deletes the whole chain those IDs describe.
  `.env` is read as data, never run as shell code, the way `entrabot.config`
  reads it: a UTF-8 byte-order mark and CRLF line ends are tolerated, blank
  lines, comments and lines without `=` are skipped, each line is split at the
  first `=`, key and value are trimmed (a value is not unquoted), and the first
  value of a repeated key wins. Only `ENTRABOT_AGENT_USER_ID`,
  `ENTRABOT_AGENT_OBJECT_ID`, `ENTRABOT_BLUEPRINT_APP_ID` and
  `ENTRABOT_BLUEPRINT_OBJECT_ID` are read. A value `.env` holds wins over the
  state file.
- **Targeted teardown** — when one or more `--agent-user-upn` values are given,
  it delegates each UPN to
  [`deprovision_entra_agent_identity.py`](deprovision-entra-agent-identity-py.md)
  and skips the state-based deletes.

Cloud (Azure Blob) storage is never deleted by this script. See
[Effects](#effects) and [Safety](#safety) below.

## Requirements

- macOS or Linux with `bash`.
- Azure CLI (`az`) signed in (`az login`). It is used to delete the Agent User
  and the recorded Provisioner app, which are ordinary directory objects.
- The project virtual environment at `.venv` with provisioning dependencies
  installed (`pip install -e ".[dev]"`). The script uses `.venv/bin/python3` to
  mint a Provisioner certificate token and to call the Microsoft Graph beta
  endpoints that delete the Agent Identity and Blueprint.
- `python3` and `keyring` for reading state and clearing keystore entries.

If the Provisioner certificate token cannot be acquired, the script shows why
and continues, but cannot delete the Agent Identity or Blueprint (see
[Common failures](#common-failures)).

The token never goes on a command line, where any local user can read it from
the process list, or into program text. Each Graph delete gets it in the
environment of that one command, and it is not exported to anything else.

## Usage

```bash
# Full teardown of the chain described by local state
./scripts/teardown.sh

# Preview only — print what would be deleted, delete nothing
./scripts/teardown.sh --dry-run

# Preview a targeted teardown: each chain is looked up in Entra (read-only) and printed
./scripts/teardown.sh --dry-run --agent-user-upn=agent-sample@contoso.onmicrosoft.com

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
| `--agent-user-upn=UPN` | none | Target a specific Agent User UPN for deprovisioning instead of the state-based chain. May be repeated or comma-separated; each UPN is trimmed and otherwise passed on exactly as given. Switches the run to targeted mode. |
| `--dry-run` | off | Print what would be deleted, then exit without deleting anything in the tenant or on disk. In state-based mode it runs nothing else: no token, no `az`. In targeted mode it also runs [`deprovision_entra_agent_identity.py`](deprovision-entra-agent-identity-py.md) with `--dry-run`, which looks each Agent User's chain up in Entra (read-only, with its own Provisioner certificate token) and prints it, along with its shared-Blueprint check; a lookup that fails exits `1`. The chain local state names is not listed in targeted mode, because a targeted teardown leaves it alone. |
| `--yes`, `-y` | off | Skip the interactive `Are you sure?` confirmation prompt. |
| `--delete-cloud-storage` | off | **Reserved and intentionally not implemented.** Supplying it exits with an error (see [Safety](#safety)). |
| `--preserve-provisioner` | off | Do not delete the shared Provisioner app. Intended for targeted, smoke-test teardowns that reuse the Provisioner. |
| `--preserve-local-state` | off | Do not remove `.env`, `.entrabot-state.json`, or local keystore entries. Intended for wrapper-managed test runs. |
| `--help`, `-h` | — | Print usage and exit. |

An unrecognized argument prints an error to stderr followed by the usage text.

## Effects

When there is nothing to remove — no Entra resources referenced by local state
and no local state files — the script prints `Nothing to clean up.` and exits
without prompting. Unless `--preserve-provisioner` is set, it also says that no
Provisioner app is recorded for this machine, so none is deleted, and how to
delete one by hand.

Otherwise it prints the resources it is about to delete, including
`Provisioner: <application ID> (recorded in .entrabot-state.json)`, or a line
saying that no application ID is recorded and none will be deleted. Unless
`--dry-run` or `--yes` is set, it then prompts for confirmation before
proceeding.

**Deletion order (state-based full teardown).** Children are removed before
parents:

1. **Agent User** — deleted with `az ad user delete --id` (an Agent User is an
   ordinary directory user, so the Azure CLI token is accepted here), and only
   by its object ID: `--id` also takes a sign-in name, so a recorded value that
   is not a GUID is shown and not used. Whatever `az` prints is shown. When `az`
   says the user does not exist it is reported as already deleted; any other
   failure shows `az`'s error and the command to run once it is fixed.
2. **Agent Identity service principal** — deleted via `DELETE
   /beta/servicePrincipals/{id}` using the Provisioner certificate token.
3. **Blueprint application** — deleted via `DELETE /beta/applications/{id}`
   using the Provisioner certificate token. This cascade also removes the
   `BlueprintPrincipal`.

   For steps 2 and 3 the object ID is an argument of the program and one URL
   path segment, never part of the program text. A `404` is reported as already
   deleted. Any other answer shows Graph's error on stderr and the status, and
   a Graph that can't be reached shows why. Each request gives up after 60
   seconds.
4. **Provisioner app** — deleted last (it is needed for steps 2–3) with
   `az ad app delete --id`, unless `--preserve-provisioner` is set. Only the app
   recorded for this machine in `.entrabot-state.json` (`PROVISIONER_CLIENT_ID`)
   is deleted, by its application ID. Teardown never deletes an app found by its
   name: another app can have the same name, and a Provisioner app holds
   tenant-wide Microsoft Graph permissions. With no application ID recorded, or
   a recorded value that is not one, no app is deleted, and teardown prints how
   to find this machine's Provisioner app in the Entra admin center and delete
   it by hand. If the delete fails, teardown shows the `az` error and the
   command to run once it is fixed, and carries on; step 5 still removes the
   state file (unless `--preserve-local-state` is set), so keep the
   application ID it printed.
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

- **`ERROR: .env exists but cannot be read`** — teardown stops (exit `1`)
  before deleting anything, because it can't tell which objects `.env` names.
  Fix the file's permissions and run it again.
- **"The Agent User ID recorded for this machine is not an object ID"** —
  `ENTRABOT_AGENT_USER_ID` (or `AGENT_USER_ID` in the state file) is not a GUID,
  so no Agent User is deleted. The value is printed; find the Agent User's
  object ID in the Entra admin center and delete it with
  `az ad user delete --id <object ID>`.
- **No Provisioner token — Agent Identity / Blueprint not deleted.** If the
  `.venv` is missing or the Provisioner certificate is unavailable, the script
  shows why (`Could not get provisioner token: ...`), warns, and cannot delete
  the Agent Identity or Blueprint: the Agent Identity beta
  APIs reject Azure CLI tokens (which carry the `Directory.AccessAsUser.All`
  scope) with a hard `403`. The Agent Identity and Blueprint then survive as
  orphans. Delete them afterward with
  [`cleanup-orphans.sh`](cleanup-orphans-sh.md) or in the Entra admin portal.
- **`Not logged in`.** Deleting the Agent User and the Provisioner app needs
  `az login`.
- **"No Provisioner app is recorded for this machine in .entrabot-state.json,
  so none is deleted."** (or "PROVISIONER_CLIENT_ID in .entrabot-state.json is
  not an application ID") — `.entrabot-state.json` is missing or names no
  Provisioner app by its application ID, and teardown does not look for one by
  name. The rest of teardown still runs. To delete this machine's Provisioner
  app yourself, find its application ID in the Entra admin center (App
  registrations > All applications: `EntraBot Agent ID Provisioner`, or
  `EntraBot Provisioner` from older setups), check that it is the one setup
  created for this machine, then run `az ad app delete --id <PROVISIONER_APP_ID>`.
- **"Could not delete Provisioner app (<application ID>): see the az error
  above"** — for example, the `az` session lacks the rights. Fix what the error
  says, then run the command teardown printed:
  `az ad app delete --id <application ID>`.
- **Resource already deleted.** A `404` from a Graph delete, or `az` saying the
  Agent User does not exist, is treated as already-gone and reported as a
  warning, not a failure.
- **`Could not delete Agent User (...)`, `Could not delete Agent Identity SP
  (..., status: ...)` or `Could not delete Blueprint (..., status: ...)`** —
  the reason is printed just above it (`az`'s error, or `Microsoft Graph
  answered <status>: ...`). Teardown carries on and still removes local state
  (unless `--preserve-local-state` is set), so keep the IDs it printed.

## Exit behavior

- `0` — success; also returned for `--help`, `--dry-run`, a `Nothing to clean
  up` no-op, an aborted confirmation, and a `--preserve-local-state` early exit.
  A Provisioner app that is not recorded, or that could not be deleted, is
  reported as a warning and does not change it.
- `1` — a targeted `--agent-user-upn` teardown or dry run failed, or `.env`
  exists but cannot be read.
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
