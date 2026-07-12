# `deprovision_entra_agent_identity.py`

Targeted teardown of a single Agent User chain, identified by UPN. Safer than a
full [`teardown.sh`](teardown-sh.md) when a tenant holds several chains and you
want to remove only one.

## Purpose

Given one or more Agent User UPNs, this script resolves each Agent User's chain
— Agent User → Agent Identity service principal → parent Blueprint application —
and deletes it in a safe order, after first removing the Agent User's
directly-assigned licenses.

Crucially, it **refuses to delete a Blueprint that is still shared** by other
Agent Identities: before touching anything, it checks whether any other Agent
Identity references the same Blueprint, and aborts the whole operation if so.
This is the key difference from the state-based full teardown in
[`teardown.sh`](teardown-sh.md), which deletes the Blueprint named in local
state without a shared-Blueprint check.

## Requirements

- Python 3.12+ with the project's provisioning dependencies installed
  (`pip install -e ".[dev]"`); run it with `.venv/bin/python3` (macOS / Linux)
  or `.venv\Scripts\python.exe` (Windows).
- A usable Provisioner certificate: the script mints a Microsoft Graph token via
  the Provisioner's certificate (`get_existing_graph_token`). It does not use
  Azure CLI tokens, which the Agent Identity beta APIs reject.

## Usage

```bash
# Always dry-run first (the target UPN is required)
.venv/bin/python3 scripts/deprovision_entra_agent_identity.py \
  --agent-user-upn agent-sample@contoso.onmicrosoft.com --dry-run

# Perform the deletion
.venv/bin/python3 scripts/deprovision_entra_agent_identity.py \
  --agent-user-upn agent-sample@contoso.onmicrosoft.com

# Remove several chains in one run
.venv/bin/python3 scripts/deprovision_entra_agent_identity.py \
  --agent-user-upn agent-a@contoso.onmicrosoft.com \
  --agent-user-upn agent-b@contoso.onmicrosoft.com
```

On Windows, substitute `.venv\Scripts\python.exe`.

## Options

| Option | Default | Effect |
| --- | --- | --- |
| `--agent-user-upn UPN` | — (required) | UPN of the Agent User chain to deprovision. Repeat the flag to process several chains in one run. |
| `--dry-run` | off | Resolve and print each chain (Agent User, Agent Identity, Blueprint) and the shared-Blueprint check without deleting or mutating anything in the tenant. |

## Effects

For each UPN, in order:

1. **Resolve the chain.** Look up the Agent User by UPN, follow
   `identityParentId` to the Agent Identity service principal, and follow
   `agentIdentityBlueprintId` to the parent Blueprint application. A UPN that
   does not resolve to a user is reported as skipped (idempotent — not an
   error).
2. **Shared-Blueprint guard.** Enumerate the Agent Identities that reference the
   same Blueprint. If any Agent Identity other than this one is found, the run
   aborts before any deletion or license change — nothing is removed, so chains
   sharing that Blueprint stay intact.
3. **Remove licenses.** Remove the Agent User's directly-assigned license SKUs.
   Group-inherited licenses cannot be removed here; they are reported and are
   released automatically when the Agent User is deleted.
4. **Delete the Agent User.**
5. **Delete the Agent Identity** service principal.
6. **Delete the parent Blueprint** application (this also removes the
   `BlueprintPrincipal`).

A `--dry-run` performs steps 1–2 only and prints what steps 3–6 would remove.

### State boundaries

- **Local state is not touched.** `.env`, `.entrabot-state.json`, and OS keystore
  entries are left in place. Clear them with
  [`teardown.sh --preserve-provisioner`](teardown-sh.md) or by hand if you no
  longer need them.
- **Azure Blob storage is out of scope.** Cloud-hosted memory containers and
  accounts are not deleted. Use
  [`deprovision_blob_storage.py`](../storage/deprovision-blob-storage-py.md).

## Safety

!!! danger "Deletion is permanent"
    Deleting the Agent User, Agent Identity, and Blueprint cannot be undone.
    Always run `--dry-run` first and confirm the resolved chain is the one you
    intend to remove.

- The shared-Blueprint guard is fail-closed: it runs before any mutation, so a
  shared Blueprint is never partially torn down.
- License removal happens before the Agent User is deleted; if license removal
  fails, the script raises and stops before deleting the Agent User, Agent
  Identity, or Blueprint.

## Common failures

- **Provisioner bootstrap failure.** If the Provisioner certificate or its
  registration is unavailable, the token acquisition raises and the script exits
  non-zero without contacting Graph.
- **`Blueprint has N other Agent Identity object(s); refusing to delete shared
  Blueprint`.** Expected when the Blueprint backs more than one Agent Identity —
  delete only the Agent User and Agent Identity through other means, or retire
  the other chains first.
- **License removal rejected.** A non-2xx response to the license removal raises
  and halts the run before any object is deleted.

## Exit behavior

- `0` — every requested UPN was processed (deleted, dry-run resolved, or skipped
  because the Agent User was already absent).
- `1` — the Provisioner token could not be acquired, or processing a UPN raised
  (including the shared-Blueprint refusal and license-removal failure).

## Related commands

- [`teardown.sh`](teardown-sh.md) — full macOS / Linux teardown; delegates to this script for `--agent-user-upn` targets.
- [`cleanup-orphans.sh`](cleanup-orphans-sh.md) — delete Agent Identity / Blueprint orphans by object ID.
- [`teardown-windows.ps1`](teardown-windows-ps1.md) — local-only teardown on Windows.
- [`deprovision_blob_storage.py`](../storage/deprovision-blob-storage-py.md) — remove cloud-memory storage.
- [Teardown reference index](../index.md#teardown)
- [Identity Lifecycle and Deprovisioning](../../../guides/identity-lifecycle.md)
- [Storage Configuration guide](../../../guides/storage-configuration.md)
