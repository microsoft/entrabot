"""The Blueprint certificate step: reuse this machine's certificate, register it, or make one.

The agent proves it is the Blueprint by signing with a private key that never leaves this
machine. On Windows that is the TPM where there is one, otherwise the Windows software key
store (``entrabot.platform.wincert``); on macOS and Linux it is the account's Keychain or
keyring, one item per certificate (``entrabot.platform.keycert``). The two stores have the
same functions, so everything below is the same on every system. Entra only accepts the
signature if the matching public certificate is registered on the Blueprint. So this step
ends in exactly one of three states:

* a certificate that is in the store, in date, signs, and is on the Blueprint;
* *unconfirmed*: the registration may have landed. The certificate is kept and recorded so
  the next run can check again;
* a :class:`CertStepError`, with nothing new left behind.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from entrabot.winsetup.console import Console
from entrabot.winsetup.helpers import RESULT_MARKER, Helpers, marked_line

_SHA1 = re.compile(r"^[0-9A-Fa-f]{40}$")
EXPIRY_WARNING_DAYS = 30

VERIFY_REGISTERED, VERIFY_NOT_REGISTERED = 0, 1
REGISTER_OK, REGISTER_UNCONFIRMED = 0, 3

# Where the private key lives, by the store's name for it, in words a non-specialist can read.
_PROTECTION = {
    "tpm": "hardware TPM (the key cannot leave this computer)",
    "software": "Windows software key store (not exportable)",
    "keychain": "macOS Keychain (this account's)",
    "keyring": "Linux keyring (this account's)",
}

# macOS and Linux, on a computer set up before the wizard: that setup kept the Blueprint's
# private key on its own, one for the whole account. Is the certificate that goes with it on
# this Blueprint? The key never leaves the wizard: only its PUBLIC half goes to this child,
# which reads the Blueprint's certificates with the Provisioner's token and hands back the
# one certificate made for that key. Exit 0 found, 1 none on the Blueprint, 2 could not check.
# argv: scripts folder, Blueprint object ID, the public key (SubjectPublicKeyInfo DER) as hex.
OLDER_KEY_SNIPPET = f"""
# Entrabot: find the Blueprint certificate that belongs to this computer's older key
import base64, contextlib, json, sys
from datetime import UTC, datetime
sys.path.insert(0, sys.argv[1])
MARK = {RESULT_MARKER!r}
try:
    import blueprint_certs
    from entra_provisioning import get_existing_graph_token
    with contextlib.redirect_stdout(sys.stderr):  # the token helper prints progress
        token = get_existing_graph_token()
    credentials = blueprint_certs.fetch_key_credentials(sys.argv[2], token)
except (SystemExit, Exception) as exc:
    print(f"could not read the Blueprint's certificates ({{exc}})", file=sys.stderr)
    sys.exit(2)
now = datetime.now(UTC)
match = blueprint_certs.find_credential_for_public_key(
    credentials, bytes.fromhex(sys.argv[3]), now=now
)
if match is not None and match.der is not None:
    print(MARK + json.dumps(dict(der=base64.b64encode(match.der).decode())))
    sys.exit(0)
unreadable = [
    item for item in blueprint_certs.unreadable_certificates(credentials) if item.is_usable_at(now)
]
if unreadable:
    print(f"{{len(unreadable)}} certificate(s) on the Blueprint could not be read", file=sys.stderr)
    sys.exit(2)
