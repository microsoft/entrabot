"""Windows certificate and private-key lifecycle: create, find, test-sign, delete.

One owner for everything that touches a signing certificate in
``Cert:\\CurrentUser\\My``. Setup, rotation, teardown, preflight and the Provisioner
all go through this module, so the rules below hold everywhere:

* **No elevation, no PowerShell 7.** Keys are created with the in-box Windows
  PowerShell 5.1 (``New-SelfSignedCertificate``), resolved by absolute path.
* **Nothing is interpolated into script text.** The scripts are constants; every
  value reaches PowerShell through an environment variable, so a profile path or a
  subject containing an apostrophe cannot break (or inject into) the command. The
  scripts use cmdlets only, so they also run under Constrained Language Mode.
* **TPM by attempt.** ``Get-Tpm`` needs an elevated session, so asking "is there a
  TPM?" as a normal user always answers no. Instead the key is created on the
  Microsoft Platform Crypto Provider and used once; if either step fails, whatever
  was created is deleted and the key is created on the software provider.
* **Always non-exportable**, RSA 2048, SHA-256, signature-only, and no TLS usages
  (``-Type Custom`` with no EKU), so the certificate never shows up in a browser's
  client-certificate picker.
* **A key is only "usable" once it has signed.** ``test_sign`` signs through the
  same CNG path the runtime uses and verifies the result against the public key.
* **Deleting a certificate deletes its key**, so nothing is orphaned in the KSP.
* **No temp files.** The public DER is read in-process from the store.

Full certificate thumbprints never appear in messages or ``repr`` output.

On any other platform every public function raises ``WinCertError("Windows only")``.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import logging
import os
import re
import subprocess  # noqa: S404 — a fixed argv to the in-box PowerShell
import sys
from collections.abc import Mapping
from ctypes import wintypes
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils

logger = logging.getLogger("entrabot.platform.wincert")

PROVIDER_TPM = "Microsoft Platform Crypto Provider"
PROVIDER_SOFTWARE = "Microsoft Software Key Storage Provider"

KSP_TPM = "tpm"
KSP_SOFTWARE = "software"
KSP_UNKNOWN = "unknown"

_PROVIDER_BY_KSP = {KSP_TPM: PROVIDER_TPM, KSP_SOFTWARE: PROVIDER_SOFTWARE}
_KSP_BY_PROVIDER = {PROVIDER_TPM.lower(): KSP_TPM, PROVIDER_SOFTWARE.lower(): KSP_SOFTWARE}

# Warn this many days before the certificate stops working.
EXPIRY_WARNING_DAYS = 30

SETUP_COMMAND = r"scripts\setup-windows.cmd"

_SHA1_RE = re.compile(r"^[0-9A-Fa-f]{40}$")
_POWERSHELL_TIMEOUT_SECONDS = 180

# Environment variables that carry values into the constant scripts below.
_ENV_SUBJECT = "ENTRABOT_WINCERT_SUBJECT"
_ENV_FRIENDLY_NAME = "ENTRABOT_WINCERT_FRIENDLY_NAME"
_ENV_PROVIDER = "ENTRABOT_WINCERT_PROVIDER"
_ENV_NOT_AFTER = "ENTRABOT_WINCERT_NOT_AFTER"
_ENV_SHA1 = "ENTRABOT_WINCERT_SHA1"

# Constant script text. Cmdlets, hashtable splatting and property reads only: no
# .NET method calls and no type accelerators, so it is Constrained-Language-Mode
# safe. Single quotes only, so it survives being passed as one argv element.
# NotBefore is left to the cmdlet, which backdates it ten minutes for clock skew.
# Progress is silenced on the first line, before any cmdlet loads a module: Windows
# PowerShell 5.1 otherwise writes "Preparing modules for first use" to stderr as
# CLIXML whenever stderr isn't a console. Either way stderr is never the verdict;
# only the exit code is, and stderr is read just to explain a non-zero one.
_CREATE_SCRIPT = """\
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
$params = @{
    Type = 'Custom'
    Subject = $env:ENTRABOT_WINCERT_SUBJECT
    FriendlyName = $env:ENTRABOT_WINCERT_FRIENDLY_NAME
    CertStoreLocation = 'Cert:\\CurrentUser\\My'
    Provider = $env:ENTRABOT_WINCERT_PROVIDER
    KeyExportPolicy = 'NonExportable'
    KeyAlgorithm = 'RSA'
    KeyLength = 2048
    HashAlgorithm = 'SHA256'
    KeyUsage = 'DigitalSignature'
    KeyUsageProperty = 'Sign'
    NotAfter = $env:ENTRABOT_WINCERT_NOT_AFTER
}
$cert = New-SelfSignedCertificate @params
Write-Output $cert.Thumbprint
"""

_DELETE_SCRIPT = """\
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
$path = Join-Path -Path 'Cert:\\CurrentUser\\My' -ChildPath $env:ENTRABOT_WINCERT_SHA1
Remove-Item -LiteralPath $path
"""

_DELETE_WITH_KEY_SCRIPT = _DELETE_SCRIPT.rstrip("\n") + " -DeleteKey\n"


class WinCertError(Exception):
    """A certificate-store or key operation failed.

    The message is meant for the person running setup: it says what to do next and
    never contains key material or a full thumbprint.
    """


@dataclass(frozen=True)
class StoreCert:
    """A certificate in ``Cert:\\CurrentUser\\My``."""

    sha1_hex: str  # 40 uppercase hex
    x5t_s256: str  # 43-char base64url SHA-256 of the DER, no padding
    der: bytes
    subject: str
    friendly_name: str
    not_before: datetime  # aware, UTC
    not_after: datetime  # aware, UTC
    has_private_key: bool
    ksp: str  # "tpm" | "software" | "unknown"

    def __repr__(self) -> str:
        return (
            f"StoreCert(subject={self.subject!r}, friendly_name={self.friendly_name!r}, "
            f"ksp={self.ksp!r}, not_after={self.not_after.date().isoformat()}, "
            f"has_private_key={self.has_private_key}, sha1={mask_thumbprint(self.sha1_hex)})"
        )


@dataclass(frozen=True)
class CertHealth:
    """Whether the configured certificate can be used, without touching the network.

    ``status`` is one of ``ok``, ``expiring``, ``expired``, ``not_yet_valid``,
    ``missing`` or ``no_private_key``.
    """

    status: str
    days_left: int | None
    not_after: datetime | None
    message: str

    @property
    def usable(self) -> bool:
        return self.status in ("ok", "expiring")


@dataclass(frozen=True)
class _RawCert:
    """What the store knows about one certificate, before any parsing."""

    der: bytes
    friendly_name: str
    provider_name: str | None  # None: the certificate has no private-key property
    # The key's name in its provider. Not key material: it is how the key is found
    # (and deleted by hand) when the certificate can't be used to reach it.
    key_container: str | None = None


def mask_thumbprint(thumbprint: str | None) -> str:
    """The last four characters only: enough to tell two certificates apart."""
    if not thumbprint:
        return "(none)"
    return "…" + thumbprint[-4:].upper()


# ─── platform guard ──────────────────────────────────────────────────────────


def _require_windows() -> None:
    if sys.platform != "win32":
        raise WinCertError("Windows only")


def _normalize_sha1(sha1_hex: str) -> str:
    if not isinstance(sha1_hex, str) or not _SHA1_RE.match(sha1_hex):
        raise WinCertError("The certificate thumbprint must be 40 hexadecimal characters.")
    return sha1_hex.upper()


# ─── in-box PowerShell ───────────────────────────────────────────────────────


def _powershell_exe() -> str:
    """Absolute path of the in-box Windows PowerShell 5.1, never ``pwsh``."""
    root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
    return str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def _decode_console(data: bytes | None) -> str:
    """Decode Windows PowerShell output, which uses the OEM code page when piped."""
    if not data:
        return ""
    for codec in ("utf-8", "oem"):
        try:
            return data.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class _PowerShellResult:
    returncode: int
    stdout: str
    stderr: str


def _run_powershell(script: str, variables: Mapping[str, str]) -> _PowerShellResult:
    """Run one of the constant scripts. ``variables`` travel in the environment."""
    _require_windows()
    argv = [
        _powershell_exe(),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]
    try:
        completed = subprocess.run(  # noqa: S603 — fixed argv, constant script
            argv,
            env=_child_environment(variables),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=_POWERSHELL_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise WinCertError(
            "Windows PowerShell (powershell.exe) was not found under %SystemRoot%\\System32."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise WinCertError(
            f"Windows PowerShell did not finish within {_POWERSHELL_TIMEOUT_SECONDS} seconds."
        ) from exc
    return _PowerShellResult(
        returncode=completed.returncode,
        stdout=_decode_console(completed.stdout),
        stderr=_decode_console(completed.stderr),
    )


def _child_environment(variables: Mapping[str, str]) -> dict[str, str]:
    """This process's environment plus ``variables``, without ``PSModulePath``.

    Setup started from a PowerShell 7 terminal inherits PowerShell 7's module path,
    and Windows PowerShell only gets a corrected one when PowerShell 7 starts it
    directly, not through an intermediate process such as this one. Left in place,
    Windows PowerShell resolves in-box modules to PowerShell 7 versions it can't load
    and their cmdlets fail with CommandNotFoundException. With the variable absent it
    constructs its own default (Microsoft Learn, about_PSModulePath, "Starting Windows
    PowerShell from PowerShell 7").
    """
    env = {name: value for name, value in os.environ.items() if name.upper() != "PSMODULEPATH"}
    env.update(variables)
    return env


_CLIXML_ERROR_RE = re.compile(r'<S S="Error">(.*?)</S>', re.DOTALL)


def _error_text(stderr: str, *, limit: int = 400) -> str:
    """The readable part of PowerShell's stderr, on one line."""
    text = stderr or ""
    if text.lstrip().startswith("#< CLIXML"):
        parts = _CLIXML_ERROR_RE.findall(text)
        text = "".join(parts).replace("_x000D__x000A_", "\n") if parts else ""
    text = " ".join(text.split())
    return (text[:limit] or "no error text").strip()


# ─── certificate store (ctypes) ──────────────────────────────────────────────

_X509_ASN_ENCODING = 0x00000001
_PKCS_7_ASN_ENCODING = 0x00010000
_ENCODING = _X509_ASN_ENCODING | _PKCS_7_ASN_ENCODING
_CERT_STORE_PROV_SYSTEM_W = 10
_CERT_SYSTEM_STORE_CURRENT_USER = 1 << 16
_CERT_STORE_READONLY_FLAG = 0x00008000
_CERT_FIND_HASH = 0x10000
_CERT_KEY_PROV_INFO_PROP_ID = 2
_CERT_FRIENDLY_NAME_PROP_ID = 11


class _CryptHashBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class _CertContext(ctypes.Structure):
    _fields_ = [
        ("dwCertEncodingType", wintypes.DWORD),
        ("pbCertEncoded", ctypes.c_void_p),
        ("cbCertEncoded", wintypes.DWORD),
        ("pCertInfo", ctypes.c_void_p),
        ("hCertStore", ctypes.c_void_p),
    ]


class _CryptKeyProvInfo(ctypes.Structure):
    _fields_ = [
        ("pwszContainerName", ctypes.c_wchar_p),
        ("pwszProvName", ctypes.c_wchar_p),
        ("dwProvType", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("cProvParam", wintypes.DWORD),
        ("rgProvParam", ctypes.c_void_p),
        ("dwKeySpec", wintypes.DWORD),
    ]


_crypt32_handle: Any = None


def _load_crypt32() -> Any:  # pragma: no cover
    """crypt32 with pinned, 64-bit-safe signatures and ``GetLastError`` capture."""
    global _crypt32_handle
    _require_windows()
    if _crypt32_handle is not None:
        return _crypt32_handle
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined]

    crypt32.CertOpenStore.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    crypt32.CertOpenStore.restype = ctypes.c_void_p
    crypt32.CertFindCertificateInStore.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    crypt32.CertFindCertificateInStore.restype = ctypes.c_void_p
    crypt32.CertEnumCertificatesInStore.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    crypt32.CertEnumCertificatesInStore.restype = ctypes.c_void_p
    crypt32.CertGetCertificateContextProperty.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
    ]
    crypt32.CertGetCertificateContextProperty.restype = wintypes.BOOL
    crypt32.CertFreeCertificateContext.argtypes = [ctypes.c_void_p]
    crypt32.CertFreeCertificateContext.restype = wintypes.BOOL
    crypt32.CertCloseStore.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    crypt32.CertCloseStore.restype = wintypes.BOOL
    _crypt32_handle = crypt32
    return crypt32


