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

from pathlib import Path

import pytest

from entrabot.storage.backend import (
    BlobBackend,
    LocalBackend,
    MemoryBackend,
    get_backend,
)


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
    (honors ``if_match`` for optimistic concurrency), ``exists``, ``list``.
    """

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self._counter = 0

    async def get(self, path: str) -> bytes:
        if path not in self.data:
            raise KeyError(path)
        return self.data[path]

    async def get_with_etag(self, path: str) -> tuple[bytes, str]:
        if path not in self.data:
            raise KeyError(path)
        return self.data[path], self.etags[path]

    async def put(self, path: str, data: bytes, *, if_match: str | None = None) -> str:
        from entrabot.storage.blob import ConcurrencyError

        if if_match is not None and self.etags.get(path) != if_match:
            raise ConcurrencyError(f"put({path!r}) refused: If-Match={if_match!r} stale")
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
