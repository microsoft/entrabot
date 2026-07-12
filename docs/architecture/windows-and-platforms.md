# Windows and Platform Support

Entrabot runs on macOS, Linux, and Windows. Python 3.12+ is required on every platform (`requires-python = ">=3.12"`). The shared abstraction across all three is `CredentialStore` — an OS-agnostic key/value secret store, not an identity provider:

```python
class CredentialStore(Protocol):
    def store(self, service: str, key: str, value: str) -> None: ...
    def retrieve(self, service: str, key: str) -> str | None: ...
    def delete(self, service: str, key: str) -> None: ...
```

Every platform module (`platform/mac.py`, `platform/linux.py`, `platform/windows.py`) implements this protocol on top of `keyring`, plus an allowlist check (`platform/keyring_backend.py::assert_allowed_keyring_backend`) that fails closed with `InsecureKeyringBackendError` if the active keyring backend isn't the expected OS keystore for that platform.

## Platform matrix

| Platform | Generic secrets | Blueprint private key |
|---|---|---|
| **macOS** | Keychain via `keyring` (`keyring.backends.macOS.Keyring`) | PEM private key stored in Keychain, retrieved into the process and signed in-process via `cryptography` |
| **Linux** | Secret Service / KWallet / libsecret via `keyring`, restricted to an explicit allowlist (`keyring.backends.SecretService.Keyring`, `keyring.backends.kwallet.DBusKeyring`, `keyring.backends.libsecret.Keyring`) | Same PEM-retrieved-and-signed-in-process model as macOS |
| **Windows** | Windows Credential Manager via `keyring` (`keyring.backends.Windows.WinVaultKeyring`), for generic string secrets (MSAL cache markers, refresh tokens) | Blueprint certificate lives in `Cert:\CurrentUser\My`; the private key is a **non-exportable** CNG key, signed through `ncrypt.dll`, backed by the Microsoft Platform Crypto Provider (TPM) when available, falling back to the Microsoft Software Key Storage Provider otherwise |

The Mac/Linux and Windows certificate paths are not equivalent in strength — see [Security Boundaries](security-boundaries.md) for why the Mac/Linux PEM-in-process model is not hardware-backed or non-exportable the way the Windows CNG path is.

## Two thumbprints, two purposes

Windows configuration carries two distinct certificate thumbprints, and they must not be confused:

- **`ENTRABOT_BLUEPRINT_CERT_THUMBPRINT`** — the base64url-encoded SHA-256 (`x5t#S256`) of the DER certificate, computed by `auth/certificate.py::compute_cert_thumbprint()`. This goes in the JWT assertion header on every platform, Windows included.
- **`ENTRABOT_BLUEPRINT_CERT_SHA1`** — the 40-character hex SHA-1 thumbprint of the certificate. Windows-only; used to locate the certificate in `Cert:\CurrentUser\My` and to identify the CNG key for `ncrypt.dll` signing (`auth/cncrypt_signer.py`, `platform/windows.py::find_cert_by_thumbprint`).

Both are written to `.env` by the Windows setup flow. The JWT header always carries the SHA-256 `x5t#S256` value regardless of platform; the SHA-1 value exists purely for Windows certificate-store lookup and has no equivalent need on macOS/Linux, where the PEM itself is the lookup key in the OS keystore.

## Setup surfaces

**macOS and Linux:**

- `scripts/prereqs-macos.sh` — installs Homebrew-managed prerequisites (Xcode CLT, Python 3.12+, git, Azure CLI, optional .NET SDK + Agent 365 DevTools CLI, optional PowerShell 7). macOS-only; there is no equivalent prereqs script for Linux — Linux hosts install Python 3.12+, git, and the Azure CLI via their own package manager before running `setup.sh`.
- `scripts/setup.sh` — the shared macOS/Linux orchestrator: provisions the Blueprint/Agent Identity/Agent User chain, generates or reuses a certificate, writes `.env`, and runs the post-setup smoke test. Supports `--use-cloud-memory` to opt into Blob-backed operational storage (see [Storage and Memory](storage-and-memory.md)).

