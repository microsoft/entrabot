"""Tests that scripts/entra_provisioning.py asserts the keyring backend
is secure before any keyring read or write.

The runtime CredentialStore already validates the backend; this closes
the gap that the provisioning path used raw ``keyring.set_password``
without validation, which would silently write the Provisioner cert and
key in cleartext on a host with an insecure backend.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from unittest.mock import patch

import pytest


def _load_entra_provisioning():
    """Import the scripts/entra_provisioning.py module on demand."""
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    path = repo_root / "scripts" / "entra_provisioning.py"
    spec = importlib.util.spec_from_file_location("entra_provisioning", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["entra_provisioning"] = module
    spec.loader.exec_module(module)
    return module


def _insecure_backend_error():
    from entrabot.errors import InsecureKeyringBackendError

    return InsecureKeyringBackendError(
        "keyrings.alt.file.PlaintextKeyring",
        ("keyring.backends.macOS.Keyring",),
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows uses file-backed store")
class TestProvisionerKeychainOpsAssertBackend:
    def test_store_cert_asserts_backend_before_writing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ep = _load_entra_provisioning()
        write_called = False

        class _FakeKeyring:
            errors = type("errors", (), {"PasswordDeleteError": Exception})

            @staticmethod
            def set_password(*_a: object, **_kw: object) -> None:
                nonlocal write_called
                write_called = True

            @staticmethod
            def get_password(*_a: object, **_kw: object) -> str | None:
                return None

            @staticmethod
            def delete_password(*_a: object, **_kw: object) -> None:
                pass

        monkeypatch.setattr(ep, "_keyring_module", lambda: _FakeKeyring)

        from entrabot.errors import InsecureKeyringBackendError

        with patch(
            "entrabot.platform.keyring_backend.assert_allowed_keyring_backend",
            side_effect=lambda: (_ for _ in ()).throw(_insecure_backend_error()),
        ), pytest.raises(InsecureKeyringBackendError):
            ep._keychain_store_cert("acct", "PEM-DATA")

        assert write_called is False, (
            "keyring.set_password was called even though the backend assertion raised"
        )

    def test_get_cert_asserts_backend_before_reading(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ep = _load_entra_provisioning()
        read_called = False

        class _FakeKeyring:
            errors = type("errors", (), {"PasswordDeleteError": Exception})

            @staticmethod
            def set_password(*_a: object, **_kw: object) -> None:
                pass

            @staticmethod
            def get_password(*_a: object, **_kw: object) -> str | None:
                nonlocal read_called
                read_called = True
                return "leaked"

            @staticmethod
            def delete_password(*_a: object, **_kw: object) -> None:
                pass

        monkeypatch.setattr(ep, "_keyring_module", lambda: _FakeKeyring)

        from entrabot.errors import InsecureKeyringBackendError

        with patch(
            "entrabot.platform.keyring_backend.assert_allowed_keyring_backend",
            side_effect=lambda: (_ for _ in ()).throw(_insecure_backend_error()),
        ), pytest.raises(InsecureKeyringBackendError):
            ep._keychain_get_cert("acct")

        assert read_called is False, (
            "keyring.get_password was called even though the backend assertion raised"
        )

    def test_store_cert_succeeds_when_backend_is_secure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ep = _load_entra_provisioning()
        written: dict[str, tuple[str, str, str]] = {}

        class _FakeKeyring:
            errors = type("errors", (), {"PasswordDeleteError": Exception})

            @staticmethod
            def set_password(svc: str, acct: str, pwd: str) -> None:
                written["call"] = (svc, acct, pwd)

            @staticmethod
            def get_password(*_a: object, **_kw: object) -> str | None:
                return None

            @staticmethod
            def delete_password(*_a: object, **_kw: object) -> None:
                pass

        monkeypatch.setattr(ep, "_keyring_module", lambda: _FakeKeyring)

        with patch(
            "entrabot.platform.keyring_backend.assert_allowed_keyring_backend",
            return_value="keyring.backends.macOS.Keyring",
        ):
            ep._keychain_store_cert("acct", "PEM-DATA")

        assert written["call"] == (ep._KEYCHAIN_SERVICE_CERT, "acct", "PEM-DATA")