def _open_my_store(crypt32: Any) -> int:  # pragma: no cover
    store = crypt32.CertOpenStore(
        _CERT_STORE_PROV_SYSTEM_W,
        0,
        None,
        _CERT_SYSTEM_STORE_CURRENT_USER | _CERT_STORE_READONLY_FLAG,
        ctypes.c_wchar_p("My"),
    )
    if not store:
        raise WinCertError(
            f"Could not open this user's certificate store (error {ctypes.get_last_error()})."  # type: ignore[attr-defined]
        )
    return store


def _context_property(crypt32: Any, context: int, prop_id: int) -> Any:  # pragma: no cover
    """A certificate property as a ctypes buffer, or None when it isn't set."""
    size = wintypes.DWORD(0)
    if not crypt32.CertGetCertificateContextProperty(context, prop_id, None, ctypes.byref(size)):
        return None
    if not size.value:
        return None
    buffer = ctypes.create_string_buffer(size.value)
    if not crypt32.CertGetCertificateContextProperty(context, prop_id, buffer, ctypes.byref(size)):
        return None
    return buffer


def _raw_from_context(crypt32: Any, context: int) -> _RawCert:  # pragma: no cover
    """Copy what's needed out of a ``PCCERT_CONTEXT`` while it is still alive."""
    info = ctypes.cast(context, ctypes.POINTER(_CertContext)).contents
    der = ctypes.string_at(info.pbCertEncoded, info.cbCertEncoded)

    friendly_name = ""
    name_buffer = _context_property(crypt32, context, _CERT_FRIENDLY_NAME_PROP_ID)
    if name_buffer is not None:
        friendly_name = ctypes.wstring_at(ctypes.addressof(name_buffer))

    provider_name: str | None = None
    key_container: str | None = None
    key_buffer = _context_property(crypt32, context, _CERT_KEY_PROV_INFO_PROP_ID)
    if key_buffer is not None:
        key_info = ctypes.cast(key_buffer, ctypes.POINTER(_CryptKeyProvInfo)).contents
        provider_name = key_info.pwszProvName or ""
        key_container = key_info.pwszContainerName or None
    return _RawCert(
        der=der,
        friendly_name=friendly_name,
        provider_name=provider_name,
        key_container=key_container,
    )


