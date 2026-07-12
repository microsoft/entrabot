# macOS Platform APIs

This page is the platform reference for how Entrabot uses macOS system services:
where it stores secrets, how it signs the Blueprint certificate assertion, how
delegated tokens are cached, and the setup surface that provisions everything.

## Credential storage: Keychain

Entrabot's `CredentialStore` on macOS is `MacCredentialStore`
(`platform/mac.py`), a thin wrapper over the `keyring` library that maps to the
login Keychain through `keyring.backends.macOS.Keyring`. It exposes the
OS-agnostic `store()` / `retrieve()` / `delete()` contract used across the
codebase for small string secrets such as the active client ID and the Blueprint
private key.

**Backend allowlist, fail closed.** At construction, `MacCredentialStore` calls
`assert_allowed_keyring_backend("Darwin")` (`platform/keyring_backend.py`). If
the active `keyring` backend is anything other than `keyring.backends.macOS.Keyring`
— or if the backend cannot be inspected at all — it raises
`InsecureKeyringBackendError` and refuses to proceed rather than silently writing
secrets to an unexpected store. Reaching the Keychain requires the user's login
Keychain to be accessible in the current session; a session where it is locked or
unavailable surfaces as a keyring failure rather than a silent fallback.

## Blueprint certificate signing

The Blueprint private key is an RSA key stored **as a PEM string in the
Keychain**, under service `entrabot` and key `blueprint-private-key`. When
Entrabot needs to sign the Hop 1 client assertion, it retrieves that PEM into the
process and signs the JWT in-process with `cryptography` and PyJWT
(`auth/certificate.py::build_client_assertion`, passed `private_key_pem`). The
assertion header carries the base64url SHA-256 (`x5t#S256`) of the DER
certificate and has a 10-minute lifetime.

This path is **not** hardware-backed and the key is **not** non-exportable:
anything that can read the Keychain under the agent's account can retrieve the
PEM. That is the deliberate macOS trade-off, distinct from the Windows
non-exportable CNG path. See
[Security Boundaries](../architecture/security-boundaries.md) for the threat-model
comparison, and
[Windows and Platform Support](../architecture/windows-and-platforms.md) for the
per-OS certificate matrix.

Once the signed assertion exists, the rest of the token flow is identical to
every other platform — see
[Identity and Token Flow](../architecture/identity-and-token-flow.md).

## Delegated-mode token cache

In delegated mode, MSAL persists its token cache through msal-extensions'
`build_encrypted_persistence`, which on macOS encrypts the cache via the
Keychain (`auth/delegated.py`). The cache file lives under the per-user cache
directory resolved by `platformdirs` (`platformdirs.user_cache_dir("entrabot")`),
and its parent directory is created with `0o700` permissions. If encrypted
persistence cannot be built, Entrabot logs the failure and falls back to an
in-memory cache for that run rather than writing an unencrypted cache to disk.

## Setup surface

- **`scripts/prereqs-macos.sh`** installs the prerequisites via Homebrew: Xcode
  Command Line Tools, Python 3.12+, git, and the Azure CLI, plus optional .NET
  SDK + Microsoft Agent 365 DevTools CLI and PowerShell 7 (both opt-out). It is
  safe to re-run and skips anything already present.
- **`scripts/setup.sh`** is the shared macOS/Linux orchestrator: it provisions
  the Blueprint → Agent Identity → Agent User chain, generates or reuses the
  Blueprint certificate, writes `.env`, registers the Entrabot MCP server, and
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
- [Linux Platform APIs](platform-linux.md) and
  [Windows Platform APIs](platform-windows.md) — the other two platforms.