**Windows:**

- `scripts/prereqs-windows.ps1` — installs PowerShell 7+, Python 3.12+, git, Azure CLI, .NET SDK + Agent 365 DevTools CLI, and Visual Studio Build Tools with the C++ workload (needed to compile native Python extensions such as `cryptography`'s). Runs from the PowerShell 5.1 that ships with Windows, so it doesn't require pwsh to already be installed.
- `scripts/setup-windows.ps1` (and its `setup-windows.cmd` launcher) — the Windows equivalent of `setup.sh`: same provisioning chain, but generates the Blueprint certificate via `generate_windows_cert.py` and writes both thumbprints described above into `.env`.
- `scripts/generate_windows_cert.py` — wraps `New-SelfSignedCertificate` with fixed crypto parameters, auto-detects TPM availability and falls back to the software KSP, and returns the SHA-1 thumbprint, SHA-256 `x5t#S256` thumbprint, and public DER bytes needed to register the cert on the Blueprint app.
- `scripts/rotate_cert_windows.py` — cert rotation logic used by `deploy-windows.ps1`: PATCHes the new certificate onto the Blueprint, smoke-tests token acquisition, and on failure rolls back in three steps (re-PATCH the old DER, restore the previous thumbprints in `.env`, invalidate the MSAL cache so a stale-signed token isn't presented after rollback).

Per-script usage: [`setup.sh` reference](../reference/scripts/setup/setup-sh.md), [`prereqs-macos.sh` reference](../reference/scripts/setup/prereqs-macos-sh.md), [`setup-windows.ps1` reference](../reference/scripts/setup/setup-windows-ps1.md), [`prereqs-windows.ps1` reference](../reference/scripts/setup/prereqs-windows-ps1.md), [`generate_windows_cert.py` reference](../reference/scripts/auth-and-certs/generate-windows-cert-py.md), [`rotate_cert_windows.py` reference](../reference/scripts/auth-and-certs/rotate-cert-windows-py.md).

## Cross-platform invariant

Once a certificate-signed JWT assertion exists, the rest of the three-hop token flow, and everything built on top of it — Graph calls, the MCP tool surface, audit attribution — is identical across platforms. The platform difference is contained entirely to *how* the assertion gets signed (`auth/certificate.py::build_client_assertion()` branches on whether `private_key_pem` or `cert_sha1` was supplied); nothing downstream of Hop 1 knows or cares which OS produced the token.

## Current limitation: no execution sandbox

Entrabot does not currently ship an execution sandbox on any platform. Process- and filesystem-level containment remains under evaluation.

## See also

- [Security Boundaries](security-boundaries.md) — why the Mac/Linux and Windows certificate paths differ in strength, and the sandbox limitation above.
- [Identity and Token Flow](identity-and-token-flow.md) — the three-hop flow the certificate assertion feeds into.
- [Platform Docs](../platform-docs/platform-macos.md) (forthcoming) — per-OS setup, rotation, and troubleshooting guidance: [macOS](../platform-docs/platform-macos.md), [Linux](../platform-docs/platform-linux.md), [Windows](../platform-docs/platform-windows.md).
- Per-script reference pages: [`setup.sh`](../reference/scripts/setup/setup-sh.md), [`prereqs-macos.sh`](../reference/scripts/setup/prereqs-macos-sh.md), [`setup-windows.ps1`](../reference/scripts/setup/setup-windows-ps1.md), [`prereqs-windows.ps1`](../reference/scripts/setup/prereqs-windows-ps1.md), [`generate_windows_cert.py`](../reference/scripts/auth-and-certs/generate-windows-cert-py.md), [`rotate_cert_windows.py`](../reference/scripts/auth-and-certs/rotate-cert-windows-py.md).