def _read_store_cert(sha1_hex: str) -> _RawCert | None:  # pragma: no cover
    """The certificate with this SHA-1 thumbprint in ``CurrentUser\\My``, or None."""
    crypt32 = _load_crypt32()
    store = _open_my_store(crypt32)
    context = None
    try:
        raw_hash = bytes.fromhex(sha1_hex)
        hash_buffer = (ctypes.c_ubyte * len(raw_hash))(*raw_hash)
        blob = _CryptHashBlob(cbData=len(raw_hash), pbData=hash_buffer)
        context = crypt32.CertFindCertificateInStore(
            store, _ENCODING, 0, _CERT_FIND_HASH, ctypes.byref(blob), None
        )
        if not context:
            return None
        return _raw_from_context(crypt32, context)
    finally:
        if context:
            crypt32.CertFreeCertificateContext(context)
        crypt32.CertCloseStore(store, 0)


def _enum_store_certs() -> list[_RawCert]:  # pragma: no cover
    """Every certificate in ``CurrentUser\\My``."""
    crypt32 = _load_crypt32()
    store = _open_my_store(crypt32)
    found: list[_RawCert] = []
    try:
        context = crypt32.CertEnumCertificatesInStore(store, None)
        while context:
            try:
                found.append(_raw_from_context(crypt32, context))
            except Exception:
                # The enumeration won't be resumed, so this context is ours to free.
                crypt32.CertFreeCertificateContext(context)
                raise
            # Passing the previous context frees it and returns the next one.
            context = crypt32.CertEnumCertificatesInStore(store, context)
    finally:
        crypt32.CertCloseStore(store, 0)
    return found


