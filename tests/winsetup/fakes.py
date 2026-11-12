"""Shared fakes for the Windows setup wizard tests.

Nothing here touches the network, the certificate store, ``az`` or a real ``.env``: child
processes are replaced by :class:`FakeSpawn`, which answers from a script of rules.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from entrabot.winsetup._winacl import ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE, Ace, Dacl
from entrabot.winsetup.console import Console, RunRequest

# The SDDL names for accounts, as Windows resolves them when it turns SDDL into a binary list.
# LA ("this machine's built-in Administrator") is relative to the machine: see dacl_from_sddl.
_ACCOUNT_ALIASES = {
    "SY": "S-1-5-18",
    "BA": "S-1-5-32-544",
    "BU": "S-1-5-32-545",
    "AU": "S-1-5-11",
    "WD": "S-1-1-0",
}
_RIGHTS = {"FA": 0x1F01FF, "FR": 0x120089, "FW": 0x120116, "FX": 0x1200A0, "GA": 0x10000000}
_ACE_TYPES = {"A": ACCESS_ALLOWED_ACE_TYPE, "D": ACCESS_DENIED_ACE_TYPE}


def dacl_from_sddl(sddl: str, *, user: str = "S-1-5-21-1-2-3-1001") -> Dacl:
    """The binary access list Windows would store for *sddl*, the way the product reads it
    back: every account as its numeric SID (``LA`` becomes the machine's ``-500`` account,
    the machine being *user*'s). Only the simple forms the tests write are understood:
    ``D:[P][AI](A|D;flags;rights;;;account)...``; anything else is a test bug."""
    match = re.fullmatch(r"D:(?P<flags>[A-Z]*)(?P<aces>(?:\([^()]*\))*)", sddl)
    assert match, f"the fake does not understand this SDDL: {sddl!r}"
    machine = "-".join(user.split("-")[:7])  # S-1-5-21-a-b-c
    entries: list[Ace] = []
    for body in re.findall(r"\(([^()]*)\)", match.group("aces")):
        kind, flags, rights, _object, _inherited, account = body.split(";")
        sid = {**_ACCOUNT_ALIASES, "LA": f"{machine}-500"}.get(account.upper(), account.upper())
        entries.append(
            Ace(
                type=_ACE_TYPES[kind],
                flags=0x10 if "ID" in flags else 0,
                mask=_RIGHTS.get(rights) or int(rights, 16),
                sid=sid,
            )
        )
    return Dacl(present=True, protected="P" in match.group("flags"), entries=tuple(entries))


@dataclass
class Reply:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


Rule = tuple[Callable[[RunRequest], bool], Reply | Callable[[RunRequest], Reply]]


@dataclass
class FakeSpawn:
    """Stands in for the real process launcher. The first matching rule answers."""

    rules: list[Rule] = field(default_factory=list)
    calls: list[RunRequest] = field(default_factory=list)
    default: Reply = field(default_factory=Reply)

    def on(
        self,
        needle: str | Sequence[str] | Callable[[RunRequest], bool],
        reply: Reply | Callable[[RunRequest], Reply],
    ) -> None:
        """Answer any command whose argv contains every string in *needle*.

        *needle* may also be a predicate, for when a temp path could contain the words.
        The most recently added rule wins.
        """
        if callable(needle):
            self.rules.insert(0, (needle, reply))
            return
        needles = [needle] if isinstance(needle, str) else list(needle)

        def matches(request: RunRequest) -> bool:
            joined = " ".join(request.argv)
            return all(item in joined for item in needles)

        self.rules.insert(0, (matches, reply))

    def __call__(
        self,
        request: RunRequest,
        on_stdout: Callable[[str], None],
        on_stderr: Callable[[str], None],
    ) -> int:
        self.calls.append(request)
        reply: Reply = self.default
        for matches, candidate in self.rules:
            if matches(request):
                reply = candidate(request) if callable(candidate) else candidate
                break
        for line in reply.stdout.splitlines():
            on_stdout(line)
        for line in reply.stderr.splitlines():
            on_stderr(line)
        return reply.returncode

    def ran(self, needle: str) -> list[RunRequest]:
        return [call for call in self.calls if needle in " ".join(call.argv)]


def make_console(
    *,
    spawn: FakeSpawn | None = None,
    answers: Mapping[str, str] | None = None,
    typed: str | None = None,
    assume_yes: bool = False,
    log_file: Path | None = None,
) -> tuple[Console, io.StringIO]:
    """A console that writes to a string and never colours or uses non-ASCII glyphs.

    *typed* is what a person types, line by line; with it the console is interactive.
    Without it the console is non-interactive unless *answers* drive it.
    """
    out = io.StringIO()
    console = Console(
        stdin=io.StringIO(typed or ""),
        stdout=out,
        interactive=typed is not None,
        assume_yes=assume_yes,
        log_file=log_file,
        answers=answers,
        spawn=spawn or FakeSpawn(),
        color=False,
        unicode=False,
    )
    return console, out


def script(name: str) -> Callable[[RunRequest], bool]:
    """Matches a command that runs the helper script called *name*."""
    return lambda request: any(Path(arg).name == name for arg in request.argv[:3])


def words(*expected: str) -> Callable[[RunRequest], bool]:
    """Matches a command whose argv holds *expected* as consecutive arguments."""
    wanted = list(expected)

    def matches(request: RunRequest) -> bool:
        argv = request.argv
        return any(argv[i : i + len(wanted)] == wanted for i in range(len(argv)))

    return matches


def snippet(marker: str) -> Callable[[RunRequest], bool]:
    """Matches ``python -c <code>`` where the code contains *marker*."""
    return lambda request: (
        len(request.argv) >= 3 and request.argv[1] == "-c" and marker in request.argv[2]
    )


def program(name: str) -> Callable[[RunRequest], bool]:
    """Matches a command by the file name of the program it starts, whichever slashes the
    path uses. Unlike a substring match, a temp folder's name can't satisfy it by accident."""
    return lambda request: PureWindowsPath(request.argv[0]).name.lower() == name.lower()


@dataclass
class FakeDirectory:
    """Answers ``az rest`` calls to Microsoft Graph ``/users`` the way Graph would.

    It decodes the URL exactly as a web server does, so a filter that was not URL-encoded
    properly behaves here as it would for real: an unencoded ``#`` cuts the request short.
    ``proxyAddresses`` is matched case-sensitively, the stricter reading of Graph.

    :meth:`attach_cli` answers the Azure CLI's own ``ad user show`` and ``ad user list``
    commands from the same users: the lookups an ``az.cmd`` install makes without a URL.
    """

    users: list[dict[str, object]] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)  # the decoded lookups, in order
    urls: list[str] = field(default_factory=list)  # the raw URLs az was given
    fail_filters: tuple[str, ...] = ()  # filters Graph rejects (e.g. "proxyAddresses")
    # Characters Graph refuses in a /users/{sign-in name} lookup. An apostrophe
    # (o'brien@contoso.com) is reported to fail there; in a $filter the same name is found.
    refused_in_path: str = ""

    def matches(self, request: RunRequest) -> bool:
        argv = request.argv
        return "rest" in argv and "--url" in argv and "/users" in argv[argv.index("--url") + 1]

    def attach(self, spawn: FakeSpawn) -> None:
        spawn.on(self.matches, self)

    def attach_cli(self, spawn: FakeSpawn) -> None:
        """Answer ``az ad user show --id X`` and ``az ad user list --filter F``."""
        spawn.on(words("ad", "user", "show"), self._cli_show)
        spawn.on(words("ad", "user", "list"), self._cli_list)

    def _by_id(
        self,
        wanted: str,
        shaped: Callable[[dict[str, object]], dict[str, object]] = lambda user: user,
    ) -> Reply:
        """One user by object ID or sign-in name, the way Graph's /users/{id} answers."""
        self.asked.append(f"id {wanted}")
        if any(character in wanted for character in self.refused_in_path):
            return Reply(returncode=1, stderr="ERROR: Bad Request: Invalid object identifier")
        for user in self.users:
            if wanted in (str(user["id"]).lower(), str(user["userPrincipalName"]).lower()):
                return Reply(stdout=json.dumps(shaped(user)))
        return Reply(returncode=3, stderr="ERROR: Resource does not exist")

    def _filtered(self, condition: str) -> list[dict[str, object]] | None:
        """The users an OData filter matches; None when Graph rejects the filter."""
        self.asked.append(condition)
        if any(word in condition for word in self.fail_filters):
            return None
        return [user for user in self.users if _user_matches(user, condition)]

    def _cli_show(self, request: RunRequest) -> Reply:
        return self._by_id(request.argv[request.argv.index("--id") + 1].lower())

    def _cli_list(self, request: RunRequest) -> Reply:
        found = self._filtered(request.argv[request.argv.index("--filter") + 1])
        if found is None:
            return Reply(returncode=1, stderr="ERROR: Unsupported or invalid query filter clause")
        return Reply(stdout=json.dumps(found))

    def __call__(self, request: RunRequest) -> Reply:
        from urllib.parse import parse_qs, unquote, urlsplit

        raw = request.argv[request.argv.index("--url") + 1]
        self.urls.append(raw)
        parts = urlsplit(raw)  # everything after a raw "#" is a fragment and never sent
        path = unquote(parts.path)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        fields = (query.get("$select") or "").split(",") if query.get("$select") else None

        def shaped(user: dict[str, object]) -> dict[str, object]:
            return {k: v for k, v in user.items() if fields is None or k in fields}

        marker = "/v1.0/users/"
        if marker in path:
            return self._by_id(path.split(marker, 1)[1].lower(), shaped)

        found = self._filtered(query.get("$filter", ""))
        if found is None:
            return Reply(returncode=1, stderr="ERROR: Unsupported or invalid query filter clause")
        return Reply(stdout=json.dumps({"value": [shaped(user) for user in found]}))


def _user_matches(user: dict[str, object], condition: str) -> bool:
    def text(key: str) -> str:
        return str(user.get(key) or "")

    def literal(found: re.Match[str]) -> str:
        return found.group(1).replace("''", "'")  # OData doubles a quote inside a string

    def many(key: str) -> list[str]:
        value = user.get(key) or []
        return [str(item) for item in value] if isinstance(value, list) else []

    if found := re.fullmatch(r"userPrincipalName eq '(.*)'", condition):
        return text("userPrincipalName").lower() == literal(found).lower()
    if found := re.fullmatch(r"mail eq '(.*)'", condition):
        return text("mail").lower() == literal(found).lower()
    if found := re.fullmatch(r"otherMails/any\(x:x eq '(.*)'\)", condition):
        return literal(found).lower() in [item.lower() for item in many("otherMails")]
    if found := re.fullmatch(r"proxyAddresses/any\(x:x eq '(.*)'\)", condition):
        return literal(found) in many("proxyAddresses")
    if found := re.fullmatch(r"startswith\(userPrincipalName,'(.*)'\)", condition):
        return text("userPrincipalName").lower().startswith(literal(found).lower())
    raise AssertionError(f"the fake directory does not understand this filter: {condition!r}")


@dataclass
class FakeWindowsAcl:
    """Windows file permissions, in memory, so the lock logic runs the same on every OS.

    It models what was measured on Windows 11: a new file in a project folder carries SYSTEM,
    Administrators and the user as EXPLICIT entries; a rename keeps a file's own entries; and
    (``leaves_others``) a "lock" that only rewrites the user's entry leaves the other two.
    """

    sid: str = "S-1-5-21-1-2-3-1001"
    # What each file's access list was set to: SDDL text as setup wrote it, or a Dacl a test
    # puts there directly for a shape SDDL can't easily say.
    acls: dict[str, str | Dacl] = field(default_factory=dict)
    events: list[str] = field(
        default_factory=list
    )  # "lock <name>", "read <name>", "replace <name>"
    fail_set: str | None = None  # setting permissions fails with this message
    fail_read: str | None = None  # reading permissions fails with this message
    leaves_others: bool = False  # the old icacls behaviour: other accounts keep their access

    def new_file_sddl(self) -> str:
        return f"D:(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;{self.sid})"

    def current_user_sid(self) -> str:
        return self.sid

    def set_dacl(self, path: Path, sddl: str) -> None:
        self.events.append(f"lock {Path(path).name}")
        if self.fail_set:
            raise OSError(self.fail_set)
        if self.leaves_others:
            sddl = f"D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;0x1301bf;;;{self.sid})"
        self.acls[str(path)] = sddl

    def read_dacl(self, path: Path) -> Dacl:
        """What Windows hands back: the binary list, every account as a numeric SID."""
        self.events.append(f"read {Path(path).name}")
        if self.fail_read:
            raise OSError(self.fail_read)
        stored = self.acls.get(str(path), self.new_file_sddl())
        return stored if isinstance(stored, Dacl) else dacl_from_sddl(stored, user=self.sid)

    def install(self, monkeypatch: object) -> None:
        """Make ``entrabot.winsetup.envfile`` behave as on Windows, against this store."""
        import os

        from entrabot.winsetup import envfile

        real_replace = os.replace

        def replace(src: object, dst: object, **kwargs: object) -> None:
            real_replace(src, dst, **kwargs)  # type: ignore[arg-type]
            self.events.append(f"replace {Path(str(dst)).name}")
            if str(src) in self.acls:  # a rename keeps the file's own permissions
                self.acls[str(dst)] = self.acls.pop(str(src))
            else:
                self.acls.pop(str(dst), None)

        monkeypatch.setattr(envfile, "_is_windows", lambda: True)  # type: ignore[attr-defined]
        monkeypatch.setattr(envfile, "_winacl", self)  # type: ignore[attr-defined]
        monkeypatch.setattr(envfile.os, "replace", replace)  # type: ignore[attr-defined]
