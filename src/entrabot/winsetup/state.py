"""Identity mode: the state file rules and the environment handed to the provisioning helper.

``scripts/create_entra_agent_ids.py`` takes no command line arguments. It is steered by
``.entrabot-state.json`` and three environment variables, exactly as ``scripts/setup.sh``
(lines 757-882) does it:

* ``ENTRABOT_NEW_CHAIN=1``  skip every "find an existing one" lookup and create a fresh chain.
* ``_ENTRABOT_UPN_SUFFIX``  the Agent User becomes ``entrabot-agent-<suffix>@<domain>``.
* ``ENTRABOT_AGENT_USER_UPN``  use exactly this Agent User (a suffix outranks it).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

IdentityMode = Literal["new", "join", "continue"]

# Left behind in a shell by an earlier run; never inherited.
_IDENTITY_ENV_KEYS = ("ENTRABOT_NEW_CHAIN", "_ENTRABOT_UPN_SUFFIX", "ENTRABOT_AGENT_USER_UPN")

_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_SUFFIX = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
_UPN = re.compile(r"^[A-Za-z0-9._%+#-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


class IdentityError(Exception):
    """The identity choice can't be applied as asked."""


@dataclass(frozen=True)
class IdentityChoice:
    """Which agent this run is for.

    ``new``       create a fresh Blueprint, Agent Identity and Agent User.
    ``join``      attach this machine to an existing Blueprint, by App ID.
    ``continue``  carry on with the agent already recorded on this machine (the repair path).
    """

    mode: IdentityMode
    blueprint_app_id: str = ""
    upn_suffix: str = ""
    agent_user_upn: str = ""
    switching: bool = False  # join: the Blueprint differs from the one recorded here


def validate_app_id(value: str) -> str | None:
    if _GUID.match(value.strip()):
        return None
    return "An App ID looks like 1a2b3c4d-1234-5678-9abc-def012345678."


def validate_suffix(value: str) -> str | None:
    if _SUFFIX.match(value):
        return None
    return (
        "Use 1-32 lowercase letters, digits or dashes, starting and ending with a letter or digit."
    )


def validate_upn(value: str) -> str | None:
    if _UPN.match(value.strip()):
        return None
    return "That should look like an email address, for example entrabot-agent@contoso.com."


def default_suffix(username: str) -> str:
    """A valid suffix made from the Windows username, so Enter is a usable answer."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", username.strip().lower()).strip("-")[:32].strip("-")
    return cleaned or "agent"


def read_state(path: Path) -> dict[str, str]:
    """The state file as a flat dict. Missing is empty; corrupt is an error."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise IdentityError(
            f"{path.name} can't be read ({type(exc).__name__}). Move it aside and run setup again."
        ) from exc
    if not isinstance(data, dict):
        raise IdentityError(f"{path.name} isn't a JSON object. Move it aside and run setup again.")
    return {str(key): "" if value is None else str(value) for key, value in data.items()}


def _write_state(path: Path, data: Mapping[str, str]) -> None:
    handle, temp_name = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as temp:
            temp.write(json.dumps(dict(data), indent=2))
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def _backup(path: Path, now: datetime) -> Path:
    backup = path.with_name(f"{path.name}.bak.{now.strftime('%Y%m%d-%H%M%S')}")
    backup.write_bytes(path.read_bytes())
    return backup


def _this_computers(key: str) -> bool:
    """Is *key* this computer's setup rather than one agent's? The provisioner app is a
    helper for this machine, and the tenant stays the same: a switch to another agent keeps
    both. Only setup's provisioning code (``scripts/entra_provisioning.py``) writes them."""
    return key.startswith("PROVISIONER") or key == "TENANT_ID"


def _agent_record(state: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in state.items() if not _this_computers(key)}


_ENV_CERTIFICATE_KEYS = ("ENTRABOT_BLUEPRINT_CERT_SHA1", "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT")
_NOT_REVOKED = (
    "This machine's certificate on the previous Blueprint is NOT revoked. "
    "Ask an administrator to remove it if this machine should lose access to that agent."
)


def certificate_recorded_on(
    blueprint_app_id: str, *, state: Mapping[str, str], env_values: Mapping[str, str]
) -> bool:
    """Does what this computer recorded say that its certificate is (or may be) registered on
    the Blueprint *blueprint_app_id*? *state* is the state file; *env_values* is ``.env``.

    These facts, and nothing else, say yes:

    * ``.env`` holds a Blueprint certificate (``ENTRABOT_BLUEPRINT_CERT_SHA1`` or
      ``ENTRABOT_BLUEPRINT_CERT_THUMBPRINT``) and names that Blueprint in
      ``ENTRABOT_BLUEPRINT_APP_ID``. Setup writes the three together
      (``envstep.write_identity_env``), and only after the certificate step registered that
      certificate on that Blueprint, found it there, or could not confirm the registration
      (which may still have landed).
    * The state file records ``BLUEPRINT_CERT_THUMBPRINT`` beside that ``BLUEPRINT_APP_ID``:
      ``scripts/register_blueprint_cert.py`` (``setup.sh``) writes it there once it has
      registered the certificate on the Blueprint that record names.

    A certificate whose record names no Blueprint at all counts too: nothing ties it to
    another one. A run that only recorded a Blueprint App ID (one that stopped at the agent
    step, say) leaves none of these behind.
    """
    wanted = blueprint_app_id.strip().lower()

    def names_it(recorded: str) -> bool:  # that Blueprint, or none at all
        return recorded.strip().lower() in ("", wanted)

    if state.get("BLUEPRINT_CERT_THUMBPRINT", "").strip() and names_it(
        state.get("BLUEPRINT_APP_ID", "")
    ):
        return True
    return any(env_values.get(key, "").strip() for key in _ENV_CERTIFICATE_KEYS) and names_it(
        env_values.get("ENTRABOT_BLUEPRINT_APP_ID", "")
    )


