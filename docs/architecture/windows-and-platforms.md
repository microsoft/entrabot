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
| **Windows** | Windows Credential Manager via `keyring` (`keyring.backends.Windows.WinVaultKeyring`). Nothing on the agent's sign-in path uses it on Windows | Blueprint certificate lives in `Cert:\CurrentUser\My`; the private key is a **non-exportable** CNG key, signed in-process through `ncrypt.dll`. It is created on the Microsoft Platform Crypto Provider (TPM) when the TPM accepts a key, and on the Microsoft Software Key Storage Provider otherwise. Setup's own Provisioner key is stored the same way |

The Mac/Linux and Windows certificate paths are not equivalent in strength — see [Security Boundaries](security-boundaries.md) for why the Mac/Linux PEM-in-process model is not hardware-backed or non-exportable the way the Windows CNG path is.

## Two thumbprints, two purposes

Windows configuration carries two distinct certificate thumbprints, and they must not be confused:

- **`ENTRABOT_BLUEPRINT_CERT_THUMBPRINT`** — the base64url-encoded SHA-256 (`x5t#S256`) of the DER certificate, computed by `auth/certificate.py::compute_cert_thumbprint()`. This goes in the JWT assertion header on every platform, Windows included.
- **`ENTRABOT_BLUEPRINT_CERT_SHA1`** — the 40-character hex SHA-1 thumbprint of the certificate. Windows-only; used to locate the certificate in `Cert:\CurrentUser\My` and to identify the CNG key for `ncrypt.dll` signing (`auth/cncrypt_signer.py`, `platform/wincert.py::find_cert`).

Both are written to `.env` by the Windows setup flow. The JWT header always carries the SHA-256 `x5t#S256` value regardless of platform; the SHA-1 value exists purely for Windows certificate-store lookup and has no equivalent need on macOS/Linux, where the PEM itself is the lookup key in the OS keystore.

## Setup surfaces

**macOS and Linux:**

- `scripts/prereqs-macos.sh` — installs Homebrew-managed prerequisites (Xcode CLT, Python 3.12+, git, Azure CLI, optional .NET SDK + Agent 365 DevTools CLI, optional PowerShell 7). macOS-only; there is no equivalent prereqs script for Linux — Linux hosts install Python 3.12+, git, and the Azure CLI via their own package manager before running `setup.sh`.
- `scripts/setup.sh` — the shared macOS/Linux orchestrator: provisions the Blueprint/Agent Identity/Agent User chain, generates or reuses a certificate, writes `.env`, and runs the post-setup smoke test. Supports `--use-cloud-memory` to opt into Blob-backed operational storage (see [Storage and Memory](storage-and-memory.md)).

**Windows:**

- `scripts/setup-windows.cmd` — the entry point. It runs `scripts/setup-windows.ps1` with the Windows PowerShell 5.1 that ships with Windows. Nothing has to be installed first: no PowerShell 7, no `winget`, no administrator rights.
- `scripts/setup-windows.ps1` — a bootstrap only. It finds Python 3.12 or newer, or downloads a private, signature-checked copy; builds `.venv`; installs Entrabot; then starts the setup wizard.
- `python -m entrabot.winsetup` (`src/entrabot/winsetup/`) — the setup wizard, and the Windows equivalent of `setup.sh`: sign-in, the same provisioning chain, the certificate, `.env`, MCP registration, and an end-to-end test, then, when asked, Word, Excel and OneDrive files (Work IQ) without Microsoft's Agent 365 CLI (`src/entrabot/winsetup/workiq.py`). It is interactive by default and scriptable with `-Yes` plus parameters. It also provides `diagnose`, `rotate-cert`, `teardown` and `status`, which `setup-windows.ps1 -Diagnose`, `deploy-windows.ps1`, `teardown-windows.ps1` and `status-windows.ps1` call.
- `src/entrabot/platform/wincert.py` — the Windows key lifecycle: create a certificate with a non-exportable key (TPM first, by creating a key and signing with it; software key store otherwise), find it, test-sign it, delete it together with its key. `scripts/generate_windows_cert.py` is a command-line wrapper over it.
- `scripts/prereqs-windows.ps1` — optional extras on request (Git for Windows, Claude Code, Microsoft's Agent 365 CLI for using it yourself, Visual Studio Build Tools). It is not a required step, and setup does not need the Agent 365 CLI.
- `start-entrabot.cmd` — starts the agent in Claude Code.

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

