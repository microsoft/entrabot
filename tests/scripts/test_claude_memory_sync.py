"""Tests for scripts/claude_memory_sync.py (ADR-005 Phase 6a).

The script is a thin CLI wrapper over :class:`PersonaBackend`:
  - ``pull``       — download all ``claude_memory/`` blobs into the
                     Claude Code memory dir
  - ``push``       — upload every local memory file not already in cloud
  - ``push-one``   — upload a single path (hot path from PostToolUse hook)

Tests inject a fake ``PersonaBackend`` via ``--backend-factory-for-tests``
or via module-level monkeypatching, so we don't need a live blob store.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "claude_memory_sync.py"


@pytest.fixture
def sync_module():
    spec = importlib.util.spec_from_file_location("claude_memory_sync", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["claude_memory_sync"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("claude_memory_sync", None)


class _FakeBackend:
    """In-memory MemoryBackend stand-in — minimal subset."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def read_text(self, key: str) -> str | None:
        return self.store.get(key)

    def write_text(self, key: str, content: str) -> None:
        self.store[key] = content

    def append_text(self, key: str, content: str) -> None:
        self.store[key] = self.store.get(key, "") + content

    def exists(self, key: str) -> bool:
        return key in self.store

    def list(self, prefix: str = "") -> list[str]:
        return [k for k in self.store if k.startswith(prefix)]


class TestPushOneSubcommand:
    def test_pushes_single_file(
        self, sync_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENTRACLAW_PERSONA_SYNC", "on")
        backend = _FakeBackend()
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        target = mem_dir / "user_brandon_role.md"
        target.write_text("Product Architect")

        monkeypatch.setattr(sync_module, "_resolve_backend", lambda: backend)
        monkeypatch.setattr(sync_module, "_resolve_memory_dir", lambda: mem_dir)

        rc = sync_module.main(["push-one", str(target)])

        assert rc == 0
        assert backend.read_text("claude_memory/user_brandon_role.md") == "Product Architect"

    def test_push_one_ignores_path_outside_memory_dir(
        self,
        sync_module,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("ENTRACLAW_PERSONA_SYNC", "on")
        backend = _FakeBackend()
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        outside = tmp_path / "other.md"
        outside.write_text("not a memory file")

        monkeypatch.setattr(sync_module, "_resolve_backend", lambda: backend)
        monkeypatch.setattr(sync_module, "_resolve_memory_dir", lambda: mem_dir)

        rc = sync_module.main(["push-one", str(outside)])

        # Non-zero but not raising — hooks must not crash Claude Code
        assert rc == 0
        assert backend.store == {}


class TestPushSubcommand:
    def test_pushes_every_memory_file(
        self, sync_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENTRACLAW_PERSONA_SYNC", "on")
        backend = _FakeBackend()
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        (mem_dir / "MEMORY.md").write_text("index")
        (mem_dir / "user_brandon_role.md").write_text("Brandon")

        monkeypatch.setattr(sync_module, "_resolve_backend", lambda: backend)
        monkeypatch.setattr(sync_module, "_resolve_memory_dir", lambda: mem_dir)

        rc = sync_module.main(["push"])

        assert rc == 0
        assert backend.read_text("claude_memory/MEMORY.md") == "index"
        assert backend.read_text("claude_memory/user_brandon_role.md") == "Brandon"


class TestPullSubcommand:
    def test_pull_downloads_all_claude_memory_keys(
        self, sync_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENTRACLAW_PERSONA_SYNC", "on")
        backend = _FakeBackend()
        backend.store["claude_memory/MEMORY.md"] = "index"
        backend.store["claude_memory/user_brandon_role.md"] = "Brandon"
        # Non-persona key must stay in cloud and NOT land locally
        backend.store["interactions/2026-04-17.jsonl"] = "agent ops"

        mem_dir = tmp_path / "memory"
        monkeypatch.setattr(sync_module, "_resolve_backend", lambda: backend)
        monkeypatch.setattr(sync_module, "_resolve_memory_dir", lambda: mem_dir)

        rc = sync_module.main(["pull"])

        assert rc == 0
        assert (mem_dir / "MEMORY.md").read_text() == "index"
        assert (mem_dir / "user_brandon_role.md").read_text() == "Brandon"
        assert not (mem_dir / "interactions").exists()


class TestFeatureFlag:
    def test_pull_noop_when_persona_sync_off(
        self,
        sync_module,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.delenv("ENTRACLAW_PERSONA_SYNC", raising=False)

        # _resolve_backend must not be called when flag is off
        def boom() -> None:
            raise AssertionError("must not reach backend when flag is off")

        monkeypatch.setattr(sync_module, "_resolve_backend", boom)
        monkeypatch.setattr(sync_module, "_resolve_memory_dir", lambda: tmp_path / "memory")

        rc = sync_module.main(["pull"])
        assert rc == 0

    def test_push_one_runs_even_without_flag(
        self, sync_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # push-one IS the hot-path hook — it respects the flag too. When
        # flag is off it must silently succeed so the hook doesn't leak
        # noise into Claude Code.
        monkeypatch.delenv("ENTRACLAW_PERSONA_SYNC", raising=False)
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        f = mem_dir / "feedback_tone.md"
        f.write_text("content")

        def boom() -> None:
            raise AssertionError("must not reach backend when flag is off")

        monkeypatch.setattr(sync_module, "_resolve_backend", boom)
        monkeypatch.setattr(sync_module, "_resolve_memory_dir", lambda: mem_dir)

        rc = sync_module.main(["push-one", str(f)])
        assert rc == 0
