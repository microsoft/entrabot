"""Microsoft Entra, as the maintenance commands (rotate-cert, teardown, status) need it.

Each of these runs in a child process with the virtual-environment interpreter, so that
``entrabot.config`` never loads into the command itself and a token never passes through it:

* take certificates off an application's list, keeping every other machine's, confirmed by
  reading the list back (``scripts/blueprint_certs.remove_key_credential``, or
  ``remove_key_credentials`` for several in one write);
* take old setup certificates an earlier setup could not remove off the Provisioner app
  (``scripts/entra_provisioning._revoke_app_certificate``, setup's own retry);
* prove that a certificate in this user's store really signs the agent in;
* find the Azure CLI and say who is signed in: the Provisioner token the certificate removals
  need is only issued to a signed-in administrator's setup, and setup's retry above works
  through that sign-in directly.

Thumbprints go to the children as arguments and never into program text. Nothing here
prints one: callers show at most the last four characters.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from entrabot.winsetup import azcli
from entrabot.winsetup.azcli import Account, AzCli
from entrabot.winsetup.console import Console
from entrabot.winsetup.helpers import RESULT_MARKER, Helpers, Paths, marked_line

# The Windows spelling, kept for code written against it: messages take the command from
# ``console.words.setup``, which is this system's.
SETUP_COMMAND = r"scripts\setup-windows.cmd"

RemovalStatus = Literal["gone", "unchanged", "unconfirmed", "no-access"]

# Exit codes of the two removal programs below.
_GONE, _UNCHANGED, _UNCONFIRMED, _NO_ACCESS = 0, 1, 3, 4

_REMOVAL_COMMON = f"""
import contextlib, json, sys
sys.path.insert(0, sys.argv[1])  # the repo's scripts folder
MARK = {RESULT_MARKER!r}

def done(code, **data):
    print(MARK + json.dumps(data))
    sys.exit(code)

try:
    import blueprint_certs
    from entra_provisioning import get_existing_graph_token, get_state
    with contextlib.redirect_stdout(sys.stderr):  # the token helper prints progress
        token = get_existing_graph_token()
except Exception as exc:
    done({_NO_ACCESS}, problem=f"could not get access to Microsoft Entra ({{exc}})")

def settle(write):
    # Runs one removal and returns its result. Anything else ends the program with what it
    # says about the list.
    try:
        return write()
    except blueprint_certs.KeyCredentialsReadError as exc:  # nothing was sent
        done({_UNCHANGED}, problem=str(exc))
    except blueprint_certs.RegistrationRejectedError as exc:  # refused: nothing changed
        done({_UNCHANGED}, problem=str(exc))
    except Exception as exc:  # the write may have landed, so don't claim otherwise
        done({_UNCONFIRMED}, problem=str(exc))

def remove(object_id, sha1, must_keep=()):
    result = settle(lambda: blueprint_certs.remove_key_credential(
        object_id=object_id, token=token, sha1_hex=sha1, must_keep=list(must_keep)
    ))
    done({_GONE}, removed=len(result.removed), kept=len(result.current))
"""

REMOVE_BLUEPRINT_CREDENTIAL_SNIPPET = (
    "# Entrabot: remove this computer's certificate from the Blueprint\n"
    + _REMOVAL_COMMON
    + """
remove(sys.argv[2], sys.argv[3], must_keep=sys.argv[4:])
"""
)

# Every certificate named, in ONE read, write and read-back: two removals in a row are two
# whole-list writes, and the second one's read can still show what the first removed, which
# its write would then put back. The result says, by position, which were not listed.
# argv: scripts folder, Blueprint object ID, then the SHA-1 thumbprints.
REMOVE_BLUEPRINT_CREDENTIALS_SNIPPET = (
    "# Entrabot: remove this computer's certificate from the Blueprint, "
    "with any rotation leftover, in one write\n"
    + _REMOVAL_COMMON
    + f"""
