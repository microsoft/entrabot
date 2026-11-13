"""Tests for the Windows CNG-backed PKCS1+SHA256 signer.

The real ``ncrypt.dll`` only exists on Windows, so all tests here mock the
DLL lookup. We assert:
- The padding-info struct contains the literal string ``"SHA256"``.
- A successful flow makes exactly two ``NCryptSignHash`` calls — first
  with a NULL output buffer to size the signature, second with the real
  buffer.
- ``NTE_BUFFER_TOO_SMALL`` (0x80090028) on a probe call still proceeds
  cleanly — that's the expected size-discovery status, not an error. ctypes
  returns it as a NEGATIVE number (``restype`` is a signed ``c_long``), so the
  mocks do too.
- A non-zero, non-NTE_BUFFER_TOO_SMALL NTSTATUS is raised with the value
  in the message.
- The cert is looked up by thumbprint against
  ``Cert:\\CurrentUser\\My``-equivalent semantics via crypt32.
"""

from __future__ import annotations

import ctypes
import gc
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from entrabot.auth import cncrypt_signer

NTE_BUFFER_TOO_SMALL = 0x80090028


def _signed(status: int) -> int:
    """A SECURITY_STATUS as ctypes hands it back: ``restype`` is a signed ``c_long``."""
    return status - (1 << 32) if status & 0x80000000 else status


def _padding_alg_address(info: cncrypt_signer._BcryptPkcs1PaddingInfo) -> int:
    return ctypes.c_void_p.from_address(ctypes.addressof(info)).value or 0


def _make_mock_dlls(
    *,
    cert_handle: int = 0xCEC0,
    key_handle: int = 0xABCD,
    sig_size_first: int = 256,
    final_signature: bytes = b"\xab" * 256,
    sign_first_status: int = _signed(NTE_BUFFER_TOO_SMALL),
    sign_second_status: int = 0,
    caller_frees_key: bool = True,
) -> tuple[MagicMock, MagicMock]:
    """Build mock crypt32 + ncrypt DLLs configured for a normal flow."""
    crypt32 = MagicMock(name="crypt32")
    ncrypt = MagicMock(name="ncrypt")

    crypt32.CertOpenStore.return_value = 0xDEADBEEF
    crypt32.CertFindCertificateInStore.return_value = cert_handle

    def acquire_key(_cert, _flags, _reserved, key_out, _spec_out, free_out):
        key_out._obj.value = key_handle
        # pfCallerFreeProvOrNCryptKey: whether this handle is the caller's to free.
        free_out._obj.value = 1 if caller_frees_key else 0
        return 1

    crypt32.CryptAcquireCertificatePrivateKey.side_effect = acquire_key

    call_state = {"count": 0}

    def sign_hash(_h, _padding, _hash, _hash_len, output, output_len, written, _flags):
        call_state["count"] += 1
        if call_state["count"] == 1:
            written._obj.value = sig_size_first
            return sign_first_status
        # Second call — copy the canned signature into the output buffer
        if output:
            for i, byte in enumerate(final_signature):
                output[i] = byte
            written._obj.value = len(final_signature)
        return sign_second_status

    ncrypt.NCryptSignHash.side_effect = sign_hash
    ncrypt.NCryptFreeObject.return_value = 0
    crypt32.CertFreeCertificateContext.return_value = 1
    crypt32.CertCloseStore.return_value = 1

    return crypt32, ncrypt


