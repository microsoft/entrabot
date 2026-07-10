"""Purpose-named local file access through the MXC sandbox.

``run_code`` is a generic "run a command" tool — the model reliably uses it for
*reading* a file (``cat``), but does not think of it as a *write* tool and tends
to route "save a file" requests to the cloud OneDrive tools instead. These
helpers expose intent-matching ``read_local_file`` / ``write_local_file`` on top
of the exact same containment machinery (operator ceiling -> clamp -> realpath ->
sandbox), so the model picks the right surface while the kernel still enforces
the operator's allow-list.

Platform-aware command construction
-----------------------------------
The sandbox runner hands ``process.commandLine`` to the platform binary, and the
two platforms execute it differently:

* **macOS / Linux** — the Seatbelt/lxc binary runs ``commandLine`` through a
  shell, so POSIX shell builtins, ``shlex.quote``-style quoting, and ``>``
  redirection all work. Read uses ``cat``; write uses ``printf '%s'``.
* **Windows** — ``wxc-exec.exe`` invokes ``commandLine`` with ``CreateProcessW``
  directly: there is **no implicit shell** (see ``windows.py`` docstring and
  ``docs/platform-learnings/mxc-windows-sandbox-preview.md`` §3). A bare ``cat``
  is not a Windows executable, which is exactly the
  ``CreateProcessW failed: ERROR_FILE_NOT_FOUND`` bug this module fixes. The
  Windows branch therefore invokes the cmd builtin ``type`` via ``cmd /c`` for
  reads, and a byte-exact Python writer for writes (see ``build_write_command``).

Injection safety (both platforms): the user-supplied path and content never
appear interpolated into executable code. On POSIX they are passed only via
``shlex.quote`` (and ``printf '%s'`` for content). On Windows the path is wrapped
in double quotes (cmd metacharacters inside quotes are inert) and the write
path/content travel as *separate argv entries* assembled with
``subprocess.list2cmdline`` (correct Windows/CreateProcessW quoting) — no
metacharacter can escape into the command.
"""

from __future__ import annotations

import base64
import os
import shlex
import subprocess
import sys

from entrabot.sandbox.base import SandboxPolicy, SandboxResult
from entrabot.sandbox.policy import canonicalize_paths, clamp_to_ceiling


def ceiling_from_env() -> SandboxPolicy:
    """Build the operator ceiling policy from ``ENTRABOT_SANDBOX_*`` env vars."""
    # Operator ceiling paths use the OS path separator (':' on POSIX, ';' on
    # Windows). os.pathsep — not a hardcoded ':' — is required on Windows so a
    # drive-letter colon in 'C:\\Users\\me' is not split into ['C', '\\Users\\me'].
    readonly = [
        p
        for p in os.environ.get("ENTRABOT_SANDBOX_READONLY_PATHS", "").split(os.pathsep)
        if p
    ]
    readwrite = [
        p
        for p in os.environ.get("ENTRABOT_SANDBOX_READWRITE_PATHS", "").split(os.pathsep)
        if p
    ]
    timeout = int(os.environ.get("ENTRABOT_SANDBOX_TIMEOUT_MS", "30000"))
    return SandboxPolicy(
        backend="process",
        command_line="",
        readonly_paths=readonly,
        readwrite_paths=readwrite,
        timeout_ms=timeout,
        network_default_policy=os.environ.get("ENTRABOT_SANDBOX_NETWORK", "block"),
        keychain_access=False,
    )


def _win_cmd_quote_path(path: str) -> str:
    """Wrap a filesystem ``path`` in double quotes for a cmd.exe command line.

    Double-quoting makes cmd metacharacters that are *legal in Windows file
    names* (``& | < > ^ ( )``) inert, so a path like ``C:\\a & b.txt`` cannot
    break out into a second command. Windows paths cannot contain a literal
    double quote, so there is nothing to escape inside; we defensively drop any
    stray quote rather than let it terminate the quoting early.

    Residual caveat: cmd still performs ``%VAR%`` expansion even inside double
    quotes. Paths reaching here come from ``os.path.expanduser`` (NOT
    ``expandvars``) and are independently bounded by the operator ceiling and the
    per-call read/write grant, so a literal ``%`` cannot *widen* access — worst
    case the kernel denies a mis-expanded path.
    """
    return '"' + path.replace('"', "") + '"'


