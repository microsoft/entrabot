"""Tests for the MemoryBackend abstraction (ADR-005, Phase 2).

The backend is a sync key→bytes/str interface that hides whether storage
lives on the local filesystem or in Azure Blob Storage. Existing call
sites in `interaction_log.py` and `daily_summary.py` are sync; the
backend matches that shape so we don't have to refactor the world.

Two implementations:
- ``LocalBackend`` — paths under a root dir on disk
- ``BlobBackend``  — wraps the async ``BlobStore`` for sync callers

Plus a ``get_backend()`` factory that returns the right one based on
config (Phase 5 will wire the cloud branch — for Phase 2 it's local-only).
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from entrabot.storage import backend as backend_module
from entrabot.storage.backend import (
    BlobBackend,
    LocalBackend,
    MemoryBackend,
    get_backend,
)


def _fail_after_exclusive_create(monkeypatch: pytest.MonkeyPatch, fail_on: str) -> None:
    """Let ``LocalBackend``'s open of the file it writes succeed, then fail the write or the close.

    This is what a full disk looks like: the file already exists on disk, empty, by
    the time the error arrives. Shadowing ``open`` in the backend module leaves the
    builtin alone for everything else. The temp file is opened through ``os.fdopen``, which
    is replaced as well, so a caller should undo both with ``monkeypatch.context()``.
    """
    real_open = open

    class _FailingFile:
        def __init__(self, target: Path | int, mode: str) -> None:
            self._file = real_open(target, mode)  # a path, or the temp file's descriptor

        def __enter__(self) -> _FailingFile:
            return self

        def __exit__(self, *exc_info: object) -> bool:
            self._file.close()
            if fail_on == "close":
                raise OSError(errno.ENOSPC, "No space left on device")
            return False

        def write(self, text: str) -> int:
            if fail_on == "write":
                raise OSError(errno.ENOSPC, "No space left on device")
            return self._file.write(text)

        def flush(self) -> None:
            self._file.flush()

        def fileno(self) -> int:
            return self._file.fileno()

    monkeypatch.setattr(backend_module, "open", _FailingFile, raising=False)
    monkeypatch.setattr(os, "fdopen", _FailingFile)


# ---------------------------------------------------------------------------
# LocalBackend
# ---------------------------------------------------------------------------
class TestLocalBackend:
    def test_implements_protocol(self, tmp_path: Path) -> None:
        backend: MemoryBackend = LocalBackend(tmp_path)
        # Protocol has read_text, write_text, append_text, exists, list
        assert callable(backend.read_text)
        assert callable(backend.write_text)
        assert callable(backend.append_text)
        assert callable(backend.exists)
        assert callable(backend.list)

    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path)
        assert backend.read_text("does/not/exist.txt") is None

    def test_write_then_read_roundtrip(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path)
        backend.write_text("a/b/c.txt", "hello\nworld")
        assert backend.read_text("a/b/c.txt") == "hello\nworld"

    def test_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path)
        backend.write_text("deep/nested/dir/file.txt", "x")
        assert (tmp_path / "deep" / "nested" / "dir" / "file.txt").exists()

    def test_append_creates_file(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path)
        backend.append_text("log.txt", "line1\n")
        assert backend.read_text("log.txt") == "line1\n"

    def test_append_extends_existing(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path)
        backend.append_text("log.txt", "line1\n")
        backend.append_text("log.txt", "line2\n")
        assert backend.read_text("log.txt") == "line1\nline2\n"

    def test_append_creates_parent_dirs(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path)
        backend.append_text("interactions/2026-04-17.jsonl", "{}\n")
        assert (tmp_path / "interactions" / "2026-04-17.jsonl").exists()

    def test_exists(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path)
        assert backend.exists("x.txt") is False
        backend.write_text("x.txt", "y")
        assert backend.exists("x.txt") is True

    def test_list_returns_relative_keys(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path)
        backend.write_text("interactions/a.jsonl", "1")
        backend.write_text("interactions/b.jsonl", "2")
        backend.write_text("summaries/c.html", "3")
        keys = sorted(backend.list("interactions/"))
        assert keys == ["interactions/a.jsonl", "interactions/b.jsonl"]

    def test_list_empty_prefix_returns_all(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path)
        backend.write_text("a.txt", "1")
        backend.write_text("d/b.txt", "2")
        keys = sorted(backend.list())
        assert keys == ["a.txt", "d/b.txt"]


# ---------------------------------------------------------------------------
# BlobBackend
# ---------------------------------------------------------------------------
class _FakeBlobStore:
    """In-memory async BlobStore stand-in.

    Mirrors the real ``BlobStore`` API surface used by ``BlobBackend``:
    async ``get`` (raises KeyError on miss), ``get_with_etag``, ``put``
    (honors ``if_match`` / ``if_none_match`` for optimistic concurrency),
    ``exists``, ``list``. ``put`` is atomic across threads, like a real
    conditional PUT, so two "instances" can share one store from worker
    threads.
    """

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self._counter = 0
        self._lock = threading.Lock()

    async def get(self, path: str) -> bytes:
        if path not in self.data:
            raise KeyError(path)
        return self.data[path]

    async def get_with_etag(self, path: str) -> tuple[bytes, str]:
        if path not in self.data:
            raise KeyError(path)
        return self.data[path], self.etags[path]

    async def put(
        self,
        path: str,
        data: bytes,
        *,
        if_match: str | None = None,
        if_none_match: str | None = None,
    ) -> str:
        from entrabot.storage.blob import ConcurrencyError

        with self._lock:
            if if_match is not None and self.etags.get(path) != if_match:
                raise ConcurrencyError(f"put({path!r}) refused: If-Match={if_match!r} stale")
            if if_none_match == "*" and path in self.data:
                raise ConcurrencyError(f"put({path!r}) refused: blob already exists")
            self._counter += 1
            etag = f'"etag-{self._counter}"'
            self.data[path] = data
            self.etags[path] = etag
            return etag

    async def exists(self, path: str) -> bool:
        return path in self.data

    async def list(self, prefix: str = "") -> list[str]:
        return [k for k in self.data if k.startswith(prefix)]


class TestBlobBackend:
    def test_implements_protocol(self) -> None:
        backend: MemoryBackend = BlobBackend(_FakeBlobStore())
        assert callable(backend.read_text)

    def test_read_missing_returns_none(self) -> None:
        backend = BlobBackend(_FakeBlobStore())
        assert backend.read_text("missing.txt") is None

    def test_write_then_read_roundtrip(self) -> None:
        backend = BlobBackend(_FakeBlobStore())
        backend.write_text("a/b.txt", "hello")
        assert backend.read_text("a/b.txt") == "hello"

    def test_append_creates_blob_when_missing(self) -> None:
        backend = BlobBackend(_FakeBlobStore())
        backend.append_text("log.jsonl", "line1\n")
        assert backend.read_text("log.jsonl") == "line1\n"

    def test_append_extends_existing_blob(self) -> None:
        backend = BlobBackend(_FakeBlobStore())
        backend.append_text("log.jsonl", "line1\n")
        backend.append_text("log.jsonl", "line2\n")
        assert backend.read_text("log.jsonl") == "line1\nline2\n"

    def test_exists(self) -> None:
        backend = BlobBackend(_FakeBlobStore())
        assert backend.exists("x") is False
        backend.write_text("x", "y")
        assert backend.exists("x") is True

    def test_list(self) -> None:
        store = _FakeBlobStore()
        backend = BlobBackend(store)
        backend.write_text("interactions/a.jsonl", "1")
        backend.write_text("interactions/b.jsonl", "2")
        backend.write_text("summaries/c.html", "3")
        assert sorted(backend.list("interactions/")) == [
            "interactions/a.jsonl",
            "interactions/b.jsonl",
        ]


# ---------------------------------------------------------------------------
# ETag optimistic concurrency (design F5) — read_text_with_etag + if_match
# ---------------------------------------------------------------------------
class TestLocalBackendEtag:
    def test_read_with_etag_absent_returns_none_none(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path)
        content, etag = backend.read_text_with_etag("absent.json")
        assert content is None
        assert etag is None

    def test_read_with_etag_present_returns_content_and_stable_etag(
        self, tmp_path: Path
    ) -> None:
        backend = LocalBackend(tmp_path)
        backend.write_text("c.json", "hello")
        content, etag = backend.read_text_with_etag("c.json")
        assert content == "hello"
        assert etag is not None
        # ETag is stable for unchanged content.
        _, etag2 = backend.read_text_with_etag("c.json")
        assert etag2 == etag

    def test_etag_changes_when_content_changes(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path)
        backend.write_text("c.json", "v1")
        _, e1 = backend.read_text_with_etag("c.json")
        backend.write_text("c.json", "v2")
        _, e2 = backend.read_text_with_etag("c.json")
        assert e1 != e2

    def test_conditional_write_succeeds_when_etag_matches(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path)
        backend.write_text("c.json", "v1")
        _, etag = backend.read_text_with_etag("c.json")
        new_etag = backend.write_text("c.json", "v2", if_match=etag)
        assert backend.read_text("c.json") == "v2"
        assert new_etag is not None and new_etag != etag

    def test_conditional_write_raises_on_stale_etag(self, tmp_path: Path) -> None:
        from entrabot.storage.blob import ConcurrencyError

        backend = LocalBackend(tmp_path)
        backend.write_text("c.json", "v1")
        _, etag = backend.read_text_with_etag("c.json")
        # A concurrent writer bumps the content out from under us.
        backend.write_text("c.json", "raced")
        with pytest.raises(ConcurrencyError):
            backend.write_text("c.json", "v2", if_match=etag)
        # The racing write survives; our stale write did not land.
        assert backend.read_text("c.json") == "raced"

    def test_conditional_write_on_absent_with_etag_raises(self, tmp_path: Path) -> None:
        from entrabot.storage.blob import ConcurrencyError

        backend = LocalBackend(tmp_path)
        with pytest.raises(ConcurrencyError):
            backend.write_text("c.json", "v1", if_match="some-etag")

    def test_unconditional_write_ignores_etag(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path)
        backend.write_text("c.json", "v1")
        # if_match=None → unconditional, always writes.
        backend.write_text("c.json", "v2", if_match=None)
        assert backend.read_text("c.json") == "v2"

    def test_if_absent_creates_missing_key_and_parents(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path)
        etag = backend.write_text("nested/dir/c.txt", "v1", if_absent=True)
        assert backend.read_text("nested/dir/c.txt") == "v1"
        assert etag == backend.read_text_with_etag("nested/dir/c.txt")[1]

    def test_if_absent_refuses_to_overwrite_existing_key(self, tmp_path: Path) -> None:
        from entrabot.storage.blob import ConcurrencyError

        backend = LocalBackend(tmp_path)
        backend.write_text("c.txt", "winner")
        with pytest.raises(ConcurrencyError):
            backend.write_text("c.txt", "loser", if_absent=True)
        assert backend.read_text("c.txt") == "winner"

    def test_if_absent_and_if_match_are_mutually_exclusive(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path)
        with pytest.raises(ValueError):
            backend.write_text("c.txt", "v1", if_match="etag", if_absent=True)

    @pytest.mark.parametrize("fail_on", ["write", "close"])
    def test_if_absent_removes_the_empty_file_when_the_content_does_not_land(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_on: str
    ) -> None:
        """If the content doesn't land, the key must stay creatable. An empty file left at
        the key would be found by every later reader (an email cursor, say) as an empty
        value that no retry could replace, since the key now exists. The content goes to a
        temp file first, so a failed write or close leaves nothing at the key and no temp
        file either."""
        backend = LocalBackend(tmp_path)

        with monkeypatch.context() as patched:
            _fail_after_exclusive_create(patched, fail_on)
            with pytest.raises(OSError, match="No space left"):
                backend.write_text("c.txt", "v1", if_absent=True)

        assert list(tmp_path.iterdir()) == []
        backend.write_text("c.txt", "v1", if_absent=True)  # a later attempt can create it
        assert backend.read_text("c.txt") == "v1"

    def test_if_absent_conflict_leaves_the_winners_file_alone(self, tmp_path: Path) -> None:
        """Only a file this call created is ever removed."""
        from entrabot.storage.blob import ConcurrencyError

        backend = LocalBackend(tmp_path)
        backend.write_text("c.txt", "winner")
        with pytest.raises(ConcurrencyError):
            backend.write_text("c.txt", "loser", if_absent=True)
        assert (tmp_path / "c.txt").read_text() == "winner"

    def test_conditional_writes_are_atomic_within_a_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ETag check and the write that follows it are one step.

        Otherwise two writers holding the same ETag can both pass the check, and the
        later write silently replaces the earlier one. That is how a shutdown flush can
        lose to a worker thread still inside a list update it started before.

        Each writer is held right after its check reads the file. Unprotected, both
        arrive holding the same ETag and both go on to write. Protected, the second
        can't get as far as the check until the first has written, so the first waits
        out the barrier alone and carries on. The timeout keeps either case from hanging.
        """
        from entrabot.storage.blob import ConcurrencyError

        LocalBackend(tmp_path).write_text("c.json", "v1")
        _, etag = LocalBackend(tmp_path).read_text_with_etag("c.json")
        target = tmp_path / "c.json"

        both_have_checked = threading.Barrier(2, timeout=0.3)
        writer = threading.local()
        real_read_text = Path.read_text

        def read_then_wait(self: Path, *args: object, **kwargs: object) -> str:
            content = real_read_text(self, *args, **kwargs)
            if self == target and getattr(writer, "active", False):
                with contextlib.suppress(threading.BrokenBarrierError):
                    both_have_checked.wait()
            return content

        monkeypatch.setattr(Path, "read_text", read_then_wait)

        def write(content: str) -> bool:
            writer.active = True
            try:
                # A new instance per writer, as get_backend() makes: the guard has to be
                # shared by every LocalBackend in the process, not held per instance.
                LocalBackend(tmp_path).write_text("c.json", content, if_match=etag)
            except ConcurrencyError:
                return False
            return True

        contents = ("writer-a", "writer-b")
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = dict(zip(contents, pool.map(write, contents), strict=True))

        winners = [content for content, won in outcomes.items() if won]
        assert len(winners) == 1, f"both writers passed the ETag check: {outcomes}"
        assert target.read_text() == winners[0], "the file holds the winner's content"