class TestSignPkcs1Sha256:
    def test_happy_path_returns_signature_bytes(self) -> None:
        crypt32, ncrypt = _make_mock_dlls()
        with patch.object(cncrypt_signer, "_load_dlls", return_value=(crypt32, ncrypt)):
            result = cncrypt_signer.sign_pkcs1_sha256(thumbprint="A" * 40, hash_bytes=b"\x00" * 32)
        assert result == b"\xab" * 256
        assert ncrypt.NCryptSignHash.call_count == 2

    def test_padding_info_carries_sha256(self) -> None:
        info = cncrypt_signer._build_pkcs1_padding_info()
        # pszAlgId is a wide-string pointer; resolve it back to the literal.
        alg_id = ctypes.wstring_at(info.pszAlgId)
        assert alg_id == "SHA256"

    def test_sha256_alg_id_is_module_level_unicode_buffer(self) -> None:
        alg_id = cncrypt_signer._SHA256_ALG_ID

        first = cncrypt_signer._build_pkcs1_padding_info()
        second = cncrypt_signer._build_pkcs1_padding_info()

        assert isinstance(alg_id, ctypes.Array)
        assert alg_id.value == "SHA256"
        assert id(cncrypt_signer._SHA256_ALG_ID) == id(alg_id)
        assert _padding_alg_address(first) == ctypes.addressof(alg_id)
        assert _padding_alg_address(second) == ctypes.addressof(alg_id)

    def test_padding_info_alg_id_survives_gc_after_struct_scope(self) -> None:
        info = cncrypt_signer._build_pkcs1_padding_info()
        alg_address = _padding_alg_address(info)

        del info
        gc.collect()

        assert alg_address == ctypes.addressof(cncrypt_signer._SHA256_ALG_ID)
        assert ctypes.wstring_at(alg_address) == "SHA256"

    def test_padding_info_alg_id_is_shared_across_threads(self) -> None:
        def build_and_read() -> tuple[int, str]:
            info = cncrypt_signer._build_pkcs1_padding_info()
            alg_address = _padding_alg_address(info)
            return alg_address, ctypes.wstring_at(alg_address)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: build_and_read(), range(2)))

        expected_address = ctypes.addressof(cncrypt_signer._SHA256_ALG_ID)
        assert results == [(expected_address, "SHA256"), (expected_address, "SHA256")]

    def test_nonzero_ntstatus_other_than_buffer_too_small_raises(self) -> None:
        crypt32, ncrypt = _make_mock_dlls(sign_first_status=_signed(0xC000_0001))
        with (
            patch.object(cncrypt_signer, "_load_dlls", return_value=(crypt32, ncrypt)),
            pytest.raises(cncrypt_signer.SigningError, match="0xC0000001"),
        ):
            cncrypt_signer.sign_pkcs1_sha256(thumbprint="A" * 40, hash_bytes=b"\x00" * 32)

    def test_buffer_too_small_is_accepted_whether_signed_or_unsigned(self) -> None:
        for status in (NTE_BUFFER_TOO_SMALL, _signed(NTE_BUFFER_TOO_SMALL)):
            crypt32, ncrypt = _make_mock_dlls(sign_first_status=status)
            with patch.object(cncrypt_signer, "_load_dlls", return_value=(crypt32, ncrypt)):
                result = cncrypt_signer.sign_pkcs1_sha256(
                    thumbprint="A" * 40, hash_bytes=b"\x00" * 32
                )
            assert result == b"\xab" * 256

    def test_sign_failure_is_reported_unsigned_with_what_to_do(self) -> None:
        # NTE_BAD_KEYSET: the key container is gone (deleted, or it lives elsewhere).
        crypt32, ncrypt = _make_mock_dlls(sign_second_status=_signed(0x80090016))
        with (
            patch.object(cncrypt_signer, "_load_dlls", return_value=(crypt32, ncrypt)),
            pytest.raises(cncrypt_signer.SigningError) as excinfo,
        ):
            cncrypt_signer.sign_pkcs1_sha256(thumbprint="A" * 40, hash_bytes=b"\x00" * 32)
        message = str(excinfo.value)
        assert "0x80090016" in message and "-0x" not in message
        assert "setup-windows.cmd" in message
        assert excinfo.value.code == 0x80090016

    def test_acquire_failure_reports_last_error(self) -> None:
        crypt32, ncrypt = _make_mock_dlls()
        crypt32.CryptAcquireCertificatePrivateKey.side_effect = lambda *a, **k: 0
        with (
            patch.object(cncrypt_signer, "_load_dlls", return_value=(crypt32, ncrypt)),
            patch.object(cncrypt_signer, "_last_error", return_value=0x8009200B),
            pytest.raises(cncrypt_signer.SigningError) as excinfo,
        ):
            cncrypt_signer.sign_pkcs1_sha256(thumbprint="C" * 40, hash_bytes=b"\x00" * 32)
        message = str(excinfo.value)
        assert "0x8009200B" in message
        assert "setup-windows.cmd" in message
        assert excinfo.value.code == 0x8009200B

    def test_messages_never_contain_the_thumbprint(self) -> None:
        thumbprint = "0123456789ABCDEF0123456789ABCDEF01234567"
        crypt32, ncrypt = _make_mock_dlls()
        crypt32.CertFindCertificateInStore.return_value = 0
        with (
            patch.object(cncrypt_signer, "_load_dlls", return_value=(crypt32, ncrypt)),
            pytest.raises(cncrypt_signer.CertNotFoundError) as excinfo,
        ):
            cncrypt_signer.sign_pkcs1_sha256(thumbprint=thumbprint, hash_bytes=b"\x00" * 32)
        assert thumbprint not in str(excinfo.value)
        assert "setup-windows.cmd" in str(excinfo.value)

        with pytest.raises(ValueError) as bad:
            cncrypt_signer.sign_pkcs1_sha256(thumbprint=thumbprint + "0", hash_bytes=b"\x00" * 32)
        assert thumbprint not in str(bad.value)

    def test_thumbprint_must_be_40_hex(self) -> None:
        with pytest.raises(ValueError, match="thumbprint"):
            cncrypt_signer.sign_pkcs1_sha256(thumbprint="not-hex", hash_bytes=b"\x00" * 32)

    def test_cert_not_found_raises(self) -> None:
        crypt32, ncrypt = _make_mock_dlls()
        crypt32.CertFindCertificateInStore.return_value = 0
        with (
            patch.object(cncrypt_signer, "_load_dlls", return_value=(crypt32, ncrypt)),
            pytest.raises(cncrypt_signer.CertNotFoundError),
        ):
            cncrypt_signer.sign_pkcs1_sha256(thumbprint="B" * 40, hash_bytes=b"\x00" * 32)

    def test_acquire_private_key_failure_raises(self) -> None:
        crypt32, ncrypt = _make_mock_dlls()
        crypt32.CryptAcquireCertificatePrivateKey.side_effect = lambda *a, **k: 0
        with (
            patch.object(cncrypt_signer, "_load_dlls", return_value=(crypt32, ncrypt)),
            pytest.raises(cncrypt_signer.SigningError, match="(?i)acquire"),
        ):
            cncrypt_signer.sign_pkcs1_sha256(thumbprint="C" * 40, hash_bytes=b"\x00" * 32)

    def test_releases_handles_on_happy_path(self) -> None:
        crypt32, ncrypt = _make_mock_dlls()
        with patch.object(cncrypt_signer, "_load_dlls", return_value=(crypt32, ncrypt)):
            cncrypt_signer.sign_pkcs1_sha256(thumbprint="D" * 40, hash_bytes=b"\x00" * 32)
        ncrypt.NCryptFreeObject.assert_called()
        crypt32.CertFreeCertificateContext.assert_called()
        crypt32.CertCloseStore.assert_called()

    def test_key_handle_is_not_freed_when_the_api_keeps_ownership(self) -> None:
        # A handle the API says it still owns (a cached one, say) must not be freed
        # here: that would be a double free when its real owner releases it.
        crypt32, ncrypt = _make_mock_dlls(caller_frees_key=False)
        with patch.object(cncrypt_signer, "_load_dlls", return_value=(crypt32, ncrypt)):
            result = cncrypt_signer.sign_pkcs1_sha256(thumbprint="D" * 40, hash_bytes=b"\x00" * 32)
        assert result == b"\xab" * 256
        ncrypt.NCryptFreeObject.assert_not_called()
        crypt32.CertFreeCertificateContext.assert_called()
        crypt32.CertCloseStore.assert_called()

    def test_releases_handles_on_failure(self) -> None:
        crypt32, ncrypt = _make_mock_dlls(sign_first_status=_signed(0xC000_0001))
        with (
            patch.object(cncrypt_signer, "_load_dlls", return_value=(crypt32, ncrypt)),
            pytest.raises(cncrypt_signer.SigningError),
        ):
            cncrypt_signer.sign_pkcs1_sha256(thumbprint="E" * 40, hash_bytes=b"\x00" * 32)
        ncrypt.NCryptFreeObject.assert_called()
        crypt32.CertFreeCertificateContext.assert_called()