targets = sys.argv[3:]
result = settle(lambda: blueprint_certs.remove_key_credentials(
    object_id=sys.argv[2], token=token, thumbprints=targets
))
not_listed = [index for index, sha1 in enumerate(targets) if sha1 in result.not_listed]
done({_GONE}, removed=len(result.removed), kept=len(result.current), not_listed=not_listed)
"""
)

REMOVE_PROVISIONER_CREDENTIAL_SNIPPET = (
    "# Entrabot: remove this computer's setup certificate from the Provisioner app\n"
    + _REMOVAL_COMMON
    + f"""
import httpx
client_id = get_state("PROVISIONER_CLIENT_ID")
if not client_id:
    done({_UNCHANGED}, problem="this folder has no record of the Provisioner app")
try:
    response = httpx.get(
        "https://graph.microsoft.com/v1.0/applications(appId='" + client_id + "')?$select=id",
        headers={{"Authorization": "Bearer " + token}},
        timeout=15.0,
    )
except httpx.HTTPError as exc:
    done({_UNCHANGED}, problem=f"could not look up the Provisioner app ({{type(exc).__name__}})")
if response.status_code == 404:
    done({_GONE}, removed=0, kept=0)  # the app itself is gone, and its certificates with it
if not response.is_success:
    done({_UNCHANGED}, problem=f"could not look up the Provisioner app ({{response.status_code}})")
remove(response.json()["id"], sys.argv[2])
"""
)

# Setup's own retry of the old Provisioner certificates it could not take off the app when it
# moved an older install off its plaintext key file (entra_provisioning._retire_old_certificates
# calls the same function). It works through the Azure CLI sign-in, not the Provisioner token,
# and changes only the entries that hold the certificate (az ad app credential delete), never
# the rest of the app's list. No keep_sha1: setup names the certificate it is moving TO, so that
# a migration never removes what may be the app's only working certificate. Teardown moves to
# nothing: it retires this computer's setup certificate itself or keeps it, and never passes it
# here as an old one. The result says, by position, what happened to each.
# argv: scripts folder, Provisioner app (client) ID, then the SHA-1 thumbprints.
REVOKE_OLD_SETUP_CERTIFICATES_SNIPPET = f"""
# Entrabot: take old setup certificates an earlier setup could not remove off the Provisioner app
import contextlib, json, sys
sys.path.insert(0, sys.argv[1])  # the repo's scripts folder
client_id, targets = sys.argv[2], sys.argv[3:]
results = []
with contextlib.redirect_stdout(sys.stderr):  # stdout carries the result line only
    try:
        from entra_provisioning import ProvisionerBootstrapError, _revoke_app_certificate
    except Exception as exc:
        problem = f"could not load setup's provisioning helper ({{type(exc).__name__}}: {{exc}})"
        results = [dict(outcome="failed", problem=problem[:400]) for _ in targets]
    else:
        for sha1 in targets:
            try:
                results.append(dict(outcome=_revoke_app_certificate(client_id, sha1)))
            except ProvisionerBootstrapError as exc:
                results.append(dict(outcome="failed", problem=str(exc)[:400]))
            except Exception as exc:  # it may or may not have been removed: claim nothing
                problem = f"{{type(exc).__name__}}: {{exc}}"
                results.append(dict(outcome="failed", problem=problem[:400]))
print({RESULT_MARKER!r} + json.dumps(dict(results=results)))
"""

PROVE_CERTIFICATE_SNIPPET = f"""
# Entrabot: prove the new certificate signs the agent in (three-hop token)
import json, sys
from entrabot.config import EntraBotConfig
from entrabot.errors import AgentIDNotAvailable, TokenExchangeError
from entrabot.tools.teams import acquire_agent_user_token
MARK = {RESULT_MARKER!r}

def report(**data):
    print(MARK + json.dumps(data))

try:
    acquire_agent_user_token(EntraBotConfig.from_env())
except TokenExchangeError as exc:
    report(ok=False, retry=True, hop=exc.hop, error=exc.error, description=exc.description[:400])
    sys.exit(1)
except AgentIDNotAvailable as exc:
    report(ok=False, retry=False, hop="", error="not_available", description=str(exc)[:400])
    sys.exit(1)
except Exception as exc:
    report(ok=False, retry=True, hop="", error=type(exc).__name__, description=str(exc)[:400])
    sys.exit(1)
