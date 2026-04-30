"""Tests for the Windows platform shim — cert-store lookup + keyring methods.

Mock-based; runs on every host. Real Cert: store calls are isolated in
``_load_crypt32`` so tests can inject a MagicMock.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from entraclaw.platform import windows


class TestKeyringPassthrough:
    def test_store_calls_keyring(self) -> None:
        store = windows.WindowsCredentialStore()
        with patch.object(windows.keyring, "set_password") as set_password:
            store.store("svc", "k", "v")
        set_password.assert_called_once_with("svc", "k", "v")

    def test_retrieve_returns_keyring_value(self) -> None:
        store = windows.WindowsCredentialStore()
        with patch.object(windows.keyring, "get_password", return_value="x"):
            assert store.retrieve("svc", "k") == "x"

    def test_delete_swallows_password_delete_error(self) -> None:
        store = windows.WindowsCredentialStore()
        with patch.object(
            windows.keyring,
            "delete_password",
            side_effect=windows.keyring.errors.PasswordDeleteError("nope"),
        ):
            store.delete("svc", "k")  # must not raise


class TestCertStoreLookup:
    def test_find_cert_by_thumbprint_returns_true_when_present(self) -> None:
        crypt32 = MagicMock()
        crypt32.CertOpenStore.return_value = 0xDEAD
        crypt32.CertFindCertificateInStore.return_value = 0xCEC0
        with patch.object(windows, "_load_crypt32", return_value=crypt32):
            assert (
                windows.WindowsCredentialStore().find_cert_by_thumbprint("A" * 40)
                is True
            )
        crypt32.CertFreeCertificateContext.assert_called_once()
        crypt32.CertCloseStore.assert_called_once()

    def test_find_cert_by_thumbprint_returns_false_when_missing(self) -> None:
        crypt32 = MagicMock()
        crypt32.CertOpenStore.return_value = 0xDEAD
        crypt32.CertFindCertificateInStore.return_value = 0
        with patch.object(windows, "_load_crypt32", return_value=crypt32):
            assert (
                windows.WindowsCredentialStore().find_cert_by_thumbprint("B" * 40)
                is False
            )
        crypt32.CertFreeCertificateContext.assert_not_called()
        crypt32.CertCloseStore.assert_called_once()

    def test_find_cert_by_thumbprint_validates_format(self) -> None:
        with pytest.raises(ValueError, match="thumbprint"):
            windows.WindowsCredentialStore().find_cert_by_thumbprint("zzz")

    def test_find_cert_by_thumbprint_raises_when_store_fails_to_open(self) -> None:
        crypt32 = MagicMock()
        crypt32.CertOpenStore.return_value = 0
        with (
            patch.object(windows, "_load_crypt32", return_value=crypt32),
            pytest.raises(RuntimeError, match="CertOpenStore"),
        ):
            windows.WindowsCredentialStore().find_cert_by_thumbprint("C" * 40)


class TestLoadCrypt32Guard:
    def test_load_crypt32_refuses_on_non_windows(self) -> None:
        with (
            patch.object(windows.sys, "platform", "darwin"),
            pytest.raises(RuntimeError, match="Windows"),
        ):
            windows._load_crypt32()