# ─── parsing ─────────────────────────────────────────────────────────────────


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _to_store_cert(raw: _RawCert) -> StoreCert:
    try:
        certificate = x509.load_der_x509_certificate(raw.der)
    except ValueError as exc:
        raise WinCertError("A certificate in the store could not be parsed.") from exc
    provider = (raw.provider_name or "").strip().lower()
    return StoreCert(
        sha1_hex=hashlib.sha1(raw.der).hexdigest().upper(),  # noqa: S324 — an identifier
        x5t_s256=_b64url(hashlib.sha256(raw.der).digest()),
        der=raw.der,
        subject=certificate.subject.rfc4514_string(),
        friendly_name=raw.friendly_name,
        not_before=certificate.not_valid_before_utc,
        not_after=certificate.not_valid_after_utc,
        has_private_key=raw.provider_name is not None,
        ksp=_KSP_BY_PROVIDER.get(provider, KSP_UNKNOWN),
    )


# ─── public API ──────────────────────────────────────────────────────────────


def find_cert(sha1_hex: str) -> StoreCert | None:
    """The certificate with this SHA-1 thumbprint in ``CurrentUser\\My``, or None."""
    raw = _read_store_cert(_normalize_sha1(sha1_hex))
    return None if raw is None else _to_store_cert(raw)