report(ok=True)
"""

_AADSTS = re.compile(r"AADSTS\d+")
_SHA1 = re.compile(r"^[0-9A-Fa-f]{40}$")


def is_sha1(value: str) -> bool:
    return bool(_SHA1.match(value))


def last4(sha1_hex: str) -> str:
    """The only part of a thumbprint that is ever shown: enough to tell two apart."""
    return sha1_hex[-4:].upper()


LocalDeletionStatus = Literal["deleted", "absent", "key-remains", "failed"]

_KEY_PROVIDERS = {
    "tpm": "Microsoft Platform Crypto Provider",
    "software": "Microsoft Software Key Storage Provider",
}


@dataclass(frozen=True)
class LocalDeletion:
    """What happened to a certificate, and its private key, in this user's store."""

    status: LocalDeletionStatus
    detail: str = ""
    ksp: str = "unknown"

    @property
    def clean(self) -> bool:
        """Neither the certificate nor its private key is on this computer any more."""
        return self.status in ("deleted", "absent")


def delete_local_cert(wincert: Any, sha1_hex: str) -> LocalDeletion:
    """Delete a certificate WITH its private key, and say truthfully what is left.

    ``wincert.delete_cert`` raises both when the certificate could not be removed and when
    the certificate went but its key could not be deleted. Those are different leftovers,
    so the store is looked at again to tell them apart: "key-remains" means the certificate
    is gone and its private key is still in the Windows key store.
    """
    try:
        before = wincert.find_cert(sha1_hex)
    except wincert.WinCertError as exc:
        return LocalDeletion("failed", str(exc))
    if before is None:
        return LocalDeletion("absent")
    ksp = str(getattr(before, "ksp", "unknown"))
    try:
        wincert.delete_cert(sha1_hex, delete_key=True)
    except wincert.WinCertError as exc:
        try:
            still_there = wincert.find_cert(sha1_hex) is not None
        except wincert.WinCertError:
            still_there = True
        return LocalDeletion("failed" if still_there else "key-remains", str(exc), ksp)
    return LocalDeletion("deleted", ksp=ksp)


def key_removal_advice(ksp: str) -> str:
    """How to delete a private key by hand once its certificate is gone.

    Needs confirmation on real Windows: the certutil invocation for each key store.
    On macOS and Linux a certificate and its key are one key store item, named for the app.
    """
    if ksp == "keychain":
        return (
            "To remove it by hand, open Keychain Access, search for entrabot, and delete the "
            "item that belonged to this certificate."
        )
    if ksp == "keyring":
        return (
            "To remove it by hand, open your keyring manager (Passwords and Keys, or KWallet "
            "Manager), search for entrabot, and delete the item that belonged to this "
            "certificate."
        )
    provider = _KEY_PROVIDERS.get(ksp)
    where = f'-csp "{provider}" ' if provider else ""
    return (
        f"To remove it by hand, list this account's keys with  certutil -user {where}-key  "
        "and delete the one that belonged to this certificate with  certutil -user "
        f"{where}-delkey <key name>."
    )


@dataclass(frozen=True)
class Removal:
    """What happened when certificates were taken off an application's list.

    *not_listed*: of the thumbprints asked for, those that were not on the list to begin with
    (known only when the removal is confirmed).
    """

    status: RemovalStatus
    removed: int = 0
    detail: str = ""
    not_listed: tuple[str, ...] = ()

    @property
    def gone(self) -> bool:
        """The certificate is not on the list (it was removed, or was never there)."""
        return self.status == "gone"


RevocationStatus = Literal["revoked", "not-listed", "failed"]

_REVOCATION_OUTCOMES: dict[str, RevocationStatus] = {
    "removed": "revoked",  # what _revoke_app_certificate returns once a read confirms it
    "not_registered": "not-listed",
}


@dataclass(frozen=True)
class Revocation:
    """What happened to one old setup certificate on the Provisioner app.

    "revoked": a read confirmed it gone. "not-listed": it was not on the app. "failed"
    claims nothing either way: it may still be listed, and *detail* says why it is not known.
    """

    status: RevocationStatus
    detail: str = ""


