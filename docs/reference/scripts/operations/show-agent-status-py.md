# `scripts/show_agent_status.py`

## Purpose

Consolidated Agent Identity status and health check. Combines local state
(`.entrabot-state.json`, `.env`-derived configuration) with live Microsoft
Graph queries to report on the Blueprint, Agent Identity, Agent User,
sponsors, permission grants, Blueprint certificates, licenses, and storage
configuration in one command — the canonical way to answer "is my Agent
Identity chain healthy?"

## Requirements

- Python 3.12+ with `entrabot` and `azure-identity` installed
  (`pip install -e ".[provisioning]"`).
- The Provisioner app must already be bootstrapped: `.entrabot-state.json`
  at the repo root needs `PROVISIONER_CLIENT_ID` and `TENANT_ID`, and the
  Provisioner's certificate private key must be present in the OS keystore
  (Keychain / Windows Certificate Store / Secret Service via `keyring`).
  This command mints its Graph token from the **Provisioner's certificate
  credential** (`CertificateCredential` + `client_credentials`) — it does
  **not** use the Agent User three-hop flow. If the Provisioner isn't
  bootstrapped, run `python3 scripts/entra_provisioning.py` (or full
  `setup.sh`) first.
- Works on macOS, Linux, and Windows.

## Usage

```bash
python scripts/show_agent_status.py
python scripts/show_agent_status.py --json
python scripts/show_agent_status.py --health-only
python scripts/show_agent_status.py --health-only --strict
python scripts/show_agent_status.py --full-thumbprints
python scripts/show_agent_status.py --help
```

## Options

- `--json` — print the full snapshot (state, platform info, storage
  config, sponsors, licenses, permission grants, key credentials, and
  health checks) as a single JSON object instead of the formatted text
  report.
- `--health-only` — print only the pass/fail/warn/skip health checks,
  skipping the detailed sections. Always returns a non-zero exit code if
  any check fails, regardless of `--strict`.
- `--strict` — return exit code `1` when any health check has failed;
  has no visible effect on output, only on the exit code. Implied by
  `--health-only`.
