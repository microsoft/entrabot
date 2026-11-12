"""Generate a signing certificate for the Blueprint on Windows.

A thin command line over ``entrabot.platform.wincert``, which owns the key
lifecycle: the key is created in the TPM when that works and in the software key
store otherwise, is always non-exportable, and has signed once before this script
reports success. It needs neither a newer PowerShell nor elevation, and it does not
probe for a TPM: it tries one.

Usage:
    python scripts/generate_windows_cert.py [--subject CN=...] [--days N]
        [--ksp {tpm,software}] [--friendly-name NAME] [--export-der PATH]

``--ksp`` pins the key store and disables the fallback: ``--ksp tpm`` fails if the
TPM can't be used.

Standard output is machine-readable and is all that callers parse:

    thumbprint=<40 uppercase hex, identifies the cert in Cert:\\CurrentUser\\My>
    ksp=<tpm|software>
    x5t_s256=<43-character base64url SHA-256 of the DER>

Everything meant for a person goes to stderr.

Exit codes: 0 created, 1 failed (nothing usable was left behind), 2 usage.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

from entrabot.platform import wincert

KSP_TPM = wincert.KSP_TPM
KSP_SOFTWARE = wincert.KSP_SOFTWARE
VALID_KSPS = {KSP_TPM, KSP_SOFTWARE}

DEFAULT_SUBJECT = "CN=entrabot-blueprint"
DEFAULT_FRIENDLY_NAME = "Entrabot Blueprint"


class GenerateCertError(RuntimeError):
    """The certificate could not be created or read back."""


@dataclass(frozen=True)
class GenerateResult:
    thumbprint: str  # SHA-1 hex, identifies cert in Cert:\CurrentUser\My
    ksp: str  # "tpm" or "software"
    der_path: Path | None = None  # public cert DER, if exported


def generate(
    *,
    subject: str,
    days_valid: int,
    ksp: str,
    friendly_name: str = DEFAULT_FRIENDLY_NAME,
) -> GenerateResult:
    """Create a certificate on exactly this key store. No fallback.

    Raises:
        ValueError: ``ksp`` is not ``"tpm"`` or ``"software"``.
        GenerateCertError: the certificate could not be created or its key can't sign.
    """
    if ksp not in VALID_KSPS:
        raise ValueError(f"ksp must be one of {sorted(VALID_KSPS)}, got: {ksp!r}")
    try:
        cert = wincert.create_signing_cert_with_ksp(
            subject=subject, friendly_name=friendly_name, days=days_valid, ksp=ksp
        )
    except wincert.WinCertError as exc:
        raise GenerateCertError(str(exc)) from exc
    return GenerateResult(thumbprint=cert.sha1_hex, ksp=cert.ksp)


def export_der(thumbprint: str, dest: Path) -> Path:
    """Write the certificate's public DER, read in-process from the store."""
    try:
        cert = wincert.find_cert(thumbprint)
    except wincert.WinCertError as exc:
        raise GenerateCertError(str(exc)) from exc
    if cert is None:
        raise GenerateCertError("The certificate is not in this user's certificate store.")
    dest.write_bytes(cert.der)
    return dest


def compute_x5t_s256(der_bytes: bytes) -> str:
    """Compute the JWT ``x5t#S256`` value from raw DER bytes."""
    digest = hashlib.sha256(der_bytes).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a non-exportable signing certificate in Cert:\\CurrentUser\\My."
    )
    parser.add_argument("--subject", default=DEFAULT_SUBJECT)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument(
        "--ksp",
        choices=sorted(VALID_KSPS),
        default=None,
        help="Pin the key store. If omitted, the TPM is tried first, then software.",
    )
    parser.add_argument("--friendly-name", default=DEFAULT_FRIENDLY_NAME)
    parser.add_argument("--export-der", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        if args.ksp is None:
            cert, reason = wincert.create_signing_cert(
                subject=args.subject,
                friendly_name=args.friendly_name,
                days=args.days,
                prefer_tpm=True,
            )
            if reason:
                print(
                    f"  TPM not used: {reason}. The key is in the software key store.",
                    file=sys.stderr,
                )
        else:
            cert = wincert.create_signing_cert_with_ksp(
                subject=args.subject,
                friendly_name=args.friendly_name,
                days=args.days,
                ksp=args.ksp,
            )
        if args.export_der is not None:
            args.export_der.write_bytes(cert.der)
    except (wincert.WinCertError, OSError) as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"thumbprint={cert.sha1_hex}")
    print(f"ksp={cert.ksp}")
    print(f"x5t_s256={cert.x5t_s256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