def list_certs(*, friendly_name: str | None = None) -> list[StoreCert]:
    """Certificates in ``CurrentUser\\My``, optionally only those with this friendly name.

    Certificates that can't be parsed are skipped: they are not ours.
    """
    certs: list[StoreCert] = []
    for raw in _enum_store_certs():
        if friendly_name is not None and raw.friendly_name != friendly_name:
            continue
        try:
            certs.append(_to_store_cert(raw))
        except WinCertError:
            continue
    return certs


def test_sign(sha1_hex: str) -> None:
    """Prove the certificate's private key signs on this machine, as this user.

    Signs a random digest through the same CNG path the runtime uses for Hop 1 and
    verifies the signature against the certificate's public key.

    Raises:
        WinCertError: the certificate is missing, or its key can't sign here.
    """
    from entrabot.auth import cncrypt_signer

    sha1 = _normalize_sha1(sha1_hex)
    cert = find_cert(sha1)
    if cert is None:
        raise WinCertError(
            "The certificate is not in this user's certificate store. "
            f"Run {SETUP_COMMAND} to create one."
        )
    digest = hashlib.sha256(os.urandom(32)).digest()
    try:
        signature = cncrypt_signer.sign_pkcs1_sha256(thumbprint=sha1, hash_bytes=digest)
    except cncrypt_signer.SigningError as exc:
        raise WinCertError(f"The certificate's private key could not sign: {exc}") from exc

    public_key = x509.load_der_x509_certificate(cert.der).public_key()
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise WinCertError("The certificate does not carry an RSA public key.")
    try:
        public_key.verify(signature, digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))
    except InvalidSignature as exc:
        raise WinCertError(
            "The private key in the key store does not match the certificate. "
            f"Run {SETUP_COMMAND} to create a new one."
        ) from exc


