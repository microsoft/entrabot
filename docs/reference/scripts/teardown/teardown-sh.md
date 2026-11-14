# `teardown.sh`

Platforms: macOS, Linux.

## Purpose

`teardown.sh` has two teardowns:

- **Disconnect this computer** (the default). It removes only what belongs to
  this computer and leaves the agent, its memory, and every other computer that
  uses it alone. `--delete-agent` also deletes the agent from the tenant, and
  `--purge-data` also deletes Entrabot's data folder. This is the same teardown
  Windows runs from [`teardown-windows.ps1`](teardown-windows-ps1.md): the work
  is done by `python -m entrabot.winsetup teardown`.
- **Tenant-wide teardown**, for test tenants and wrappers. It deletes the
  agent's whole chain (Agent User, Agent Identity, Blueprint), the Provisioner
  app recorded for this computer, and local state. It runs only with
  `--tenant-wide` or one of its own flags (`--agent-user-upn=`, `--dry-run`,
  `--preserve-provisioner`, `--preserve-local-state`, `--delete-cloud-storage`).

!!! warning "`--yes` no longer deletes the agent"
    Before the default changed, `./scripts/teardown.sh --yes` deleted the
    agent's whole chain from the tenant. It now only disconnects this computer.
    To delete the agent, add `--delete-agent`. A wrapper that relied on the old
    behaviour can pass `--tenant-wide --yes`.

