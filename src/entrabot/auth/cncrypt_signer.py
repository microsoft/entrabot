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

SETUP_COMMAND = r"scripts\setup-windows.cmd"

_THUMBPRINT_RE = re.compile(r"^[0-9A-Fa-f]{40}$")
_SHA256_ALG_ID = ctypes.create_unicode_buffer("SHA256")

_KEY_UNUSABLE = (
    "the certificate's private key is not usable on this machine by this user "
    f"— run {SETUP_COMMAND} to create a new one"
)

# What the common failures mean, in the words of someone who has to fix them.
# Codes are unsigned HRESULT / SECURITY_STATUS / Win32 values.
_STATUS_TEXT: dict[int, str] = {
    0x8009000B: f"the key is in the wrong state for signing (NTE_BAD_KEY_STATE); {_KEY_UNUSABLE}",
    0x8009000D: f"the key does not exist (NTE_NO_KEY); {_KEY_UNUSABLE}",
    0x80090010: (
        "access to the key was denied (NTE_PERM). The certificate was probably created by a "
        f"different Windows account — run {SETUP_COMMAND} as the user who runs Entrabot"
    ),
    0x80090016: (
        "the key container no longer exists (NTE_BAD_KEYSET): it was deleted, or the "
        f"certificate was copied from another machine; {_KEY_UNUSABLE}"
    ),
    0x80090022: (
        "the key provider wants to show a prompt, which a background process can't do "
        f"(NTE_SILENT_CONTEXT); {_KEY_UNUSABLE}"
    ),
    0x80090029: f"the key provider does not support this signature (NTE_NOT_SUPPORTED); "
    f"{_KEY_UNUSABLE}",
    0x80090030: (
        "the TPM is not ready (NTE_DEVICE_NOT_READY). Restart Windows; if it keeps happening, "
        f"run {SETUP_COMMAND} to create a new certificate"
    ),
    0x8009200B: (
        "the certificate has no private key on this machine (CRYPT_E_NO_KEY_PROPERTY); "
        f"run {SETUP_COMMAND} to create a new one"
    ),
    0x80280921: (
        "the TPM is locked out after too many failed attempts (TPM_20_E_LOCKOUT). Wait for "
        "the lockout to clear or restart Windows, then try again"
    ),
    0x80280400: f"the TPM command is blocked by policy (TPM_E_COMMAND_BLOCKED); {_KEY_UNUSABLE}",
    1312: (
        "this logon session has no credentials (ERROR_NO_SUCH_LOGON_SESSION), which is what "
        "an SSH key-authenticated or scheduled-task session looks like. Run Entrabot from a "
        "normal desktop sign-in"
    ),
}


def _unsigned(status: int) -> int:
    """A status as the documentation writes it.

    ``SECURITY_STATUS`` comes back from ctypes as a signed ``c_long``, so every
    failure is a negative number until it is masked.
    """
    return status & 0xFFFFFFFF


def describe_status(status: int) -> str:
    """What a CNG / crypt32 / TPM failure code means and what to do about it."""
    code = _unsigned(status)
    known = _STATUS_TEXT.get(code)
    if known is not None:
        return known
    if code & 0xFFFF0000 == 0x80280000:
        return (
            "the TPM reported an error. Restart Windows; if it keeps happening, "
            f"run {SETUP_COMMAND} to create a new certificate"
        )
    if code & 0xFFFF0000 == 0x80100000:
        return f"the smart card subsystem reported an error; {_KEY_UNUSABLE}"
    return _KEY_UNUSABLE


def _last_error() -> int:
    """``GetLastError`` as captured by ctypes for the calling thread (0 off Windows)."""
    getter = getattr(ctypes, "get_last_error", None)
    return _unsigned(getter()) if getter is not None else 0


class SigningError(RuntimeError):
    """Raised when CNG signing fails for any reason other than cert-not-found.

    ``code`` is the unsigned Windows status, when there is one. The message says
    what to do and never contains the certificate thumbprint.
    """

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class CertNotFoundError(SigningError):
    """Raised when no cert with the given thumbprint exists in CurrentUser\\My."""


def _failure(operation: str, status: int) -> SigningError:
    code = _unsigned(status)
    return SigningError(f"{operation} failed (0x{code:08X}): {describe_status(code)}", code=code)


class _BcryptPkcs1PaddingInfo(ctypes.Structure):
    _fields_ = [("pszAlgId", ctypes.c_wchar_p)]