# pytest would otherwise collect this as a test wherever it is imported by name.
test_sign.__test__ = False  # type: ignore[attr-defined]


# Status codes that mean "there is no key behind this certificate" (NTE_BAD_KEYSET,
# NTE_NO_KEY, NTE_KEYSET_NOT_DEF, CRYPT_E_NO_KEY_PROPERTY), as opposed to "the key is
# there and something stopped us".
_KEY_ABSENT_CODES = frozenset({0x80090016, 0x8009000D, 0x80090019, 0x8009200B})


def _key_is_absent(sha1: str) -> bool:
    """True when the certificate's private key demonstrably no longer exists.

    Decided from the key store's own status code, by asking it to sign, rather than
    from PowerShell's (localised) error text. Anything other than a clear "no such
    key" counts as "the key may still be there".
    """
    from entrabot.auth import cncrypt_signer

    try:
        cncrypt_signer.sign_pkcs1_sha256(
            thumbprint=sha1, hash_bytes=hashlib.sha256(b"entrabot-key-probe").digest()
        )
    except cncrypt_signer.SigningError as exc:
        return exc.code in _KEY_ABSENT_CODES
    except Exception:  # noqa: BLE001 — unknown means "don't claim the key is gone"
        return False
    return False


def _key_location(raw: _RawCert, sha1: str) -> str:
    """Where the private key lives, for a message: provider and key container name.

    The container name is left out if it would show the full thumbprint.
    """
    provider = raw.provider_name or "its key storage provider"
    container = (raw.key_container or "").strip()
    if container and sha1.lower() not in container.lower():
        return f"the {provider}, key container {container!r}"
    return f"the {provider}"


def delete_cert(sha1_hex: str, *, delete_key: bool = True) -> bool:
    """Remove the certificate and, by default, its private key, together.

    The key is deleted with the certificate (``Remove-Item -DeleteKey``). If the key
    can't be deleted, the certificate is LEFT in the store: a retry can still delete
    both, and the key stays identifiable through it. The certificate is never removed
    on its own after a failed key deletion; a caller that wants exactly that asks for
    it with ``delete_key=False``.

    Returns:
        ``False`` when there was no such certificate (nothing runs). ``True`` only
        when the certificate is gone and, with ``delete_key``, so is its private key.

    Raises:
        WinCertError: the certificate and its key are both still on this computer
            because the key could not be deleted; or the key no longer exists, -DeleteKey
            failed because of that, and the certificate is still in the store; or the
            certificate is gone but its key remains; or the certificate could not be
            removed. The message says which, and where the key is.
    """
    sha1 = _normalize_sha1(sha1_hex)
    raw = _read_store_cert(sha1)
    if raw is None:
        return False

    variables = {_ENV_SHA1: sha1}
    masked = mask_thumbprint(sha1)
    # A certificate with no private-key property has nothing for -DeleteKey to delete.
    with_key = delete_key and raw.provider_name is not None
    result = _run_powershell(_DELETE_WITH_KEY_SCRIPT if with_key else _DELETE_SCRIPT, variables)

    if result.returncode != 0 and with_key:
        key_error = _error_text(result.stderr)
        where = _key_location(raw, sha1)
        if _read_store_cert(sha1) is None:
            # Not expected with -DeleteKey (the key goes first), but if it happens the
            # key is orphaned: nothing in the store points at it any more.
            raise WinCertError(
                f"Certificate {masked} is no longer in the certificate store, but its "
                f"private key remains in {where}: it could not be deleted ({key_error}). "
                f'Find it with: certutil -user -csp "{raw.provider_name}" -key'
            )
        if _key_is_absent(sha1):
            raise WinCertError(
                f"Certificate {masked} is still in the certificate store: its private key "
                f"no longer exists, and -DeleteKey failed because of that ({key_error}). "
                "Remove the certificate on its own (delete_cert with delete_key=False), "
                "or delete it in certmgr.msc > Personal > Certificates."
            )
        raise WinCertError(
            f"Certificate {masked} and its private key are still on this computer: the "
            f"private key in {where} could not be deleted ({key_error}), so the "
            "certificate was left in the certificate store to keep the key identifiable. "
            "Try again, or delete it in certmgr.msc > Personal > Certificates."
        )

    if result.returncode != 0 or _read_store_cert(sha1) is not None:
        raise WinCertError(
            f"Could not remove certificate {masked} from the certificate "
            f"store: {_error_text(result.stderr)}"
        )
    return True