class TestLoadDllsGuard:
    def test_load_dlls_refuses_on_non_windows(self) -> None:
        with (
            patch.object(cncrypt_signer.sys, "platform", "darwin"),
            pytest.raises(RuntimeError, match="Windows"),
        ):
            cncrypt_signer._load_dlls()


class TestDescribeStatus:
    @pytest.mark.parametrize(
        ("code", "needle"),
        [
            (0x80090016, "key"),  # NTE_BAD_KEYSET
            (0x8009200B, "private key"),  # CRYPT_E_NO_KEY_PROPERTY
            (0x80090030, "TPM"),  # NTE_DEVICE_NOT_READY
            (0x80280921, "TPM"),  # TPM 2.0 lockout
            (0x8010006E, "smart card"),  # SCARD_W_CANCELLED_BY_USER
            (1312, "logon session"),  # ERROR_NO_SUCH_LOGON_SESSION
        ],
    )
    def test_known_codes_get_actionable_text(self, code: int, needle: str) -> None:
        text = cncrypt_signer.describe_status(code)
        assert needle.lower() in text.lower()

    def test_unknown_code_still_says_what_to_do(self) -> None:
        text = cncrypt_signer.describe_status(0x80091234)
        assert "setup-windows.cmd" in text

    def test_accepts_signed_values(self) -> None:
        assert cncrypt_signer.describe_status(
            _signed(0x80090016)
        ) == cncrypt_signer.describe_status(0x80090016)