class _CryptIntegerBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _build_pkcs1_padding_info() -> _BcryptPkcs1PaddingInfo:
    return _BcryptPkcs1PaddingInfo(pszAlgId=ctypes.cast(_SHA256_ALG_ID, ctypes.c_wchar_p))


_dll_cache: tuple[Any, Any] | None = None


def _load_dlls() -> tuple[Any, Any]:
    """Return ``(crypt32, ncrypt)`` DLL handles with 64-bit-safe signatures.

    Lifted into a function so unit tests on non-Windows hosts can patch it
    to inject mocks. Argtypes + restype are pinned explicitly so 64-bit
    pointer-sized handles (HCERTSTORE, PCCERT_CONTEXT, NCRYPT_KEY_HANDLE)
    don't get truncated to c_int on Win64.
    """
    global _dll_cache
    if sys.platform != "win32":
        raise RuntimeError("cncrypt_signer requires Windows; got platform=" + sys.platform)
    if _dll_cache is not None:  # pragma: no cover
        return _dll_cache
    # use_last_error: ctypes saves GetLastError right after each call, so a failure
    # can be reported with the reason instead of a bare "failed".
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)  # pragma: no cover
    ncrypt = ctypes.WinDLL("ncrypt", use_last_error=True)  # pragma: no cover

    crypt32.CertOpenStore.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    crypt32.CertOpenStore.restype = ctypes.c_void_p

    crypt32.CertFindCertificateInStore.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    crypt32.CertFindCertificateInStore.restype = ctypes.c_void_p

    crypt32.CryptAcquireCertificatePrivateKey.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    crypt32.CryptAcquireCertificatePrivateKey.restype = wintypes.BOOL

    crypt32.CertFreeCertificateContext.argtypes = [ctypes.c_void_p]
    crypt32.CertFreeCertificateContext.restype = wintypes.BOOL

    crypt32.CertCloseStore.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    crypt32.CertCloseStore.restype = wintypes.BOOL

    ncrypt.NCryptSignHash.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    # SECURITY_STATUS is signed here, so failures arrive negative; _unsigned() masks them.
    ncrypt.NCryptSignHash.restype = ctypes.c_long

    ncrypt.NCryptFreeObject.argtypes = [ctypes.c_void_p]
    ncrypt.NCryptFreeObject.restype = ctypes.c_long

    _dll_cache = (crypt32, ncrypt)
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
        # The value isn't echoed: a near-miss is still most of a real thumbprint.
        raise ValueError("thumbprint must be 40 hex chars")
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
        raise _failure("CertOpenStore(CurrentUser\\My)", _last_error())

    cert_ctx = 0
    key_handle = wintypes.HANDLE(0)
    free_key = wintypes.BOOL(False)
    try:
        _buf, blob = _thumbprint_to_blob(thumbprint)
        cert_ctx = crypt32.CertFindCertificateInStore(
            store, 0x00010001, 0, CERT_FIND_HASH, ctypes.byref(blob), None
        )
        if not cert_ctx:
            raise CertNotFoundError(
                "the configured Blueprint certificate is not in this user's certificate "
                f"store — run {SETUP_COMMAND} as the user who runs Entrabot"
            )

        spec = wintypes.DWORD(0)
        ok = crypt32.CryptAcquireCertificatePrivateKey(
            cert_ctx,
            CRYPT_ACQUIRE_ONLY_NCRYPT_KEY_FLAG | CRYPT_ACQUIRE_SILENT_FLAG,
            None,
            ctypes.byref(key_handle),
            ctypes.byref(spec),
            ctypes.byref(free_key),
        )
        if not ok or not key_handle.value:
            raise _failure("CryptAcquireCertificatePrivateKey", _last_error())

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
        if status != 0 and _unsigned(status) != NTE_BUFFER_TOO_SMALL:
            raise _failure("NCryptSignHash size probe", status)

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
            raise _failure("NCryptSignHash", status)

        return bytes(sig_buf[: written.value])
    finally:
        # Free the key handle only when CryptAcquireCertificatePrivateKey said it is
        # ours (pfCallerFreeProvOrNCryptKey). A handle it kept ownership of is released
        # with the certificate context; freeing it here as well would be a double free.
        if key_handle.value and free_key.value:
            ncrypt.NCryptFreeObject(key_handle)
        if cert_ctx:
            crypt32.CertFreeCertificateContext(cert_ctx)
        crypt32.CertCloseStore(store, 0)
