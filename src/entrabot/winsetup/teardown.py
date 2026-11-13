"""Take Entrabot off this computer, and optionally delete the agent itself.

    python -m entrabot.winsetup teardown --project-root PATH
        [--delete-agent] [--purge-data] [--keep-setup-credential] [--yes]

Three scopes, each one explicit:

* default - **disconnect this computer**. Its certificate comes off the agent's Blueprint
  (so it stops being trusted today, not when it expires), the certificate and its private
  key are deleted here, the ``entrabot`` entry leaves the assistants' configs, and ``.env``
  and ``.entrabot-state.json`` are backed up and removed. The agent, its memory, and every
  other computer that uses it are untouched.
* ``--delete-agent`` - additionally delete the agent from the tenant (Agent User, Agent
  Identity, Blueprint), for every computer.
* ``--purge-data`` - additionally delete Entrabot's data folder on this computer.

The setup credential goes too. Setup signs in to Microsoft Entra with its own certificate
(the Provisioner's), whose key can manage applications across the tenant. A computer that
is being disconnected should not keep that key, so once no other Entrabot agent on this
computer needs it, it is removed from the Provisioner app and deleted here with its private
key - last, because taking this computer's certificate off the Blueprint needs it to sign
in with. ``--keep-setup-credential`` keeps it, for someone about to set up again.

The certificate is chosen by the thumbprint in ``.env``, never by its subject: another
agent's certificate on the same computer is not this one's to delete. And ``.env`` only says
which certificate the folder was set up with, not that it is on THIS computer: a copied or
shared ``.env`` names another computer's. So a certificate comes off the Blueprint only when
it is in this computer's store. One that is not (or a store that can't be read, which is not
proof either) is left there and said so; interactively the person is asked, default No.

A rotation that could not finish leaves its old certificate's thumbprint in ``.env``
(``ENTRABOT_BLUEPRINT_CERT_RETIRING_SHA1``). That certificate goes too when it is in this
store (off the Blueprint, and deleted with its key); when it is not, it is reported. Both come
off the Blueprint in ONE write: the list is replaced whole, and a second write built from a
read that has not caught up with the first would put the first certificate back.

Setup may have left a note in ``.entrabot-state.json`` (``PROVISIONER_CERT_REVOKE_PENDING``):
an old setup certificate, whose private key an older install kept in a plaintext file, that it
could not take off the Provisioner app. Setup retries on its next run, but the note goes with
the state file, so teardown tries first, the way setup's retry does. If that does not work it
is a to-do with where to remove it by hand; with ``--keep-setup-credential`` (someone is about
to set up again) the state file is left holding only that note, so setup's retry still finds it.

What can't be done is said precisely, with what to do about it. If Microsoft Entra can't be
reached the local cleanup still completes; a certificate that stays listed there has lost
its private key and cannot be used. A private key that could not be deleted is never
reported as deleted: it is a to-do with how to remove it, and teardown exits 1.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from entrabot.winsetup import certstep, hosts
from entrabot.winsetup.certstep import CertStepError
from entrabot.winsetup.console import Cancelled, Console
from entrabot.winsetup.directory import (
    SETUP_COMMAND,
    Access,
    Directory,
    LocalDeletion,
    Revocation,
    connect,
    delete_local_cert,
    is_sha1,
    key_removal_advice,
    last4,
)
from entrabot.winsetup.envfile import EnvLockError, lock_to_current_user, read_env
from entrabot.winsetup.helpers import Paths
from entrabot.winsetup.rotation import RETIRING_KEY
from entrabot.winsetup.state import IdentityError, read_state
from entrabot.winsetup.wizard import World

EXIT_OK, EXIT_INCOMPLETE, EXIT_USAGE, EXIT_CANCELLED = 0, 1, 2, 130

SERVER_NAME = "entrabot"
TEARDOWN_COMMAND = r"powershell -ExecutionPolicy Bypass -File scripts\teardown-windows.ps1"
_ENTRA_CERTS = (
    "Microsoft Entra admin center > App registrations > All applications > {name} > "
    "Certificates & secrets"
)
_PROVISIONER_APP = "EntraBot Agent ID Provisioner"

# The note setup leaves in the state file when it could not take an old setup certificate off
# the Provisioner app: SHA-1 thumbprints, lowercase and comma-separated, and their expiry dates
# in the same order (scripts/entra_provisioning.py, _REVOKE_PENDING and _set_pending_revocations).
REVOKE_PENDING_KEY = "PROVISIONER_CERT_REVOKE_PENDING"
REVOKE_PENDING_EXPIRES_KEY = "PROVISIONER_CERT_REVOKE_PENDING_EXPIRES"
_PROVISIONER_CLIENT_KEY = "PROVISIONER_CLIENT_ID"
_TENANT_KEY = "TENANT_ID"


@dataclass(frozen=True)
class _OldSetupCert:
    """An old setup (Provisioner) certificate setup could not take off the Provisioner app,
    exactly as the state file's note names it."""

    sha1: str
    expires: str = ""  # as setup recorded it; empty when it did not know

    @property
    def when(self) -> str:
        return f"expires {self.expires}" if self.expires else "expiry date unknown"

    @property
    def ident(self) -> str:
        return f"thumbprint ending {last4(self.sha1)}, {self.when}"


def _idents(items: Sequence[_OldSetupCert]) -> str:
    return "; ".join(item.ident for item in items)


def _pending_revocations(state: Mapping[str, str]) -> list[_OldSetupCert]:
    """Read the note the way setup reads it (``entra_provisioning._pending_revocations``):
    empty and malformed entries are skipped, and an expiry belongs to the thumbprint in the
    same position."""
    sha1s = [part.strip() for part in state.get(REVOKE_PENDING_KEY, "").split(",")]
    expiries = [part.strip() for part in state.get(REVOKE_PENDING_EXPIRES_KEY, "").split(",")]
    found: dict[str, _OldSetupCert] = {}
    for index, sha1 in enumerate(sha1s):
        if is_sha1(sha1):
            expires = expiries[index] if index < len(expiries) else ""
            found.setdefault(sha1.upper(), _OldSetupCert(sha1.upper(), expires))
    return list(found.values())


