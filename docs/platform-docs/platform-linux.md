# Linux Platform APIs

This page is the platform reference for how Entrabot uses Linux system services:
where it stores secrets, how it signs the Blueprint certificate assertion, how
delegated tokens are cached, and how to prepare a Linux host for setup.

## Credential storage: Secret Service / KWallet

Entrabot's `CredentialStore` on Linux is `LinuxCredentialStore`
(`platform/linux.py`), a thin wrapper over the `keyring` library that maps to the
desktop secret store — GNOME Keyring / Secret Service, KWallet, or libsecret. It
exposes the OS-agnostic `store()` / `retrieve()` / `delete()` contract used
across the codebase for small string secrets such as the active client ID and the
Blueprint private key.

**Backend allowlist, fail closed.** At construction, `LinuxCredentialStore`
calls `assert_allowed_keyring_backend("Linux")` (`platform/keyring_backend.py`),
which accepts only these backends:

- `keyring.backends.SecretService.Keyring`
- `keyring.backends.kwallet.DBusKeyring`
- `keyring.backends.libsecret.Keyring`

If the active `keyring` backend is anything else — for example a plaintext or
in-memory alternative — or cannot be inspected, it raises
`InsecureKeyringBackendError` and refuses to proceed rather than writing secrets
to an insecure store. A headless server with no Secret Service provider installed,
or a session with no D-Bus available to reach one, therefore fails closed here
rather than silently degrading. Run Entrabot in a session where a supported
secret-store provider is present and unlocked.

## Blueprint certificate signing

The Blueprint private key is an RSA key stored **as a PEM string in the secret
store**, under service `entrabot` and key `blueprint-private-key`. When Entrabot
signs the Hop 1 client assertion, it retrieves that PEM into the process and
signs the JWT in-process with `cryptography` and PyJWT
(`auth/certificate.py::build_client_assertion`, passed `private_key_pem`). The
assertion header carries the base64url SHA-256 (`x5t#S256`) of the DER
certificate and has a 10-minute lifetime.

As on macOS, this path is **not** hardware-backed and the key is **not**
non-exportable: anything that can read the secret store under the agent's account
can retrieve the PEM. See
[Security Boundaries](../architecture/security-boundaries.md) for the threat-model
comparison and
[Windows and Platform Support](../architecture/windows-and-platforms.md) for the
per-OS certificate matrix. Downstream of the signed assertion, the token flow is
identical across platforms — see
[Identity and Token Flow](../architecture/identity-and-token-flow.md).

## Delegated-mode token cache

In delegated mode, MSAL persists its token cache through msal-extensions'
`build_encrypted_persistence`, which on Linux encrypts the cache via the Secret
Service when one is available (`auth/delegated.py`). The cache file lives under
the per-user cache directory resolved by `platformdirs`
(`platformdirs.user_cache_dir("entrabot")`), and its parent directory is created
with `0o700` permissions. If encrypted persistence cannot be built — for example
on a host with no Secret Service provider — Entrabot logs the failure and falls
back to an in-memory cache for that run rather than writing an unencrypted cache
to disk.

## Preparing a Linux host

There is no dedicated Linux prerequisites installer. Using your distribution's
package manager, install:

- Python 3.12+
- git
- the Azure CLI
- a supported keyring provider (GNOME Keyring / Secret Service, KWallet, or
  libsecret) so credential storage does not fail closed

Then run **`scripts/setup.sh`**, the shared macOS/Linux orchestrator: it
provisions the Blueprint → Agent Identity → Agent User chain, generates or reuses
the Blueprint certificate, writes `.env`, registers the Entrabot MCP server, and
runs a post-setup smoke test. Operational storage stays local by default; pass
`--use-cloud-memory` to opt into Azure Blob-backed storage.

For step-by-step setup, see [macOS and Linux setup](../getting-started/macos-linux.md)
and [Prerequisites](../getting-started/prerequisites.md).

## Default paths

- **Operational data, logs, and audit** default to `~/.entrabot/` (for example
  `~/.entrabot/data`, `~/.entrabot/logs`, `~/.entrabot/audit`), each overridable
  by its corresponding `ENTRABOT_*_DIR` environment variable (`config.py`).
- **The MSAL delegated token cache** lives under the `platformdirs` per-user
  cache directory for `entrabot`, as described above.

## See also

- [Windows and Platform Support](../architecture/windows-and-platforms.md) — the
  cross-platform `CredentialStore` abstraction and per-OS certificate matrix.
- [Security Boundaries](../architecture/security-boundaries.md) — why the
  PEM-in-process model differs in strength from the Windows CNG path.
- [Identity and Token Flow](../architecture/identity-and-token-flow.md) — the
  three-hop flow the signed assertion feeds into.
- [macOS Platform APIs](platform-macos.md) and
  [Windows Platform APIs](platform-windows.md) — the other two platforms.
