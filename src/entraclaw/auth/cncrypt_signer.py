"""Windows CNG (ncrypt.dll) signer for non-exportable Blueprint cert keys.

The Mac/Linux flow loads a PEM private key from the OS keystore and hands it
to ``cryptography`` for signing. On Windows the Blueprint private key lives
in ``Cert:\\CurrentUser\\My`` under a CNG provider — TPM-backed (Microsoft
Platform Crypto Provider) when available, software-backed (Microsoft
Software Key Storage Provider) otherwise. Either way the key is
non-exportable; signing has to happen via ``ncrypt.dll``.

This module exposes one operation:

    sign_pkcs1_sha256(thumbprint, hash_bytes) -> bytes

Callers pass a SHA-256 hash (already computed) and a 40-char hex thumbprint
identifying the cert. We hand back the raw RSA PKCS#1 v1.5 signature bytes.
That's exactly what RS256 JWT requires.

The whole module imports cleanly on every platform — DLL access happens
inside ``_load_dlls`` which raises on non-Windows, so unit tests on Mac
mock ``_load_dlls`` and never touch ctypes-Win32 at all.
"""

from __future__ import annotations

import ctypes
import re
import sys
from ctypes import wintypes
from typing import Any

# CNG / crypt32 constants we need.
CERT_STORE_PROV_SYSTEM_W = 10
CERT_SYSTEM_STORE_CURRENT_USER = 1 << 16
CERT_FIND_HASH = 0x10000
CRYPT_ACQUIRE_ONLY_NCRYPT_KEY_FLAG = 0x00040000
CRYPT_ACQUIRE_SILENT_FLAG = 0x00000040
CERT_NCRYPT_KEY_SPEC = 0xFFFFFFFF
BCRYPT_PAD_PKCS1 = 0x2
NTE_BUFFER_TOO_SMALL = 0x80090028

_THUMBPRINT_RE = re.compile(r"^[0-9A-Fa-f]{40}$")


class SigningError(RuntimeError):
    """Raised when CNG signing fails for any reason other than cert-not-found."""


class CertNotFoundError(SigningError):
    """Raised when no cert with the given thumbprint exists in CurrentUser\\My."""


class _BcryptPkcs1PaddingInfo(ctypes.Structure):
    _fields_ = [("pszAlgId", ctypes.c_wchar_p)]


class _CryptIntegerBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _build_pkcs1_padding_info() -> _BcryptPkcs1PaddingInfo:
    return _BcryptPkcs1PaddingInfo(pszAlgId=ctypes.c_wchar_p("SHA256"))


def _load_dlls() -> tuple[Any, Any]:
    """Return ``(crypt32, ncrypt)`` DLL handles with 64-bit-safe signatures.

    Lifted into a function so unit tests on non-Windows hosts can patch it
    to inject mocks. Argtypes + restype are pinned explicitly so 64-bit
    pointer-sized handles (HCERTSTORE, PCCERT_CONTEXT, NCRYPT_KEY_HANDLE)
    don't get truncated to c_int on Win64.
    """
    if sys.platform != "win32":
        raise RuntimeError(
            "cncrypt_signer requires Windows; got platform=" + sys.platform
        )
    crypt32 = ctypes.windll.crypt32  # pragma: no cover
    ncrypt = ctypes.windll.ncrypt  # pragma: no cover

    crypt32.CertOpenStore.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
        ctypes.c_ulong, ctypes.c_void_p,
    ]
    crypt32.CertOpenStore.restype = ctypes.c_void_p

    crypt32.CertFindCertificateInStore.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p,
    ]
    crypt32.CertFindCertificateInStore.restype = ctypes.c_void_p

    crypt32.CryptAcquireCertificatePrivateKey.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    crypt32.CryptAcquireCertificatePrivateKey.restype = wintypes.BOOL

    crypt32.CertFreeCertificateContext.argtypes = [ctypes.c_void_p]
    crypt32.CertFreeCertificateContext.restype = wintypes.BOOL

    crypt32.CertCloseStore.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    crypt32.CertCloseStore.restype = wintypes.BOOL

    ncrypt.NCryptSignHash.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
        ctypes.c_void_p, ctypes.c_ulong,
    ]
    ncrypt.NCryptSignHash.restype = ctypes.c_long  # SECURITY_STATUS

    ncrypt.NCryptFreeObject.argtypes = [ctypes.c_void_p]
    ncrypt.NCryptFreeObject.restype = ctypes.c_long

    return crypt32, ncrypt


