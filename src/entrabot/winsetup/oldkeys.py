"""Older setup keys: Provisioner certificates an earlier install left trusted in Microsoft Entra.

An older setup kept the Provisioner's private key in a plain file. On Windows
``scripts/entra_provisioning.py`` moves this computer onto a key in the certificate store and
then removes the old certificate from the Provisioner app, which holds tenant-wide Microsoft
Graph permissions. A removal that fails is recorded in ``.entrabot-state.json`` by
``_retire_old_certificates`` and retried on every run: lowercase SHA-1 thumbprints,
comma-separated, under :data:`REVOKE_PENDING`, and their expiry dates in the same order ("" when
unknown) under :data:`REVOKE_PENDING_EXPIRES`. Teardown with ``--keep-setup-credential`` can
leave a state file that holds only that note.

The wizard, the health check (``entrabot.preflight``), ``status`` and the status script
(``scripts/show_agent_status.py``) all read the record here, so they agree on what it says.
Only the last four characters of a thumbprint are ever kept: enough to find the certificate in
the admin center, not enough to look it up. Nothing here imports ``entrabot.config``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import NamedTuple

from entrabot.winsetup.state import IdentityError, read_state

REVOKE_PENDING = "PROVISIONER_CERT_REVOKE_PENDING"
REVOKE_PENDING_EXPIRES = "PROVISIONER_CERT_REVOKE_PENDING_EXPIRES"
PROVISIONER_APP_NAME = "EntraBot Agent ID Provisioner"  # the app registration's display name
ENTRA_ADMIN_CENTER = "https://entra.microsoft.com"
CHECK_NAME = "Older setup keys"  # the health check's name, in diagnose and in status

_SHA1_THUMBPRINT = re.compile(r"[0-9A-Fa-f]{40}")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_RETRY = "Setup will try again the next time it runs."


@dataclass(frozen=True)
class OldSetupKey:
    """An older setup's Provisioner certificate that Microsoft Entra may still trust. Only the
    end of its thumbprint is kept: enough to find it in the admin center, not to look it up."""

    ending: str  # the thumbprint's last four characters, upper case
    expires: str  # YYYY-MM-DD, or "" when the record does not say


@dataclass(frozen=True)
class OldSetupKeys:
    """What the state file says about older setup keys that could not be switched off."""

    keys: tuple[OldSetupKey, ...] = ()
    unreadable: bool = False  # something is recorded that can't be read as a certificate
    app_id: str = ""  # the Provisioner app's application ID
    current: str = ""  # the end of this computer's own Provisioner certificate's thumbprint

    @property
    def pending(self) -> bool:
        return bool(self.keys) or self.unreadable


class HealthFinding(NamedTuple):
    """An older setup key, as a health check reports it: what is wrong, and what to do."""

    detail: str
    fix: str


def _text(value: object) -> str:
    """A recorded value as text, the way ``state.read_state`` turns it into text."""
    return "" if value is None else str(value).strip()


def _recorded_date(text: str) -> str:
    if not _ISO_DATE.fullmatch(text):
        return ""
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def old_setup_keys(recorded: Mapping[str, object]) -> OldSetupKeys:
    """The older Provisioner certificates the state file says are still on the app.

    Read as ``_pending_revocations`` in ``scripts/entra_provisioning.py`` reads them: blank
    entries are skipped, and expiry dates line up with thumbprints by position. An entry that
    is not a thumbprint is not dropped silently: the record is then *unreadable*, and the person
    is asked to check, because what it named may still be trusted. *recorded* is the state file
    as a mapping; a value that is missing or None counts as empty.
    """
    thumbprints = [part.strip() for part in _text(recorded.get(REVOKE_PENDING)).split(",")]
    expiries = [part.strip() for part in _text(recorded.get(REVOKE_PENDING_EXPIRES)).split(",")]
    keys: list[OldSetupKey] = []
    seen: set[str] = set()
    unreadable = False
    for index, thumbprint in enumerate(thumbprints):
        if not thumbprint:
            continue
        if not _SHA1_THUMBPRINT.fullmatch(thumbprint):
            unreadable = True
            continue
        if thumbprint.upper() in seen:
            continue
        seen.add(thumbprint.upper())
        expires = expiries[index] if index < len(expiries) else ""
        keys.append(OldSetupKey(ending=thumbprint[-4:].upper(), expires=_recorded_date(expires)))
    current = _text(recorded.get("PROVISIONER_CERT_THUMBPRINT"))
    return OldSetupKeys(
        keys=tuple(keys),
        unreadable=unreadable,
        app_id=_text(recorded.get("PROVISIONER_CLIENT_ID")),
        current=current[-4:].upper() if _SHA1_THUMBPRINT.fullmatch(current) else "",
    )


def read_old_setup_keys(path: Path) -> OldSetupKeys:
    """The record in the state file at *path*. No file is nothing pending. A file that can't
    be read is treated like a record that can't: nobody can tell what it named, so the
    person is asked to check."""
    try:
        return old_setup_keys(read_state(path))
    except IdentityError:
        return OldSetupKeys(unreadable=True)


def listed(items: Sequence[str]) -> str:
    """``A``, ``A and B``, ``A, B and C``."""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _app(note: OldSetupKeys) -> str:
    return f"'{PROVISIONER_APP_NAME}'" + (f" (application ID {note.app_id})" if note.app_id else "")


def _named(note: OldSetupKeys) -> list[str]:
    return [
        f"{key.ending} ({f'expires {key.expires}' if key.expires else 'expiry date unknown'})"
        for key in note.keys
    ]


def _certificates(note: OldSetupKeys) -> str:
    """Which certificates, by the end of their thumbprints and their expiry."""
    named = _named(note)
    if len(named) == 1:
        return f"the certificate whose thumbprint ends in {named[0]}"
    return f"the certificates whose thumbprints end in {listed(named)}"


def _keep(note: OldSetupKeys) -> list[str]:
    if not note.current:
        return []
    return [f"Keep the one whose thumbprint ends in {note.current}: this computer uses it now."]


def old_setup_key_lines(note: OldSetupKeys) -> list[str]:
    """The warning about older setup keys, in plain words: the first line says what is wrong,
    the rest which key it is, why it matters and how to remove it. Empty when nothing is
    pending."""
    if not note.pending:
        return []
    where = [f"  App registrations > {_app(note)} >", "  Certificates & secrets > Certificates."]
    if not note.keys:
        # Nothing to retry: setup does not know which certificate the record meant.
        return [
            "Setup could not read its record of older setup keys for this computer.",
            "Microsoft Entra may still trust a key that an earlier version of setup kept in an "
            "unprotected file.",
            f"Check by hand in the Microsoft Entra admin center ({ENTRA_ADMIN_CENTER}):",
            *where,
            "Remove any certificate there that you can't account for.",
            *_keep(note),
        ]
    named = _named(note)
    if len(note.keys) == 1:
        lines = [
            "An older setup key for this computer could not be switched off in Microsoft Entra.",
            "An earlier version of setup kept that key in an unprotected file.",
            "Anyone with a copy of that file can still use it to make changes in your tenant.",
            f"It is the certificate whose thumbprint ends in {named[0]}.",
            f"Remove it by hand in the Microsoft Entra admin center ({ENTRA_ADMIN_CENTER}):",
        ]
    else:
        lines = [
            f"{len(note.keys)} older setup keys for this computer could not be switched off in "
            "Microsoft Entra.",
            "An earlier version of setup kept those keys in unprotected files.",
            "Anyone with a copy of those files can still use them to make changes in your tenant.",
            f"They are the certificates whose thumbprints end in {listed(named)}.",
            f"Remove them by hand in the Microsoft Entra admin center ({ENTRA_ADMIN_CENTER}):",
        ]
    lines += where
    if note.unreadable:
        lines += [
            "Setup's record also has an entry it could not read: remove any other certificate "
            "there that you can't account for.",
            *_keep(note),
        ]
    lines.append(_RETRY)
    return lines


def old_setup_key_check(note: OldSetupKeys) -> HealthFinding | None:
    """The same warning for a health check: one line saying which keys could not be switched
    off (by the end of their thumbprints, with their expiry), and one saying where in the
    admin center to remove them and that setup tries again. None when nothing is pending."""
    if not note.pending:
        return None
    where = (
        f"the Microsoft Entra admin center ({ENTRA_ADMIN_CENTER}): App registrations > "
        f"{_app(note)} > Certificates & secrets > Certificates"
    )
    keep = _keep(note)
    if not note.keys:
        return HealthFinding(
            detail="Setup could not read its record of older setup keys for this computer. "
            "Microsoft Entra may still trust a key that an earlier version of setup kept in "
            "an unprotected file.",
            fix=" ".join(
                [
                    f"Check by hand in {where}, and remove any certificate there that you "
                    "can't account for.",
                    *keep,
                ]
            ),
        )
    one = len(note.keys) == 1
    kept = "its key in an unprotected file" if one else "their keys in unprotected files"
    detail = (
        f"{'An older setup key' if one else f'{len(note.keys)} older setup keys'} for this "
        f"computer could not be switched off in Microsoft Entra: {_certificates(note)}. An "
        f"earlier version of setup kept {kept}."
    )
    others: list[str] = []
    if note.unreadable:
        detail += " Setup's record also has an entry it could not read."
        others = ["Also remove any other certificate there that you can't account for.", *keep]
    fix = " ".join([f"Remove {'it' if one else 'them'} by hand in {where}.", *others, _RETRY])
    return HealthFinding(detail=detail, fix=fix)


def old_setup_key_waiting(note: OldSetupKeys) -> str:
    """One line for a folder that holds nothing but setup's note: which older setup keys are
    still waiting to be switched off, and that setup tries again when it runs here. Empty
    when nothing is pending."""
    if not note.pending:
        return ""
    look = (
        f"the certificates of the {_app(note)} app in the Microsoft Entra admin center "
        f"({ENTRA_ADMIN_CENTER})"
    )
    if not note.keys:
        return (
            f"Setup could not read its record of an older setup key here: check {look}, and "
            "remove any you can't account for."
        )
    one = len(note.keys) == 1
    line = (
        f"{'An older setup key is' if one else f'{len(note.keys)} older setup keys are'} still "
        f"waiting to be switched off in Microsoft Entra: {_certificates(note)}. Setup will try "
        "again the next time it runs here."
    )
    if note.unreadable:
        line += f" Part of the record could not be read: also check {look}."
    return line
