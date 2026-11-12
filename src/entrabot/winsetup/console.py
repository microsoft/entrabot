"""All of the wizard's terminal I/O: what is shown, what is asked, and what children print.

Everything goes through one :class:`Console` so that

* tests can script the answers and read what a person would have seen;
* every line shown also lands in the setup log, with secrets taken out first;
* a terminal that can't draw a character (a cp1252 console, a redirected file) gets plain
  ASCII instead of a ``UnicodeEncodeError`` halfway through setup.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TextIO


class Cancelled(Exception):
    """The person stopped the wizard: Ctrl+C, end of input, or "no" at the review screen."""


@dataclass(frozen=True)
class Choice:
    key: str
    label: str
    hint: str = ""


@dataclass(frozen=True)
class MissingAnswer:
    """A question that had no default and nobody to ask (``--yes`` or no terminal)."""

    key: str
    prompt: str
    flag: str


@dataclass(frozen=True)
class RunRequest:
    """One child process to start."""

    argv: list[str]
    env: Mapping[str, str] | None
    cwd: Path | None
    interactive: bool = False
    input_text: str | None = None
    heartbeat_seconds: float | None = None
    silent: bool = False  # nothing the child prints is on screen, so it never counts as "busy"


class RunResult(subprocess.CompletedProcess[str]):
    """What a child printed. ``output`` is both streams, in the order the lines arrived;
    ``shown`` says whether the person already saw them."""

    output: str = ""
    shown: bool = True


# A launcher starts the process, feeds each output line to a callback, and returns the exit code.
Spawn = Callable[[RunRequest, Callable[[str], None], Callable[[str], None]], int]

_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_SHA1_HEX = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{40}(?![0-9A-Fa-f])")
_SHA256_HEX = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")
# A SHA-256 thumbprint in base64 (43 characters, as in a JWT's x5t#S256) looks like any other
# 43-character word, so it is only masked where something calls it a thumbprint, and only
# when it has the shape of one: 32 random bytes mix upper and lower case, and in 43
# characters they leave the last two bits zero, so the last character is one of
# A E I M Q U Y c g k o s w 0 4 8. Standard base64 (with "+" and "/") must carry its "="
# padding, which keeps a path from passing for one.
_LABELLED_THUMBPRINT = re.compile(
    r"((?i:thumbprint|x5t(?:#s256|_s256)?)[^\n]{0,40}?)"
    r"(?<![A-Za-z0-9_+/-])"
    r"(?=[A-Za-z0-9_+/-]*[a-z])(?=[A-Za-z0-9_+/-]*[A-Z])"
    r"(?:([A-Za-z0-9_-]{42}[AEIMQUYcgkosw048])=?|([A-Za-z0-9+/]{42}[AEIMQUYcgkosw048])=)"
    r"(?![A-Za-z0-9_+/=-])"
)
_KEY_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_KEY_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")


def redact(text: str, *, ellipsis: str = "...") -> str:
    """Take tokens and full certificate thumbprints out of *text*.

    A thumbprint keeps its last four characters, which is enough to tell two certificates
    apart and not enough to look one up. Covered: SHA-1 and SHA-256 thumbprints in hex
    (Entra echoes one back in errors such as AADSTS700027), and a base64 SHA-256 thumbprint
    where it is labelled as one.
    """
    text = _JWT.sub("[token hidden]", text)
    text = _LABELLED_THUMBPRINT.sub(
        lambda match: match.group(1) + ellipsis + (match.group(2) or match.group(3))[-4:], text
    )
    for digest in (_SHA256_HEX, _SHA1_HEX):  # SHA-256 and SHA-1 thumbprints, in hex
        text = digest.sub(lambda match: ellipsis + match.group(0)[-4:].upper(), text)
    return text


_ASCII_GLYPHS = {"ok": "[ok]", "warn": "[!!]", "fail": "[xx]", "bullet": "-", "rule": "="}
_UNICODE_GLYPHS = {"ok": "✓", "warn": "!", "fail": "✗", "bullet": "•", "rule": "─"}
_COLORS = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "cyan": "\033[36m",
}
_RESET = "\033[0m"


def _enable_windows_vt() -> bool:
    """Turn on ANSI colour in a classic Windows console. True when it worked.

    Needs confirmation on real Windows: conhost before Windows 10 1511 refuses the mode.
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # any failure just means "no colour"
        return False


def _isatty(stream: object) -> bool:
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        return False


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").rstrip("\r\n")


def _pump(stream: IO[bytes], sink: Callable[[str], None], touch: Callable[[], None]) -> None:
    for raw in iter(stream.readline, b""):
        touch()
        sink(_decode(raw))
    stream.close()