def create_signing_cert_with_ksp(
    *, subject: str, friendly_name: str, days: int = 365, ksp: str
) -> StoreCert:
    """Create a signing certificate on exactly this provider, with no fallback.

    The key is tested before this returns; if it can't sign, the certificate and
    its key are deleted again.
    """
    if ksp not in _PROVIDER_BY_KSP:
        raise WinCertError(f"ksp must be one of {sorted(_PROVIDER_BY_KSP)}.")
    if days < 1:
        raise WinCertError("The certificate lifetime must be at least one day.")
    if not subject.strip() or not friendly_name.strip():
        raise WinCertError("The certificate needs a subject and a friendly name.")

    not_after = (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = _run_powershell(
        _CREATE_SCRIPT,
        {
            _ENV_SUBJECT: subject,
            _ENV_FRIENDLY_NAME: friendly_name,
            _ENV_PROVIDER: _PROVIDER_BY_KSP[ksp],
            _ENV_NOT_AFTER: not_after,
        },
    )
    if result.returncode != 0:
        raise WinCertError(
            f"New-SelfSignedCertificate failed on the {ksp} key store: {_error_text(result.stderr)}"
        )

    sha1 = result.stdout.strip()
    # Exactly one line of exactly 40 hex characters; anything else means the output
    # was corrupted and must not be trusted as an identifier (Learning #29).
    if not re.fullmatch(r"[0-9A-F]{40}", sha1):
        raise WinCertError(
            "New-SelfSignedCertificate did not return a certificate thumbprint (unexpected output)."
        )

    try:
        raw = _read_store_cert(sha1)
        if raw is None:
            raise WinCertError("The new certificate is not in the certificate store.")
        cert = _to_store_cert(raw)
        test_sign(sha1)
    except WinCertError:
        _discard(sha1)
        raise
    # The store's provider name is authoritative, but an unrecognised spelling
    # must not hide which provider this key was just created on.
    if cert.ksp == KSP_UNKNOWN:
        cert = replace(cert, ksp=ksp)
    return cert


def create_signing_cert(
    *, subject: str, friendly_name: str, days: int = 365, prefer_tpm: bool = True
) -> tuple[StoreCert, str | None]:
    """Create a non-exportable signing certificate, in the TPM when that works.

    Returns the certificate and the reason the TPM was not used, or ``None`` when
    the key is in the TPM.

    The TPM is tried by doing: the key is created on the Platform Crypto Provider
    and signs once. If either step fails, what was created is deleted and the key
    is created on the software provider instead.
    """
    if not prefer_tpm:
        cert = create_signing_cert_with_ksp(
            subject=subject, friendly_name=friendly_name, days=days, ksp=KSP_SOFTWARE
        )
        return cert, "a software-protected key was requested"

    try:
        cert = create_signing_cert_with_ksp(
            subject=subject, friendly_name=friendly_name, days=days, ksp=KSP_TPM
        )
    except WinCertError as exc:
        reason = _tpm_fallback_reason(str(exc))
        logger.info("TPM key not used: %s", reason)
    else:
        return cert, None

    cert = create_signing_cert_with_ksp(
        subject=subject, friendly_name=friendly_name, days=days, ksp=KSP_SOFTWARE
    )
    return cert, reason


def _discard(sha1: str) -> None:
    """Best-effort removal of a certificate that turned out to be unusable."""
    try:
        delete_cert(sha1, delete_key=True)
    except WinCertError as exc:
        logger.warning("Could not clean up an unusable certificate: %s", exc)


_TPM_REASONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("0x80090030", "not ready for use", "device that is required"),
        "this machine has no TPM that is ready to use",
    ),
    (
        ("0x80090029", "not supported"),
        "the TPM does not support this kind of key",
    ),
    (
        ("0x80280921", "lockout", "0x80280400"),
        "the TPM is locked out or blocked by policy",
    ),
    (
        ("0x8009001d", "provider dll failed", "0x80090016", "keyset does not exist"),
        "the TPM key storage provider is not available to this user",
    ),
)