@dataclass(frozen=True)
class Proof:
    """Whether Microsoft Entra accepts a certificate, and how far sign-in got."""

    accepted: bool  # Hop 1, the only step the Blueprint certificate signs, succeeded
    complete: bool = False  # all three hops succeeded
    retry: bool = False
    hop: str = ""
    code: str = ""
    detail: str = ""


@dataclass(frozen=True)
class Access:
    """The helper-script runner, with the Azure CLI on its PATH, and who is signed in."""

    helpers: Helpers
    az: AzCli | None
    account: Account | None

    @property
    def problem(self) -> tuple[str, str] | None:
        """Why Microsoft Entra can't be reached from here, as (what, fix), else None."""
        setup = self.helpers.console.words.setup
        if self.az is None:
            return (
                "The Azure CLI (Microsoft's sign-in tool) was not found on this computer.",
                f"Run {setup}: it installs it and signs you in.",
            )
        if self.account is None:
            return (
                "You are not signed in to Microsoft.",
                f"Run {setup}: it signs you in. Then run this again.",
            )
        return None


def connect(
    console: Console,
    paths: Paths,
    env: Mapping[str, str],
    *,
    platform: str,
    which: Callable[[str], str | None],
) -> Access:
    """Find the Azure CLI and the signed-in account. Changes nothing and never signs in."""
    location = azcli.find_az(env=env, data_dir=paths.data_root, platform=platform, which=which)
    if location is None:
        return Access(helpers=Helpers(console, paths, env), az=None, account=None)
    az = AzCli(location, console, env=env, platform=platform)
    return Access(helpers=Helpers(console, paths, az.child_env(env)), az=az, account=az.account())


def _result(output: str) -> dict[str, Any]:
    line = marked_line(output, RESULT_MARKER)
    if line is None:
        return {}
    try:
        data = json.loads(line)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _last_lines(text: str, count: int = 3) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " ".join(lines[-count:])[-400:]


