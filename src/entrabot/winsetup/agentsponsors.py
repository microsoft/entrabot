"""The agent's sponsors in the directory: who it actually takes instructions from in Teams.

At runtime the source of truth is the Agent Identity's ``sponsors`` relationship in Microsoft
Entra (``entrabot.identity.sponsors``): the sponsor gate matches inbound Teams senders
against that list. The sponsor lines in ``.env`` are read by other code (who to write to),
but they do not decide who the agent obeys. A new agent's only sponsor is whoever created it.

So setup reads that list, adds the people the person chose who are not on it yet, and reads
it back. It only ever adds: removing a sponsor is a deliberate act with its own script.

The Graph calls run in a child process with the provisioner's token (see
``helpers.SPONSOR_SNIPPET``); the signed-in user's Azure CLI token is refused by the Agent
Identity APIs. The people to add are passed as object IDs that setup has already resolved,
so there is exactly one answer to "who is pat@contoso.com" in a run.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from entrabot.winsetup.helpers import RESULT_MARKER, SPONSOR_SNIPPET, Helpers, Paths, marked_line

_PLAIN_ARGUMENT = re.compile(r"^[A-Za-z0-9@._+=:/\\-]+$")
# Characters that PowerShell ($ `) or cmd (% !) still act on inside double quotes, and the
# double quote itself. An address can legally hold any of them except '"'.
_EXPANDS_IN_QUOTES = re.compile(r'[$`%!"]')


@dataclass(frozen=True)
class DirectorySponsor:
    object_id: str
    display_name: str = ""
    upn: str = ""
    mail: str = ""

    @property
    def label(self) -> str:
        """How to name this person on screen: an address when there is one."""
        if self.mail:
            return self.mail
        if "#EXT#" in self.upn:
            # name_domain.com#EXT#@tenant.onmicrosoft.com was made from name@domain.com
            local, _, domain = self.upn.split("#EXT#", 1)[0].rpartition("_")
            if local and domain:
                return f"{local}@{domain}"
        return self.upn or self.display_name or self.object_id


@dataclass(frozen=True)
class SponsorSync:
    """What the directory said before and after, and what happened to each requested add."""

    read: bool = False  # the directory could be read at all
    before: tuple[DirectorySponsor, ...] = ()
    after: tuple[DirectorySponsor, ...] = ()
    added: tuple[str, ...] = ()  # object IDs added AND seen in the read-back
    unconfirmed: tuple[str, ...] = ()  # added, but the read-back does not show them
    failed: Mapping[str, str] = field(default_factory=dict)  # object ID -> why
    error: str = ""

    @property
    def problems(self) -> bool:
        return not self.read or bool(self.failed) or bool(self.unconfirmed)

    def was_sponsor(self, object_id: str) -> bool:
        return any(item.object_id.lower() == object_id.lower() for item in self.before)

    def is_sponsor(self, object_id: str) -> bool:
        return any(item.object_id.lower() == object_id.lower() for item in self.after)


def _sponsors(items: Any) -> tuple[DirectorySponsor, ...]:
    found = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("id"):
            found.append(
                DirectorySponsor(
                    object_id=str(item["id"]),
                    display_name=str(item.get("displayName") or ""),
                    upn=str(item.get("userPrincipalName") or ""),
                    mail=str(item.get("mail") or ""),
                )
            )
    return tuple(found)


def sync_sponsors(
    helpers: Helpers,
    agent_object_id: str,
    add_object_ids: Sequence[str],
    *,
    env: Mapping[str, str],
) -> SponsorSync:
    """Read the agent's sponsors, add *add_object_ids* that are missing, and read back.

    With nothing to add this is a plain read. Never raises: what went wrong is in the result.
    """
    if not agent_object_id:
        return SponsorSync(error="The agent's directory ID is not recorded on this computer.")
    wanted = list(dict.fromkeys(item for item in add_object_ids if item))
    result = helpers.snippet(
        SPONSOR_SNIPPET, [str(helpers.paths.scripts), agent_object_id, *wanted], env=env
    )
    raw = marked_line(result.stdout, RESULT_MARKER)
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        last = (result.stderr.strip().splitlines() or ["the helper printed nothing"])[-1]
        return SponsorSync(error=last[:400])

    failed = {str(key): str(value) for key, value in (data.get("failed") or {}).items()}
    if data.get("before") is None or data.get("after") is None:
        return SponsorSync(failed=failed, error=str(data.get("error") or "no sponsor list"))
    after = _sponsors(data["after"])
    listed = {item.object_id.lower() for item in after}
    attempted = [str(item) for item in data.get("added") or []]
    return SponsorSync(
        read=True,
        before=_sponsors(data["before"]),
        after=after,
        added=tuple(item for item in attempted if item.lower() in listed),
        unconfirmed=tuple(item for item in attempted if item.lower() not in listed),
        failed=failed,
        error=str(data.get("error") or ""),
    )


def _script_command(paths: Paths, script: str, address: str, object_id: str = "") -> str:
    python = paths.python
    with contextlib.suppress(ValueError):  # outside the project folder: keep the full path
        python = python.relative_to(paths.project_root)
    program = str(python) if python.is_absolute() else "\\".join(python.parts)
    if address and _EXPANDS_IN_QUOTES.search(address) and object_id:
        # No quoting writes such an address the same way for both shells, and a copy that
        # expands "$x" names somebody else. The object ID is the same person everywhere,
        # and both scripts accept one.
        address = object_id
    elif address and not _PLAIN_ARGUMENT.match(address):
        # An apostrophe starts a string in PowerShell and "&" separates commands in cmd:
        # inside double quotes both shells take them literally.
        address = f'"{address}"'
    return f"{program} scripts\\{script} {address}"


def list_command(paths: Paths) -> str:
    """The exact command that lists the agent's sponsors, run from the project folder."""
    return _script_command(paths, "list_sponsors.py", "").strip()


def add_command(paths: Paths, address: str, *, object_id: str = "") -> str:
    """The exact command that adds *address* as a sponsor by hand, run from the project
    folder. *object_id* is used instead where a shell would change the address."""
    return _script_command(paths, "add_agent_sponsor.py", address, object_id)


def remove_command(paths: Paths, address: str, *, object_id: str = "") -> str:
    """The exact command that removes *address* from the agent's sponsors. *object_id* is
    used instead where a shell would change the address."""
    return _script_command(paths, "remove_agent_sponsor.py", address, object_id)
