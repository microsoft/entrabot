# `cleanup-orphans.sh`

Delete orphaned Blueprint and Agent Identity resources by object ID, using a
Provisioner certificate token. For cleaning up after a teardown that could not
delete those objects because the Azure CLI token was rejected.

## Purpose

The Agent Identity beta APIs reject Azure CLI tokens: an `az` token carries the
`Directory.AccessAsUser.All` scope, which those endpoints refuse with a hard
`403`. When a teardown does not have a Provisioner certificate token available,
the Blueprint and Agent Identity can survive as orphans in the tenant.

`cleanup-orphans.sh` deletes those specific objects. It mints a clean Microsoft
Graph token with the Provisioner's certificate (via
[`provisioner-token.py`](../auth-and-certs/provisioner-token-py.md)) and issues
the beta deletes with it.

The script does **not** discover orphans. You supply the object IDs explicitly;
it deletes exactly what you name and nothing else.

## Requirements

- macOS or Linux with `bash`.
- Azure CLI signed in (`az login`) — needed so the Provisioner token helper can
  bootstrap (and, if necessary, re-create) the Provisioner registration.
- The project virtual environment at `.venv` with provisioning dependencies, or
  a system `python3` that has them; the script prefers `.venv/bin/python3`.
- The object IDs of the orphans, obtained from the Entra admin portal
  (App registrations / Enterprise applications) or from the status command.

## Usage

```bash
# Delete an orphaned Blueprint only
./scripts/cleanup-orphans.sh <blueprint-object-id>

# Delete an orphaned Blueprint and its Agent Identity
./scripts/cleanup-orphans.sh <blueprint-object-id> <agent-identity-object-id>
```

Example with placeholder GUIDs (substitute your real object IDs):

```bash
./scripts/cleanup-orphans.sh \
  11111111-1111-1111-1111-111111111111 \
  22222222-2222-2222-2222-222222222222
```

## Options

This script takes positional arguments, not flags:

| Argument | Required | Effect |
| --- | --- | --- |
| `<blueprint-object-id>` | yes | Object ID of the Blueprint application to delete. |
| `<agent-identity-object-id>` | no | Object ID of the Agent Identity service principal to delete. When present, it is deleted before the Blueprint. |

Invoking the script with no arguments prints usage guidance and where to find
the object IDs.

## Effects

1. Confirm `az` is logged in and print the tenant and the object IDs that will
   be deleted.
2. Prompt for confirmation (`Get a Provisioner cert-auth token and delete
   these?`). Any answer other than `y`/`Y` aborts without changes.
3. **Step 1 — acquire token.** Mint a Graph token via the Provisioner
   certificate. The token is passed to the delete step through an environment
   variable, never on the command line, so it does not appear in `ps`.
4. **Step 2 — delete.** If an Agent Identity object ID was given, delete it
   first via `DELETE /beta/servicePrincipals/{id}`; then delete the Blueprint via
   `DELETE /beta/applications/{id}`. A `404` on either is reported as
   already-deleted.

The Provisioner app and its certificate in the OS keystore are left in place, so
you can run the script again. To remove the Provisioner too, run a full
[`teardown.sh`](teardown-sh.md).

## Safety

!!! danger "Deletes exactly the object IDs you pass"
    There is no discovery and no undo. Deleting the wrong object ID removes the
    wrong Blueprint or Agent Identity. Double-check each ID in the Entra admin
    portal before confirming.

- The delete step reports a failed delete (any non-2xx, non-404 status) but does
  not abort the run, so a Blueprint delete is still attempted even if the Agent
  Identity delete failed. Re-check the tenant afterward.
- The token never appears in the process argument list or on disk.

## Common failures

- **`Not logged in. Run: az login`.** The Provisioner token helper needs an
  Azure CLI session to bootstrap.
- **`Failed to acquire Provisioner token`.** The certificate or Provisioner
  registration is unavailable; the run exits before attempting any delete.
- **Delete reports a non-404 error.** Usually a wrong object ID or insufficient
  Provisioner permissions; confirm the ID and the Provisioner's directory role.

## Exit behavior

- `1` — no object ID was supplied, `az` is not logged in, or the Provisioner
  token could not be acquired.
- `0` — the confirmation was declined, or the delete step ran to completion
  (individual delete failures are printed but do not change the exit status).

## Related commands

- [`teardown.sh`](teardown-sh.md) — full teardown; orphans arise when its token fallback is used.
- [`deprovision_entra_agent_identity.py`](deprovision-entra-agent-identity-py.md) — targeted, shared-Blueprint-safe chain teardown.
- [`provisioner-token.py`](../auth-and-certs/provisioner-token-py.md) — mints the certificate token this script uses.
- [`show_agent_status.py`](../operations/show-agent-status-py.md) — inspect the chain and spot orphaned resources.
- [Teardown reference index](../index.md#teardown)
- [Identity Lifecycle and Deprovisioning](../../../guides/identity-lifecycle.md)