class Directory:
    """Certificate operations against Microsoft Entra, through child processes."""

    def __init__(self, console: Console, paths: Paths, helpers: Helpers) -> None:
        self._console = console
        self._paths = paths
        self._helpers = helpers

    def _remove(
        self, code: str, args: list[str], waiting: str, *, targets: Sequence[str] = ()
    ) -> Removal:
        result = self._console.run(
            [str(self._paths.python), "-c", code, str(self._paths.scripts), *args],
            env=self._helpers.base_env,
            cwd=self._paths.project_root,
            capture=True,
            quiet=True,
            heartbeat=waiting,
        )
        data = _result(result.stdout)
        detail = str(data.get("problem") or "")
        if result.returncode == _GONE and data:
            # The child names the ones that were not listed by position, never by thumbprint.
            positions = data.get("not_listed")
            not_listed = tuple(
                targets[index]
                for index in (positions if isinstance(positions, list) else [])
                if isinstance(index, int) and 0 <= index < len(targets)
            )
            return Removal("gone", removed=int(data.get("removed") or 0), not_listed=not_listed)
        if result.returncode == _NO_ACCESS:
            return Removal("no-access", detail=detail)
        if result.returncode == _UNCHANGED:
            return Removal("unchanged", detail=detail)
        # Anything else, including a child that died before it reported: the list may have
        # changed, so nothing is claimed either way.
        return Removal("unconfirmed", detail=detail or _last_lines(result.stderr))

    def remove_blueprint_credential(
        self, blueprint_object_id: str, sha1_hex: str, *, must_keep: Sequence[str] = ()
    ) -> Removal:
        """Take one certificate off the Blueprint. Every other machine's stays.

        The list is replaced by the one that was read, so a read that does not yet show a
        certificate added moments ago would drop it. *must_keep* names such certificates:
        nothing is written from a read that doesn't show them (rotation passes the new
        certificate when it retires the old one).
        """
        return self._remove(
            REMOVE_BLUEPRINT_CREDENTIAL_SNIPPET,
            [blueprint_object_id, sha1_hex, *must_keep],
            "Waiting for Microsoft Entra to confirm the certificate is gone",
        )

    def remove_blueprint_credentials(
        self, blueprint_object_id: str, sha1s: Sequence[str]
    ) -> Removal:
        """Take several certificates off the Blueprint in ONE write. Every other machine's stays.

        One read, one write, one confirmed read-back for all of them. Calling
        :meth:`remove_blueprint_credential` for each instead is a whole-list write each, and the
        second one's read can land on a replica that still lists the certificate the first
        took off: that write puts it back, and both say they succeeded. The result is "gone"
        only when a read showed every one of them gone; ``not_listed`` says which were not
        there to begin with.
        """
        targets = list(sha1s)
        return self._remove(
            REMOVE_BLUEPRINT_CREDENTIALS_SNIPPET,
            [blueprint_object_id, *targets],
            "Waiting for Microsoft Entra to confirm the "
            + ("certificate is gone" if len(targets) == 1 else "certificates are gone"),
            targets=targets,
        )

    def remove_provisioner_credential(self, sha1_hex: str) -> Removal:
        """Take this computer's setup certificate off the Provisioner app."""
        return self._remove(
            REMOVE_PROVISIONER_CREDENTIAL_SNIPPET,
            [sha1_hex],
            "Waiting for Microsoft Entra to confirm the setup certificate is gone",
        )

    def revoke_old_setup_certificates(
        self, provisioner_client_id: str, sha1s: Sequence[str]
    ) -> list[Revocation]:
        """Take old setup certificates off the Provisioner app the way setup's retry does.

        These are the certificates an earlier setup could not remove when it moved an older
        install off its plaintext key file (``PROVISIONER_CERT_REVOKE_PENDING`` in the state
        file). Returns one :class:`Revocation` per thumbprint, in order; a child that did
        not say what happened to one leaves it "failed", never "revoked".
        """
        targets = list(sha1s)
        result = self._console.run(
            [
                str(self._paths.python),
                "-c",
                REVOKE_OLD_SETUP_CERTIFICATES_SNIPPET,
                str(self._paths.scripts),
                provisioner_client_id,
                *targets,
            ],
            env=self._helpers.base_env,
            cwd=self._paths.project_root,
            capture=True,
            quiet=True,
            heartbeat="Waiting for Microsoft Entra to confirm the old setup certificate is gone",
        )
        answers = _result(result.stdout).get("results")
        if not isinstance(answers, list) or len(answers) != len(targets):
            why = _last_lines(result.stderr) or "the removal could not be confirmed"
            return [Revocation("failed", why) for _ in targets]
        revocations: list[Revocation] = []
        for answer in answers:
            fields = answer if isinstance(answer, dict) else {}
            status = _REVOCATION_OUTCOMES.get(str(fields.get("outcome")), "failed")
            if status == "failed":
                why = str(fields.get("problem") or "") or "the removal could not be confirmed"
                revocations.append(Revocation("failed", why))
            else:
                revocations.append(Revocation(status))
        return revocations

    def prove_certificate(self, *, sha1_hex: str, x5t_s256: str, ksp: str) -> Proof:
        """Sign the agent in with the named certificate, in a fresh process.

        Everything else comes from ``.env``; only the certificate is overridden, so the
        proof uses exactly the configuration the agent will have after the switch.
        """
        env = self._helpers.runtime_env()
        env["ENTRABOT_BLUEPRINT_CERT_SHA1"] = sha1_hex
        env["ENTRABOT_BLUEPRINT_CERT_THUMBPRINT"] = x5t_s256
        env["ENTRABOT_BLUEPRINT_KSP"] = ksp
        result = self._helpers.snippet(PROVE_CERTIFICATE_SNIPPET, env=env)
        data = _result(result.stdout)
        if not data:
            return Proof(accepted=False, detail=_last_lines(result.stderr) or "the check crashed")
        if data.get("ok") is True:
            return Proof(accepted=True, complete=True)
        hop = str(data.get("hop") or "")
        description = str(data.get("description") or "")
        found = _AADSTS.search(description)
        code = found.group(0) if found else str(data.get("error") or "")
        past_the_certificate = bool(hop) and not hop.startswith("hop1")
        return Proof(
            accepted=past_the_certificate,
            retry=bool(data.get("retry")),
            hop=hop,
            code=code,
            detail=description,
        )
