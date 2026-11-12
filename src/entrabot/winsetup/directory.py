"""Microsoft Entra, as the maintenance commands (rotate-cert, teardown, status) need it.

Three things, each run in a child process with the virtual-environment interpreter, so that
``entrabot.config`` never loads into the command itself and a token never passes through it:

* take ONE certificate off an application's list, keeping every other machine's, confirmed
  by reading the list back (``scripts/blueprint_certs.remove_key_credential``);
* prove that a certificate in this user's store really signs the agent in;
* find the Azure CLI and say who is signed in, because the Provisioner token the first two
  need is only issued to a signed-in administrator's setup.

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

def remove(object_id, sha1, must_keep=()):
    try:
        result = blueprint_certs.remove_key_credential(
            object_id=object_id, token=token, sha1_hex=sha1, must_keep=list(must_keep)
        )
    except blueprint_certs.KeyCredentialsReadError as exc:  # nothing was sent
        done({_UNCHANGED}, problem=str(exc))
    except blueprint_certs.RegistrationRejectedError as exc:  # refused: nothing changed
        done({_UNCHANGED}, problem=str(exc))
    except Exception as exc:  # the write may have landed, so don't claim otherwise
        done({_UNCONFIRMED}, problem=str(exc))
    done({_GONE}, removed=len(result.removed), kept=len(result.current))
"""

REMOVE_BLUEPRINT_CREDENTIAL_SNIPPET = (
    "# Entrabot: remove this computer's certificate from the Blueprint\n"
    + _REMOVAL_COMMON
    + """
remove(sys.argv[2], sys.argv[3], must_keep=sys.argv[4:])
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
    """
    provider = _KEY_PROVIDERS.get(ksp)
    where = f'-csp "{provider}" ' if provider else ""
    return (
        f"To remove it by hand, list this account's keys with  certutil -user {where}-key  "
        "and delete the one that belonged to this certificate with  certutil -user "
        f"{where}-delkey <key name>."
    )


@dataclass(frozen=True)
class Removal:
    """What happened when a certificate was taken off an application's list."""

    status: RemovalStatus
    removed: int = 0
    detail: str = ""

    @property
    def gone(self) -> bool:
        """The certificate is not on the list (it was removed, or was never there)."""
        return self.status == "gone"


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
        if self.az is None:
            return (
                "The Azure CLI (Microsoft's sign-in tool) was not found on this computer.",
                f"Run {SETUP_COMMAND}: it installs it and signs you in.",
            )
        if self.account is None:
            return (
                "You are not signed in to Microsoft.",
                f"Run {SETUP_COMMAND}: it signs you in. Then run this again.",
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
    az = AzCli(location, console)
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

    def _remove(self, code: str, args: list[str], waiting: str) -> Removal:
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
            return Removal("gone", removed=int(data.get("removed") or 0))
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

    def remove_provisioner_credential(self, sha1_hex: str) -> Removal:
        """Take this computer's setup certificate off the Provisioner app."""
        return self._remove(
            REMOVE_PROVISIONER_CREDENTIAL_SNIPPET,
            [sha1_hex],
            "Waiting for Microsoft Entra to confirm the setup certificate is gone",
        )

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