@dataclass(frozen=True)
class _Cert:
    """A Blueprint certificate this folder's settings name, and whether it is HERE.

    ``.env`` says which certificate the folder was set up with, not that it is on this
    computer: a copied or shared ``.env`` names another computer's. *here* is True only
    when this computer's store was read and holds it; None means the store could not be
    read, which is not proof either way.
    """

    sha1: str
    here: bool | None
    retiring: bool = False  # left behind by a rotation (ENTRABOT_BLUEPRINT_CERT_RETIRING_SHA1)
    problem: str = ""  # why the store could not be read

    @property
    def label(self) -> str:
        if self.retiring:
            return "The old certificate an earlier certificate rotation left behind"
        return "This computer's certificate"


@dataclass
class _Facts:
    """What this computer holds for the agent, read before anything is changed."""

    env: dict[str, str]
    state: dict[str, str]
    cert_sha1: str
    blueprint_object_id: str
    blueprint_app_id: str
    tenant_id: str
    upn: str
    mcp_files: list[Path]
    certs: list[_Cert] = field(default_factory=list)  # the current one first
    # Old setup certificates setup still has to take off the Provisioner app (its note).
    old_setup_certs: list[_OldSetupCert] = field(default_factory=list)

    @property
    def agent(self) -> str:
        return self.upn or "the agent"

    @property
    def current(self) -> _Cert | None:
        return next((cert for cert in self.certs if not cert.retiring), None)

    @property
    def retiring(self) -> _Cert | None:
        return next((cert for cert in self.certs if cert.retiring), None)

    @property
    def has_local_cert(self) -> bool:
        """Something may be in this computer's store (an unreadable store may hold it)."""
        return any(cert.here is not False for cert in self.certs)


@dataclass
class _SetupCredential:
    """What will happen to this computer's setup (Provisioner) certificate, decided before
    the plan is shown so the plan, the action and the report all say the same thing."""

    decision: str  # "none" | "retire" | "keep-asked" | "keep-other-agent" | "unknown"
    certs: list[Any] = field(default_factory=list)
    problem: str = ""


@dataclass
class _Report:
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)


