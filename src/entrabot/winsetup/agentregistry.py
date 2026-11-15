"""Optional, the second half of Agent 365 integration: the agent's entry in the Agent 365
registry, so Microsoft lists it in the Microsoft 365 admin center (Agents > All agents >
Registry). The first half is telemetry (:mod:`entrabot.winsetup.telemetry`).

Setup does it in two places, both only when the person says yes to Agent 365 integration:

1. The permission. ``scripts/entra_provisioning.py`` runs with
   ``ENTRABOT_GRANT_AGENT_REGISTRATION=1`` and adds the Microsoft Graph application permission
   ``AgentRegistration.ReadWrite.All`` to the setup (Provisioner) app, with admin consent,
   through the signed-in administrator's Azure CLI: the way it gives the app its other
   permissions. It prints one ``ENTRABOT_REGISTRY_PERMISSION=`` line (:func:`permission_from`).
   A setup that says no never adds it.
2. The registration, after telemetry: ``scripts/register_agent_instance.py --apply``, with the
   setup app's token, in a child process. It creates the registration, or updates the one
   recorded in the state file (``AGENT_REGISTRATION_ID``), and records the new ID itself.

The registry has no lookup by agent, so what a computer has not recorded it cannot know. A
computer that joined an agent that existed before it (``state.JOINED_EXISTING_KEY``) does not
create a registration: another computer may have made one already. It updates one it has
recorded, and the person can record one made elsewhere (``--registration-id``).

A failure never stops setup and never undoes telemetry. No token passes through the wizard.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from entrabot.a365.registry import ADMIN_CENTER_PLACE, REQUIRED_ROLE, STATE_KEY
from entrabot.winsetup.console import Console
from entrabot.winsetup.helpers import RESULT_MARKER, Helpers, marked_line
from entrabot.winsetup.state import JOINED_EXISTING_KEY, IdentityError, read_state
from entrabot.winsetup.words import Words

SCRIPT = "register_agent_instance.py"
GRANT_ENV = "ENTRABOT_GRANT_AGENT_REGISTRATION"
# Printed by scripts/entra_provisioning.py; tests on both sides pin it.
PERMISSION_MARKER = "ENTRABOT_REGISTRY_PERMISSION="
# When the permission was granted in this run and the setup app's token does not carry it
# yet: about a minute and a half more, on top of the bootstrap's own wait.
RETRY_WAITS: tuple[float, ...] = (15, 30, 45)
REGISTERED = ("created", "updated", "recreated")
__all__ = [
    "ADMIN_CENTER_PLACE",
    "GRANT_ENV",
    "PERMISSION_MARKER",
    "Permission",
    "Registration",
    "delete_registration",
    "permission_from",
    "recorded",
    "register_agent",
    "script_command",
]


@dataclass(frozen=True)
class Permission:
    """What the bootstrap did about ``AgentRegistration.ReadWrite.All`` in this run.

    ``status``: ``granted`` (in this run; on an app setup took over, by granting this one
    permission on its own), ``present`` (already), ``failed``, or ``unknown`` (no line was
    printed). ``skipped`` came from older bootstraps, which gave a taken-over app nothing."""

    status: str
    reason: str = ""

    @property
    def held(self) -> bool:
        return self.status in ("granted", "present")


@dataclass(frozen=True)
class Registration:
    """What the registration step did. ``outcome`` is the script's (``created``,
    ``updated``, ``recreated``, ``permission-missing``, ``no-access``, ``refused``,
    ``could-not-check``, ``not-ours``, ``no-id``, ``bad-id``, ``usage``), or ``joined``
    when this computer did not register an agent that existed before it, or ``failed``
    when the script said nothing."""

    outcome: str
    registration_id: str = ""
    reason: str = ""

    @property
    def registered(self) -> bool:
        return self.outcome in REGISTERED


def permission_from(output: str) -> Permission:
    """The bootstrap's line about the registry permission, from its *output*."""
    raw = marked_line(output, PERMISSION_MARKER)
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return Permission("unknown")
    return Permission(str(data.get("status") or "unknown"), str(data.get("reason") or ""))


def recorded(state_file: Path) -> tuple[str, bool]:
    """(the registration ID this computer recorded, whether it joined an existing agent)."""
    try:
        values = read_state(state_file)
    except IdentityError:
        return "", False
    return values.get(STATE_KEY, "").strip(), values.get(JOINED_EXISTING_KEY, "") == "true"


def script_command(words: Words, *args: str) -> str:
    """How a person runs the registration script on this system, from the project folder."""
    python = r".venv\Scripts\python.exe" if words.windows else ".venv/bin/python"
    script = rf"scripts\{SCRIPT}" if words.windows else f"scripts/{SCRIPT}"
    return " ".join([python, script, *args])


def _run(helpers: Helpers, args: Sequence[str], heartbeat: str) -> Registration:
    result = helpers.script(
        SCRIPT, [*args, "--print-result"], capture=True, quiet=True, heartbeat=heartbeat
    )
    raw = marked_line(result.stdout, RESULT_MARKER)
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        tail = " ".join((result.stderr or result.stdout or "").strip().splitlines()[-3:])
        said = tail[-300:] or f"the registration script exited {result.returncode}"
        return Registration("failed", reason=said)
    return Registration(
        str(data.get("outcome") or "failed"),
        str(data.get("id") or ""),
        str(data.get("reason") or ""),
    )


def register_agent(
    *,
    state_file: Path,
    helpers: Helpers,
    console: Console,
    owner_ids: Sequence[str],
    permission: Permission,
    sleep: Callable[[float], None],
    waits: tuple[float, ...] = RETRY_WAITS,
) -> Registration:
    """Register the agent, or update its recorded registration. Never raises for a refusal:
    the result says what happened."""
    registration_id, joined = recorded(state_file)
    if joined and not registration_id:
        return Registration("joined")
    args = ["--apply", *[part for owner in owner_ids for part in ("--owner-id", owner)]]
    heartbeat = "Still registering the agent in Agent 365"
    result = _run(helpers, args, heartbeat)
    if result.outcome == "permission-missing" and permission.status == "granted":
        # Granted moments ago: Microsoft can take a little while to put it in new tokens.
        for wait in waits:
            console.detail(
                "Microsoft does not include the new permission in the setup app's sign-in yet. "
                f"Trying again in {wait:.0f}s."
            )
            sleep(wait)
            result = _run(helpers, args, heartbeat)
            if result.outcome != "permission-missing":
                break
    if result.outcome == "permission-missing" and permission.reason:
        result = replace(
            result,
            reason=f"setup could not give its app {REQUIRED_ROLE}: {permission.reason}",
        )
    return result


def delete_registration(helpers: Helpers) -> Registration:
    """Delete the registration this computer recorded (teardown, when the agent is deleted)."""
    return _run(helpers, ["--delete", "--apply"], "Deleting the agent's Agent 365 registration")