- `--full-thumbprints` — show certificate thumbprints in full, in text and in
  `--json`, for comparing them with the Microsoft Entra admin center. Without
  it only their last four characters are shown (see
  [Effects](#effects)). An older setup key's thumbprint is shown by its last
  four characters either way.

The health labels are coloured only when standard output is a terminal that can
draw colour. Piped or captured output is plain text, and the `NO_COLOR`
environment variable turns colour off.

No positional arguments.

## Effects

- Reads `.entrabot-state.json` keys: `TENANT_ID`, `PROVISIONER_CLIENT_ID`,
  `PROVISIONER_CERT_THUMBPRINT`, `BLUEPRINT_APP_ID`, `BLUEPRINT_OBJECT_ID`,
  `BLUEPRINT_CERT_THUMBPRINT`, `AGENT_ID`, `AGENT_OBJECT_ID`,
  `AGENT_USER_ID`, `AGENT_USER_UPN`, and `AGENT_USER_WORK_IQ_LICENSE_SKU`.
- Shows every certificate thumbprint by its last four characters after an
  ellipsis (`…B3C4`), the form the rest of the tooling uses, unless
  `--full-thumbprints` is given. A hex (SHA-1) thumbprint is shown in upper
  case; a base64 (SHA-256) one keeps its own letters, because their case is
  part of the value. That covers the current Provisioner and
  Blueprint certificates, each Blueprint key credential's computed SHA-256
  thumbprint and its `customKeyIdentifier` (which Graph sets to the
  certificate's SHA-1 thumbprint), and the **Certificate** check's warning
  (`Local thumbprint …B3C4 not found in Entra`). Matching is done on the full
  values. In `--json` the field names are unchanged, but by default these
  values are now the `…B3C4` form instead of the full thumbprint (a thumbprint
  that is not recorded is still `null`): `provisioner.cert_thumbprint`,
  `blueprint.cert_thumbprint`, `state.PROVISIONER_CERT_THUMBPRINT`,
  `state.BLUEPRINT_CERT_THUMBPRINT`, `key_credentials[].computedThumbprint` and
  `key_credentials[].customKeyIdentifier`. A reader that needs the full values
  passes `--full-thumbprints`.
- Reads setup's record of older setup keys (`PROVISIONER_CERT_REVOKE_PENDING`
  and `PROVISIONER_CERT_REVOKE_PENDING_EXPIRES`) for the **Older setup keys**
  health check, on every platform (Windows setup writes the record). It warns
  while an older setup key that setup could not switch off in Microsoft Entra is
  still waiting to be removed from the Provisioner app, naming it by the last
  four characters of its thumbprint and its expiry date, with where in the
  Microsoft Entra admin center to remove it. A record it cannot read is a
  warning too. Otherwise the check is `PASS`, or `SKIP` when no Provisioner app
  is recorded. These two keys are not part of the `state` object in `--json`.
- Reads storage configuration from the process environment / `.env` via
  `entrabot.config.get_config()`.
- Issues read-only Graph GET requests (no writes to Graph or local state):
  - `/servicePrincipals/{agentOid}/microsoft.graph.agentIdentity/sponsors` (beta)
  - `/subscribedSkus` and `/users/{agentUserId}` (assigned licenses, v1.0)
  - `/oauth2PermissionGrants?$filter=clientId eq '...' and principalId eq '...'` (v1.0)
  - `/servicePrincipals/{resourceId}` per distinct grant resource, to resolve display names
  - `/applications/{blueprintObjectId}` for Blueprint `keyCredentials` (v1.0)
- Recomputes the SHA-256 JWT thumbprint of every Blueprint key credential
  locally and compares it against `BLUEPRINT_CERT_THUMBPRINT` to detect
  certificate drift between Entra and local state.
- The **Local certificate** health check looks at the certificate `.env` names
  in `ENTRABOT_BLUEPRINT_CERT_SHA1`, on this computer only (no network): is it in
  this account's certificate store with its private key, and in date? On Windows
  that store is `Cert:\CurrentUser\My`; on macOS and Linux it is the Keychain or
  keyring item that holds the certificate and its key together. It is `PASS`
  with the expiry date, `WARN` when 30 days or fewer remain (or when the store
  could not be read), and `FAIL` when the certificate is missing, has no private
  key, has expired, or is not valid yet. With a Blueprint configured but no
  SHA-1 thumbprint, it fails on Windows; on macOS and Linux that is a setup from
  before the wizard, whose key has no certificate on this computer to read, and
  the check is left out. It is `SKIP` when no Blueprint is configured here.
- All output goes to stdout — the formatted text report, the health-only
  list, or the `--json` payload. The single error path (Provisioner token
  acquisition failure) also prints to stdout (`ERROR: Cannot acquire
  token: ...`), not stderr.

## Exit behavior

- Returns `1` if the Provisioner Graph token cannot be acquired
  (`ProvisionerBootstrapError`).
- Otherwise returns `1` when `(--strict or --health-only)` is true **and**
  at least one health check has `status == "fail"`.
- Returns `0` in every other case — including runs where checks report
  `warn` or `skip` but no `fail` (an older setup key still waiting is a
  `warn`), and non-strict, non-health-only runs regardless of check outcomes.
- `--help` exits `0` (argparse default).

## Common failures

- **`ERROR: Cannot acquire token: ...`** — the Provisioner app isn't
  bootstrapped, or its certificate is missing from the local OS keystore.
  Run `python3 scripts/entra_provisioning.py` or `setup.sh`.
- **"State completeness" check fails** — one or more required keys is
  missing from `.entrabot-state.json`; re-run `setup.sh`.
- **"Certificate" check warns "Local thumbprint …XXXX not found in Entra"** —
  the local `BLUEPRINT_CERT_THUMBPRINT` doesn't match any key credential currently
  on the Blueprint app, typically after an out-of-band cert rotation; see
  [`verify_blueprint_cert.py`](../auth-and-certs/verify-blueprint-cert-py.md).
- **"Local certificate" check fails or warns** — the certificate this computer
  signs the agent in with is missing, expired, or close to expiry. Before it
  expires, rotate it (`./scripts/setup.sh --rotate-cert`, or
  `scripts\deploy-windows.ps1` on Windows); after, run setup again, which creates
  and registers a new one. On macOS and Linux a key store that cannot be read in
  this session (a locked keychain over SSH) shows here too.
- **"Older setup keys" check warns** — Windows setup moved this machine off an
  older setup key file but could not take that key's certificate off the
  Provisioner app, and a copy of the file can still get tokens for it. Remove the
  certificate where the check says, or run Windows setup again, which retries.
  See
  [Windows troubleshooting](../../../troubleshooting/windows.md#an-older-setup-key-for-this-computer-could-not-be-switched-off-in-microsoft-entra).
- **"Sponsors" / "Licenses" / "Permissions" checks fail** — use
  [`add_agent_sponsor.py`](../provisioning/add-agent-sponsor-py.md),
  [`assign_agent_user_licenses.py`](../provisioning/assign-agent-user-licenses-py.md),
  or [`grant_consent.py`](../auth-and-certs/grant-consent-py.md) respectively.

## Related commands

- [`status.sh`](status-sh.md) / [`status-windows.ps1`](status-windows-ps1.md) — thin wrappers that call this command. `status.sh` bootstraps the venv if needed; `status-windows.ps1` does not, and asks you to run setup instead.
- [`health_check.py`](health-check-py.md) — compatibility wrapper that forwards to `--health-only`.
- [`show_permissions.py`](show-permissions-py.md) — a focused view of just the permission grants, using the same Provisioner-cert token.
- Platform docs: [Agent Identity Blueprints and Users](../../../platform-docs/agent-id-blueprints-and-users.md)
- Architecture: [Identity and Token Flow](../../../architecture/identity-and-token-flow.md)
- [Operations scripts index](../index.md#operations)
