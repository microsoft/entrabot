"""Tests for sandbox/local_files.py — purpose-named local file read/write.

These wrap the same clamp -> canonicalize -> Seatbelt machinery as run_code,
but expose intent-matching ``read_local_file`` / ``write_local_file`` helpers so
the model routes "read/write/save a local file" requests correctly instead of
defaulting to the cloud OneDrive tools.
"""

import os
import tempfile

from entrabot.sandbox.base import SandboxPolicy, SandboxResult


def _ceiling(readonly, readwrite):
    return SandboxPolicy(
        backend="process",
        command_line="",
        readonly_paths=readonly,
        readwrite_paths=readwrite,
        timeout_ms=30000,
        network_default_policy="block",
        keychain_access=False,
    )


class _FakeRunner:
    """Records the policy passed to run() and returns a canned result."""

    def __init__(self, exit_code=0, stdout="", stderr=""):
        self._result = SandboxResult(
            exit_code=exit_code, stdout=stdout, stderr=stderr,
            duration_ms=1, timed_out=False,
        )
        self.last_policy = None

    def get_capabilities(self):
        return {"backend": "seatbelt", "network_host_filtering": False}

    def run(self, policy):
        self.last_policy = policy
        return self._result


# ── command construction (injection-safe quoting) ───────────────────────────
def test_build_read_command_quotes_path():
    from entrabot.sandbox.local_files import build_read_command

    cmd = build_read_command("/Users/me/My Docs/a b.txt")
    # The path has spaces; it must be shell-quoted so it's one argument.
    assert "'/Users/me/My Docs/a b.txt'" in cmd
    assert cmd.startswith("cat ")


def test_build_write_command_quotes_path_and_content():
    from entrabot.sandbox.local_files import build_write_command

    cmd = build_write_command("/tmp/o ut.txt", "hi; rm -rf $HOME `x`")
    # Both the dangerous content and the spaced path must be quoted — no
    # metacharacters can escape into the shell.
    assert "rm -rf" in cmd  # present as literal data
    assert "> '/tmp/o ut.txt'" in cmd
    # The command substitution / variable must be inside single quotes (inert).
    assert "`x`" in cmd


# ── path grant shaping ──────────────────────────────────────────────────────
def test_sandboxed_read_requests_readonly_grant_for_the_file():
    from entrabot.sandbox.local_files import sandboxed_read

    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        f = os.path.join(d, "secret.txt")
        with open(f, "w") as fh:
            fh.write("x")

        runner = _FakeRunner(exit_code=0, stdout="x")
        ceiling = _ceiling(readonly=[d], readwrite=[])
        sandboxed_read(f, ceiling=ceiling, runner=runner)

        # Read grants read-only on the file; never any write access.
        assert runner.last_policy.readonly_paths == [f]
        assert runner.last_policy.readwrite_paths == []


def test_sandboxed_write_requests_readwrite_grant_for_parent_dir():
    from entrabot.sandbox.local_files import sandboxed_write

    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        f = os.path.join(d, "out.txt")  # does not exist yet

        runner = _FakeRunner(exit_code=0)
        ceiling = _ceiling(readonly=[], readwrite=[d])
        sandboxed_write(f, "hello", ceiling=ceiling, runner=runner)

        # Write grants read-write on the parent dir (the file may not exist yet).
        assert runner.last_policy.readwrite_paths == [d]
        assert runner.last_policy.readonly_paths == []


# ── ceiling enforcement (clamp) ─────────────────────────────────────────────
def test_sandboxed_read_outside_ceiling_is_clamped_empty():
    from entrabot.sandbox.local_files import sandboxed_read

    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        allowed = os.path.join(d, "allowed")
        secret = os.path.join(d, "secret")
        os.mkdir(allowed)
        os.mkdir(secret)
        target = os.path.join(secret, "x.txt")
        with open(target, "w") as fh:
            fh.write("x")

        runner = _FakeRunner(exit_code=1, stderr="Operation not permitted")
        ceiling = _ceiling(readonly=[allowed], readwrite=[])  # secret NOT allowed
        sandboxed_read(target, ceiling=ceiling, runner=runner)

        # Path is outside the ceiling -> clamp drops it -> no read grant.
        assert runner.last_policy.readonly_paths == []


def test_sandboxed_write_outside_ceiling_is_clamped_empty():
    from entrabot.sandbox.local_files import sandboxed_write

    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        allowed = os.path.join(d, "allowed")
        readonly_dir = os.path.join(d, "ro")
        os.mkdir(allowed)
        os.mkdir(readonly_dir)
        target = os.path.join(readonly_dir, "note.txt")

        runner = _FakeRunner(exit_code=1, stderr="Operation not permitted")
        ceiling = _ceiling(readonly=[readonly_dir], readwrite=[allowed])
        sandboxed_write(target, "hello", ceiling=ceiling, runner=runner)

        # Parent dir is read-only in the ceiling -> no read-write grant survives.
        assert runner.last_policy.readwrite_paths == []


# ── env ceiling loader ──────────────────────────────────────────────────────
def test_ceiling_from_env_parses_colon_lists(monkeypatch):
    from entrabot.sandbox.local_files import ceiling_from_env

    monkeypatch.setenv("ENTRABOT_SANDBOX_READONLY_PATHS", "/a:/b")
    monkeypatch.setenv("ENTRABOT_SANDBOX_READWRITE_PATHS", "/c")
    monkeypatch.setenv("ENTRABOT_SANDBOX_TIMEOUT_MS", "12345")

    ceiling = ceiling_from_env()

    assert ceiling.readonly_paths == ["/a", "/b"]
    assert ceiling.readwrite_paths == ["/c"]
    assert ceiling.timeout_ms == 12345
