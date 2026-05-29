# Auth and certificate scripts

Scripts that manage the certificates and OAuth consent grants behind the agent's token flows.

Private keys live in the OS keystore (Keychain on macOS, Keyring on Linux, Cert Store on Windows). The public certs are registered on the Blueprint app in Entra. No client secrets on disk anywhere — ADR-003.

## `provisioner-token.py`

Print a Graph API access token minted by the Provisioner app's cert. Shell helper that replaces curl-with-client-secret flows.

### Usage

```bash
python3 scripts/provisioner-token.py
```

Exits 0 with the token on stdout, or 1 with the error on stderr.

### What it does

- Shells out to `entra_provisioning.get_existing_graph_token()`.
- `CertificateCredential` reads the private key from Keychain in memory only.
- Prints the token to stdout — no token ever lands on disk.

## `find_local_blueprint_cert.py`

Recover the registered Blueprint cert thumbprint matching the local private key, when worktree-local state is missing `BLUEPRINT_CERT_THUMBPRINT` but the cert is still registered.

### Usage

```bash
python scripts/find_local_blueprint_cert.py <BLUEPRINT_OBJECT_ID>
```

### What it does

- Reads the local private key from the OS credential store.
- Computes the public cert's SHA-256 b64url thumbprint.
- Queries the Blueprint app's `keyCredentials` for a match.
- Prints the matching thumbprint to stdout, diagnostics to stderr.

`setup.sh` uses this so it can reuse an existing cert instead of prompting to rotate.

## `list_blueprint_certs.py`

Print registered certs on the Blueprint app.

### Usage

```bash
python scripts/list_blueprint_certs.py <BLUEPRINT_OBJECT_ID>
```

### What it does

- Queries the Blueprint app via Graph.
- stdout: a single integer (count of `keyCredentials`).
- stderr: one line per cert: `    - <displayName>  expires <YYYY-MM-DD>`.

`setup.sh` uses the stdout count for branching, the stderr detail for human-visible output.

## `verify_blueprint_cert.py`

Verify a locally-cached Blueprint cert thumbprint is still registered on the Blueprint app.

### Usage

```bash
python scripts/verify_blueprint_cert.py <BLUEPRINT_OBJECT_ID> <EXPECTED_THUMBPRINT>
```

### Exit codes

- `0` — thumbprint present (cache is valid).
- `1` — thumbprint not present (cache is stale; regenerate).
- `2` — usage error.

### What it does

`setup.sh`'s cached-thumbprint fast path skips cert regeneration when `BLUEPRINT_CERT_THUMBPRINT` is in state. If a teammate has rotated the cert from another machine, the cached thumbprint is stale and the local key no longer has a matching public cert — Hop 1 fails with cryptic `invalid_client`. This script catches it first.

## `generate_windows_cert.py`

Generate the Blueprint cert on Windows. Wraps `New-SelfSignedCertificate` with hard-locked crypto parameters.

### Usage

```bash
python scripts/generate_windows_cert.py
```

### What it does

- Auto-detects TPM availability and falls back to the software KSP.
- Returns the SHA-1 thumbprint, SHA-256 b64url thumbprint, and public DER bytes.
- Validates the thumbprint with a regex to defend against stdout corruption (Learning #29).

Pytest can drive this end-to-end by mocking subprocess and asserting the crypto flags land verbatim. Reused by `rotate_cert_windows.py`.

## `rotate_cert_windows.py`

Cert rotation logic for Windows, extracted from `deploy-windows.ps1` for testability.

### Usage

Called by `deploy-windows.ps1` — not a CLI entry point.

### What it does

The PS1 wrapper supplies the old DER + new DER + thumbprints. This module:

1. PATCHes the new cert to the Blueprint's `keyCredentials`.
2. Runs a smoke test (token acquisition end-to-end).
3. On smoke failure, three-step rollback:
   - Re-PATCH the original DER back to the Blueprint.
   - Restore the previous thumbprints in `.env`.
   - Invalidate the MSAL cache — otherwise the next call presents a token signed by the now-invalidated new key and 401s.

## `grant_consent.py`

Grant delegated `oauth2PermissionGrant` for the Agent Identity.

### Usage

```bash
# Default resource: Microsoft Graph
python scripts/grant_consent.py --scopes "Chat.Create,Mail.Read"

# Different resource (e.g., Azure Storage)
python scripts/grant_consent.py \
  --scopes "user_impersonation" \
  --resource-app-id "e406a681-f3d4-42a8-90b6-c2b029497af1"
```

### What it does

- Mints a Provisioner Graph token.
- Looks up an existing grant for `(client_id, resource_id, principal_id)`.
- If missing, POSTs to `/oauth2PermissionGrants` with the requested scopes.
- If present, PATCHes only the missing scopes.

Generalized form of the consent logic in `create_entra_agent_ids.py`.

## `grant_files_consent.py`

Add the Files / Sites scopes to the Agent User's existing grant. Thin wrapper around `grant_consent.py`.

### Usage

```bash
python scripts/grant_files_consent.py
```

### What it does

- PATCHes the existing `oauth2PermissionGrant` to add `Files.Read.All`, `Sites.Read.All`, and `Sites.ReadWrite.All`.
- Idempotent — only adds missing scopes.

Use when `MissingPermissionError` fires on a Files MCP tool call because the Agent User was provisioned before the Files scopes were added.

## `revoke_consent.py`

Revoke or pare-down the `oauth2PermissionGrant` for the Agent Identity. Inverse of `grant_consent.py`.

### Usage

```bash
# Remove specific scopes
python3 scripts/revoke_consent.py --scopes "Mail.Read,Files.ReadWrite"

# Remove the entire grant
python3 scripts/revoke_consent.py --all
```

### What it does

- Reads `AGENT_OBJECT_ID` and `AGENT_USER_ID` from `.entrabot-state.json`.
- With `--scopes`: PATCH to drop just those scopes.
- With `--all`: DELETE the grant entirely.
