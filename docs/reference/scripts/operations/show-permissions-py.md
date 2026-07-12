# `scripts/show_permissions.py`

## Purpose

Show the delegated `oauth2PermissionGrants` scoped to the Agent Identity's
service principal and Agent User — a focused, permissions-only view.
Compare against [`show_agent_status.py`](show-agent-status-py.md), which
includes this same data plus the rest of the Agent Identity chain.

## Requirements

- Python 3.12+ with `entrabot` installed.
- The Provisioner app must already be bootstrapped with its certificate
  private key present in the OS keystore — this command mints its Graph
  token the same way `show_agent_status.py` does (Provisioner
  `CertificateCredential`), **not** the Agent User three-hop flow.
- `.entrabot-state.json` must contain `AGENT_OBJECT_ID` and
  `AGENT_USER_ID`.
- Works on macOS, Linux, and Windows.

## Usage

```bash
python scripts/show_permissions.py
python scripts/show_permissions.py --json
python scripts/show_permissions.py --help
```

## Options

- `--json` — print the grants as a JSON array (each entry has `id`,
  `resourceId`, `scopes`, `consentType`, and `resourceName` when it could
  be resolved) instead of the human-readable text block.

No positional arguments.

## Effects

- Acquires a Provisioner Graph token (read-only).
- Reads `AGENT_OBJECT_ID` and `AGENT_USER_ID` from local state.
- GETs `/oauth2PermissionGrants?$filter=clientId eq '{agentOid}' and
  principalId eq '{agentUserId}'`.
- For each distinct `resourceId` on the returned grants, GETs
  `/servicePrincipals/{resourceId}` to resolve a display name — best
  effort; a failed lookup is silently skipped, not fatal.
- Prints either the JSON array, or per grant: the resolved resource name
  (or the raw `resourceId` if unresolved), consent type, and
  space-separated scopes, followed by a total grant count.
- No writes to Graph or local state.

## Exit behavior

- Returns `1` if the Provisioner token can't be acquired
  (`ProvisionerBootstrapError`; message printed to stdout).
- Returns `1` if `AGENT_OBJECT_ID` or `AGENT_USER_ID` is missing from
  state, with a hint to run `setup.sh` first (printed to stdout).
- Returns `1` if the `/oauth2PermissionGrants` GET responds with a
  non-200 status (status code and body printed to stdout).
- Returns `0` in every other case, including when zero grants are found
  (prints "No permission grants found for this Agent Identity.").
- `--help` exits `0` (argparse default).

## Common failures

- **"Missing AGENT_OBJECT_ID or AGENT_USER_ID in state"** — the Agent
  Identity / Agent User hasn't been provisioned yet; run `setup.sh` (or
  `create_entra_agent_ids.py`) first.
- **Non-200 on the grants query** — the Provisioner app lacks Graph read
  permission for `oauth2PermissionGrants`, or the object IDs in state are
  stale; re-run `setup.sh` or check the "Permissions" row in
  [`show_agent_status.py`](show-agent-status-py.md).
- **"No permission grants found"** — no consent has been granted for this
  Agent Identity yet; see
  [`grant_consent.py`](../auth-and-certs/grant-consent-py.md) or the
  consent step in `setup.sh`.

## Related commands

- [`show_agent_status.py`](show-agent-status-py.md) — the fuller status/health view that includes this same permission data.
- [`grant_consent.py`](../auth-and-certs/grant-consent-py.md) — add scopes to the grant this command reads.
- [`revoke_consent.py`](../auth-and-certs/revoke-consent-py.md) — remove scopes from, or delete, that grant.
- [Operations scripts index](../index.md#operations)
