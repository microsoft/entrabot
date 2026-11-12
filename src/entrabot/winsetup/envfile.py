"""Read, rewrite and lock ``.env``.

The runtime (``entrabot.config``) trims keys and values, skips comments, and keeps the FIRST
value of a repeated key. Setup has to read the file the same way, or it would decide things
from a value the agent never uses. It also has to *replace* lines, never append: a stale line
left above a new one is the one the agent would use.

Every write is protected. An empty temp file is created beside ``.env`` and restricted to
the current user; only then is the new content written to it, and it is moved into place.
A rename keeps the file's own permissions, so ``.env`` is never, even briefly, an ordinary
new file, whoever does the writing (setup, a repair, certificate rotation), and the new
content never sits in a file that still carries the folder's permissions.

"Restricted" on Windows means the access list is REPLACED by exactly one entry: this user,
modify. Removing inheritance and granting the user is not enough: a new file in a project
folder carries SYSTEM and Administrators as explicit entries, which that leaves in place
(see ``_winacl``). It is done in-process, so a write starts no other program. Whether a file
really is restricted is answered by reading its access list back (:func:`lock_problem`),
never by the fact that locking returned.
"""

from __future__ import annotations

import contextlib
import locale
import os
import re
import stat
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from entrabot.winsetup import _winacl
from entrabot.winsetup.console import Console


class EnvLockError(RuntimeError):
    """``.env`` could not be restricted to the current user."""


@dataclass
class EnvGuard:
    """What a write to ``.env`` does when the file can't be restricted to the current user.

    By default a failed lock stops the write and leaves the old file as it was. A caller that
    would rather finish and say so (setup: the file holds IDs, not secrets) sets
    *tolerate_failure*; each failure is then recorded in *problems*.

    *console*, *platform* and *env* are accepted for callers written against the first
    version and are not used: locking is done in-process, by the operating system the file
    is really on.
    """

    console: Console | None = None
    platform: str | None = None
    env: Mapping[str, str] | None = None
    tolerate_failure: bool = False
    problems: list[str] = field(default_factory=list)


def _read_text(path: Path) -> str:
    """UTF-8 with or without a byte-order mark; a hand-saved ANSI file is read as best it can.

    Mirrors ``entrabot.config._read_dotenv_text`` so setup and the runtime agree.
    """
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False), errors="replace")


def _key_of(line: str) -> str:
    """The key a line sets, or '' for a comment, a blank line, or a line with no '='."""
    text = line.strip()
    if not text or text.startswith("#") or "=" not in text:
        return ""
    return text.partition("=")[0].strip()


