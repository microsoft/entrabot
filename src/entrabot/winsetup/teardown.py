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
store (off the Blueprint, and deleted with its key); when it is not, it is reported.

What can't be done is said precisely, with what to do about it. If Microsoft Entra can't be
reached the local cleanup still completes; a certificate that stays listed there has lost
its private key and cannot be used. A private key that could not be deleted is never
reported as deleted: it is a to-do with how to remove it, and teardown exits 1.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Sequence
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
        for cert in facts.certs:
            self._disconnect_certificate(facts, cert, access, directory, agent_deleted)
        self._remove_assistant_entries(facts)
        if self.delete_agent and not agent_deleted:
            # A second attempt at deleting the agent needs the settings and the setup
            # certificate, so they stay until the agent is really gone.
            self.report.kept.append(
                "This folder's settings (.env, .entrabot-state.json) and the setup "
                "certificate: deleting the agent again needs them."
            )
        else:
            # Last of the Microsoft Entra steps: everything above signed in with it.
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
        if access.problem and (facts.cert_sha1 or self.delete_agent):
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
                "and the Azure CLI. The project's .venv will need setup to rebuild it."
            )
        console.blank()
        if not self.delete_agent:
            untouched = f"Not touched: the agent itself ({facts.agent} still exists in your tenant)"
            if not self.purge_data:
                untouched += ", its memory, and Entrabot's data folder on this computer"
            console.info(untouched + ".")

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

    def _disconnect_certificate(
        self,
        facts: _Facts,
        cert: _Cert,
        access: Access,
        directory: Directory | None,
        agent_deleted: bool,
    ) -> None:
        """Take one certificate off the Blueprint (unless the agent is gone), then off this
        computer with its private key, and report exactly what is left."""
        why_listed = (
            None if agent_deleted else self._take_off_blueprint(facts, cert, access, directory)
        )
        local = self._delete_here(cert, listed=why_listed is not None)
        if why_listed is not None:
            self.report.todo.append(
                self._still_listed(facts, cert, why_listed, key_gone=local.clean)
            )

    def _take_off_blueprint(
        self, facts: _Facts, cert: _Cert, access: Access, directory: Directory | None
    ) -> str | None:
        """Remove *cert* from the Blueprint. Returns why it is still listed, or None when it
        is gone or was deliberately left (which is reported here)."""
        unverified = cert.here is not True
        if unverified and (cert.retiring or not self.remove_unverified):
            self._leave_unverified(facts, cert)
            return None
        if not facts.blueprint_object_id:
            return "this folder has no record of the Blueprint."
        if directory is None:
            assert access.problem is not None
            self.console.warn(f"Could not reach Microsoft Entra. {access.problem[0]}")
            return access.problem[0]
        removal = directory.remove_blueprint_credential(facts.blueprint_object_id, cert.sha1)
        if removal.gone:
            if cert.retiring:
                self.console.ok(
                    "Removed the old certificate an earlier certificate rotation left behind "
                    "from the agent's Blueprint"
                    if removal.removed
                    else "The old certificate an earlier rotation left behind was no longer "
                    "listed on the agent's Blueprint"
                )
                self.report.removed.append(
                    f"The old certificate an earlier certificate rotation left behind (ending "
                    f"{last4(cert.sha1)}), from the agent's Blueprint in Microsoft Entra."
                )
            elif unverified:
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
            return None
        why = removal.detail or "the change could not be confirmed"
        self.console.warn(f"The certificate could not be taken off the Blueprint: {why}")
        return f"{why.rstrip('.')}."

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
            self.console.ok(f"Removed {path.name} (backup: {backup.name})")
            self.report.removed.append(f"{path.name} (a copy is kept as {backup.name}).")

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
