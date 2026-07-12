# Platform Abstraction Layer

## Purpose

Every OS stores secrets differently, and the Blueprint's private key must never leave whatever store it lives in. This layer gives the rest of Entrabot one interface — `CredentialStore` — and hides the macOS/Linux/Windows differences behind it.

## The `CredentialStore` protocol

`src/entrabot/platform/base.py` defines the interface every OS module implements:

```python
class CredentialStore(Protocol):
    def store(self, service: str, key: str, value: str) -> None: ...
    def retrieve(self, service: str, key: str) -> str | None: ...
    def delete(self, service: str, key: str) -> None: ...
```

Callers never construct an OS-specific class directly. `entrabot.platform.get_credential_store()` inspects `platform.system()` and returns a `MacCredentialStore`, `LinuxCredentialStore`, or `WindowsCredentialStore` — all three satisfy the same protocol, so `auth/` and `tools/teams.py` call `store()`/`retrieve()`/`delete()` without knowing which OS they're on. An unrecognized platform raises `RuntimeError` rather than guessing.

## OS-specific implementations

| OS | Backing store | Notes |
|---|---|---|
| macOS | `keyring` → Keychain | The Blueprint's PEM private key is stored and retrieved as a plain string entry under the `entrabot` service. |
| Linux | `keyring` → Secret Service / KWallet | Requires one of the allow-listed backends (Secret Service, KWallet, or `libsecret`) to actually be running; `assert_allowed_keyring_backend` fails closed if the active backend isn't one of them. |
| Windows | `keyring` → Credential Manager for generic key/value secrets | The Blueprint private key is **not** stored as a PEM — it lives as a non-exportable CNG key in `Cert:\CurrentUser\My`, backed by the TPM (Microsoft Platform Crypto Provider) when available and falling back to a software-protected key store otherwise. Signing happens through `ncrypt.dll`, keyed by the certificate's SHA-1 thumbprint, never by exporting the key material. |

On every platform, `assert_allowed_keyring_backend()` runs before any store/retrieve/delete call and raises `InsecureKeyringBackendError` if the active `keyring` backend isn't on the OS's allow-list — a misconfigured or fallback in-memory backend fails loud instead of silently storing secrets somewhere insecure.

## Runtime dispatch

```python
def get_credential_store() -> CredentialStore:
    system = platform.system()
    if system == "Darwin":
        return MacCredentialStore()
    if system == "Windows":
        return WindowsCredentialStore()
    if system == "Linux":
        return LinuxCredentialStore()
    raise RuntimeError(f"Unsupported platform: {system}")
```

See the [Platform Docs](../../platform-docs/platform-macos.md) section (forthcoming) for per-OS setup, rotation, and troubleshooting guidance — [macOS](../../platform-docs/platform-macos.md), [Linux](../../platform-docs/platform-linux.md), and [Windows](../../platform-docs/platform-windows.md).
