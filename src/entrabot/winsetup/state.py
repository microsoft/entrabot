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


def apply_identity_choice(path: Path, choice: IdentityChoice, *, now: datetime) -> list[str]:
    """Bring the state file in line with *choice*. Returns notes worth showing the person."""
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
        kept = {
            key: value
            for key, value in state.items()
            if key.startswith("PROVISIONER") or key == "TENANT_ID"
        }
        kept["BLUEPRINT_APP_ID"] = choice.blueprint_app_id
        _write_state(path, kept)
        return [
            f"Switching agents. Backed up the previous record to {backup.name}.",
            "This machine's certificate on the previous Blueprint is NOT revoked. "
            "Ask an administrator to remove it if this machine should lose access to that agent.",
        ]
    if current != choice.blueprint_app_id:
        state["BLUEPRINT_APP_ID"] = choice.blueprint_app_id
        _write_state(path, state)
    return []


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