def build_read_command(path: str) -> str:
    """Command that reads ``path`` to stdout.

    POSIX: ``cat -- <shlex-quoted path>`` (runs through the platform shell).

    Windows: ``cmd /c type "<path>"``. ``wxc-exec.exe`` has no implicit shell, so
    ``cat`` is not found; ``type`` is the cmd builtin that prints a file to
    stdout and the processcontainer backend auto-grants the cmd.exe + system-DLL
    baseline (mxc-windows-sandbox-preview.md §4), so no extra read grant is
    needed to run it. The path is force-quoted for cmd (see ``_win_cmd_quote_path``),
    NOT shell-quoted with ``shlex.quote`` (which is POSIX-only).
    """
    if os.name == "nt":
        return f"cmd /c type {_win_cmd_quote_path(path)}"
    return f"cat -- {shlex.quote(path)}"


# Inline Python program for the Windows write path. It takes two argv entries —
# the target path (argv[1]) and base64-encoded UTF-8 content (argv[2]) — decodes
# the content, and writes the exact bytes. ``base64``/``sys`` are stdlib.
_WINDOWS_WRITER_PROGRAM = (
    "import base64,sys;"
    "open(sys.argv[1],'wb').write(base64.b64decode(sys.argv[2]))"
)


def build_write_command(path: str, content: str) -> str:
    """Command that writes ``content`` to ``path`` byte-for-byte.

    POSIX: ``printf '%s' <quoted-content> > <quoted-path>`` — ``printf`` (not
    ``echo``) so arbitrary content (leading dashes, backslashes, no trailing
    newline) is written verbatim.

    Windows: a Python writer invoked as
    ``<python.exe> -c "<writer>" <path> <base64-content>``.

    Why Python on Windows rather than ``cmd /c echo > file``:

    * **Byte fidelity (the decisive factor).** ``cmd`` ``echo`` always appends
      CRLF, cannot emit content without a trailing newline, cannot emit
      multi-line content from one redirection, and mangles ``< > | & ^ %`` and
      quotes. The contract requires writing arbitrary bytes verbatim, which cmd
      redirection simply cannot guarantee. Decoding base64 in Python writes the
      exact bytes with no transformation.
    * **Injection safety.** The path and the (base64) content travel as
      *separate argv entries* and are never interpolated into the program text;
      the command string is built with ``subprocess.list2cmdline`` so Windows
      (CreateProcessW / MSVCRT) quoting is correct. ``python.exe`` has no cmd
      metacharacter or ``%VAR%`` layer, so no character in the path or content
      can escape into a shell.

    Containment note: the inner ``python.exe`` must be loadable inside the
    processcontainer. ``sandboxed_write`` therefore grants the interpreter's
    runtime dirs (venv root + base install) read-only, post-clamp, via
    ``_windows_interpreter_grants`` — without that grant the venv launcher dies
    with ``failed to locate pyvenv.cfg: Access is denied`` (exit 106). This is
    validated end-to-end against the real ``wxc-exec.exe`` (byte-exact write of
    content with embedded quotes, ``&``, ``%PATH%``, CRLF, and no trailing
    newline). The MXC backend auto-grants the cmd.exe + system-DLL baseline
    (mxc-windows-sandbox-preview.md §4); the interpreter grant supplies the rest.
    """
    if os.name == "nt":
        content_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        return subprocess.list2cmdline(
            [sys.executable, "-c", _WINDOWS_WRITER_PROGRAM, path, content_b64]
        )
    return f"printf '%s' {shlex.quote(content)} > {shlex.quote(path)}"