def _must_not_run(*args: object, **kwargs: object) -> None:
    raise AssertionError("the exclusive-create fallback ran although hard links work")


class TestLocalBackendAtomicPublish:
    """A value is published whole or not at all.

    Local readers take no lock, so a value written in place can be seen empty or half
    written, and a failed overwrite can truncate what was there. The content goes to a temp
    file in the same directory first and is moved into place.
    """

    def test_a_failed_overwrite_keeps_the_existing_value_and_leaves_no_temp_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = LocalBackend(tmp_path)
        backend.write_text("c.json", "v1")

        def disk_error(fd: int) -> None:
            raise OSError(errno.EIO, "Input/output error")

        monkeypatch.setattr(os, "fsync", disk_error)
        with pytest.raises(OSError, match="Input/output error"):
            backend.write_text("c.json", "v2")

        assert backend.read_text("c.json") == "v1"
        assert [p.name for p in tmp_path.iterdir()] == ["c.json"]

    def test_create_only_on_an_existing_key_is_refused_by_the_atomic_link(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The atomic link itself refuses an existing key, with no temp file left behind.
        The exclusive-create fallback is only for volumes that can't hard-link."""
        from entrabot.storage.blob import ConcurrencyError

        backend = LocalBackend(tmp_path)
        backend.write_text("c.txt", "winner")
        monkeypatch.setattr(backend_module, "_create_exclusively", _must_not_run)

        with pytest.raises(ConcurrencyError):
            backend.write_text("c.txt", "loser", if_absent=True)

        assert backend.read_text("c.txt") == "winner"
        assert [p.name for p in tmp_path.iterdir()] == ["c.txt"]

    def test_create_only_falls_back_to_an_exclusive_create_without_hard_links(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from entrabot.storage.blob import ConcurrencyError

        attempts: list[tuple[object, object]] = []

        def no_hard_links(src: object, dst: object, *args: object, **kwargs: object) -> None:
            attempts.append((src, dst))
            raise PermissionError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr(os, "link", no_hard_links)
        backend = LocalBackend(tmp_path)

        etag = backend.write_text("nested/c.txt", "v1", if_absent=True)

        assert len(attempts) == 1, "hard-linking is tried first"
        assert backend.read_text("nested/c.txt") == "v1"
        assert etag == backend.read_text_with_etag("nested/c.txt")[1]
        assert [p.name for p in (tmp_path / "nested").iterdir()] == ["c.txt"]
        with pytest.raises(ConcurrencyError):  # and the fallback still refuses an existing key
            backend.write_text("nested/c.txt", "v2", if_absent=True)
        assert backend.read_text("nested/c.txt") == "v1"

    def test_list_does_not_return_a_leftover_temp_file(self, tmp_path: Path) -> None:
        """A process that dies mid-write leaves its temp file behind, and a list taken
        during a write can see one in flight. Neither is a key."""
        backend = LocalBackend(tmp_path)
        backend.write_text("interactions/a.jsonl", "1")
        (tmp_path / "interactions" / ".tmp-0123456789abcdef").write_text("partial")

        assert backend.list() == ["interactions/a.jsonl"]
        assert backend.list("interactions/") == ["interactions/a.jsonl"]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_the_temp_file_is_not_readable_by_others_while_it_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The temp file used to be created with the process umask and given the target's mode
        only once it was full. With a 0o600 target and umask 022 the value was readable by
        others for the whole write, and for good if the process died then."""
        backend = LocalBackend(tmp_path)
        backend.write_text("secret.json", "v1")
        (tmp_path / "secret.json").chmod(0o600)
        modes: list[int] = []
        real_fsync = os.fsync

        def record_mode_then_sync(fd: int) -> None:
            modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", record_mode_then_sync)
        previous_umask = os.umask(0o022)
        try:
            backend.write_text("secret.json", "v2")
        finally:
            os.umask(previous_umask)

        assert modes == [0o600]
        assert backend.read_text("secret.json") == "v2"
        assert stat.S_IMODE((tmp_path / "secret.json").stat().st_mode) == 0o600

    @pytest.mark.parametrize("create_only", [False, True])
    def test_the_temp_file_has_a_short_fixed_name_in_the_keys_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, create_only: bool
    ) -> None:
        """The key's own name is not part of it, so a long key can't make it too long, and
        it is the shape ``list`` and the migration helper hide."""
        moved: list[Path] = []
        real_replace, real_link = os.replace, os.link

        def replace(src: Path, dst: Path) -> None:
            moved.append(Path(src))
            real_replace(src, dst)

        def link(src: Path, dst: Path, *args: object, **kwargs: object) -> None:
            moved.append(Path(src))
            real_link(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(os, "link", link)

        LocalBackend(tmp_path).write_text("nested/some-key.json", "v1", if_absent=create_only)

        assert len(moved) == 1
        temp = moved[0]
        assert temp.parent == tmp_path / "nested"
        assert temp.name.startswith(".tmp-")
        digits = temp.name.removeprefix(".tmp-")
        assert len(digits) == 16
        assert set(digits) <= set("0123456789abcdef")
        assert backend_module.is_temp_file_name(temp.name)

    @pytest.mark.skipif(sys.platform == "win32", reason="MAX_PATH bites before the name limit")
    def test_a_key_near_the_file_name_limit_can_be_written(self, tmp_path: Path) -> None:
        """The temp name used to contain the key's own, so a name near the 255-byte limit made
        the write itself fail."""
        backend = LocalBackend(tmp_path)
        overwritten, created = "o" * 240, "c" * 240

        backend.write_text(overwritten, "v1")
        backend.write_text(overwritten, "v2")  # an overwrite goes through a temp file
        backend.write_text(created, "v1", if_absent=True)  # and so does a create-only write

        assert backend.read_text(overwritten) == "v2"
        assert backend.read_text(created) == "v1"
        assert sorted(backend.list()) == sorted([overwritten, created])
        assert sorted(p.name for p in tmp_path.iterdir()) == sorted([overwritten, created])

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_overwrite_keeps_the_existing_files_mode(self, tmp_path: Path) -> None:
        """A replacement file is born with the default mode, which could widen a file the
        operator restricted. 0o750 is a mode no umask can give a newly created file."""
        backend = LocalBackend(tmp_path)
        backend.write_text("c.json", "v1")
        (tmp_path / "c.json").chmod(0o750)
        # Read back rather than assume: Windows only keeps a file's read-only bit.
        mode = stat.S_IMODE((tmp_path / "c.json").stat().st_mode)

        backend.write_text("c.json", "v2")

        assert backend.read_text("c.json") == "v2"
        assert stat.S_IMODE((tmp_path / "c.json").stat().st_mode) == mode

    def test_a_replace_blocked_by_a_reader_on_windows_is_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Readers take no lock, and on Windows a file open for reading can't be replaced."""
        backend = LocalBackend(tmp_path)
        backend.write_text("c.json", "v1")
        real_replace = os.replace
        attempts: list[int] = []

        def blocked_twice(source: object, target: object) -> None:
            attempts.append(1)
            if len(attempts) <= 2:
                raise PermissionError(errno.EACCES, "in use")
            real_replace(source, target)

        monkeypatch.setattr(backend_module, "_REPLACE_CAN_BE_BLOCKED", True)
        monkeypatch.setattr(backend_module, "_REPLACE_RETRY_SECONDS", 0)
        monkeypatch.setattr(backend_module.os, "replace", blocked_twice)

        backend.write_text("c.json", "v2")

        assert len(attempts) == 3
        assert backend.read_text("c.json") == "v2"

    def test_a_replace_that_stays_blocked_fails_and_keeps_the_old_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = LocalBackend(tmp_path)
        backend.write_text("c.json", "v1")
        attempts: list[int] = []

        def always_blocked(_source: object, _target: object) -> None:
            attempts.append(1)
            raise PermissionError(errno.EACCES, "in use")

        monkeypatch.setattr(backend_module, "_REPLACE_CAN_BE_BLOCKED", True)
        monkeypatch.setattr(backend_module, "_REPLACE_RETRY_SECONDS", 0)
        monkeypatch.setattr(backend_module.os, "replace", always_blocked)

        with pytest.raises(PermissionError):
            backend.write_text("c.json", "v2")

        assert len(attempts) == backend_module._REPLACE_ATTEMPTS
        assert backend.read_text("c.json") == "v1"
        assert backend.list() == ["c.json"]

    def test_elsewhere_a_permission_error_on_replace_is_real_and_not_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = LocalBackend(tmp_path)
        backend.write_text("c.json", "v1")
        attempts: list[int] = []

        def denied(_source: object, _target: object) -> None:
            attempts.append(1)
            raise PermissionError(errno.EACCES, "denied")

        monkeypatch.setattr(backend_module, "_REPLACE_CAN_BE_BLOCKED", False)
        monkeypatch.setattr(backend_module.os, "replace", denied)

        with pytest.raises(PermissionError):
            backend.write_text("c.json", "v2")

        assert len(attempts) == 1


class TestBlobBackendEtag:
    def test_read_with_etag_absent_returns_none_none(self) -> None:
        backend = BlobBackend(_FakeBlobStore())
        assert backend.read_text_with_etag("absent") == (None, None)

    def test_read_with_etag_present_returns_content_and_etag(self) -> None:
        store = _FakeBlobStore()
        backend = BlobBackend(store)
        backend.write_text("c.json", "hello")
        content, etag = backend.read_text_with_etag("c.json")
        assert content == "hello"
        assert etag

    def test_conditional_write_raises_concurrency_on_stale(self) -> None:
        from entrabot.storage.blob import ConcurrencyError

        store = _FakeBlobStore()
        backend = BlobBackend(store)
        backend.write_text("c.json", "v1")
        _, etag = backend.read_text_with_etag("c.json")
        backend.write_text("c.json", "raced")  # bumps etag
        with pytest.raises(ConcurrencyError):
            backend.write_text("c.json", "v2", if_match=etag)

    def test_if_absent_creates_missing_key(self) -> None:
        backend = BlobBackend(_FakeBlobStore())
        etag = backend.write_text("c.txt", "v1", if_absent=True)
        assert backend.read_text_with_etag("c.txt") == ("v1", etag)

    def test_if_absent_refuses_to_overwrite_existing_key(self) -> None:
        from entrabot.storage.blob import ConcurrencyError

        backend = BlobBackend(_FakeBlobStore())
        backend.write_text("c.txt", "winner")
        with pytest.raises(ConcurrencyError):
            backend.write_text("c.txt", "loser", if_absent=True)
        assert backend.read_text("c.txt") == "winner"

    def test_if_absent_and_if_match_are_mutually_exclusive(self) -> None:
        backend = BlobBackend(_FakeBlobStore())
        with pytest.raises(ValueError):
            backend.write_text("c.txt", "v1", if_match="etag", if_absent=True)

    def test_unconditional_write_does_not_request_create_only(self) -> None:
        """Stores predating ``if_none_match`` keep working: the kwarg is only
        passed when a caller actually asks for a create-only write."""

        class LegacyStore(_FakeBlobStore):
            async def put(  # type: ignore[override]
                self, path: str, data: bytes, *, if_match: str | None = None
            ) -> str:
                return await super().put(path, data, if_match=if_match)

        backend = BlobBackend(LegacyStore())
        backend.write_text("c.txt", "v1")
        assert backend.read_text("c.txt") == "v1"


# ---------------------------------------------------------------------------
# get_backend factory
# ---------------------------------------------------------------------------
class TestGetBackend:
    def test_default_returns_local_rooted_at_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("ENTRABOT_BLOB_ENDPOINT", raising=False)
        monkeypatch.delenv("ENTRABOT_BLOB_CONTAINER", raising=False)
        # Phase 5 will introduce cloud branching; for Phase 2 default = Local.
        backend = get_backend()
        assert isinstance(backend, LocalBackend)
        backend.write_text("probe.txt", "ok")
        assert (tmp_path / "probe.txt").read_text() == "ok"

    def test_keep_memory_local_flag_forces_local(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("ENTRABOT_KEEP_MEMORY_LOCAL", "true")
        assert isinstance(get_backend(), LocalBackend)

    def test_blob_endpoint_set_returns_blob_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When blob_endpoint and blob_container are both set and
        keep_memory_local is False, get_backend() returns a BlobBackend.
        """
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("ENTRABOT_BLOB_ENDPOINT", "https://entclaw.blob.core.windows.net")
        monkeypatch.setenv("ENTRABOT_BLOB_CONTAINER", "agent-abc-123")
        monkeypatch.delenv("ENTRABOT_KEEP_MEMORY_LOCAL", raising=False)
        # Stub the storage-token acquisition so this doesn't hit Entra
        monkeypatch.setattr(
            "entrabot.storage.backend.acquire_agent_user_storage_token",
            lambda cfg: "fake-storage-token",
        )
        backend = get_backend()
        assert isinstance(backend, BlobBackend)

    def test_blob_backend_invalidates_cached_storage_token_on_401(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#128: the BlobStore built by get_backend() must drop the cached
        storage token when Azure answers 401, so the retry gets a fresh one."""
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("ENTRABOT_BLOB_ENDPOINT", "https://entclaw.blob.core.windows.net")
        monkeypatch.setenv("ENTRABOT_BLOB_CONTAINER", "agent-abc-123")
        monkeypatch.delenv("ENTRABOT_KEEP_MEMORY_LOCAL", raising=False)
        invalidated: list[object] = []
        monkeypatch.setattr(
            "entrabot.storage.backend.invalidate_agent_user_storage_token",
            lambda cfg, *, rejected_token: invalidated.append(rejected_token),
        )
        backend = get_backend()
        assert isinstance(backend, BlobBackend)
        assert backend._store._on_unauthorized is not None
        backend._store._on_unauthorized("rejected-tok")
        assert invalidated == ["rejected-tok"]

    def test_half_configured_blob_endpoint_without_container_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F2: half-configured cloud (endpoint without container) is a HARD
        misconfiguration, not a silent Local fallback.

        A silent fallback is exactly how a mis-enved fleet instance ends up on
        an empty Local store and re-bootstraps every chat → replay flood. Fail
        loud so the operator sees it instead of the fleet diverging.
        """
        from entrabot.errors import BackendMisconfiguredError

        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("ENTRABOT_BLOB_ENDPOINT", "https://entclaw.blob.core.windows.net")
        monkeypatch.delenv("ENTRABOT_BLOB_CONTAINER", raising=False)
        monkeypatch.delenv("ENTRABOT_KEEP_MEMORY_LOCAL", raising=False)
        with pytest.raises(BackendMisconfiguredError):
            get_backend()

    def test_half_configured_blob_container_without_endpoint_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from entrabot.errors import BackendMisconfiguredError

        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("ENTRABOT_BLOB_ENDPOINT", raising=False)
        monkeypatch.setenv("ENTRABOT_BLOB_CONTAINER", "agent-abc-123")
        monkeypatch.delenv("ENTRABOT_KEEP_MEMORY_LOCAL", raising=False)
        with pytest.raises(BackendMisconfiguredError):
            get_backend()

    def test_keep_memory_local_suppresses_half_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The explicit local escape hatch wins even over a half-config, so an
        operator can always force Local without untangling blob env first."""
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("ENTRABOT_BLOB_ENDPOINT", "https://entclaw.blob.core.windows.net")
        monkeypatch.delenv("ENTRABOT_BLOB_CONTAINER", raising=False)
        monkeypatch.setenv("ENTRABOT_KEEP_MEMORY_LOCAL", "true")
        assert isinstance(get_backend(), LocalBackend)

    def test_keep_memory_local_overrides_blob_endpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with blob endpoint configured, the escape-hatch flag wins."""
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("ENTRABOT_BLOB_ENDPOINT", "https://entclaw.blob.core.windows.net")
        monkeypatch.setenv("ENTRABOT_BLOB_CONTAINER", "agent-abc-123")
        monkeypatch.setenv("ENTRABOT_KEEP_MEMORY_LOCAL", "true")
        assert isinstance(get_backend(), LocalBackend)


# ---------------------------------------------------------------------------
# assert_backend_config — boot-time uniform-backend assertion (F2)
# ---------------------------------------------------------------------------
class TestAssertBackendConfig:
    def test_returns_local_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from entrabot.storage.backend import assert_backend_config

        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("ENTRABOT_BLOB_ENDPOINT", raising=False)
        monkeypatch.delenv("ENTRABOT_BLOB_CONTAINER", raising=False)
        summary = assert_backend_config()
        assert summary["backend"] == "LocalBackend"

    def test_returns_blob_summary_with_container(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from entrabot.storage.backend import assert_backend_config

        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("ENTRABOT_BLOB_ENDPOINT", "https://entclaw.blob.core.windows.net")
        monkeypatch.setenv("ENTRABOT_BLOB_CONTAINER", "agent-abc-123")
        monkeypatch.delenv("ENTRABOT_KEEP_MEMORY_LOCAL", raising=False)
        monkeypatch.setattr(
            "entrabot.storage.backend.acquire_agent_user_storage_token",
            lambda cfg: "fake-storage-token",
        )
        summary = assert_backend_config()
        assert summary["backend"] == "BlobBackend"
        assert summary["container"] == "agent-abc-123"

    def test_raises_on_half_configured_blob(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from entrabot.errors import BackendMisconfiguredError
        from entrabot.storage.backend import assert_backend_config

        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("ENTRABOT_BLOB_ENDPOINT", "https://entclaw.blob.core.windows.net")
        monkeypatch.delenv("ENTRABOT_BLOB_CONTAINER", raising=False)
        monkeypatch.delenv("ENTRABOT_KEEP_MEMORY_LOCAL", raising=False)
        with pytest.raises(BackendMisconfiguredError):
            assert_backend_config()