sys.exit(1)
"""


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
        return protection_label(self.ksp)


def protection_label(ksp: str) -> str:
    return _PROTECTION.get(ksp, "Windows key store")


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


def load_cert_store(platform: str) -> Any:
    """This system's certificate store: ``wincert`` on Windows, ``keycert`` (the Keychain or
    keyring) on macOS and Linux. They have the same functions. Imported late, for the same
    reasons as :func:`load_wincert`."""
    if platform == "win32":
        return load_wincert()
    try:
        from entrabot.platform import keycert
    except ImportError as exc:
        raise CertStepError(
            "This copy of Entrabot is missing its key store support "
            f"({exc}). Update the project folder and run setup again."
        ) from exc
    return keycert


def _adopt_older_key(
    wincert: Any,
    *,
    blueprint_object_id: str,
    blueprint_app_id: str,
    helpers: Helpers,
    console: Console,
) -> Any:
    """macOS and Linux: a computer set up before the wizard has its Blueprint key in the
    account's single key store slot, and its certificate only in Microsoft Entra. When that
    key's certificate is on THIS Blueprint, the two are stored together as this agent's own
    item, so nothing new is created or registered and the agent keeps signing in as it did.

    Returns the certificate, or None when there is no older key or it is not this
    Blueprint's (another agent's key is never touched). The older slot is left as it is.
    """
    read_key = getattr(wincert, "legacy_private_key_pem", None)
    if read_key is None:
        return None  # Windows: there never was such a slot
    try:
        key_pem = read_key()
        public_key = wincert.public_key_der(key_pem) if key_pem else None
    except wincert.WinCertError as exc:
        console.debug(f"The older key store slot was not used: {exc}")
        return None
    if not key_pem or public_key is None:
        return None
    found = helpers.snippet(
        OLDER_KEY_SNIPPET,
        [str(helpers.paths.scripts), blueprint_object_id, public_key.hex()],
        env=helpers.base_env,
    )
    if found.returncode == 1:
        return None  # that key is not this Blueprint's
    try:
        data = json.loads(marked_line(found.stdout, RESULT_MARKER) or "")
        der = base64.b64decode(data["der"], validate=True)
    except (ValueError, KeyError, TypeError):
        der = b""
    if found.returncode != 0 or not der:
        raise CertStepError(
            "This computer has a sign-in key from an earlier setup, and setup could not check "
            "whether it belongs to this agent "
            f"({found.stderr.strip()[-300:] or 'the check did not answer'}). "
            "Nothing was generated or changed."
        )
    try:
        cert = wincert.import_cert(
            private_key_pem=key_pem, der=der, friendly_name=f"Entrabot Blueprint {blueprint_app_id}"
        )
    except wincert.WinCertError as exc:
        raise CertStepError(
            f"Could not move this computer's existing sign-in key into its own item: {exc}"
        ) from exc
    console.ok(
        "Kept this computer's existing sign-in key, now stored as this agent's own item in the "
        f"{console.words.key_store}"
    )
    return cert


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
    replace_all: bool = False,
) -> CertOutcome:
    """Make sure this machine has a working certificate that is registered on the Blueprint.

    *replace_all*: a new certificate is created whatever this computer already has, and
    registering it REMOVES every other certificate on the Blueprint (other computers' too).
    The caller has asked the person; nothing here asks again.
    """
    if not blueprint_object_id:
        raise CertStepError(
            "The Blueprint's ID is missing from this machine's record, so a certificate "
            "can't be registered on it. Run setup again once the agent has been created."
        )

    cert = None
    replaced: Any = None  # this computer's earlier certificate, when every one is replaced
    recorded = env_values.get("ENTRABOT_BLUEPRINT_CERT_SHA1", "").strip()
    if _SHA1.match(recorded):
        try:
            cert = wincert.find_cert(recorded.upper())
        except wincert.WinCertError as exc:
            raise CertStepError(f"Could not read this computer's certificate store: {exc}") from exc
    if cert is None and not replace_all:
        # No certificate of this agent's in the store: a computer set up before the wizard,
        # or settings copied from one. Its key may be in the account's older single slot.
        cert = _adopt_older_key(
            wincert,
            blueprint_object_id=blueprint_object_id,
            blueprint_app_id=blueprint_app_id,
            helpers=helpers,
            console=console,
        )
    if replace_all:
        replaced, cert = cert, None
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
        if cert.ksp not in ("tpm", "software", "unknown"):
            console.ok(
                f"Created a certificate whose key is kept in the {console.words.key_store}"
            )
        elif tpm_reason:
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
                [
                    "--blueprint-object-id",
                    blueprint_object_id,
                    "--der",
                    str(der_path),
                    *(["--remove-all-existing-certs"] if replace_all else []),
                ],
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
    elif replace_all:
        console.ok("Certificate registered on the Blueprint. It is now the only one there.")
        _delete_replaced(wincert, replaced, keep=cert, console=console)
    elif needs_registering:
        console.ok("Certificate registered on the Blueprint")
    else:
        console.ok(f"This computer's certificate is registered and working ({outcome.ksp})")
    if outcome.days_left <= EXPIRY_WARNING_DAYS:
        console.warn(
            f"The certificate expires in {outcome.days_left} days "
            f"({outcome.not_after:%d %b %Y}). Rotate it soon with {_rotate_command(console)}."
        )
    return outcome


def _rotate_command(console: Console) -> str:
    # Windows names the script alone here, as it always has; the full command is in the docs.
    return r"scripts\deploy-windows.ps1" if console.words.windows else console.words.rotate


def _delete_replaced(wincert: Any, replaced: Any, *, keep: Any, console: Console) -> None:
    """After every certificate on the Blueprint was replaced: this computer's earlier one is
    no longer registered anywhere, so it goes from this computer too, with its key."""
    if replaced is None or replaced.sha1_hex == keep.sha1_hex:
        return
    try:
        wincert.delete_cert(replaced.sha1_hex, delete_key=True)
    except wincert.WinCertError as exc:
        console.warn(
            f"This computer's earlier certificate could not be deleted from the "
            f"{console.words.key_store}: {exc}"
        )
        return
    console.detail("This computer's earlier certificate and its key were deleted.")
