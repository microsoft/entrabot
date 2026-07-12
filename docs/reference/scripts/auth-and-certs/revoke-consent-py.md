# revoke_consent.py

Remove specific scopes from, or entirely delete, the delegated consent grant
that lets the Agent Identity act as the Agent User. The inverse of
[`grant_consent.py`](grant-consent-py.md).

Part of the [auth-and-certs command reference](../index.md#auth-and-certs).

## Purpose

Pares down or deletes the Agent Identity → Agent User
`oauth2PermissionGrant`. Use it to remove delegated scopes the agent no longer
needs, or to fully revoke the grant. It operates only on the delegated consent
record (`oauth2PermissionGrants`); it does not touch application permissions
(`appRoleAssignments`) or any other resource.

See [Token flows → Consent records](../../token-flows.md#consent-records) for how
the grant is consumed at Hop 3.

## Requirements

- `.entrabot-state.json` must contain `AGENT_OBJECT_ID` (Agent Identity
  service-principal object ID) and `AGENT_USER_ID` (Agent User object ID).
- The Provisioner app must already exist with its certificate in the OS keystore;
  this command uses the existing-only token helper and never bootstraps it.
- The Provisioner needs `DelegatedPermissionGrant.ReadWrite.All`.

## Usage

```bash
# Remove specific scopes (comma- or space-separated)
python3 scripts/revoke_consent.py --scopes "Mail.Read,Files.ReadWrite"

# Delete the entire grant
python3 scripts/revoke_consent.py --all
```

## Options

- `--scopes SCOPES` — comma- or space-separated scope names to remove.
- `--all` — remove every current scope, which deletes the grant.
- `--help`, `-h` — print usage and exit.

Exactly one of `--scopes` or `--all` is required.

## Effects

1. Reads the Agent Identity and Agent User object IDs from state and mints a
   Provisioner Graph token (certificate key read from the OS keystore in memory
   only).
2. Finds the matching grant by `clientId` and `principalId` (first match).
3. Computes the change:
   - With `--all`, targets every current scope.
   - With `--scopes`, intersects the request with the grant's current scopes.
     Scopes not present in the grant are ignored.
4. Applies the smallest change:
   - **No requested scope is actually present** → prints a no-op message, makes
     no change.
   - **Scopes remain after removal** → `PATCH` the grant to the reduced scope
     set.
   - **No scopes remain** (or `--all`) → `DELETE` the grant entirely.

## Exit behavior

- `0` — scopes removed, grant deleted, or nothing to remove (no matching scopes).
- `1` — missing state, Provisioner token failure, no grant found for the
  Agent Identity/Agent User pair, or a Graph `PATCH`/`DELETE` failure.
- `2` — neither `--scopes` nor `--all` was supplied (usage error).
- `--help`/`-h` prints usage and exits `0`.

## Security

The Provisioner token and certificate material never touch disk or logs.
Revocation is immediate on the directory side, but tokens already minted remain
valid until they expire.

## Common failures

- **Revoking a still-needed scope** — later MCP tool calls that rely on it will
  fail with `403`; re-grant with [`grant_consent.py`](grant-consent-py.md).
- **Deleting the whole grant** — Hop 3 of the three-hop flow can no longer mint a
  delegated Agent User token until the grant is recreated.

## Related commands

- [`grant_consent.py`](grant-consent-py.md) — add scopes or create the grant.
- [`grant_files_consent.py`](grant-files-consent-py.md) — restore the Files/Sites
  scope set.
- [`show_permissions.py`](../operations/show-permissions-py.md) — confirm the
  remaining grants.
