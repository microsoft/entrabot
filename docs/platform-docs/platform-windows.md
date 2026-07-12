# Windows Platform APIs

This page is the platform reference for how Entrabot uses Windows system
services: where it stores secrets, how the non-exportable Blueprint key signs the
certificate assertion, the two certificate thumbprints and their distinct
purposes, how delegated tokens are cached, and the setup and rotation surface.

## Credential storage: Windows Credential Manager

Entrabot's generic `CredentialStore` on Windows is `WindowsCredentialStore`
(`platform/windows.py`), a thin wrapper over the `keyring` library that maps to
Windows Credential Manager through `keyring.backends.Windows.WinVaultKeyring`. It
exposes the same OS-agnostic `store()` / `retrieve()` / `delete()` contract used
across the codebase for small string secrets such as MSAL cache markers and the
active client ID.

**Backend allowlist, fail closed.** At construction, `WindowsCredentialStore`
calls `assert_allowed_keyring_backend("Windows")` (`platform/keyring_backend.py`).
If the active `keyring` backend is anything other than
`keyring.backends.Windows.WinVaultKeyring`, or cannot be inspected, it raises
`InsecureKeyringBackendError` and refuses to proceed.

Unlike macOS and Linux, there is **no** `blueprint-private-key` PEM entry in the
credential store — the Blueprint private key lives in the certificate store and
never leaves it (see below).

## Blueprint certificate and non-exportable CNG key

On Windows the Blueprint certificate lives in `Cert:\CurrentUser\My`, and its
private key is a **non-exportable CNG key**. Signing happens through
`ncrypt.dll` — only the resulting signature crosses back into the process, never
the key material (`auth/cncrypt_signer.py::sign_pkcs1_sha256`).

The key is provisioned **TPM-first**: `generate_windows_cert.py` auto-detects TPM
availability and uses the **Microsoft Platform Crypto Provider** (TPM-backed) when
present, falling back to the **Microsoft Software Key Storage Provider**
otherwise. Either way the key is non-exportable; only the TPM-backed case is
additionally hardware-backed. This is the strongest of the three per-OS
certificate paths — see
[Security Boundaries](../architecture/security-boundaries.md) for the
threat-model comparison.

`WindowsCredentialStore.find_cert_by_thumbprint` (and the module-level
`find_cert_by_thumbprint`) query `Cert:\CurrentUser\My` by SHA-1 thumbprint to
confirm the cert is present without touching the private key — used by preflight
checks and rotation.

Downstream of the signed assertion, the token flow is identical across
platforms — see
[Identity and Token Flow](../architecture/identity-and-token-flow.md).

## Two thumbprints, two purposes

Windows configuration carries two distinct certificate thumbprints, and they must
not be confused (`auth/certificate.py`, `auth/cncrypt_signer.py`,
`platform/windows.py`):

- **`ENTRABOT_BLUEPRINT_CERT_THUMBPRINT`** — the base64url-encoded SHA-256
  (`x5t#S256`) of the DER certificate. This goes in the JWT assertion header on
  every platform, Windows included.
- **`ENTRABOT_BLUEPRINT_CERT_SHA1`** — the 40-character hex SHA-1 thumbprint.
  Windows-only; used to locate the certificate in `Cert:\CurrentUser\My` and to
  identify the CNG key for `ncrypt.dll` signing.

Both are written to `.env` by the Windows setup flow. The JWT header always
carries the SHA-256 `x5t#S256` value; the SHA-1 value exists purely for
certificate-store lookup and has no equivalent on macOS/Linux, where the PEM
itself is the keystore lookup key.

## Delegated-mode token cache

In delegated mode, MSAL persists its token cache through msal-extensions'
`build_encrypted_persistence`, which on Windows encrypts the cache with DPAPI
(`auth/delegated.py`). The cache file lives under the per-user cache directory
resolved by `platformdirs` (`platformdirs.user_cache_dir("entrabot")`). If
encrypted persistence cannot be built, Entrabot logs the failure and falls back
to an in-memory cache for that run rather than writing an unencrypted cache to
disk.

## Setup, prerequisites, and rotation

Windows setup runs under **PowerShell**, and the setup orchestrator requires
**PowerShell 7+** (the prerequisite installer itself runs from the Windows
PowerShell 5.1 that ships with Windows). Run the scripts on a native Windows host,
not under WSL — `setup-windows.ps1` refuses to run under WSL and directs WSL users
to `setup.sh`.

- **`scripts/prereqs-windows.ps1`** installs PowerShell 7+, Python 3.12+, git, the
  Azure CLI, the .NET SDK + Microsoft Agent 365 DevTools CLI, and Visual Studio
  Build Tools with the C++ workload (needed to compile native Python extensions
  such as `cryptography`'s). It runs from PowerShell 5.1 and is safe to re-run.
- **`scripts/setup-windows.ps1`** (launched via `setup-windows.cmd`) is the
  Windows equivalent of `setup.sh`: it provisions the same identity chain,
  generates the Blueprint certificate, PATCHes it onto the Blueprint app, writes
  both thumbprints into `.env`, and registers the Entrabot MCP server.
- **`scripts/generate_windows_cert.py`** wraps `New-SelfSignedCertificate` with
  fixed crypto parameters, auto-detects TPM and falls back to the software KSP,
  and returns the SHA-1 thumbprint, the SHA-256 `x5t#S256` thumbprint, and the
  public DER bytes needed to register the cert.
- **`scripts/rotate_cert_windows.py`** performs certificate rotation: it PATCHes
  the new certificate onto the Blueprint, smoke-tests token acquisition, and on
  failure rolls back in three steps — re-PATCH the old DER, restore the previous
  thumbprints in `.env`, and invalidate the MSAL cache so a stale-signed token is
  not presented after rollback.

For step-by-step setup, see [Windows setup](../getting-started/windows.md) and
[Prerequisites](../getting-started/prerequisites.md).

## Default paths

- **Operational data, logs, and audit** default to a per-user directory under
  `%LOCALAPPDATA%\entrabot` (for example `%LOCALAPPDATA%\entrabot\data`), each
  overridable by its corresponding `ENTRABOT_*_DIR` environment variable
  (`config.py`).
- **The MSAL delegated token cache** lives under the `platformdirs` per-user
  cache directory for `entrabot`, as described above.

## See also

- [Windows and Platform Support](../architecture/windows-and-platforms.md) — the
  cross-platform `CredentialStore` abstraction and per-OS certificate matrix.
- [Security Boundaries](../architecture/security-boundaries.md) — why the CNG
  path is the strongest of the three certificate paths.
- [Identity and Token Flow](../architecture/identity-and-token-flow.md) — the
  three-hop flow the signed assertion feeds into.
- [macOS Platform APIs](platform-macos.md) and
  [Linux Platform APIs](platform-linux.md) — the other two platforms.
