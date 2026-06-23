"""Purpose-named local file access through the MXC sandbox.

``run_code`` is a generic "run a command" tool — the model reliably uses it for
*reading* a file (``cat``), but does not think of it as a *write* tool and tends
to route "save a file" requests to the cloud OneDrive tools instead. These
helpers expose intent-matching ``read_local_file`` / ``write_local_file`` on top
of the exact same containment machinery (operator ceiling -> clamp -> realpath ->
Seatbelt), so the model picks the right surface while the kernel still enforces
the operator's allow-list.

Injection safety: the user-supplied path and content are passed to the sandbox
shell only via ``shlex.quote`` (and ``printf '%s'`` for content), so no
metacharacter can escape into the command. The command template itself contains
no interpolated user data.
"""

from __future__ import annotations

import os
import shlex

from entrabot.sandbox.base import SandboxPolicy, SandboxResult
from entrabot.sandbox.policy import canonicalize_paths, clamp_to_ceiling


def ceiling_from_env() -> SandboxPolicy:
    """Build the operator ceiling policy from ``ENTRABOT_SANDBOX_*`` env vars."""
    readonly = [p for p in os.environ.get("ENTRABOT_SANDBOX_READONLY_PATHS", "").split(":") if p]
    readwrite = [p for p in os.environ.get("ENTRABOT_SANDBOX_READWRITE_PATHS", "").split(":") if p]
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


def build_read_command(path: str) -> str:
    """Shell command that reads ``path`` to stdout (path is shell-quoted)."""
    return f"cat -- {shlex.quote(path)}"


def build_write_command(path: str, content: str) -> str:
    """Shell command that writes ``content`` to ``path`` (both shell-quoted).

    Uses ``printf '%s'`` rather than ``echo`` so arbitrary content (including
    leading dashes, backslashes, no trailing newline) is written verbatim.
    """
    return f"printf '%s' {shlex.quote(content)} > {shlex.quote(path)}"


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
    """Read a local file inside the sandbox, granting read-only on that file."""
    expanded = os.path.expanduser(path)
    command = build_read_command(expanded)
    policy = _prepare_policy(
        command,
        readonly_paths=[expanded],
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
    return runner.run(policy)
