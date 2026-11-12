"""The Blueprint certificate step: reuse this machine's certificate, register it, or make one.

The agent proves it is the Blueprint by signing with a private key that never leaves this
machine (the TPM where there is one, otherwise the Windows software key store). Entra only
accepts that signature if the matching public certificate is registered on the Blueprint.
So this step ends in exactly one of three states:

* a certificate that is in the store, in date, signs, and is on the Blueprint;
* *unconfirmed*: the registration may have landed. The certificate is kept and recorded so
  the next run can check again;
* a :class:`CertStepError`, with nothing new left behind.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from entrabot.winsetup.console import Console
from entrabot.winsetup.helpers import Helpers

_SHA1 = re.compile(r"^[0-9A-Fa-f]{40}$")
EXPIRY_WARNING_DAYS = 30

VERIFY_REGISTERED, VERIFY_NOT_REGISTERED = 0, 1
REGISTER_OK, REGISTER_UNCONFIRMED = 0, 3


class CertStepError(Exception):
    """The certificate step failed and left nothing new behind."""


@dataclass(frozen=True)
class CertOutcome:
    sha1_hex: str
    x5t_s256: str
    ksp: str  # "tpm" | "software" | "unknown"
    not_after: datetime
    days_left: int
    generated: bool
    unconfirmed: bool
    tpm_fallback_reason: str | None
    # This run added the certificate to the Blueprint: a new one, or one of this computer's
    # that was not on it yet. Microsoft Entra may take a while to accept it for sign-in.
    registered: bool = False

    @property
    def protection(self) -> str:
        """Where the private key lives, in words a non-specialist can read."""
        if self.ksp == "tpm":
            return "hardware TPM (the key cannot leave this computer)"
        if self.ksp == "software":
            return "Windows software key store (not exportable)"
        return "Windows key store"


def load_wincert() -> Any:
    """The Windows certificate primitives. Imported late so tests can pass a fake, and so the
    rest of the wizard imports on any OS."""
    try:
        from entrabot.platform import wincert
    except ImportError as exc:
        raise CertStepError(
            "This copy of Entrabot is missing its Windows certificate support "
            f"({exc}). Update the project folder and run setup again."
        ) from exc
    return wincert


def _usable(cert: Any, wincert: Any, now: datetime) -> str | None:
    """Why *cert* can't be used, or None when it can sign right now."""
    if not cert.has_private_key:
        return "its private key is not on this computer"
    if now < cert.not_before:
        return "it is not valid yet (check this computer's clock)"
    if now > cert.not_after:
        return "it has expired"
    try:
        wincert.test_sign(cert.sha1_hex)
    except wincert.WinCertError as exc:
        return f"its private key can't sign ({exc})"
    return None


def _outcome(
    cert: Any,
    now: datetime,
    *,
    generated: bool,
    unconfirmed: bool,
    tpm_reason: str | None,
    registered: bool,
) -> CertOutcome:
    return CertOutcome(
        sha1_hex=cert.sha1_hex,
        x5t_s256=cert.x5t_s256,
        ksp=cert.ksp,
        not_after=cert.not_after,
        days_left=max(0, (cert.not_after - now).days),
        generated=generated,
        unconfirmed=unconfirmed,
        tpm_fallback_reason=tpm_reason,
        registered=registered,
    )


def ensure_blueprint_cert(
    *,
    env_values: Mapping[str, str],
    blueprint_object_id: str,
    blueprint_app_id: str,
    helpers: Helpers,
    console: Console,
    wincert: Any,
    now: datetime,
) -> CertOutcome:
    """Make sure this machine has a working certificate that is registered on the Blueprint."""
    if not blueprint_object_id:
        raise CertStepError(
            "The Blueprint's ID is missing from this machine's record, so a certificate "
            "can't be registered on it. Run setup again once the agent has been created."
        )

    cert = None
    recorded = env_values.get("ENTRABOT_BLUEPRINT_CERT_SHA1", "").strip()
    if _SHA1.match(recorded):
        try:
            cert = wincert.find_cert(recorded.upper())
        except wincert.WinCertError as exc:
            raise CertStepError(f"Could not read this computer's certificate store: {exc}") from exc
    if cert is not None:
        problem = _usable(cert, wincert, now)
        if problem:
            console.warn(f"This computer's certificate can't be used: {problem}.")
            console.detail("Creating a new one.")
            cert = None

    generated = False
    tpm_reason: str | None = None
    needs_registering = True

    if cert is not None:
        # The store's own SHA-256 thumbprint is the truth; .env may hold a stale one.
        verify = helpers.script("verify_blueprint_cert.py", [blueprint_object_id, cert.x5t_s256])
        if verify.returncode == VERIFY_REGISTERED:
            needs_registering = False
        elif verify.returncode == VERIFY_NOT_REGISTERED:
            console.warn("This computer's certificate is not on the Blueprint yet. Registering it.")
        else:
            raise CertStepError(
                "Could not check whether this computer's certificate is registered "
                f"(exit {verify.returncode}, see above). Nothing was generated or changed."
            )
    else:
        try:
            cert, tpm_reason = wincert.create_signing_cert(
                subject=f"CN=Entrabot Blueprint {blueprint_app_id[:8]}",
                friendly_name=f"Entrabot Blueprint {blueprint_app_id}",
                days=365,
                prefer_tpm=True,
            )
        except wincert.WinCertError as exc:
            raise CertStepError(f"Could not create a certificate on this computer: {exc}") from exc
        generated = True
        if tpm_reason:
            console.info("Created a certificate in the Windows software key store.")
            console.detail(f"The hardware TPM was not used: {tpm_reason}")
        else:
            console.ok("Created a certificate whose key is held by this computer's TPM")

    unconfirmed = False
    if needs_registering:
        work_dir = Path(tempfile.mkdtemp(prefix="entrabot-cert-"))
        try:
            der_path = work_dir / "blueprint.cer"
            der_path.write_bytes(cert.der)
            register = helpers.script(
                "register_windows_blueprint_cert.py",
                ["--blueprint-object-id", blueprint_object_id, "--der", str(der_path)],
                heartbeat="Waiting for Microsoft Entra to confirm the certificate",
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        if register.returncode == REGISTER_UNCONFIRMED:
            unconfirmed = True
        elif register.returncode != REGISTER_OK:
            if generated:
                # Nothing was written to the Blueprint, so this certificate is useless. Remove
                # it together with its private key, or the key stays behind in the key store.
                try:
                    wincert.delete_cert(cert.sha1_hex, delete_key=True)
                except wincert.WinCertError as exc:
                    console.warn(f"Could not remove the unused certificate: {exc}")
            raise CertStepError(
                "The certificate was not registered on the Blueprint "
                f"(exit {register.returncode}, see above). Nothing was changed."
            )

    outcome = _outcome(
        cert,
        now,
        generated=generated,
        unconfirmed=unconfirmed,
        tpm_reason=tpm_reason,
        registered=needs_registering,
    )
    if unconfirmed:
        console.warn("Microsoft Entra has not confirmed the certificate yet.")
    elif needs_registering:
        console.ok("Certificate registered on the Blueprint")
    else:
        console.ok(f"This computer's certificate is registered and working ({outcome.ksp})")
    if outcome.days_left <= EXPIRY_WARNING_DAYS:
        console.warn(
            f"The certificate expires in {outcome.days_left} days "
            f"({outcome.not_after:%d %b %Y}). Rotate it soon with scripts\\deploy-windows.ps1."
        )
    return outcome
