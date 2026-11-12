"""Shared fakes for the Windows setup wizard tests.

Nothing here touches the network, the certificate store, ``az`` or a real ``.env``: child
processes are replaced by :class:`FakeSpawn`, which answers from a script of rules.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from entrabot.winsetup.console import Console, RunRequest


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
    """

    users: list[dict[str, object]] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)  # the decoded lookups, in order
    urls: list[str] = field(default_factory=list)  # the raw URLs az was given
    fail_filters: tuple[str, ...] = ()  # filters Graph rejects (e.g. "proxyAddresses")

    def matches(self, request: RunRequest) -> bool:
        argv = request.argv
        return "rest" in argv and "--url" in argv and "/users" in argv[argv.index("--url") + 1]

    def attach(self, spawn: FakeSpawn) -> None:
        spawn.on(self.matches, self)

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
            wanted = path.split(marker, 1)[1].lower()
            self.asked.append(f"id {wanted}")
            for user in self.users:
                if wanted in (str(user["id"]).lower(), str(user["userPrincipalName"]).lower()):
                    return Reply(stdout=json.dumps(shaped(user)))
            return Reply(returncode=3, stderr="ERROR: Resource does not exist")

        condition = query.get("$filter", "")
        self.asked.append(condition)
        if any(word in condition for word in self.fail_filters):
            return Reply(returncode=1, stderr="ERROR: Unsupported or invalid query filter clause")
        return Reply(
            stdout=json.dumps(
                {"value": [shaped(user) for user in self.users if _user_matches(user, condition)]}
            )
        )


def _user_matches(user: dict[str, object], condition: str) -> bool:
    import re

    def text(key: str) -> str:
        return str(user.get(key) or "")

    def literal(found: re.Match[str]) -> str:
        return found.group(1).replace("''", "'")  # OData doubles a quote inside a string

    def many(key: str) -> list[str]:
        value = user.get(key) or []
        return [str(item) for item in value] if isinstance(value, list) else []

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
    acls: dict[str, str] = field(default_factory=dict)
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

    def canonical_sid(self, text: str) -> str:
        """What Windows does with the two-letter names SDDL uses for well-known accounts:
        ``LA`` is this machine's built-in Administrator (the account whose SID ends -500)."""
        if text.upper().startswith("S-1-"):
            return text.upper()
        machine = "-".join(self.sid.split("-")[:7])  # S-1-5-21-a-b-c
        known = {
            "SY": "S-1-5-18",
            "BA": "S-1-5-32-544",
            "BU": "S-1-5-32-545",
            "WD": "S-1-1-0",
            "LA": f"{machine}-500",
        }
        if text.upper() not in known:
            raise OSError(f"ConvertStringSidToSidW failed for {text!r}")
        return known[text.upper()]

    def set_dacl(self, path: Path, sddl: str) -> None:
        self.events.append(f"lock {Path(path).name}")
        if self.fail_set:
            raise OSError(self.fail_set)
        if self.leaves_others:
            sddl = f"D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;0x1301bf;;;{self.sid})"
        self.acls[str(path)] = sddl

    def read_dacl_sddl(self, path: Path) -> str:
        self.events.append(f"read {Path(path).name}")
        if self.fail_read:
            raise OSError(self.fail_read)
        return self.acls.get(str(path), self.new_file_sddl())

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