def _windows_interpreter_grants() -> list[str]:
    """Read-only dirs the inner ``python.exe`` needs to boot inside the sandbox.

    The Windows write path (``build_write_command``) spawns ``sys.executable`` —
    typically a venv ``python.exe`` — to decode base64 and write the bytes. For
    that interpreter to start, the processcontainer must be able to *read* its own
    runtime: the venv root (``pyvenv.cfg`` + ``Scripts\\python.exe``) and the base
    CPython install (``pythonXY.dll`` + the standard library). Without these, the
    launcher dies with ``failed to locate pyvenv.cfg: Access is denied`` (exit
    106) and nothing is written — the bug this grant fixes.

    These are sandbox *infrastructure* grants, not agent-requested paths: they
    expose only the Python runtime (no user data) and are therefore added AFTER
    the operator-ceiling clamp. The clamp governs which *user* files the agent may
    touch; it must not strip the writer's own interpreter — exactly as MXC already
    auto-grants the system-DLL baseline needed to launch ``cmd.exe``. The grant is
    read-only and the write target stays clamped to the operator ceiling, so this
    cannot widen what user data the agent can read or write.
    """
    candidates = {
        os.path.dirname(sys.executable),  # ...\.venv\Scripts (holds python.exe)
        sys.prefix,  # venv root (pyvenv.cfg); == base install when not in a venv
        sys.base_prefix,  # base CPython install (pythonXY.dll + stdlib)
    }
    return [p for p in candidates if p and os.path.isdir(p)]


def _prepare_policy(
    command_line: str,
    *,
    readonly_paths: list[str],
    readwrite_paths: list[str],
    ceiling: SandboxPolicy,
    runner,
) -> SandboxPolicy:
    """Clamp the requested grant to the operator ceiling and canonicalize it."""
    caps = runner.get_capabilities()
    requested = SandboxPolicy(
        backend="process",
        command_line=command_line,
        readonly_paths=readonly_paths,
        readwrite_paths=readwrite_paths,
        timeout_ms=ceiling.timeout_ms,
        network_default_policy="block",  # local file I/O never needs network
        keychain_access=False,
    )
    clamped = clamp_to_ceiling(requested, ceiling, caps)
    if clamped.readonly_paths:
        clamped.readonly_paths = canonicalize_paths(clamped.readonly_paths)
    if clamped.readwrite_paths:
        clamped.readwrite_paths = canonicalize_paths(clamped.readwrite_paths)
    return clamped


def sandboxed_read(path: str, *, ceiling: SandboxPolicy, runner) -> SandboxResult:
    """Read a local file inside the sandbox, granting read-only for the read.

    Grant granularity is platform-specific:

    * **POSIX / Seatbelt** — a file-level read grant is sufficient, so grant the
      file itself (minimal surface).
    * **Windows / AppContainer** — opening a file requires directory-traversal
      access to its parent, so a file-only grant is *deterministically* denied
      with ``Access is denied`` (verified live against ``wxc-exec.exe``: file
      grant -> denied; parent-dir grant -> allowed). Grant the parent directory
      instead. This mirrors ``sandboxed_write`` (which already grants the parent)
      and stays clamped to the operator ceiling — the ceiling's read paths are
      directory-level, so this never widens beyond the operator's allowance.
    """
    expanded = os.path.expanduser(path)
    command = build_read_command(expanded)
    read_grant = (
        os.path.dirname(os.path.abspath(expanded)) if os.name == "nt" else expanded
    )
    policy = _prepare_policy(
        command,
        readonly_paths=[read_grant],
        readwrite_paths=[],
        ceiling=ceiling,
        runner=runner,
    )
    return runner.run(policy)


def sandboxed_write(
    path: str, content: str, *, ceiling: SandboxPolicy, runner
) -> SandboxResult:
    """Write a local file inside the sandbox, granting read-write on its parent.

    The grant is the parent directory (which exists) rather than the file itself,
    so a not-yet-created file can be written. Containment is unchanged: the parent
    must be within the operator's read-write ceiling or the kernel denies it.
    """
    expanded = os.path.expanduser(path)
    parent = os.path.dirname(os.path.abspath(expanded))
    command = build_write_command(expanded, content)
    policy = _prepare_policy(
        command,
        readonly_paths=[],
        readwrite_paths=[parent],
        ceiling=ceiling,
        runner=runner,
    )
    if os.name == "nt":
        # The Windows writer spawns python.exe to write the bytes; grant its
        # runtime dirs read access AFTER the clamp. This is an infrastructure
        # grant (the interpreter's own files, no user data) — see
        # _windows_interpreter_grants — and must bypass the operator-ceiling
        # clamp so the writer can boot. Dedup while preserving order.
        infra = canonicalize_paths(_windows_interpreter_grants())
        policy.readonly_paths = list(dict.fromkeys(policy.readonly_paths + infra))
    return runner.run(policy)
