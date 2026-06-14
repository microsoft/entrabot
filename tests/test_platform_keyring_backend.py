"""Tests for fail-closed keyring backend validation."""

from __future__ import annotations

import importlib
from collections.abc import Callable

import keyring
import pytest

from entrabot import errors
from entrabot.platform import linux, mac, windows


def _native_backend(path: str) -> object:
    module_name, class_name = path.rsplit(".", 1)
    cls = getattr(importlib.import_module(module_name), class_name)
    return object.__new__(cls)


def _fake_backend(module_name: str, class_name: str) -> object:
    cls = type(class_name, (), {"__module__": module_name})
    return cls()


def _insecure_error() -> type[Exception]:
    return getattr(errors, "InsecureKeyringBackendError", Exception)


def _backend_validator() -> object:
    return importlib.import_module("entrabot.platform.keyring_backend")


@pytest.mark.parametrize(
    ("system", "backend_path"),
    [
        ("Darwin", "keyring.backends.macOS.Keyring"),
        ("Linux", "keyring.backends.SecretService.Keyring"),
        ("Linux", "keyring.backends.kwallet.DBusKeyring"),
        ("Linux", "keyring.backends.libsecret.Keyring"),
        ("Windows", "keyring.backends.Windows.WinVaultKeyring"),
    ],
)
def test_backend_validation_accepts_allowed_os_backend(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    backend_path: str,
) -> None:
    monkeypatch.setattr(keyring, "get_keyring", lambda: _native_backend(backend_path))

    assert _backend_validator().assert_allowed_keyring_backend(system) == backend_path


@pytest.mark.parametrize(
    ("system", "backend"),
    [
        ("Darwin", _fake_backend("keyrings.alt.file", "PlaintextKeyring")),
        ("Darwin", _fake_backend("keyring.backends.fail", "Keyring")),
        ("Linux", _fake_backend("keyrings.alt.file", "PlaintextKeyring")),
        ("Windows", _fake_backend("keyrings.alt.file", "PlaintextKeyring")),
        ("Windows", _fake_backend("keyring.backends.fail", "Keyring")),
        ("Linux", _fake_backend("keyring.backends.fail", "Keyring")),
    ],
)
def test_backend_validation_rejects_insecure_backend(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    backend: object,
) -> None:
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)

    with pytest.raises(_insecure_error(), match=backend.__class__.__module__):
        _backend_validator().assert_allowed_keyring_backend(system)


@pytest.mark.parametrize(
    ("store_factory", "module", "backend_path"),
    [
        (mac.MacCredentialStore, mac, "keyring.backends.macOS.Keyring"),
        (linux.LinuxCredentialStore, linux, "keyring.backends.SecretService.Keyring"),
        (windows.WindowsCredentialStore, windows, "keyring.backends.Windows.WinVaultKeyring"),
    ],
)
def test_credential_store_constructors_validate_active_backend(
    monkeypatch: pytest.MonkeyPatch,
    store_factory: Callable[[], object],
    module: object,
    backend_path: str,
) -> None:
    calls = 0

    def fake_get_keyring() -> object:
        nonlocal calls
        calls += 1
        return _native_backend(backend_path)

    monkeypatch.setattr(module.keyring, "get_keyring", fake_get_keyring)

    store_factory()

    assert calls == 1


@pytest.mark.parametrize(
    ("store_factory", "module"),
    [
        (mac.MacCredentialStore, mac),
        (linux.LinuxCredentialStore, linux),
        (windows.WindowsCredentialStore, windows),
    ],
)
def test_credential_store_constructors_reject_plaintext_backend(
    monkeypatch: pytest.MonkeyPatch,
    store_factory: Callable[[], object],
    module: object,
) -> None:
    monkeypatch.setattr(
        module.keyring,
        "get_keyring",
        lambda: _fake_backend("keyrings.alt.file", "PlaintextKeyring"),
    )

    with pytest.raises(_insecure_error(), match="keyrings.alt.file.PlaintextKeyring"):
        store_factory()