def _has_entry(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return path.is_file()  # unreadable: worth looking at, and reporting
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return isinstance(servers, dict) and SERVER_NAME in servers


def remove_mcp_entry(path: Path) -> str:
    """Remove the ``entrabot`` server from one assistant config.

    Every other server and every other setting is kept, in the order it was in, and the file
    is written back as UTF-8. Returns "removed", "absent", or "unreadable" (left as it was).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return "absent"
    except (OSError, ValueError):
        return "unreadable"
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return "absent"
    del servers[SERVER_NAME]
    try:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    except OSError:
        return "unreadable"
    return "removed"


def _storage_account(endpoint: str) -> str:
    host = urlsplit(endpoint).hostname or ""
    return host.split(".")[0]


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return True


class _Teardown:
    def __init__(
        self,
        console: Console,
        paths: Paths,
        world: World,
        *,
        delete_agent: bool,
        purge_data: bool,
        keep_setup_credential: bool,
        running_from: Sequence[Path] | None,
    ) -> None:
        self.console = console
        self.paths = paths
        self.world = world
        self.delete_agent = delete_agent
        self.purge_data = purge_data
        self.keep_setup_credential = keep_setup_credential
        self.setup = _SetupCredential("none")
        # Where the running interpreter lives: never deleted from under itself.
        self.running_from = (
            [Path(sys.base_prefix), Path(sys.prefix), Path(sys.executable).parent]
            if running_from is None
            else list(running_from)
        )
        self.report = _Report()
        self.wincert: Any = None
        self.wincert_problem = ""
        # Whether the person explicitly said yes to removing, from the Blueprint, a
        # certificate that is not known to be this computer's. Never assumed.
        self.remove_unverified = False
        # The old setup certificates teardown takes off the Provisioner app (setup's note,
        # less this computer's own setup certificate, which is never an "old" one).
        self.old_setup_certs: list[_OldSetupCert] = []
        # Those it could not, when the note is to be left for setup's next run, and the
        # state file's content that keeps it (-KeepSetupCredential).
        self.left_for_setup: list[_OldSetupCert] = []
        self.state_note: dict[str, str] = {}

    # ── What is here ─────────────────────────────────────────────────────────

    def _look_up(self, sha1: str, *, retiring: bool = False) -> _Cert:
        if self.wincert is None:
            return _Cert(sha1, None, retiring, self.wincert_problem)
        try:
            return _Cert(sha1, self.wincert.find_cert(sha1) is not None, retiring)
        except self.wincert.WinCertError as exc:
            return _Cert(sha1, None, retiring, str(exc))

    def _facts(self) -> _Facts:
        env = read_env(self.paths.env_file)
        try:
            state = read_state(self.paths.state_file)
        except IdentityError:
            state = {}
        sha1 = env.get("ENTRABOT_BLUEPRINT_CERT_SHA1", "").strip().upper()
        sha1 = sha1 if is_sha1(sha1) else ""
        retiring = env.get(RETIRING_KEY, "").strip().upper()
        retiring = retiring if is_sha1(retiring) and retiring != sha1 else ""
        try:
            self.wincert = self.world.wincert or certstep.load_wincert()
        except CertStepError as exc:
            self.wincert_problem = str(exc)
        certs = [self._look_up(sha1)] if sha1 else []
        if retiring:
            certs.append(self._look_up(retiring, retiring=True))
        targets = [self.paths.claude_mcp, hosts.copilot_mcp_path(self.world.env)]
        return _Facts(
            env=env,
            state=state,
            cert_sha1=sha1,
            blueprint_object_id=(
                env.get("ENTRABOT_BLUEPRINT_OBJECT_ID") or state.get("BLUEPRINT_OBJECT_ID") or ""
            ).strip(),
            blueprint_app_id=(
                env.get("ENTRABOT_BLUEPRINT_APP_ID") or state.get("BLUEPRINT_APP_ID") or ""
            ).strip(),
            tenant_id=(env.get("ENTRABOT_TENANT_ID") or state.get("TENANT_ID") or "").strip(),
            upn=(
                env.get("ENTRABOT_AGENT_UPN")
                or env.get("ENTRABOT_AGENT_USER_UPN")
                or state.get("AGENT_USER_UPN")
                or ""
            ).strip(),
            mcp_files=[path for path in targets if _has_entry(path)],
            certs=certs,
            old_setup_certs=_pending_revocations(state),
        )

    def _setup_credential(self, facts: _Facts) -> _SetupCredential:
        """Find this computer's setup certificate for the tenant and decide its fate.

        It is retired unless the person asked to keep it, or another Entrabot agent's
        Blueprint certificate is in this user's store: that agent's setup still needs it.
        """
        wincert = self.wincert
        if wincert is None or not facts.tenant_id:
            return _SetupCredential("none")
        try:
            every = wincert.list_certs()
        except wincert.WinCertError as exc:
            return _SetupCredential("unknown", problem=str(exc))
        certs = [
            cert
            for cert in every
            if cert.friendly_name == f"Entrabot Provisioner {facts.tenant_id}"
        ]
        if not certs:
            return _SetupCredential("none")
        if self.keep_setup_credential:
            return _SetupCredential("keep-asked", certs)
        mine = f"Entrabot Blueprint {facts.blueprint_app_id}"
        another_agent = any(
            cert.friendly_name.startswith("Entrabot Blueprint ") and cert.friendly_name != mine
            for cert in every
        )
        return _SetupCredential("keep-other-agent" if another_agent else "retire", certs)

    def _has_data(self) -> bool:
        root = self.paths.data_root
        return root.is_dir() and any(root.iterdir())

    # ── Entry ────────────────────────────────────────────────────────────────

    def run(self) -> int:
        console = self.console
        console.banner("Entrabot teardown")
        facts = self._facts()
        settings = [p for p in (self.paths.env_file, self.paths.state_file) if p.is_file()]
        something_here = bool(settings or facts.has_local_cert or facts.mcp_files)
        if not something_here and not (self.purge_data and self._has_data()):
            console.ok("Nothing to remove. This computer is not connected to an agent.")
            return EXIT_OK
        if self.delete_agent and not facts.upn:
            console.fail(
                "This computer's settings don't say which agent to delete.",
                fix="Nothing was changed. To disconnect this computer only, run teardown "
                "without -DeleteAgent.",
            )
            return EXIT_USAGE

        self.setup = self._setup_credential(facts)
        # Setup never removes the certificate it signs in with as an old one; nor does teardown.
        in_use = {cert.sha1_hex.upper() for cert in self.setup.certs}
        self.old_setup_certs = [item for item in facts.old_setup_certs if item.sha1 not in in_use]
        # Looked up before the question is asked, so the answer is given knowing whether
        # Microsoft Entra can be reached. Reads only; never signs in.
        access = connect(
            console,
            self.paths,
            self.world.env,
            platform=self.world.platform,
            which=self.world.which,
        )
        self._show_plan(facts, settings, access)
        if not console.interactive and not console.assume_yes:
            console.fail(
                "Teardown needs a yes before it removes anything.",
                fix="Run it in a terminal and answer the question, or add -Yes (--yes).",
            )
            return EXIT_USAGE
        try:
            if not self._confirmed(facts, access):
                console.info("Nothing was changed.")
                return EXIT_CANCELLED
        except (Cancelled, KeyboardInterrupt):
            console.blank()
            console.info("Nothing was changed.")
            return EXIT_CANCELLED

        directory = None if access.problem else Directory(console, self.paths, access.helpers)

        console.blank()
        agent_deleted = self._delete_agent(facts, access) if self.delete_agent else False
        # A deleted agent's Blueprint is gone, and its certificate list with it.
        listed = {} if agent_deleted else self._take_off_blueprint(facts, access, directory)
        for cert in facts.certs:
            self._disconnect_certificate(facts, cert, listed.get(cert.sha1))
        self._remove_assistant_entries(facts)
        if self.delete_agent and not agent_deleted:
            # A second attempt at deleting the agent needs the settings and the setup
            # certificate, so they stay until the agent is really gone.
            kept = (
                "This folder's settings (.env, .entrabot-state.json) and the setup "
                "certificate: deleting the agent again needs them."
            )
            if self.old_setup_certs:
                kept += (
                    " The settings also keep setup's note about the old setup (Provisioner) "
                    f"certificate{'s' if len(self.old_setup_certs) > 1 else ''} it could not "
                    f"take off the Provisioner app ({_idents(self.old_setup_certs)}): teardown "
                    "tries again when it runs again, and so does setup."
                )
            self.report.kept.append(kept)
        else:
            # Before the setup credential and the state file, which holds the note about them.
            self._settle_old_setup_certs(facts, access, directory)
            # Last of the Microsoft Entra steps: the Blueprint and agent steps signed in with it.
            self._settle_setup_credential(directory)
            if agent_deleted:
                self._mention_cloud_memory(facts)
            self._remove_settings(settings)
        if self.purge_data:
            self._purge_data()
        return self._finish(facts)

    # ── Saying what will happen, and asking ──────────────────────────────────

    def _show_plan(self, facts: _Facts, settings: list[Path], access: Access) -> None:
        console = self.console
        console.info(f"This will disconnect THIS computer from {facts.agent}:")
        current, retiring = facts.current, facts.retiring
        will_ask = self._asks_about_unverified(facts, access)
        if current is not None and current.here:
            console.bullet(
                "Microsoft Entra: this computer's certificate comes off the agent's Blueprint. "
                "The agent's other computers keep working."
            )
            console.bullet("This computer: the certificate and its private key are deleted.")
        elif current is not None and not self.delete_agent:  # deleting it takes the list too
            if current.here is None:
                why = (
                    "this computer's certificate store could not be opened "
                    f"({current.problem.rstrip('.')}), so it can't be checked that the "
                    f"certificate this folder's settings name (thumbprint ending "
                    f"{last4(current.sha1)}) is this computer's"
                )
            else:
                why = (
                    "the certificate this folder's settings name (thumbprint ending "
                    f"{last4(current.sha1)}) is not in this computer's certificate store"
                )
            console.bullet(
                f"Microsoft Entra: {why}. It may be another computer's (a copied or shared "
                ".env), and removing it would cut that computer off, so it is left on the "
                "agent's Blueprint" + (" unless you say otherwise." if will_ask else ".")
            )
        if retiring is not None and retiring.here:
            also = "" if self.delete_agent else ", and taken off the agent's Blueprint if listed"
            console.bullet(
                "An earlier certificate rotation left its old certificate on this computer "
                f"(thumbprint ending {last4(retiring.sha1)}). It is deleted here with its "
                f"private key{also}."
            )
        elif retiring is not None and not self.delete_agent:
            console.bullet(
                "An earlier certificate rotation was retiring a certificate (thumbprint ending "
                f"{last4(retiring.sha1)}) that is "
                + (
                    "not in this computer's certificate store"
                    if retiring.here is False
                    else "in a certificate store that could not be opened"
                )
                + ". It may be another computer's, so it is not taken off the agent's "
                "Blueprint; the report at the end says where to check it."
            )
        if self.setup.decision == "retire":
            console.bullet(
                "Setup credential: this computer's setup (Provisioner) certificate is retired "
                "too. It is removed from the Provisioner app in Microsoft Entra and deleted "
                "here with its private key. Setup signs in with it and it can manage "
                "applications in your tenant, so it does not stay on a computer you are "
                "done with. (About to set up again? Add -KeepSetupCredential.)"
            )
        elif self.setup.decision == "keep-asked":
            console.bullet(
                "Setup credential: this computer's setup (Provisioner) certificate is kept, "
                "as asked (-KeepSetupCredential). It can manage applications in your tenant."
            )
        elif self.setup.decision == "keep-other-agent":
            console.bullet(
                "Setup credential: this computer's setup (Provisioner) certificate is kept: "
                "another Entrabot agent on this computer still uses it."
            )
        self._plan_old_setup_certs()
        if access.problem and (facts.cert_sha1 or self.delete_agent or self.old_setup_certs):
            console.blank()
            console.warn(
                f"{access.problem[0]} Nothing can be changed in Microsoft Entra right now, so "
                "only this computer would be cleaned up."
            )
            console.detail(f"To do both: {access.problem[1]}")
            console.blank()
        for path in facts.mcp_files:
            console.bullet(f"{path}: the entrabot entry is removed. Other entries are kept.")
        if settings:
            names = " and ".join(path.name for path in settings)
            console.bullet(f"{names}: backed up next to the original, then removed.")
        if self.delete_agent:
            console.blank()
            console.warn(f"It will ALSO delete the agent {facts.agent} from your tenant:")
            console.bullet(
                "The Agent User, Agent Identity and Blueprint are deleted for every computer "
                "that uses them. This cannot be undone."
            )
            console.bullet("Cloud memory, if any, is not deleted.")
        if self.purge_data:
            console.blank()
            console.warn(f"It will ALSO delete Entrabot's data folder: {self.paths.data_root}")
            console.bullet(
                "The agent's local memory and logs, and Entrabot's private copies of Python "
                "and the Azure CLI, and of the .NET SDK and the Microsoft Agent 365 CLI if "
                "an earlier version of setup installed them. The project's .venv will need "
                "setup to rebuild it."
            )
        console.blank()
        if not self.delete_agent:
            untouched = f"Not touched: the agent itself ({facts.agent} still exists in your tenant)"
            if not self.purge_data:
                untouched += ", its memory, and Entrabot's data folder on this computer"
            console.info(untouched + ".")

    def _plan_old_setup_certs(self) -> None:
        items = self.old_setup_certs
        if not items:
            return
        if len(items) == 1:
            what, keys, it = (
                "an old setup (Provisioner) certificate",
                "its private key in a plaintext file",
                "it",
            )
        else:
            what, keys, it = (
                "old setup (Provisioner) certificates",
                "their private keys in plaintext files",
                "them",
            )
        if self.setup.decision == "keep-asked":
            otherwise = (
                f".entrabot-state.json is left holding only the note about {it}, so setup "
                "tries again on its next run"
            )
        else:
            otherwise = f"the report at the end says where to remove {it} by hand"
        self.console.bullet(
            f"Microsoft Entra: an earlier setup could not take {what} off the Provisioner app "
            f"({_idents(items)}). An older install kept {keys}, so a copy can still sign in "
            "to the Provisioner app, which can manage applications in your tenant. Teardown "
            f"removes {it} there now. If that does not work, {otherwise}."
        )

    def _asks_about_unverified(self, facts: _Facts, access: Access) -> bool:
        """Whether the person is asked about a configured certificate not known to be here.

        Only when someone is there to answer (never with --yes, which leaves it), and only
        when the answer could change anything: Microsoft Entra is reachable, the Blueprint
        is known, and it is not being deleted anyway.
        """
        current = facts.current
        return (
            current is not None
            and current.here is not True
            and not self.console.assume_yes
            and not self.delete_agent
            and bool(facts.blueprint_object_id)
            and access.problem is None
        )

    def _confirmed(self, facts: _Facts, access: Access) -> bool:
        console = self.console
        if console.assume_yes:
            return True
        question = (
            "Delete the agent and disconnect this computer?"
            if self.delete_agent
            else "Disconnect this computer?"
        )
        if not console.confirm(question, default=False, key="teardown"):
            return False
        if self.delete_agent:
            typed = console.ask_text(
                f"To confirm deleting the agent, type its address ({facts.upn})",
                default="",
                key="teardown_agent",
            )
            if typed.strip().lower() != facts.upn.lower():
                console.warn("That is not the agent's address.")
                return False
        current = facts.current
        if current is not None and self._asks_about_unverified(facts, access):
            console.warn(
                f"The certificate this folder's settings name (thumbprint ending "
                f"{last4(current.sha1)}) is not known to be this computer's. If another "
                "computer uses it, removing it from the Blueprint cuts that computer off."
            )
            self.remove_unverified = console.confirm(
                "Remove it from the agent's Blueprint anyway?",
                default=False,
                key="teardown_remove_unverified_certificate",
            )
        return True

    # ── Microsoft Entra ──────────────────────────────────────────────────────

    def _delete_agent(self, facts: _Facts, access: Access) -> bool:
        console = self.console
        if access.problem:
            console.fail(f"The agent was NOT deleted. {access.problem[0]}")
            self.report.todo.append(
                f"The agent {facts.agent} was NOT deleted: {access.problem[0]} "
                f"{access.problem[1]} Then run teardown with -DeleteAgent again."
            )
            return False
        result = access.helpers.script(
            "deprovision_entra_agent_identity.py",
            ["--agent-user-upn", facts.upn],
            summary=True,
            heartbeat="Deleting the agent from your tenant",
        )
        if result.returncode != 0:
            console.fail(f"The agent {facts.agent} was NOT deleted (see below).")
            console.show_tail(result)
            self.report.todo.append(
                f"The agent {facts.agent} was NOT deleted. Fix what the message above says, "
                f"then run:  {TEARDOWN_COMMAND} -DeleteAgent"
            )
            return False
        console.ok(f"Deleted the agent {facts.agent} from your tenant")
        self.report.removed.append(
            f"The agent {facts.agent}: Agent User, Agent Identity and Blueprint."
        )
        return True

    def _blueprint_place(self, facts: _Facts) -> str:
        name = (
            f"the agent's Blueprint (App ID {facts.blueprint_app_id})"
            if facts.blueprint_app_id
            else "the agent's Blueprint"
        )
        return _ENTRA_CERTS.format(name=name)

    def _still_listed(self, facts: _Facts, cert: _Cert, why: str, *, key_gone: bool) -> str:
        """The to-do for a certificate that should have come off the Blueprint and didn't.

        Whether it can still be used depends on its private key, so that is said only
        once the local deletion has been tried.
        """
        if key_gone:
            usable = (
                "With its private key deleted from this computer it cannot be used, but it "
                "stays listed until it expires or an administrator removes it"
            )
        else:
            usable = (
                "Its private key is still on this computer (see the item about it), so it "
                "can still sign the agent in until it is removed from the Blueprint"
            )
        return (
            f"{cert.label} is still listed on the agent's Blueprint (thumbprint ending "
            f"{last4(cert.sha1)}): {why} {usable}: {self._blueprint_place(facts)}."
        )

    def _disconnect_certificate(self, facts: _Facts, cert: _Cert, why_listed: str | None) -> None:
        """Take *cert* and its private key off this computer, and report exactly what is left.

        *why_listed* is why it is still listed on the Blueprint, or None when it is not there
        or was deliberately left there (:meth:`_take_off_blueprint` has reported that)."""
        local = self._delete_here(cert, listed=why_listed is not None)
        if why_listed is not None:
            self.report.todo.append(
                self._still_listed(facts, cert, why_listed, key_gone=local.clean)
            )

    def _take_off_blueprint(
        self, facts: _Facts, access: Access, directory: Directory | None
    ) -> dict[str, str]:
        """Remove this folder's certificates from the Blueprint, all of them in ONE write.

        Graph replaces the whole list on every write and a read can land on a replica that
        has not caught up, so a second removal's read could still show the certificate the
        first one took off, and its write would put it back. One write leaves no second read.

        Returns, by thumbprint, why a certificate that should have come off is still listed.
        One deliberately left there (not known to be this computer's) is reported here and is
        not in the result.
        """
        leaving: list[_Cert] = []
        for cert in facts.certs:
            if cert.here is not True and (cert.retiring or not self.remove_unverified):
                self._leave_unverified(facts, cert)
            else:
                leaving.append(cert)
        if not leaving:
            return {}
        if not facts.blueprint_object_id:
            return {cert.sha1: "this folder has no record of the Blueprint." for cert in leaving}
        if directory is None:
            assert access.problem is not None
            self.console.warn(f"Could not reach Microsoft Entra. {access.problem[0]}")
            return {cert.sha1: access.problem[0] for cert in leaving}
        removal = directory.remove_blueprint_credentials(
            facts.blueprint_object_id, [cert.sha1 for cert in leaving]
        )
        if removal.gone:
            for cert in leaving:
                self._off_blueprint(cert, was_listed=cert.sha1 not in removal.not_listed)
            return {}
        why = removal.detail or "the change could not be confirmed"
        what = "The certificate" if len(leaving) == 1 else "The certificates"
        self.console.warn(f"{what} could not be taken off the Blueprint: {why}")
        return {cert.sha1: f"{why.rstrip('.')}." for cert in leaving}

    def _off_blueprint(self, cert: _Cert, *, was_listed: bool) -> None:
        """Say that *cert* is confirmed off the Blueprint."""
        if cert.retiring:
            self.console.ok(
                "Removed the old certificate an earlier certificate rotation left behind "
                "from the agent's Blueprint"
                if was_listed
                else "The old certificate an earlier rotation left behind was no longer "
                "listed on the agent's Blueprint"
            )
            self.report.removed.append(
                f"The old certificate an earlier certificate rotation left behind (ending "
                f"{last4(cert.sha1)}), from the agent's Blueprint in Microsoft Entra."
            )
        elif cert.here is not True:
            self.console.ok(
                "Removed the certificate this folder's settings name from the agent's "
                "Blueprint, as you asked"
            )
            self.report.removed.append(
                f"The certificate this folder's settings named (ending {last4(cert.sha1)}), "
                "from the agent's Blueprint in Microsoft Entra, as you asked."
            )
        else:
            self.console.ok("Removed this computer's certificate from the agent's Blueprint")
            self.report.removed.append(
                "This computer's certificate, from the agent's Blueprint in Microsoft Entra."
            )

    def _leave_unverified(self, facts: _Facts, cert: _Cert) -> None:
        """Say that a certificate not known to be this computer's stays on the Blueprint.

        Not in this store: it may be another computer's, so leaving it is the right
        outcome and is listed as kept. A store that could not be read: it may be this
        computer's, still with its key, so that is something left to do.
        """
        tail = last4(cert.sha1)
        place = self._blueprint_place(facts)
        if cert.here is None:
            reason = (
                "this computer's certificate store could not be opened, so it could not be "
                "checked that it is this computer's (it may be another computer's)"
            )
        else:
            reason = (
                "it is not in this computer's certificate store, so it may be another "
                "computer's, and removing it would cut that computer off"
            )
        if cert.retiring:
            self.console.info(
                "Left the old certificate an earlier certificate rotation was retiring on the "
                "agent's Blueprint: it is not known to be this computer's."
            )
            text = (
                f"The old certificate an earlier certificate rotation was retiring (thumbprint "
                f"ending {tail}) was left on the agent's Blueprint, if it is still listed: "
                f"{reason}. If no computer uses it, remove it in the {place}."
            )
        else:
            self.console.info(
                "Left the certificate this folder's settings name on the agent's Blueprint: "
                "it is not known to be this computer's."
            )
            text = (
                f"The certificate this folder's settings named (thumbprint ending {tail}) was "
                f"left on the agent's Blueprint: {reason}. If no computer uses it any more, "
                f"remove it in the {place}."
            )
        (self.report.todo if cert.here is None else self.report.kept).append(text)

    def _settle_old_setup_certs(
        self, facts: _Facts, access: Access, directory: Directory | None
    ) -> None:
        """Take the old setup certificates in setup's note off the Provisioner app.

        The note is in the state file, which teardown removes, and nothing else would ever
        retry it; so this runs before the settings go, and before the setup credential is
        settled. The removal is setup's own retry, signed in through the Azure CLI. One that
        does not work is a to-do saying where to remove it by hand; with -KeepSetupCredential
        its note is also left for setup's next run (see :meth:`_leave_state_note`).
        """
        items = self.old_setup_certs
        if not items:
            return
        client_id = facts.state.get(_PROVISIONER_CLIENT_KEY, "").strip()
        outcomes: list[Revocation]
        if access.problem:
            outcomes = [Revocation("failed", access.problem[0])] * len(items)
        elif not client_id:
            no_app = "this folder has no record of the Provisioner app"
            outcomes = [Revocation("failed", no_app)] * len(items)
        else:
            assert directory is not None
            outcomes = directory.revoke_old_setup_certificates(
                client_id, [item.sha1 for item in items]
            )
        left: list[_OldSetupCert] = []
        for item, outcome in zip(items, outcomes, strict=True):
            tail = last4(item.sha1)
            if outcome.status == "revoked":
                self.console.ok(
                    f"Removed the old setup certificate (ending {tail}) from the Provisioner app"
                )
                self.report.removed.append(
                    f"An old setup (Provisioner) certificate an earlier setup could not remove "
                    f"({item.ident}), from the Provisioner app in Microsoft Entra."
                )
            elif outcome.status == "not-listed":
                self.console.ok(
                    f"The old setup certificate (ending {tail}) was no longer on the "
                    "Provisioner app"
                )
                self.report.removed.append(
                    f"An old setup (Provisioner) certificate an earlier setup could not remove "
                    f"({item.ident}): it is no longer on the Provisioner app."
                )
            else:
                why = outcome.detail.strip().rstrip(".") or "the removal could not be confirmed"
                self.console.warn(
                    f"The old setup certificate (ending {tail}) could not be removed from the "
                    f"Provisioner app: {why}"
                )
                self.report.todo.append(self._old_setup_cert_todo(item, why, client_id))
                left.append(item)
        if left and self.setup.decision == "keep-asked":
            # Someone is about to set up again with the kept credential, and setup is what
            # retries this. Only the note and which tenant's Provisioner app it is about: the
            # recorded app is the one setup trusts and retries against (with none recorded it
            # looks for one by name and may settle on another, where the certificate is "not
            # registered" and the note is dropped), and the tenant lets a later teardown find
            # the kept setup certificate. Setup itself takes the tenant from the sign-in. No
            # agent, so setup is not steered to one, and no recorded setup certificate, so
            # setup uploads the kept one again rather than trust that it is still on the app.
            self.left_for_setup = left
            self.state_note = {
                key: value
                for key, value in (
                    (_TENANT_KEY, facts.tenant_id),
                    (_PROVISIONER_CLIENT_KEY, client_id),
                    (REVOKE_PENDING_KEY, ",".join(item.sha1.lower() for item in left)),
                    (REVOKE_PENDING_EXPIRES_KEY, ",".join(item.expires for item in left)),
                )
                if value or key == REVOKE_PENDING_EXPIRES_KEY
            }

    @staticmethod
    def _old_setup_cert_todo(item: _OldSetupCert, why: str, client_id: str) -> str:
        """Where to remove it by hand, in the words the provisioning script uses for it."""
        app = f"'{_PROVISIONER_APP}'" + (f" (application ID {client_id})" if client_id else "")
        return (
            f"An old setup (Provisioner) certificate may still be listed on the Provisioner app "
            f"({item.ident}): an earlier setup could not remove it, and neither could teardown "
            f"({why}). An older install kept its private key in a plaintext file, so a copy of "
            "that file can still get tokens for the Provisioner app, which can manage "
            "applications in your tenant, until the certificate is removed. Remove it in the "
            f"Entra admin center: App registrations > {app} > Certificates & secrets > "
            f"Certificates: the certificate whose thumbprint ends in {last4(item.sha1)} "
            f"({item.when})."
        )

    def _settle_setup_credential(self, directory: Directory | None) -> None:
        """Carry out what :meth:`_setup_credential` decided, and record it for the report."""
        setup, wincert = self.setup, self.wincert
        if setup.decision == "unknown":
            self.report.todo.append(
                f"This computer's setup (Provisioner) certificate could not be looked up: "
                f"{setup.problem}. If one is still here it can manage applications in your "
                "tenant: delete it in certmgr.msc > Personal > Certificates (named 'Entrabot "
                "Provisioner ...') and remove it in the "
                + _ENTRA_CERTS.format(name="EntraBot Agent ID Provisioner")
                + "."
            )
            return
        if setup.decision == "keep-asked":
            self.console.info("Kept the setup certificate, as asked (-KeepSetupCredential).")
            self.report.kept.append(
                "This computer's setup (Provisioner) certificate, as asked "
                "(-KeepSetupCredential). It can manage applications in your tenant, so do "
                "not leave it on a computer you are done with: delete it in certmgr.msc > "
                "Personal > Certificates (named 'Entrabot Provisioner ...') and remove it in "
                "the " + _ENTRA_CERTS.format(name="EntraBot Agent ID Provisioner") + "."
            )
            return
        if setup.decision == "keep-other-agent":
            self.console.info(
                "Kept the setup certificate: another Entrabot agent on this computer uses it."
            )
            self.report.kept.append(
                "This computer's setup (Provisioner) certificate: another Entrabot agent on "
                "this computer still uses it. It is retired when that agent is torn down."
            )
            return
        if setup.decision != "retire" or wincert is None:
            return
        provisioner_certs = _ENTRA_CERTS.format(name=_PROVISIONER_APP)
        for cert in setup.certs:
            removal = (
                directory.remove_provisioner_credential(cert.sha1_hex)
                if directory is not None
                else None
            )
            listed = removal is None or not removal.gone
            why = (removal.detail if removal is not None else "") or (
                "Microsoft Entra could not be reached"
                if removal is None
                else "the change could not be confirmed"
            )
            tail = last4(cert.sha1_hex)
            # The key goes even when Microsoft Entra could not be changed: without it the
            # certificate that stays listed there cannot be used by anyone.
            gone = delete_local_cert(wincert, cert.sha1_hex)
            detail = gone.detail.rstrip(".")
            if gone.status == "failed":
                self.console.warn(f"The setup certificate could not be deleted: {detail}")
                self.report.todo.append(
                    f"This computer's setup (Provisioner) certificate (ending {tail}) could not "
                    f"be deleted from this computer: {detail}. It, and its private key, may "
                    "still be in the Windows certificate store, and it can manage applications "
                    "in your tenant. Delete it in certmgr.msc > Personal > Certificates (named "
                    "'Entrabot Provisioner ...')."
                    + (
                        f" It is also still listed on the Provisioner app: remove it in the "
                        f"{provisioner_certs}."
                        if listed
                        else " It was removed from the Provisioner app, so it can no longer "
                        "sign in there."
                    )
                )
                continue
            if gone.status == "key-remains":
                self.console.warn(
                    "The setup certificate was removed from this computer, but its private key "
                    f"could not be deleted: {detail}"
                )
                if listed:
                    self.console.warn(
                        f"The setup certificate could not be taken off the Provisioner app: {why}"
                    )
                    self.report.todo.append(
                        f"The private key of this computer's setup (Provisioner) certificate "
                        f"(ending {tail}) could not be deleted: {detail}. The certificate is "
                        f"also still listed on the Provisioner app ({why.rstrip('.')}), so that "
                        "key can still sign in to set up and manage applications in your "
                        f"tenant. Remove the certificate in the {provisioner_certs}, and delete "
                        f"the key. {key_removal_advice(gone.ksp)}"
                    )
                else:
                    self.console.ok(
                        "Removed this computer's setup certificate from the Provisioner app"
                    )
                    self.report.removed.append(
                        "This computer's setup (Provisioner) certificate, from the Provisioner "
                        "app in Microsoft Entra and from this computer's certificate store."
                    )
                    self.report.todo.append(
                        f"The private key of this computer's setup (Provisioner) certificate "
                        f"(ending {tail}) could not be deleted: {detail}. The certificate was "
                        "removed from the Provisioner app, so the key can no longer sign in "
                        "with it, but it should not stay on this computer. "
                        f"{key_removal_advice(gone.ksp)}"
                    )
                continue
            if listed:
                self.console.warn(
                    f"The setup certificate could not be taken off the Provisioner app: {why}"
                )
                self.console.ok("Deleted the setup certificate and its private key here")
                self.report.removed.append(
                    "This computer's setup (Provisioner) certificate and its private key, "
                    "from this computer (it is still listed in Microsoft Entra, see below)."
                )
                self.report.todo.append(
                    "This computer's setup certificate is still listed on the Provisioner app "
                    f"(thumbprint ending {last4(cert.sha1_hex)}): {why.rstrip('.')}. Its "
                    "private key has been deleted from this computer, so it cannot be used, "
                    "but it stays listed until it expires or an administrator removes it: "
                    + _ENTRA_CERTS.format(name="EntraBot Agent ID Provisioner")
                    + "."
                )
            else:
                self.console.ok("Retired this computer's setup certificate")
                self.report.removed.append(
                    "This computer's setup (Provisioner) certificate and its private key, "
                    "from the Provisioner app in Microsoft Entra and from this computer."
                )

    def _mention_cloud_memory(self, facts: _Facts) -> None:
        endpoint = facts.env.get("ENTRABOT_BLOB_ENDPOINT", "").strip()
        container = facts.env.get("ENTRABOT_BLOB_CONTAINER", "").strip()
        if not endpoint or not container:
            return
        self.report.kept.append(
            f"The agent's cloud memory ({endpoint}/{container}) was not deleted. To delete "
            "it:  python scripts\\deprovision_blob_storage.py "
            f"--storage-account {_storage_account(endpoint)} --container {container}"
        )

    # ── This computer ────────────────────────────────────────────────────────

    def _delete_here(self, cert: _Cert, *, listed: bool) -> LocalDeletion:
        """Delete *cert* and its private key from this computer, and say what is left.

        *listed*: it is still listed on the Blueprint, so a key that stays can still sign
        the agent in. "Deleted ... and its private key" is said only when both are gone.
        """
        tail = last4(cert.sha1)
        if self.wincert is None:
            gone = LocalDeletion("failed", self.wincert_problem)
        else:
            gone = delete_local_cert(self.wincert, cert.sha1)
        detail = gone.detail.rstrip(".")
        if gone.status == "deleted":
            if cert.retiring:
                self.console.ok(
                    "Deleted the old certificate an earlier rotation left behind, and its "
                    "private key"
                )
                self.report.removed.append(
                    f"The old certificate an earlier certificate rotation left behind (ending "
                    f"{tail}) and its private key, from this computer."
                )
            else:
                self.console.ok("Deleted this computer's certificate and its private key")
                self.report.removed.append("This computer's certificate and its private key.")
        elif gone.status == "key-remains":
            self.console.warn(
                f"{cert.label} was removed from this computer, but its private key could not "
                f"be deleted: {detail}"
            )
            off = (
                ""
                if listed
                else "It is off the agent's Blueprint, so the key can no longer sign the agent "
                "in, but it should not stay on this computer. "
            )
            self.report.todo.append(
                f"{cert.label} (thumbprint ending {tail}) was removed from this computer's "
                f"certificate store, but its private key could not be deleted: {detail}. "
                f"{off}{key_removal_advice(gone.ksp)}"
            )
        elif gone.status == "failed":
            self.console.warn(f"{cert.label} could not be deleted from this computer: {detail}")
            self.report.todo.append(
                f"{cert.label} (thumbprint ending {tail}) could not be deleted from this "
                f"computer: {detail}. It, and its private key, may still be in this account's "
                "Windows certificate store: delete it in certmgr.msc > Personal > Certificates "
                "(named 'Entrabot Blueprint ...')."
            )
        return gone

    def _remove_assistant_entries(self, facts: _Facts) -> None:
        for path in facts.mcp_files:
            outcome = remove_mcp_entry(path)
            if outcome == "removed":
                self.console.ok(f"Removed the entrabot entry from {path}")
                self.report.removed.append(f"The entrabot entry in {path}.")
            elif outcome == "unreadable":
                self.console.warn(f"{path} could not be read as JSON; it was left as it is.")
                self.report.todo.append(
                    f"{path} could not be read, so it was left alone. If it has an "
                    f'"{SERVER_NAME}" entry under "mcpServers", delete that entry by hand.'
                )

    def _backup_then_remove(self, path: Path) -> Path:
        stamp = self.world.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak.{stamp}")
        backup.write_bytes(path.read_bytes())
        try:
            lock_to_current_user(
                backup, console=self.console, platform=self.world.platform, env=self.world.env
            )
        except EnvLockError as exc:
            self.console.debug(f"The backup {backup.name} could not be locked: {exc}")
        path.unlink()
        return backup

    def _remove_settings(self, settings: list[Path]) -> None:
        for path in settings:
            try:
                backup = self._backup_then_remove(path)
            except OSError as exc:
                self.console.warn(f"{path.name} could not be removed: {exc}")
                self.report.todo.append(f"{path} could not be removed: {exc}. Delete it by hand.")
                continue
            if path == self.paths.state_file and self.state_note:
                self._leave_state_note(path, backup)
                continue
            self.console.ok(f"Removed {path.name} (backup: {backup.name})")
            self.report.removed.append(f"{path.name} (a copy is kept as {backup.name}).")

    def _leave_state_note(self, path: Path, backup: Path) -> None:
        """Put back a state file holding only setup's note about the old setup certificates
        teardown could not take off the Provisioner app.

        Setup's retry reads the note from this file and nowhere else, so this is the one
        place it can live; written as setup writes the file, so setup reads it as its own.
        """
        items = self.left_for_setup
        which = _idents(items)
        try:
            path.write_text(json.dumps(self.state_note, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            self.console.warn(f"The note setup needs could not be left in {path.name}: {exc}")
            self.report.removed.append(f"{path.name} (a copy is kept as {backup.name}).")
            self.report.todo.append(
                f"Setup will not try again to remove the old setup (Provisioner) certificate "
                f"({which}): the note it reads could not be left in {path.name} ({exc}). "
                "Remove it by hand, as said above."
            )
            return
        self.console.ok(
            f"Cleared {path.name} down to the note setup needs to retry (backup: {backup.name})"
        )
        self.report.kept.append(
            f"{path.name}, holding only setup's note that the old setup (Provisioner) "
            f"certificate{'s' if len(items) > 1 else ''} ({which}) still "
            f"{'have' if len(items) > 1 else 'has'} to come off the Provisioner app: setup "
            "reads it and tries again on its next run. Everything else in it was removed (a "
            f"copy of the whole file is kept as {backup.name})."
        )

    def _purge_data(self) -> None:
        root = self.paths.data_root
        if not root.is_dir():
            return
        log_file = self.console.log_file
        failures: list[str] = []
        kept_python = False
        kept_log = False
        for child in sorted(root.iterdir()):
            if any(_inside(spot, child) for spot in self.running_from):
                kept_python = True
                continue
            if log_file is not None and _inside(log_file, child):
                # This run's log is open inside this folder: keep it, delete its siblings.
                kept_log = True
                for item in sorted(child.iterdir()):
                    if item.resolve() == log_file.resolve():
                        continue
                    self._delete(item, failures)
                continue
            self._delete(child, failures)
        if failures:
            self.console.warn("Some of the data folder could not be deleted.")
            self.report.todo.append(
                f"Parts of {root} could not be deleted (in use?): "
                + "; ".join(failures[:5])
                + ". Close your assistant and delete the folder by hand."
            )
        else:
            self.console.ok(f"Deleted Entrabot's data in {root}")
            self.report.removed.append(f"Entrabot's data folder contents ({root}).")
        if kept_python:
            self.report.kept.append(
                f"Entrabot's private copy of Python in {root}: this command is running from "
                "it. Delete that folder by hand to remove it too; the project's .venv then "
                f"stops working until {SETUP_COMMAND} rebuilds it."
            )
        else:
            self.report.kept.append(
                "The project's .venv folder. If it was built on Entrabot's private copy of "
                f"Python it no longer works; {SETUP_COMMAND} rebuilds it."
            )
        if kept_log:
            self.report.kept.append(f"This run's log: {log_file}")

    @staticmethod
    def _delete(path: Path, failures: list[str]) -> None:
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            failures.append(f"{path.name} ({type(exc).__name__})")

    # ── Finish ───────────────────────────────────────────────────────────────

    def _finish(self, facts: _Facts) -> int:
        console = self.console
        report = self.report
        if report.todo:
            title = "Teardown finished, with something left to do"
        elif self.delete_agent:
            title = "The agent is deleted and this computer is disconnected"
        else:
            title = "This computer is disconnected"
        console.banner(title)
        if report.removed:
            console.info("Removed:")
            for item in report.removed:
                console.bullet(item)
        if report.kept:
            console.info("Kept:")
            for item in report.kept:
                console.bullet(item)
        if not self.delete_agent:
            console.info(
                f"The agent {facts.agent} still exists in your tenant. To use it from this "
                f"computer again, run {SETUP_COMMAND}."
            )
        if report.todo:
            console.blank()
            console.warn("Still to do:")
            for item in report.todo:
                console.bullet(item)
            return EXIT_INCOMPLETE
        return EXIT_OK


def run_teardown(
    console: Console,
    paths: Paths,
    world: World,
    *,
    delete_agent: bool = False,
    purge_data: bool = False,
    keep_setup_credential: bool = False,
    running_from: Sequence[Path] | None = None,
) -> int:
    """Disconnect this computer (and optionally delete the agent, or the local data).

    Returns the process exit code: 0 done or nothing to do, 1 finished with something left
    (said precisely), 2 usage (no confirmation possible, or no agent to delete), 130 cancelled.
    """
    return _Teardown(
        console,
        paths,
        world,
        delete_agent=delete_agent,
        purge_data=purge_data,
        keep_setup_credential=keep_setup_credential,
        running_from=running_from,
    ).run()