def apply_identity_choice(
    path: Path,
    choice: IdentityChoice,
    *,
    now: datetime,
    env_values: Mapping[str, str] | None = None,
) -> list[str]:
    """Bring the state file in line with *choice*. Returns notes worth showing the person.

    *env_values* is what ``.env`` records. A switch to another agent says that this
    computer's certificate on the previous Blueprint is not revoked only when the record says
    there is one (:func:`certificate_recorded_on`); a caller that passes None can't tell, and
    the note is kept.
    """
    state = read_state(path)

    if choice.mode == "continue":
        if not state.get("BLUEPRINT_APP_ID"):
            raise IdentityError("There is no agent recorded on this machine to continue with.")
        return []

    if choice.mode == "new":
        if not path.is_file():
            return []
        backup = _backup(path, now)
        # The provisioner app is a helper for this machine, not part of the agent's identity.
        _write_state(path, {k: v for k, v in state.items() if k.startswith("PROVISIONER")})
        return [f"Backed up the previous agent's record to {backup.name}."]

    if not choice.blueprint_app_id:
        raise IdentityError("Joining an existing agent needs its Blueprint App ID.")
    current = state.get("BLUEPRINT_APP_ID", "")
    if current and current.lower() != choice.blueprint_app_id.lower():
        backup = _backup(path, now)
        kept = {key: value for key, value in state.items() if _this_computers(key)}
        kept["BLUEPRINT_APP_ID"] = choice.blueprint_app_id
        _write_state(path, kept)
        notes = [f"Switching agents. Backed up the previous record to {backup.name}."]
        if env_values is None or certificate_recorded_on(
            current, state=state, env_values=env_values
        ):
            notes.append(_NOT_REVOKED)
        return notes
    if current != choice.blueprint_app_id:
        state["BLUEPRINT_APP_ID"] = choice.blueprint_app_id
        _write_state(path, state)
    return []


def undo_join(path: Path, *, before: Mapping[str, str], applied: Mapping[str, str]) -> bool:
    """Take back the Blueprint a join recorded, once the agent step has said it does not exist.

    *before* is the state file as it was before :func:`apply_identity_choice` recorded the
    Blueprint to join, and *applied* is the file as that left it. The agent's part of the
    record goes back to *before*, so the next run is not a switch away from an agent that was
    never there. This computer's part (:func:`_this_computers`) is kept as it is now: the
    provisioning step may have changed it in this run (a new setup key, an old one still to
    be switched off, the tenant), and that must not be lost.

    Nothing is written when the agent's part is not exactly what the join left: something
    else wrote to it since, and that is not this function's to undo. Returns True when the
    file was changed.
    """
    current = read_state(path)
    if _agent_record(current) != _agent_record(applied):
        return False
    restored = {key: value for key, value in current.items() if _this_computers(key)}
    restored.update(_agent_record(before))
    if restored == current:
        return False
    _write_state(path, restored)
    return True


def identity_environment(choice: IdentityChoice, *, base: Mapping[str, str]) -> dict[str, str]:
    """The environment for ``create_entra_agent_ids.py``: *base* plus this run's identity choice.

    Identity variables already in *base* are dropped first. A different agent gets an explicit
    empty ``ENTRABOT_AGENT_USER_UPN`` when none was asked for: the helper loads ``.env`` for
    any variable that is not set, and ``.env`` still names the previous agent.
    """
    env = {key: value for key, value in base.items() if key not in _IDENTITY_ENV_KEYS}

    if choice.mode == "continue":
        return env

    if choice.mode == "new":
        if not choice.agent_user_upn and not choice.upn_suffix:
            raise IdentityError("A new agent needs a short name (or a full Agent User address).")
        env["ENTRABOT_NEW_CHAIN"] = "1"
        env["ENTRABOT_AGENT_USER_UPN"] = choice.agent_user_upn
        if not choice.agent_user_upn:
            env["_ENTRABOT_UPN_SUFFIX"] = choice.upn_suffix
        return env

    if choice.agent_user_upn:
        env["ENTRABOT_AGENT_USER_UPN"] = choice.agent_user_upn
    elif choice.upn_suffix:
        env["_ENTRABOT_UPN_SUFFIX"] = choice.upn_suffix
    elif choice.switching:
        env["ENTRABOT_AGENT_USER_UPN"] = ""
    return env