def spawn_process(
    request: RunRequest,
    on_stdout: Callable[[str], None],
    on_stderr: Callable[[str], None],
    *,
    on_idle: Callable[[float], None] | None = None,
) -> int:
    """Run a child, hand its output over line by line, and return its exit code.

    Output is read as bytes and decoded as UTF-8 with replacement, so a child that prints in
    the console code page can't raise here. A program that can't be started is reported as
    exit 127 with the reason on stderr, the way a shell would.
    """
    if request.interactive:
        try:
            return subprocess.run(  # noqa: S603 - argv is built by the wizard, never a shell string
                request.argv,
                env=dict(request.env) if request.env is not None else None,
                cwd=request.cwd,
                check=False,
            ).returncode
        except OSError as exc:
            on_stderr(f"Could not start {request.argv[0]}: {exc}")
            return 127

    try:
        process = subprocess.Popen(  # noqa: S603 - argv is built by the wizard, never a shell string
            request.argv,
            env=dict(request.env) if request.env is not None else None,
            cwd=request.cwd,
            stdin=subprocess.PIPE if request.input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        on_stderr(f"Could not start {request.argv[0]}: {exc}")
        return 127

    last_output = [time.monotonic()]
    started = time.monotonic()
    lock = threading.Lock()

    def touch() -> None:
        last_output[0] = time.monotonic()

    def saw_output() -> None:
        # Output nobody can see is not a sign of life: the heartbeat keeps its own time.
        if not request.silent:
            touch()

    def locked(sink: Callable[[str], None]) -> Callable[[str], None]:
        def write(line: str) -> None:
            with lock:
                sink(line)

        return write

    assert process.stdout is not None and process.stderr is not None
    readers = [
        threading.Thread(
            target=_pump, args=(process.stdout, locked(on_stdout), saw_output), daemon=True
        ),
        threading.Thread(
            target=_pump, args=(process.stderr, locked(on_stderr), saw_output), daemon=True
        ),
    ]
    for reader in readers:
        reader.start()

    try:
        if request.input_text is not None and process.stdin is not None:
            with contextlib.suppress(OSError):
                process.stdin.write(request.input_text.encode("utf-8"))
                process.stdin.close()
        interval = request.heartbeat_seconds
        while True:
            try:
                process.wait(timeout=0.1 if interval else None)
                break
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                if interval and on_idle and now - last_output[0] >= interval:
                    with lock:
                        on_idle(now - started)
                    touch()
    except KeyboardInterrupt:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        raise
    finally:
        for reader in readers:
            reader.join(timeout=5)
    return process.returncode


class Console:
    """The wizard's terminal. See the module docstring."""

    def __init__(
        self,
        *,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        interactive: bool | None = None,
        assume_yes: bool = False,
        log_file: Path | None = None,
        answers: Mapping[str, str] | None = None,
        spawn: Spawn | None = None,
        color: bool | None = None,
        unicode: bool | None = None,
        env: Mapping[str, str] | None = None,
        heartbeat_seconds: float = 20.0,
        verbose: bool = False,
    ) -> None:
        self.stdin: TextIO = stdin if stdin is not None else sys.stdin
        self.stdout: TextIO = stdout if stdout is not None else sys.stdout
        self.assume_yes = assume_yes
        self.answers: dict[str, str] = dict(answers or {})
        self.missing: list[MissingAnswer] = []
        self.log_file = log_file
        self.heartbeat_seconds = heartbeat_seconds
        self.verbose = verbose
        self._spawn = spawn
        self._log: TextIO | None = None
        self._log_failed = False
        self._in_private_key = False
        environ = env if env is not None else os.environ

        if interactive is None:
            interactive = _isatty(self.stdin) and not assume_yes
        self.interactive = interactive and not assume_yes

        if unicode is None:
            encoding = (getattr(self.stdout, "encoding", "") or "").lower()
            unicode = "utf" in encoding and (
                sys.platform != "win32" or bool(environ.get("WT_SESSION"))
            )
        self.unicode = unicode
        self._glyphs = _UNICODE_GLYPHS if unicode else _ASCII_GLYPHS
        self._ellipsis = "…" if unicode else "..."

        if color is None:
            color = (
                _isatty(self.stdout)
                and not environ.get("NO_COLOR")
                and (
                    sys.platform != "win32"
                    or bool(environ.get("WT_SESSION"))
                    or _enable_windows_vt()
                )
            )
        self.color = color

    # ── Writing ──────────────────────────────────────────────────────────────

    def _paint(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        return "".join(_COLORS[style] for style in styles) + text + _RESET

    def _clean(self, line: str) -> str | None:
        """Redact one line; None when it is part of a private key block."""
        if self._in_private_key:
            if _KEY_END.search(line):
                self._in_private_key = False
            return None
        if _KEY_BEGIN.search(line):
            self._in_private_key = not _KEY_END.search(line)
            return "[private key hidden]"
        return redact(line, ellipsis=self._ellipsis)

    def _emit(self, text: str) -> None:
        try:
            self.stdout.write(text)
        except UnicodeEncodeError:
            encoding = getattr(self.stdout, "encoding", None) or "ascii"
            self.stdout.write(text.encode(encoding, errors="replace").decode(encoding))
        with contextlib.suppress(OSError, ValueError):
            self.stdout.flush()

    def _record(self, line: str) -> None:
        """Append one already-redacted line to the setup log. A log that can't be written is
        reported once and setup goes on: the log is a convenience, not an audit trail."""
        if self.log_file is None or self._log_failed:
            return
        try:
            if self._log is None:
                self.log_file.parent.mkdir(parents=True, exist_ok=True)
                self._log = self.log_file.open("a", encoding="utf-8", newline="\n")
            stamp = datetime.now(UTC).strftime("%H:%M:%S")
            self._log.write(f"{stamp}  {line}\n")
            self._log.flush()
        except OSError as exc:
            self._log_failed = True
            self._emit(f"  {self._glyphs['warn']} Could not write the setup log ({exc}).\n")

    def _line(self, text: str, *styles: str, show: bool = True, log: bool = True) -> None:
        for raw in text.split("\n") or [""]:
            clean = self._clean(raw)
            if clean is None:
                continue
            if log:
                self._record(clean)
            if show:
                self._emit(self._paint(clean, *styles) + "\n")

    def close(self) -> None:
        if self._log is not None:
            with contextlib.suppress(OSError):
                self._log.close()
            self._log = None

    def blank(self) -> None:
        self._emit("\n")

    def banner(self, title: str, lines: Sequence[str] = ()) -> None:
        width = min(max([len(title), *(len(line) for line in lines)]) + 4, 64)
        rule = self._glyphs["rule"] * width
        self.blank()
        self._line(f"  {rule}", "cyan")
        self._line(f"    {title}", "bold")
        self._line(f"  {rule}", "cyan")
        for line in lines:
            self._line(f"    {line}")

    def step(self, number: int, total: int, title: str) -> None:
        self.blank()
        self._line(f"[{number}/{total}] {title}", "bold", "cyan")

    def info(self, message: str) -> None:
        self._line("\n".join(f"  {part}" for part in message.split("\n")))

    def detail(self, message: str) -> None:
        self._line("\n".join(f"     {part}" for part in message.split("\n")), "dim")

    def debug(self, message: str) -> None:
        """A line for the log. On screen only in verbose mode."""
        self._line(f"     {message}", "dim", show=self.verbose)

    def ok(self, message: str) -> None:
        self._line(f"  {self._glyphs['ok']} {message}", "green")

    def warn(self, message: str) -> None:
        self._line(f"  {self._glyphs['warn']} {message}", "yellow")

    def fail(self, message: str, fix: str | None = None) -> None:
        self._line(f"  {self._glyphs['fail']} {message}", "red")
        if fix:
            for part in fix.split("\n"):
                self._line(f"       {part}")

    def bullet(self, message: str) -> None:
        self._line(f"    {self._glyphs['bullet']} {message}")

    # ── Asking ───────────────────────────────────────────────────────────────

    def _read(self, prompt: str) -> str:
        self._emit(self._paint(prompt, "bold"))
        line = self.stdin.readline()
        if line == "":
            self._emit("\n")
            raise Cancelled("input ended")
        answer = line.strip()
        self._record(f"{prompt}{redact(answer, ellipsis=self._ellipsis)}")
        return answer

    def _unattended(self, prompt: str, key: str | None, flag: str | None) -> None:
        self.missing.append(MissingAnswer(key=key or prompt, prompt=prompt, flag=flag or ""))

    def ask_text(
        self,
        prompt: str,
        *,
        default: str | None = None,
        validate: Callable[[str], str | None] | None = None,
        key: str | None = None,
        flag: str | None = None,
    ) -> str:
        """Ask for a line of text. *validate* returns a message for a bad answer, else None."""
        if key is not None and key in self.answers:
            return self.answers[key]
        if not self.interactive:
            if default is not None:
                self._record(f"{prompt}: {default} (default)")
                return default
            self._unattended(prompt, key, flag)
            return ""
        suffix = f" [{default}]" if default else ""
        while True:
            answer = self._read(f"  {prompt}{suffix}: ")
            if not answer:
                if default is not None:
                    return default
                continue
            problem = validate(answer) if validate else None
            if problem is None:
                return answer
            self.warn(problem)

    def ask_choice(
        self,
        prompt: str,
        options: Sequence[Choice],
        *,
        default: str,
        key: str | None = None,
    ) -> str:
        """Ask the person to pick one option, by number or by name. Enter takes *default*."""
        keys = [option.key for option in options]
        if key is not None and key in self.answers:
            return self.answers[key]
        if not self.interactive:
            self._record(f"{prompt}: {default} (default)")
            return default
        while True:
            self.blank()
            self._line(f"  {prompt}", "bold")
            for index, option in enumerate(options, start=1):
                marker = " (default)" if option.key == default else ""
                self._line(f"    {index}. {option.label}{marker}")
                if option.hint:
                    self.detail(f"   {option.hint}")
            default_number = keys.index(default) + 1
            answer = self._read(f"  Choose 1-{len(options)} [{default_number}]: ")
            if not answer:
                return default
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return keys[int(answer) - 1]
            if answer.lower() in keys:
                return answer.lower()
            self.warn(f"Type a number from 1 to {len(options)}, or press Enter for the default.")

    def confirm(self, prompt: str, *, default: bool, key: str | None = None) -> bool:
        if key is not None and key in self.answers:
            return self.answers[key].strip().lower() in ("y", "yes", "true", "1")
        if not self.interactive:
            self._record(f"{prompt}: {'yes' if default else 'no'} (default)")
            return default
        hint = "[Y/n]" if default else "[y/N]"
        while True:
            answer = self._read(f"  {prompt} {hint}: ").lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.warn("Type y or n, or press Enter for the default.")

    def pause(self, prompt: str = "Press Enter to continue") -> None:
        if self.interactive:
            self._read(f"  {prompt}: ")

    # ── Running child processes ──────────────────────────────────────────────

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        capture: bool = False,
        quiet: bool = False,
        sensitive: bool = False,
        interactive: bool = False,
        input_text: str | None = None,
        heartbeat: str | None = None,
        summary: bool = False,
    ) -> RunResult:
        """Run a child process and return what it printed.

        * By default both streams are shown, indented, as they arrive.
        * ``capture``: stdout is data for the caller (``KEY=value`` lines, JSON) and is not
          shown. stderr still is: a child's errors are never hidden.
        * ``quiet``: nothing is shown. Everything still goes to the log, and the caller gets
          stderr back to show when the exit code says something went wrong.
        * ``sensitive``: stdout holds a secret. It is returned, never shown and never logged.
        * ``interactive``: the child talks to the person directly (a sign-in, a UAC prompt),
          so nothing is captured.
        * ``heartbeat``: a line to show whenever the child has been silent for a while, so a
          long wait doesn't look like a hang.
        * ``summary``: the child writes for engineers (banners, IDs, tables). Its output goes
          to the log only, and the caller says in a line or two what happened; on failure
          :meth:`show_tail` puts the end of it on screen. ``verbose`` mode shows it all.
        """
        hidden = quiet or (summary and not self.verbose)
        request = RunRequest(
            argv=[str(part) for part in argv],
            env=env,
            cwd=cwd,
            interactive=interactive,
            input_text=input_text,
            heartbeat_seconds=self.heartbeat_seconds if heartbeat else None,
            silent=hidden,
        )
        self._record(f"$ {redact(' '.join(request.argv), ellipsis=self._ellipsis)}")
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        all_lines: list[str] = []

        def on_stdout(line: str) -> None:
            stdout_lines.append(line)
            if sensitive:
                return
            all_lines.append(line)
            self._line(f"     {line}", "dim", show=not (capture or hidden))

        def on_stderr(line: str) -> None:
            stderr_lines.append(line)
            all_lines.append(line)
            self._line(f"     {line}", "dim", show=not hidden)

        def on_idle(elapsed: float) -> None:
            minutes, seconds = divmod(int(elapsed), 60)
            waited = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
            self._line(f"     {heartbeat}{self._ellipsis} ({waited})", "dim")

        if self._spawn is not None:
            returncode = self._spawn(request, on_stdout, on_stderr)
        else:
            returncode = spawn_process(request, on_stdout, on_stderr, on_idle=on_idle)
        self._record(f"  exit {returncode}")

        def joined(lines: list[str]) -> str:
            return "".join(f"{line}\n" for line in lines)

        result = RunResult(request.argv, returncode, joined(stdout_lines), joined(stderr_lines))
        result.output = joined(all_lines)
        result.shown = not hidden and not capture and not interactive
        return result

    def show_tail(self, result: subprocess.CompletedProcess[str], *, lines: int = 25) -> None:
        """After a failed child: put the end of what it printed on screen, and say where the
        rest is. Nothing is repeated when the stream was on screen already."""
        output = getattr(result, "output", "") or (result.stdout or "") + (result.stderr or "")
        if not getattr(result, "shown", False):
            for line in [item for item in output.splitlines() if item.strip()][-lines:]:
                self._line(f"     {line}", "dim", log=False)  # the log has it already
        if self.log_file is not None:
            self._line(f"     The full output is in the setup log: {self.log_file}", "dim")