def _tpm_fallback_reason(detail: str) -> str:
    lowered = detail.lower()
    for needles, reason in _TPM_REASONS:
        if any(needle in lowered for needle in needles):
            return reason
    return f"the TPM could not create or use the key ({detail[:200]})"


def cert_health(sha1_hex: str | None, *, now: datetime | None = None) -> CertHealth:
    """Is the configured certificate present, in date, and backed by a key?

    Local only: one certificate-store read, no network, no signing.
    """
    if not sha1_hex:
        return CertHealth(
            status="missing",
            days_left=None,
            not_after=None,
            message=(
                "No Blueprint certificate is configured (ENTRABOT_BLUEPRINT_CERT_SHA1 is "
                f"not set). Run {SETUP_COMMAND}."
            ),
        )
    cert = find_cert(sha1_hex)
    if cert is None:
        return CertHealth(
            status="missing",
            days_left=None,
            not_after=None,
            message=(
                "The Blueprint certificate named in .env is not in this user's certificate "
                "store. It was created by a different Windows account or has been removed. "
                f"Run {SETUP_COMMAND} as the user who runs Entrabot."
            ),
        )

    now = now or datetime.now(UTC)
    days_left = (cert.not_after - now).days
    expires = cert.not_after.date().isoformat()
    if not cert.has_private_key:
        status, message = (
            "no_private_key",
            "The Blueprint certificate is in the certificate store but has no private key "
            f"on this machine. Run {SETUP_COMMAND} to create a new one.",
        )
    elif now > cert.not_after:
        status, message = (
            "expired",
            f"The Blueprint certificate expired on {expires}. "
            f"Run {SETUP_COMMAND} to create and register a new one.",
        )
    elif now < cert.not_before:
        status, message = (
            "not_yet_valid",
            "The Blueprint certificate is not valid yet "
            f"(valid from {cert.not_before.date().isoformat()}). Check this machine's clock.",
        )
    elif days_left <= EXPIRY_WARNING_DAYS:
        status, message = (
            "expiring",
            f"The Blueprint certificate expires on {expires} ({days_left} day(s) left). "
            f"Run {SETUP_COMMAND} to renew it.",
        )
    else:
        status, message = (
            "ok",
            f"The Blueprint certificate is valid until {expires} ({cert.ksp} key store).",
        )
    return CertHealth(status=status, days_left=days_left, not_after=cert.not_after, message=message)


def days_until_expiry(sha1_hex: str, *, now: datetime | None = None) -> int | None:
    """Whole days until the certificate expires (negative once it has), or None if missing."""
    cert = find_cert(sha1_hex)
    if cert is None:
        return None
    return (cert.not_after - (now or datetime.now(UTC))).days


__all__ = [
    "EXPIRY_WARNING_DAYS",
    "KSP_SOFTWARE",
    "KSP_TPM",
    "KSP_UNKNOWN",
    "PROVIDER_SOFTWARE",
    "PROVIDER_TPM",
    "SETUP_COMMAND",
    "CertHealth",
    "StoreCert",
    "WinCertError",
    "cert_health",
    "create_signing_cert",
    "create_signing_cert_with_ksp",
    "days_until_expiry",
    "delete_cert",
    "find_cert",
    "list_certs",
    "mask_thumbprint",
    "test_sign",
]