def _thumbprint_to_blob(thumbprint: str) -> tuple[ctypes.Array[ctypes.c_ubyte], _CryptIntegerBlob]:
    raw = bytes.fromhex(thumbprint)
    buf = (ctypes.c_ubyte * len(raw))(*raw)
    blob = _CryptIntegerBlob(cbData=len(raw), pbData=buf)
    return buf, blob


def sign_pkcs1_sha256(*, thumbprint: str, hash_bytes: bytes) -> bytes:
    """Sign a SHA-256 hash with the cert's CNG private key.

    Args:
        thumbprint: 40-char hex SHA-1 thumbprint of the cert in
            ``Cert:\\CurrentUser\\My``.
        hash_bytes: The 32-byte SHA-256 digest to sign.

    Returns:
        Raw RSA PKCS#1 v1.5 signature bytes (typically 256 for a 2048-bit
        key) — ready to base64url-encode for an RS256 JWT.

    Raises:
        ValueError: malformed thumbprint.
        CertNotFoundError: no cert found.
        SigningError: any other CNG / crypt32 failure.
    """
    if not _THUMBPRINT_RE.match(thumbprint or ""):
        raise ValueError(f"thumbprint must be 40 hex chars, got: {thumbprint!r}")
    if not isinstance(hash_bytes, (bytes, bytearray)) or len(hash_bytes) != 32:
        raise ValueError("hash_bytes must be 32 bytes (SHA-256 digest)")

    crypt32, ncrypt = _load_dlls()

    store = crypt32.CertOpenStore(
        CERT_STORE_PROV_SYSTEM_W,
        0,
        0,
        CERT_SYSTEM_STORE_CURRENT_USER,
        ctypes.c_wchar_p("My"),
    )
    if not store:
        raise SigningError("CertOpenStore(CurrentUser\\My) failed")

    cert_ctx = 0
    key_handle = wintypes.HANDLE(0)
    try:
        _buf, blob = _thumbprint_to_blob(thumbprint)
        cert_ctx = crypt32.CertFindCertificateInStore(
            store, 0x00010001, 0, CERT_FIND_HASH, ctypes.byref(blob), None
        )
        if not cert_ctx:
            raise CertNotFoundError(f"cert not found: {thumbprint}")

        spec = wintypes.DWORD(0)
        free_key = wintypes.BOOL(False)
        ok = crypt32.CryptAcquireCertificatePrivateKey(
            cert_ctx,
            CRYPT_ACQUIRE_ONLY_NCRYPT_KEY_FLAG | CRYPT_ACQUIRE_SILENT_FLAG,
            None,
            ctypes.byref(key_handle),
            ctypes.byref(spec),
            ctypes.byref(free_key),
        )
        if not ok or not key_handle.value:
            raise SigningError("CryptAcquireCertificatePrivateKey failed")

        padding = _build_pkcs1_padding_info()
        hash_buf = (ctypes.c_ubyte * len(hash_bytes))(*hash_bytes)

        sig_size = wintypes.DWORD(0)
        status = ncrypt.NCryptSignHash(
            key_handle,
            ctypes.byref(padding),
            hash_buf,
            len(hash_bytes),
            None,
            0,
            ctypes.byref(sig_size),
            BCRYPT_PAD_PKCS1,
        )
        if status != 0 and status != NTE_BUFFER_TOO_SMALL:
            raise SigningError(f"NCryptSignHash size probe failed: {hex(status)}")

        sig_buf = (ctypes.c_ubyte * sig_size.value)()
        written = wintypes.DWORD(0)
        status = ncrypt.NCryptSignHash(
            key_handle,
            ctypes.byref(padding),
            hash_buf,
            len(hash_bytes),
            sig_buf,
            sig_size.value,
            ctypes.byref(written),
            BCRYPT_PAD_PKCS1,
        )
        if status != 0:
            raise SigningError(f"NCryptSignHash failed: {hex(status)}")

        return bytes(sig_buf[: written.value])
    finally:
        if key_handle.value:
            ncrypt.NCryptFreeObject(key_handle)
        if cert_ctx:
            crypt32.CertFreeCertificateContext(cert_ctx)
        crypt32.CertCloseStore(store, 0)
