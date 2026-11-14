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
| **macOS** | Keychain via `keyring` (`keyring.backends.macOS.Keyring`) | One Keychain item per certificate (service `entrabot-blueprint-cert`, named by its SHA-1 thumbprint) holding the PEM private key and the certificate together (`platform/keycert.py`); the key is retrieved into the process and signed in-process via `cryptography`. Setup's Provisioner key is kept the same way, one item per tenant |
| **Linux** | Secret Service / KWallet / libsecret via `keyring`, restricted to an explicit allowlist (`keyring.backends.SecretService.Keyring`, `keyring.backends.kwallet.DBusKeyring`, `keyring.backends.libsecret.Keyring`) | Same model as macOS, in the keyring |
| **Windows** | Windows Credential Manager via `keyring` (`keyring.backends.Windows.WinVaultKeyring`). Nothing on the agent's sign-in path uses it on Windows | Blueprint certificate lives in `Cert:\CurrentUser\My`; the private key is a **non-exportable** CNG key, signed in-process through `ncrypt.dll`. It is created on the Microsoft Platform Crypto Provider (TPM) when the TPM accepts a key, and on the Microsoft Software Key Storage Provider otherwise. Setup's own Provisioner key is stored the same way |

The Mac/Linux and Windows certificate paths are not equivalent in strength — see [Security Boundaries](security-boundaries.md) for why the Mac/Linux PEM-in-process model is not hardware-backed or non-exportable the way the Windows CNG path is.

## Two thumbprints, two purposes

The configuration carries two distinct certificate thumbprints, and they must not be confused:

- **`ENTRABOT_BLUEPRINT_CERT_THUMBPRINT`** — the base64url-encoded SHA-256 (`x5t#S256`) of the DER certificate, computed by `auth/certificate.py::compute_cert_thumbprint()`. This goes in the JWT assertion header on every platform.
- **`ENTRABOT_BLUEPRINT_CERT_SHA1`** — the 40-character hex SHA-1 thumbprint of the certificate. It finds the certificate and its private key in this account's store: on Windows the certificate in `Cert:\CurrentUser\My` and the CNG key behind it for `ncrypt.dll` signing (`auth/cncrypt_signer.py`, `platform/wincert.py::find_cert`), on macOS and Linux the Keychain or keyring item named by it (`platform/keycert.py::find_cert`).

Both are written to `.env` by the setup wizard on every platform, with `ENTRABOT_BLUEPRINT_KSP` saying which store holds the key (`tpm` or `software` on Windows, `keychain` or `keyring` elsewhere). A macOS or Linux setup from before the wizard has no SHA-1 value: its key is the account's single PEM under `entrabot` / `blueprint-private-key`, which the runtime uses only then.

## Setup surfaces

**macOS and Linux:**

