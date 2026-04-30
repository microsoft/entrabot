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

from entraclaw.storage.backend import (
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
    async ``get`` (raises KeyError on miss), ``put``, ``exists``, ``list``.
    """

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def get(self, path: str) -> bytes:
        if path not in self.data:
            raise KeyError(path)
        return self.data[path]

    async def put(self, path: str, data: bytes, *, if_match: str | None = None) -> str:
        self.data[path] = data
        return f'"etag-{len(self.data)}"'

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
# get_backend factory
# ---------------------------------------------------------------------------
class TestGetBackend:
    def test_default_returns_local_rooted_at_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENTRACLAW_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("ENTRACLAW_BLOB_ENDPOINT", raising=False)
        monkeypatch.delenv("ENTRACLAW_BLOB_CONTAINER", raising=False)
        # Phase 5 will introduce cloud branching; for Phase 2 default = Local.
        backend = get_backend()
        assert isinstance(backend, LocalBackend)
        backend.write_text("probe.txt", "ok")
        assert (tmp_path / "probe.txt").read_text() == "ok"

    def test_keep_memory_local_flag_forces_local(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENTRACLAW_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("ENTRACLAW_KEEP_MEMORY_LOCAL", "true")
        assert isinstance(get_backend(), LocalBackend)

    def test_blob_endpoint_set_returns_blob_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When blob_endpoint and blob_container are both set and
        keep_memory_local is False, get_backend() returns a BlobBackend.
        """
        monkeypatch.setenv("ENTRACLAW_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("ENTRACLAW_BLOB_ENDPOINT", "https://entclaw.blob.core.windows.net")
        monkeypatch.setenv("ENTRACLAW_BLOB_CONTAINER", "agent-abc-123")
        monkeypatch.delenv("ENTRACLAW_KEEP_MEMORY_LOCAL", raising=False)
        # Stub the storage-token acquisition so this doesn't hit Entra
        monkeypatch.setattr(
            "entraclaw.storage.backend.acquire_agent_user_storage_token",
            lambda cfg: "fake-storage-token",
        )
        backend = get_backend()
        assert isinstance(backend, BlobBackend)

    def test_blob_endpoint_without_container_falls_back_local(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Half-configured cloud (endpoint without container) falls back
        to Local — better safe than panicking inside the hot path.
        """
        monkeypatch.setenv("ENTRACLAW_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("ENTRACLAW_BLOB_ENDPOINT", "https://entclaw.blob.core.windows.net")
        monkeypatch.delenv("ENTRACLAW_BLOB_CONTAINER", raising=False)
        monkeypatch.delenv("ENTRACLAW_KEEP_MEMORY_LOCAL", raising=False)
        assert isinstance(get_backend(), LocalBackend)

    def test_keep_memory_local_overrides_blob_endpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with blob endpoint configured, the escape-hatch flag wins."""
        monkeypatch.setenv("ENTRACLAW_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("ENTRACLAW_BLOB_ENDPOINT", "https://entclaw.blob.core.windows.net")
        monkeypatch.setenv("ENTRACLAW_BLOB_CONTAINER", "agent-abc-123")
        monkeypatch.setenv("ENTRACLAW_KEEP_MEMORY_LOCAL", "true")
        assert isinstance(get_backend(), LocalBackend)
