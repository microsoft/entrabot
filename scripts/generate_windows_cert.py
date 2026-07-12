"""Generate the Blueprint cert on Windows.

Wraps ``New-SelfSignedCertificate`` with hard-locked crypto parameters.
Auto-detects TPM availability and falls
back to the software KSP. Returns the SHA-1 thumbprint, the SHA-256
b64url JWT thumbprint, and the public DER bytes ready for upload to
the Blueprint app.

Why a Python helper around PowerShell, instead of inlining in
``setup-windows.ps1``? Three reasons:

1. The cert generation is the one Windows-only flow that pytest can
   meaningfully validate (we mock subprocess and assert that the
   crypto flags land verbatim).
2. The thumbprint extraction needs strict validation (regex) — the
   ``setup.sh`` Mac path was bitten by stdout corruption (Learning
   #29); same defense lives here.
3. ``rotate_cert_windows.py`` reuses this helper.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import subprocess  # noqa: S404 — subprocess is the whole point
import sys
from dataclasses import dataclass
from pathlib import Path

THUMBPRINT_RE = re.compile(r"^[A-F0-9]{40}$")

KSP_TPM = "tpm"
KSP_SOFTWARE = "software"
VALID_KSPS = {KSP_TPM, KSP_SOFTWARE}

PROVIDER_TPM = "Microsoft Platform Crypto Provider"
PROVIDER_SOFTWARE = "Microsoft Software Key Storage Provider"


class GenerateCertError(RuntimeError):
    pass


class PowerShellError(GenerateCertError):
    pass


class ThumbprintValidationError(GenerateCertError):
    pass


@dataclass(frozen=True)
class GenerateResult:
    thumbprint: str  # SHA-1 hex, identifies cert in Cert:\CurrentUser\My
    ksp: str  # "tpm" or "software"
    der_path: Path | None = None  # public cert DER, if exported


def _run_pwsh(script: str) -> subprocess.CompletedProcess:
    """Invoke pwsh -NoProfile -Command <script>. Returns the result."""
    return subprocess.run(  # noqa: S603 — fixed argv, controlled inputs
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )


def probe_tpm_ready() -> bool:
    """True iff ``Get-Tpm`` reports the TPM is present and ready.

    Falls back to ``False`` on any error — the caller picks the software
    KSP and logs the fallback reason.
    """
    result = _run_pwsh("(Get-Tpm).TpmReady")
    if result.returncode != 0:
        return False
    return result.stdout.strip().lower() == "true"


def _build_command(*, subject: str, days_valid: int, ksp: str) -> list[str]:
    provider = PROVIDER_TPM if ksp == KSP_TPM else PROVIDER_SOFTWARE
    # NonExportable is the meaningful guarantee for the TPM path; harmless
    # to set on the software path too — DPAPI binds the private key to the
    # user profile either way. We add it on TPM because the plan's threat
    # model assumes the TPM key is the strongest baseline.
    extra = "-KeyExportPolicy NonExportable " if ksp == KSP_TPM else ""
    pwsh_block = (
        "$ErrorActionPreference = 'Stop'; "
        f"$cert = New-SelfSignedCertificate -Subject '{subject}' "
        f"-CertStoreLocation Cert:\\CurrentUser\\My "
        f"-Provider '{provider}' "
        f"{extra}"
        # ── HARD-LOCKED CRYPTO PARAMS (D9) ────────────────────────────
        "-KeyAlgorithm RSA "
        "-KeyLength 2048 "
        "-HashAlgorithm SHA256 "
        "-KeyUsage DigitalSignature "
        "-KeyUsageProperty Sign "
        # ───────────────────────────────────────────────────────────────
        f"-NotAfter (Get-Date).AddDays({days_valid}); "
        "Write-Output $cert.Thumbprint"
    )
    return ["pwsh", "-NoProfile", "-NonInteractive", "-Command", pwsh_block]


def generate(*, subject: str, days_valid: int, ksp: str) -> GenerateResult:
    """Generate a Blueprint cert in ``Cert:\\CurrentUser\\My``.

    Args:
        subject: ``Subject`` for ``New-SelfSignedCertificate`` (e.g.,
            ``"CN=entrabot-blueprint"``).
        days_valid: lifetime in days.
        ksp: ``"tpm"`` or ``"software"``. The caller decides; this
            function does not auto-fallback (use ``probe_tpm_ready``
            first).

    Returns:
        :class:`GenerateResult` with the SHA-1 thumbprint and the
        chosen KSP.
    """
    if ksp not in VALID_KSPS:
        raise ValueError(f"ksp must be one of {sorted(VALID_KSPS)}, got: {ksp!r}")

    cmd = _build_command(subject=subject, days_valid=days_valid, ksp=ksp)
    result = subprocess.run(  # noqa: S603 — fixed argv, validated args
        cmd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise PowerShellError(
            f"New-SelfSignedCertificate failed: {result.stderr.strip() or '(no stderr)'}"
        )

    raw = result.stdout.strip()
    if "\n" in raw or "\r" in raw:
        raise ThumbprintValidationError(
            f"thumbprint stdout contains line breaks (corruption suspected): {raw!r}"
        )
    if not THUMBPRINT_RE.match(raw):
        raise ThumbprintValidationError(f"thumbprint validation failed — not 40 hex chars: {raw!r}")

    return GenerateResult(thumbprint=raw, ksp=ksp)


def export_der(thumbprint: str, dest: Path) -> Path:
    """Export the public cert DER bytes for upload to the Blueprint app."""
    pwsh = (
        "$ErrorActionPreference = 'Stop'; "
        f"$c = Get-Item Cert:\\CurrentUser\\My\\{thumbprint}; "
        f"[IO.File]::WriteAllBytes('{dest}', $c.GetRawCertData())"
    )
    result = _run_pwsh(pwsh)
    if result.returncode != 0:
        raise PowerShellError(f"Cert export failed: {result.stderr.strip() or '(no stderr)'}")
    return dest


def compute_x5t_s256(der_bytes: bytes) -> str:
    """Compute the JWT ``x5t#S256`` value from raw DER bytes."""
    digest = hashlib.sha256(der_bytes).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", default="CN=entrabot-blueprint")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument(
        "--ksp",
        choices=sorted(VALID_KSPS),
        default=None,
        help="If omitted, auto-probes TPM and falls back to software.",
    )
    parser.add_argument("--export-der", type=Path, default=None)
    args = parser.parse_args(argv)

    ksp = args.ksp
    if ksp is None:
        ksp = KSP_TPM if probe_tpm_ready() else KSP_SOFTWARE
        print(f"TPM probe: chose KSP={ksp}", file=sys.stderr)

    result = generate(subject=args.subject, days_valid=args.days, ksp=ksp)
    print(f"thumbprint={result.thumbprint}")
    print(f"ksp={result.ksp}")

    if args.export_der is not None:
        export_der(result.thumbprint, args.export_der)
        der = args.export_der.read_bytes()
        print(f"x5t_s256={compute_x5t_s256(der)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