On macOS and Linux the default teardown is covered by unit tests, and this
script's choice between the two teardowns was run for real under bash with a
stub interpreter. It has not been run against a real tenant from macOS or Linux;
on Windows the same teardown has been. The tenant-wide teardown is this script's
earlier teardown, unchanged apart from also deleting this agent's own key store
item. See
[What has been tested](../../../getting-started/macos-linux.md#what-has-been-tested).

## Requirements

Disconnect this computer:

- macOS or Linux with `bash`.
- `.venv` in the Entrabot folder, from a [`setup.sh`](../setup/setup-sh.md)
  run. Without it, teardown says there is nothing to remove when the folder has
  no `.env` and no `.entrabot-state.json`, and otherwise stops (exit `1`).
- The account's key store (the macOS Keychain or the Linux keyring), unlocked in
  this session: the certificate and its key are deleted there.
- To change anything in Microsoft Entra: a signed-in Azure CLI session with the
  rights setup needed (the sign-in setup made is used). Without one, the local
  cleanup still completes and the report says what is still listed in Microsoft
  Entra.

Tenant-wide teardown:

- The Azure CLI (`az`), signed in: it deletes the Agent User and the recorded
  Provisioner app. When no `az` is on `PATH`, the private copy setup may have
  installed (`~/.entrabot/tools/azure-cli`) is used.
- `.venv` with the provisioning packages, from a `setup.sh` run. It mints a
  Provisioner certificate token to delete the Agent Identity and Blueprint,
  which reject Azure CLI tokens.
- `python3` and `keyring`, to read state and clear key store entries.

## Usage

```bash
# Disconnect this computer (asks first)
./scripts/teardown.sh

# Also delete the agent from the tenant. Cannot be undone.
./scripts/teardown.sh --delete-agent

# Also delete Entrabot's data folder on this computer
./scripts/teardown.sh --purge-data

# Disconnect, but keep the setup certificate because you are setting up again
./scripts/teardown.sh --keep-setup-credential

# Unattended: only what the other flags name is removed
./scripts/teardown.sh --yes

# Tenant-wide teardown of the chain described by local state
./scripts/teardown.sh --tenant-wide

# Tenant-wide, preview only
./scripts/teardown.sh --dry-run

# Tenant-wide, targeted by Agent User
./scripts/teardown.sh --agent-user-upn=agent-sample@contoso.onmicrosoft.com

# Preview a targeted teardown: each chain is looked up in Entra (read-only) and printed
./scripts/teardown.sh --dry-run --agent-user-upn=agent-sample@contoso.onmicrosoft.com
```

## Options

The value of `--log-file` and `--agent-user-upn` may follow after `=` or after a space.

Disconnect this computer:

| Option | Effect |
| --- | --- |
| `--delete-agent` | Also delete the agent from the tenant (Agent User, Agent Identity, Blueprint), for every computer that uses it. Cannot be undone. At a terminal you must also type the agent's address to confirm. |
| `--purge-data` | Also delete Entrabot's data folder on this computer, `~/.entrabot`: memory kept here, logs, and the private Azure CLI. |
| `--keep-setup-credential` | Keep this computer's setup (Provisioner) certificate, for setting up again soon. By default it is retired once no other Entrabot agent here, and no other folder set up for this agent, needs it, because its key can manage applications in your tenant. |
| `--yes`, `-y` | Do not ask. Only what the other flags name is removed. Without a terminal, teardown refuses to run without it. |
| `--verbose` | Show everything the helper scripts print. |
| `--log-file=PATH` | Where to write the log. Default: `~/.entrabot/logs/teardown-<date>-<time>.log`. |

Tenant-wide teardown (any of these selects it):

| Option | Effect |
| --- | --- |
| `--tenant-wide` | Run the tenant-wide teardown with none of the changes below: what `./scripts/teardown.sh` did before it had the default above. |
| `--agent-user-upn=UPN` | Target a specific Agent User UPN for deprovisioning instead of the state-based chain. May be repeated or comma-separated; each UPN is trimmed and otherwise passed on exactly as given. |
| `--dry-run` | Print what would be deleted, then exit without deleting anything in the tenant or on disk. In state-based mode it runs nothing else: no token, no `az`. In targeted mode it also runs [`deprovision_entra_agent_identity.py`](deprovision-entra-agent-identity-py.md) with `--dry-run`, which looks each Agent User's chain up in Entra (read-only, with its own Provisioner certificate token) and prints it, along with its shared-Blueprint check; a lookup that fails exits `1`. The chain local state names is not listed in targeted mode, because a targeted teardown leaves it alone. |
| `--preserve-provisioner` | Do not delete the shared Provisioner app. Intended for targeted, smoke-test teardowns that reuse it. |
| `--preserve-local-state` | Do not remove `.env`, `.entrabot-state.json`, or local key store entries. Intended for wrapper-managed test runs. |
| `--delete-cloud-storage` | **Reserved and intentionally not implemented.** Supplying it exits with an error (see [Safety](#safety)). |

`--yes` works with both teardowns. The two sets of options cannot be mixed:
`--delete-agent` with `--tenant-wide`, for example, is refused with exit `2`.
`--help` (`-h`) prints the usage and exits `0`. An unrecognized option prints an
error to stderr followed by the usage, changes nothing, and exits `2`.

## Effects

### Disconnect this computer

It first shows what it will remove, and checks whether it can reach Microsoft
Entra, then asks "Disconnect this computer?" (with `--delete-agent`, "Delete the
agent and disconnect this computer?"). The suggested answer is No.

| | Default | `--delete-agent` | `--purge-data` |
|---|---|---|---|
| This computer's certificate on the agent's Blueprint | Removed | Gone with the Blueprint | Removed |
| This computer's certificate and its private key (one Keychain or keyring item) | Deleted | Deleted | Deleted |
| An older copy of the same key in the account's single `entrabot` / `blueprint-private-key` item | Deleted | Deleted | Deleted |
| A certificate `.env` names that is not in this computer's key store | Left on the Blueprint (asked first, default No; left with `--yes`) | Gone with the Blueprint | Left on the Blueprint (as default) |
| The `entrabot` entry in `.mcp.json` and in GitHub Copilot CLI's `mcp-config.json` | Removed; every other entry is kept | Same | Same |
| `.env` and `.entrabot-state.json` | Backed up as `<name>.bak.<stamp>`, then removed | Same, once the agent is gone | Same |
| The agent (Agent User, Agent Identity, Blueprint) | Kept | Deleted | Kept |
| An old setup (Provisioner) certificate that setup noted it could not remove (`PROVISIONER_CERT_REVOKE_PENDING`) | Removed from the Provisioner app; if that fails, listed under "Still to do" | Same, once the agent is gone | Same |
| This computer's setup (Provisioner) certificate | Retired: removed from the Provisioner app and deleted from the key store with its private key. Kept if another Entrabot agent on this account still needs it, if this same agent is set up in a second folder on this account (it then has another certificate in the same store), or with `--keep-setup-credential` | Same | Same |
| `~/.entrabot` (local memory, logs, the private Azure CLI) | Kept | Kept | Deleted |
| `start-entrabot.sh`, `.venv`, the agent's sponsors, cloud storage | Never touched | Never touched | Never touched |

Notes:

- The certificate to remove is chosen by the SHA-1 thumbprint in `.env`
  (`ENTRABOT_BLUEPRINT_CERT_SHA1`), never by its name, so another agent's
  certificate is not touched. A certificate is taken off the Blueprint only when
  it is in this computer's key store: one that is not may be another computer's
  (a copied or shared `.env`).
- An older single `entrabot` / `blueprint-private-key` item that holds a
  different key belongs to another agent and is left alone. While one is there,
  the setup certificate is kept for that agent.
- A computer set up before the wizard (`.env` names no SHA-1 thumbprint, and the
  older single item holds a key) is refused, with nothing changed: teardown
  cannot tell whether that key is this agent's. It says "Run ./scripts/setup.sh
  once", which stores the key as the agent's own item; then run teardown again.
  `--tenant-wide` works on such a computer as it always did.
- A certificate rotation that could not finish leaves its old certificate's
  thumbprint in `.env` (`ENTRABOT_BLUEPRINT_CERT_RETIRING_SHA1`). Teardown
  cleans that certificate up too, in the same write to the Blueprint.
- If a private key cannot be deleted, the report says so, with where to delete
  it by hand (Keychain Access on a Mac, or your keyring manager on Linux, under
  `entrabot-blueprint-cert` or `entrabot-provisioner-cert`), and teardown exits
  `1`. A certificate is reported as deleted only when it and its key are both
  gone.
- If Microsoft Entra cannot be reached, the certificate is still deleted from
  this computer with its key, so it cannot be used by anyone, and the report says
  where it is still listed.
- If `--delete-agent` fails, `.env`, the state file and the setup certificate
  are kept so that a second attempt can work.
- Cloud storage is never deleted. After `--delete-agent`, the report prints the
  [`deprovision_blob_storage.py`](../storage/deprovision-blob-storage-py.md)
  command to run if you want that.
- `--purge-data` leaves `.venv` in place. It deletes the private Azure CLI with
  the rest of `~/.entrabot`; the next `./scripts/setup.sh` offers to install it
  again if there is no other.

The [`teardown-windows.ps1` reference](teardown-windows-ps1.md) describes the
same teardown in more detail; only the names of places differ.

### Tenant-wide teardown

When there is nothing to remove (no Entra resources referenced by local state
and no local state files), it prints `Nothing to clean up.` and exits without
asking. Unless `--preserve-provisioner` is set, it also says that no Provisioner
app is recorded for this computer, so none is deleted, and how to delete one by
hand.

Otherwise it prints the resources it is about to delete, including
`Provisioner: <application ID> (recorded in .entrabot-state.json)`, or a line
saying that no application ID is recorded and none will be deleted. Unless
`--dry-run` or `--yes` is set, it then asks `Are you sure? (y/N)`.

`.env` is read as data, never run as shell code, the way `entrabot.config`
reads it: a UTF-8 byte-order mark and CRLF line ends are tolerated, blank
lines, comments and lines without `=` are skipped, each line is split at the
first `=`, key and value are trimmed (a value is not unquoted), and the first
value of a repeated key wins. Only `ENTRABOT_AGENT_USER_ID`,
`ENTRABOT_AGENT_OBJECT_ID`, `ENTRABOT_BLUEPRINT_APP_ID` and
`ENTRABOT_BLUEPRINT_OBJECT_ID` are read. A value `.env` holds wins over the
state file. A `.env` that is there but can't be read stops teardown (exit `1`)
before anything is deleted.

The Provisioner token never goes on a command line, where any local user can
read it from the process list, or into program text. Each Graph delete gets it
in the environment of that one command, and it is not exported to anything
else. If the token cannot be acquired, the script shows why and continues, but
cannot delete the Agent Identity or Blueprint (see
[Common failures](#common-failures)).

**Deletion order (state-based).** Children are removed before parents:

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
   recorded for this computer in `.entrabot-state.json` (`PROVISIONER_CLIENT_ID`)
   is deleted, by its application ID. Teardown never deletes an app found by its
   name: another app can have the same name, and a Provisioner app holds
   tenant-wide Microsoft Graph permissions. With no application ID recorded, or
   a recorded value that is not one, no app is deleted, and teardown prints how
   to find this computer's Provisioner app in the Entra admin center and delete
   it by hand. If the delete fails, teardown shows the `az` error and the
   command to run once it is fixed, and carries on; step 5 still removes the
   state file (unless `--preserve-local-state` is set), so keep the
   application ID it printed.
5. **Local state** — unless `--preserve-local-state` is set: clears the older
   key store entries under service `entrabot` (`blueprint-private-key`,
   `blueprint-private-key.pending`, and older secrets), deletes this agent's own
   certificate item named in `.env` (and one a rotation left behind), then
   removes `.env` and `.entrabot-state.json`.

**Targeted teardown.** Each `--agent-user-upn` is handed to
[`deprovision_entra_agent_identity.py`](deprovision-entra-agent-identity-py.md),
which removes the Agent User's directly assigned licences, the Agent User, the
Agent Identity, and the Blueprint for that UPN. After a targeted run the
state-based deletes in steps 1–3 are skipped; step 4 (Provisioner) and step 5
(local state) still run unless the corresponding `--preserve-*` flag is set.

**Cloud storage.** Any Azure Blob container, storage account, or resource group
used for cloud memory is left untouched. Remove it separately with
[`deprovision_blob_storage.py`](../storage/deprovision-blob-storage-py.md).

## Safety

!!! danger "The tenant-wide teardown and `--delete-agent` permanently delete Entra identities"
    They delete the Agent User, Agent Identity, and Blueprint from the tenant,
    for every computer that uses them. There is no undo. For the tenant-wide
    teardown, run `--dry-run` first and confirm the printed resource IDs are the
    ones you intend to remove.

- **`--delete-cloud-storage` is refused.** The switch is reserved but not
  implemented: passing it prints an error explaining that cloud storage is not
  deleted here and exits `2`, so no partial storage teardown can occur. Back up
  any cloud memory, then delete containers or accounts by hand or with
  [`deprovision_blob_storage.py`](../storage/deprovision-blob-storage-py.md).
- **The state-based tenant-wide teardown does not check for shared Blueprints.**
  Steps 2–3 delete the Agent Identity and Blueprint named in local state
  unconditionally. If several Agent Identities share one Blueprint, it removes
  that Blueprint out from under the others. To delete a single chain while
  leaving a shared Blueprint intact, use targeted mode (`--agent-user-upn`),
  which delegates to
  [`deprovision_entra_agent_identity.py`](deprovision-entra-agent-identity-py.md)
  and refuses to delete a Blueprint still referenced by other Agent Identities.
- **The tenant-wide teardown clears the older `entrabot` key store entries
  whatever they hold**, as it always did, including a key another agent set up
  before the wizard may still use.
- Both confirmation prompts default to **No**.

## Exit behavior

Disconnect this computer:

- `0` — done, or there was nothing to do; or `--help`.
- `1` — finished with something left to do (for example a private key that
  could not be deleted, or an old setup certificate that could not be removed
  from the Provisioner app); the report lists it and how to finish. Also `1` when
  `.venv` is missing, and for a computer set up before the wizard (nothing
  changed; run `./scripts/setup.sh` once).
- `2` — usage: run without a terminal and without `--yes`, options from both
  teardowns, or `--delete-agent` when this computer's settings do not say which
  agent to delete.
- `130` — cancelled, or "no" at the question.

Tenant-wide teardown:

- `0` — success; also returned for a `--dry-run`, a `Nothing to clean up`
  no-op, an aborted confirmation, and a `--preserve-local-state` early exit. A
  Provisioner app that is not recorded, or that could not be deleted, is
  reported as a warning and does not change it.
- `1` — a targeted `--agent-user-upn` teardown or dry run failed, or `.env`
  exists but cannot be read.
- `2` — `--delete-cloud-storage` was supplied (refused).

Because the script runs under `set -euo pipefail`, an unexpected error in a
non-tolerated step also aborts with a non-zero status.

## Common failures

- **"Could not reach Microsoft Entra"** (disconnect) — not signed in, or no
  rights. Local cleanup completed. The report names the Blueprint and the last
  four characters of the certificate's thumbprint so an administrator can remove
  it in the Entra admin center. Run `./scripts/setup.sh` to sign in, then run
  teardown again.
- **"This computer was set up by an earlier version of Entrabot's setup"** —
  run `./scripts/setup.sh` once, then teardown again.
- **"Teardown needs a yes before it removes anything."** — run it in a terminal,
  or add `--yes`.
- **The agent was NOT deleted** — `--delete-agent` could not finish. Fix what the
  report names and run it again.
- **The agent was not deleted, and you expected it to be** — `--yes` alone only
  disconnects this computer now. Add `--delete-agent`.
- **No Provisioner token — Agent Identity / Blueprint not deleted**
  (tenant-wide). If `.venv` is missing or the Provisioner certificate is
  unavailable, the script warns and cannot delete the Agent Identity or
  Blueprint: the Agent Identity beta APIs reject Azure CLI tokens (which carry
  the `Directory.AccessAsUser.All` scope) with a hard `403`. They then survive as
  orphans. Delete them afterward with [`cleanup-orphans.sh`](cleanup-orphans-sh.md)
  or in the Entra admin portal.
- **"No Provisioner app is recorded for this machine in .entrabot-state.json,
  so none is deleted."** (tenant-wide; or "PROVISIONER_CLIENT_ID in
  .entrabot-state.json is not an application ID") — `.entrabot-state.json` is
  missing or names no Provisioner app by its application ID, and teardown does
  not look for one by name. The rest of teardown still runs. To delete this
  computer's Provisioner app yourself, find its application ID in the Entra
  admin center (App registrations > All applications:
  `EntraBot Agent ID Provisioner`, or `EntraBot Provisioner` from older setups),
  check that it is the one setup created for this computer, then run
  `az ad app delete --id <PROVISIONER_APP_ID>`.
- **"Could not delete Provisioner app (<application ID>): see the az error
  above"** (tenant-wide) — for example, the `az` session lacks the rights. Fix
  what the error says, then run the command teardown printed.
- **`ERROR: .env exists but cannot be read`** (tenant-wide) — teardown stops
  (exit `1`) before deleting anything, because it can't tell which objects
  `.env` names. Fix the file's permissions and run it again.
- **"The Agent User ID recorded for this machine is not an object ID"**
  (tenant-wide) — `ENTRABOT_AGENT_USER_ID` (or `AGENT_USER_ID` in the state
  file) is not a GUID, so no Agent User is deleted. The value is printed; find
  the Agent User's object ID in the Entra admin center and delete it with
  `az ad user delete --id <object ID>`.
- **`Could not delete Agent User (...)`, `Could not delete Agent Identity SP
  (..., status: ...)` or `Could not delete Blueprint (..., status: ...)`**
  (tenant-wide) — the reason is printed just above it (`az`'s error, or
  `Microsoft Graph answered <status>: ...`). Teardown carries on and still
  removes local state (unless `--preserve-local-state` is set), so keep the IDs
  it printed.
- **Resource already deleted** (tenant-wide). A `404` from a Graph delete, or
  `az` saying the Agent User does not exist, is treated as already gone and
  reported as a warning, not a failure.

## Related commands

- [Teardown reference index](../index.md#teardown)
- [`setup.sh`](../setup/setup-sh.md) — sets this computer up again.
- [`teardown-windows.ps1`](teardown-windows-ps1.md) — the same disconnect on
  Windows.
- [`deprovision_entra_agent_identity.py`](deprovision-entra-agent-identity-py.md) —
  the script `--delete-agent` and targeted teardown use; refuses to delete a
  shared Blueprint.
- [`cleanup-orphans.sh`](cleanup-orphans-sh.md) — delete Agent Identity /
  Blueprint orphans left after a token failure.
- [`deprovision_blob_storage.py`](../storage/deprovision-blob-storage-py.md) —
  remove cloud-memory storage.
- [Identity Lifecycle and Deprovisioning](../../../guides/identity-lifecycle.md)