- `scripts/setup.sh` — a bootstrap only. It finds Python 3.12 or newer, builds `.venv` (reinstalling only when `pyproject.toml` changes), then starts the same setup wizard as Windows (below). Every flag it took before the wizard still works, translated for the wizard. Supports `--use-cloud-memory` to opt into Blob-backed operational storage (see [Storage and Memory](storage-and-memory.md)), `--diagnose`, `--rotate-cert` and `--status`.
- `scripts/teardown.sh` — disconnects this computer by default (the wizard's teardown); the older tenant-wide teardown runs only with `--tenant-wide` or one of its own flags.
- `src/entrabot/platform/keycert.py` — the macOS and Linux key lifecycle, with the same functions as `wincert.py`: one Keychain or keyring item per certificate, holding the certificate and its private key; create, find, test-sign, delete with the key.
- `start-entrabot.sh` — starts the agent in Claude Code.
- `scripts/prereqs-macos.sh` — optional. Installs the Xcode Command Line Tools and, through Homebrew, Python 3.12+, git and the Azure CLI; `--with-a365-cli` adds the .NET SDK, Microsoft's Agent 365 CLI and PowerShell 7, which setup does not need. macOS-only; Linux hosts install Python 3.12+ and a keyring provider with their own package manager.

**Windows:**

- `scripts/setup-windows.cmd` — the entry point. It runs `scripts/setup-windows.ps1` with the Windows PowerShell 5.1 that ships with Windows. Nothing has to be installed first: no PowerShell 7, no `winget`, no administrator rights.
- `scripts/setup-windows.ps1` — a bootstrap only. It finds Python 3.12 or newer, or downloads a private, signature-checked copy; builds `.venv`; installs Entrabot; then starts the setup wizard.
- `python -m entrabot.winsetup` (`src/entrabot/winsetup/`) — the setup wizard, on Windows, macOS and Linux (the package name is from when it was Windows-only): sign-in, the provisioning chain, the certificate, `.env`, MCP registration, and an end-to-end test, then, when asked, Word, Excel and OneDrive files (Work IQ) without Microsoft's Agent 365 CLI (`src/entrabot/winsetup/workiq.py`). It is interactive by default and scriptable with `-Yes` (`--yes`) plus parameters. It also provides `diagnose`, `rotate-cert`, `teardown` and `status`, which `setup-windows.ps1 -Diagnose`, `deploy-windows.ps1`, `teardown-windows.ps1` and `status-windows.ps1` call on Windows, and `setup.sh --diagnose`, `setup.sh --rotate-cert` and `teardown.sh` on macOS and Linux. `src/entrabot/winsetup/words.py` holds the commands and flags each system's messages name.
- `src/entrabot/platform/wincert.py` — the Windows key lifecycle: create a certificate with a non-exportable key (TPM first, by creating a key and signing with it; software key store otherwise), find it, test-sign it, delete it together with its key. `scripts/generate_windows_cert.py` is a command-line wrapper over it.
- `scripts/prereqs-windows.ps1` — optional extras on request (Git for Windows, Claude Code, Microsoft's Agent 365 CLI for using it yourself, Visual Studio Build Tools). It is not a required step, and setup does not need the Agent 365 CLI.
- `start-entrabot.cmd` — starts the agent in Claude Code.

Per-script usage: [`setup.sh` reference](../reference/scripts/setup/setup-sh.md), [`teardown.sh` reference](../reference/scripts/teardown/teardown-sh.md), [`start-entrabot.sh` reference](../reference/scripts/setup/start-entrabot-sh.md), [`prereqs-macos.sh` reference](../reference/scripts/setup/prereqs-macos-sh.md), [`setup-windows.ps1` reference](../reference/scripts/setup/setup-windows-ps1.md), [`prereqs-windows.ps1` reference](../reference/scripts/setup/prereqs-windows-ps1.md), [`generate_windows_cert.py` reference](../reference/scripts/auth-and-certs/generate-windows-cert-py.md), [`rotate_cert_windows.py` reference](../reference/scripts/auth-and-certs/rotate-cert-windows-py.md).

## Cross-platform invariant

Once a certificate-signed JWT assertion exists, the rest of the three-hop token flow, and everything built on top of it — Graph calls, the MCP tool surface, audit attribution — is identical across platforms. The platform difference is contained entirely to *how* the assertion gets signed (`auth/certificate.py::build_client_assertion()` branches on whether `private_key_pem` or `cert_sha1` was supplied); nothing downstream of Hop 1 knows or cares which OS produced the token.

## Current limitation: no execution sandbox

Entrabot does not currently ship an execution sandbox on any platform. Process- and filesystem-level containment remains under evaluation.

## See also

- [Security Boundaries](security-boundaries.md) — why the Mac/Linux and Windows certificate paths differ in strength, and the sandbox limitation above.
- [Identity and Token Flow](identity-and-token-flow.md) — the three-hop flow the certificate assertion feeds into.
- [Platform Docs](../platform-docs/platform-macos.md) (forthcoming) — per-OS setup, rotation, and troubleshooting guidance: [macOS](../platform-docs/platform-macos.md), [Linux](../platform-docs/platform-linux.md), [Windows](../platform-docs/platform-windows.md).
- Per-script reference pages: [`setup.sh`](../reference/scripts/setup/setup-sh.md), [`teardown.sh`](../reference/scripts/teardown/teardown-sh.md), [`start-entrabot.sh`](../reference/scripts/setup/start-entrabot-sh.md), [`prereqs-macos.sh`](../reference/scripts/setup/prereqs-macos-sh.md), [`setup-windows.ps1`](../reference/scripts/setup/setup-windows-ps1.md), [`prereqs-windows.ps1`](../reference/scripts/setup/prereqs-windows-ps1.md), [`generate_windows_cert.py`](../reference/scripts/auth-and-certs/generate-windows-cert-py.md), [`rotate_cert_windows.py`](../reference/scripts/auth-and-certs/rotate-cert-windows-py.md).