def read_env(path: Path) -> dict[str, str]:
    """The values the runtime would see in *path*. A missing file is empty."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in _read_text(path).splitlines():
        key = _key_of(line)
        if key and key not in values:
            values[key] = line.strip().partition("=")[2].strip()
    return values


def update_env(
    path: Path,
    set: Mapping[str, str],  # noqa: A002 - the name is part of the shared setup interface
    remove: Iterable[str] = (),
    drop_comment_prefixes: Iterable[str] = (),
    *,
    guard: EnvGuard | None = None,
) -> None:
    """Rewrite *path* in place, restricted to the current user.

    Every existing line for a key in *set* or *remove* is dropped, every other line is kept in
    order, and the *set* lines are appended. Comment lines starting with one of
    *drop_comment_prefixes* are dropped too. The result is UTF-8 without a byte-order mark,
    with LF newlines, and replaces the old file atomically: a failed write leaves it as it was.

    The temp file is locked to the current user while it is still empty, then written, then
    moved over the old file (see the module docstring). *guard* says what a failed lock does;
    the default is strict (the old file is left as it was) and needs no console.
    """
    guard = guard if guard is not None else EnvGuard()
    gone = {*set, *remove}
    prefixes = tuple(drop_comment_prefixes)
    kept: list[str] = []
    if path.is_file():
        for line in _read_text(path).splitlines():
            if _key_of(line) in gone:
                continue
            if prefixes and line.strip().startswith(prefixes):
                continue
            kept.append(line)
    lines = [*kept, *(f"{key}={value}" for key, value in set.items())]
    text = "".join(f"{line}\n" for line in lines)

    path.parent.mkdir(parents=True, exist_ok=True)
    handle: int | None
    handle, temp_name = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=path.parent)
    try:
        # Lock first, while the temp file is still empty, and only then write: the settings
        # are never in a file that carries the folder's permissions, not even briefly.
        try:
            lock_to_current_user(
                Path(temp_name), console=guard.console, platform=guard.platform, env=guard.env
            )
        except EnvLockError as exc:
            if not guard.tolerate_failure:
                raise
            guard.problems.append(str(exc).replace(Path(temp_name).name, path.name))
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as temp:
            handle = None  # the file object owns it now, and closes it
            temp.write(text)
        os.replace(temp_name, path)
    except BaseException:
        if handle is not None:  # Windows can't delete a file that is still open
            with contextlib.suppress(OSError):
                os.close(handle)
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


MODIFY_RIGHTS = "0x1301bf"  # read, write, execute, delete: what icacls calls (M)
# The well-known accounts a folder's permissions usually name, in SDDL shorthand.
_SDDL_NAMES = {
    "SY": "SYSTEM",
    "BA": "Administrators",
    "BU": "Users",
    "AU": "Authenticated Users",
    "WD": "Everyone",
    "IU": "Interactive users",
    "CO": "Creator Owner",
    "S-1-5-18": "SYSTEM",
    "S-1-5-32-544": "Administrators",
    "S-1-5-32-545": "Users",
    "S-1-5-11": "Authenticated Users",
    "S-1-1-0": "Everyone",
}
_SDDL_FLAGS = re.compile(r"[A-Z_]*")
# The kinds of entry an access list (DACL) can hold, by their SDDL names. "X" and "Z" are the
# conditional (callback) kinds, whose condition is in parentheses of its own.
_SDDL_ALLOW = frozenset({"A", "OA", "XA", "ZA"})
_SDDL_DENY = frozenset({"D", "OD", "XD"})


@dataclass(frozen=True)
class _Dacl:
    """An access list as setup checks it: inheritance off or not, and who is allowed in."""

    protected: bool
    allowed: tuple[str, ...]  # who is allowed in, as SDDL prints them (a SID or a 2-letter name)
    unknown: int = 0  # entries of a kind setup does not know, so can't vouch for
    everyone: bool = False  # no access list at all (a NULL DACL): nothing is restricted


def _sddl_entries(text: str) -> list[str] | None:
    """The ``(...)`` entries at the start of *text*, nested parentheses kept whole. None when
    anything else is there, or a parenthesis is left open: half-read is never a pass."""
    entries: list[str] = []
    index = 0
    while index < len(text) and text[index] == "(":
        depth, start = 0, index
        while index < len(text):
            depth += {"(": 1, ")": -1}.get(text[index], 0)
            index += 1
            if depth == 0:
                break
        if depth != 0:
            return None
        entries.append(text[start + 1 : index - 1])
    # Only a system access list (SACL, "S:") may follow, and it is not about who can read.
    if index < len(text) and not text.startswith("S:", index):
        return None
    return entries


def _parse_sddl_dacl(sddl: str) -> _Dacl | None:
    """The access list in *sddl*, or None when it can't be read with certainty.

    The owner and group come first and never contain a colon, so the first ``D:`` is where the
    access list starts (``G:DUD:P...`` is the group DU, then the list).
    """
    start = (sddl or "").find("D:")
    if start < 0:
        return None
    rest = sddl[start + 2 :]
    flags_match = _SDDL_FLAGS.match(rest)
    flags = flags_match.group(0) if flags_match else ""
    entries = _sddl_entries(rest[len(flags) :])
    if entries is None:
        return None
    if "NO_ACCESS_CONTROL" in flags:
        return _Dacl(protected=False, allowed=(), everyone=True)
    allowed: list[str] = []
    unknown = 0
    for entry in entries:
        # type;flags;rights;object;inherited object;account[;condition]
        parts = entry.split(";", 6)
        if len(parts) < 6 or not parts[5]:
            return None
        kind = parts[0].upper()
        if kind in _SDDL_ALLOW:
            allowed.append(parts[5])
        elif kind not in _SDDL_DENY:
            unknown += 1  # an entry setup can't interpret is never assumed harmless
    return _Dacl(protected="P" in flags, allowed=tuple(allowed), unknown=unknown)


def _is_windows() -> bool:
    """Is the file system this process writes to a Windows one? The real operating system
    decides how a file is locked, not a setting: a file is locked where it actually is."""
    return sys.platform == "win32"


def private_sddl(sid: str) -> str:
    """The access list of a locked file: inheritance off, one entry, *sid* with modify.

    Modify, not read-only: the next setup run and certificate rotation update the file.
    """
    return f"D:P(A;;{MODIFY_RIGHTS};;;{sid})"


def current_user_sid(
    console: Console | None = None, *, env: Mapping[str, str] | None = None
) -> str:
    """The Windows SID of the account running setup, read from this process's own token.

    A SID, not ``DOMAIN\\name``: name lookup is unreliable on Entra-joined machines.
    Raises :class:`EnvLockError` off Windows or when the token can't be read.
    """
    if not _is_windows():
        raise EnvLockError("Only a Windows account has a SID.")
    try:
        return _winacl.current_user_sid()
    except OSError as exc:
        raise EnvLockError(f"Could not read the current user's SID: {exc}") from exc


def lock_to_current_user(
    path: Path,
    *,
    console: Console | None = None,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Make *path* readable and writable by the current user and nobody else.

    Windows: the file's access list is replaced by :func:`private_sddl`, in one call,
    whatever it held before. POSIX: ``chmod 600``. Raises :class:`EnvLockError` if the file
    could not be locked. *console*, *platform* and *env* are accepted and not used.

    :func:`update_env` already does this on every write. This stays public for a file that
    was put in place some other way (a backup, for example).
    """
    try:
        if _is_windows():
            _winacl.set_dacl(path, private_sddl(_winacl.current_user_sid()))
        else:
            os.chmod(path, 0o600)
    except OSError as exc:
        raise EnvLockError(f"Could not restrict {path.name} to your account: {exc}") from exc


def read_sddl(
    path: Path, *, console: Console | None = None, env: Mapping[str, str] | None = None
) -> str:
    """The file's access list as text (SDDL), which names accounts by SID and does not change
    with the display language. Windows only. Raises :class:`EnvLockError` when it can't be
    read."""
    if not _is_windows():
        raise EnvLockError("Only a Windows file has an access list to read.")
    try:
        return _winacl.read_dacl_sddl(path)
    except OSError as exc:
        raise EnvLockError(str(exc)) from exc


def sddl_lock_problem(
    sddl: str, sid: str, *, resolve: Callable[[str], str] | None = None
) -> str | None:
    """Why the permissions in *sddl* are not "this user only", or None when they are.

    Locked means: inheritance is switched off, the user has an entry of their own, and nobody
    else is allowed in.

    Accounts are compared as SIDs, never as text. SDDL prints well-known accounts under
    two-letter names (the built-in Administrator is ``LA``), so the name in the text and the
    user's SID can be the same account spelled differently. *resolve* turns such a name into
    its SID (``_winacl.canonical_sid`` on Windows). A name it can't resolve is somebody else:
    unknown never counts as the user.

    The whole list is read, a conditional entry's own parentheses included, so no entry can
    hide the ones after it. A list that can't be read with certainty, an entry of a kind this
    does not know, and a missing list (everyone allowed) are all problems, never a pass.
    """
    dacl = _parse_sddl_dacl(sddl)
    if dacl is None:
        return "its permissions could not be understood"
    if dacl.everyone:
        return "it has no access list at all, so Everyone can open it"

    def as_sid(trustee: str) -> str:
        if resolve is None or trustee.upper().startswith("S-1-"):
            return trustee.upper()
        try:
            return resolve(trustee).upper()
        except OSError:
            return trustee.upper()

    mine = False
    others: list[str] = []
    for trustee in dacl.allowed:
        account = as_sid(trustee)
        if account == sid.upper():
            mine = True
            continue
        name = _SDDL_NAMES.get(trustee.upper()) or _SDDL_NAMES.get(account) or trustee
        if name not in others:
            others.append(name)
    problems: list[str] = []
    if others:
        problems.append(f"these also have access: {', '.join(others)}")
    if dacl.unknown:
        problems.append(f"it has {dacl.unknown} entry(s) of a kind setup cannot check")
    if not dacl.protected:
        problems.append("it inherits permissions from its folder")
    if not mine:
        problems.append("your account has no entry of its own")
    return "; ".join(problems) or None


def lock_problem(
    path: Path,
    *,
    console: Console | None = None,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Read the file's permissions back. None when only the current user can read or change
    it; otherwise what is wrong, in words. Anything that can't be read is a problem, never a
    pass: this is what backs the sentence "only your account can read it".

    *console*, *platform* and *env* are accepted and not used.
    """
    if not _is_windows():
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError as exc:
            return f"its permissions could not be read ({exc})"
        return None if mode & 0o077 == 0 else f"other accounts can open it (mode {mode:o})"
    try:
        sid = current_user_sid()
        sddl = read_sddl(path)
    except EnvLockError as exc:
        return f"its permissions could not be read ({exc})"
    return sddl_lock_problem(sddl, sid, resolve=_winacl.canonical_sid)
