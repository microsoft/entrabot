"""Tests for PersonaBackend + claude_code_memory_dir (Phase 6a).

PersonaBackend wraps the agent's existing :class:`MemoryBackend`
(typically :class:`BlobBackend` in production) scoped to the
``claude_memory/`` key prefix. It adds three directory-level operations
that the per-key backend doesn't have: pull_all (download everything to
a local dir), push_all (upload everything from a local dir), and
push_one (upload a single file by its absolute path).

claude_code_memory_dir resolves the Claude Code auto-memory directory
using Claude Code's slug convention: absolute project path with every
``/`` and space replaced by ``-``, rooted under
``~/.claude/projects/<slug>/memory``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from entraclaw.storage.backend import LocalBackend
from entraclaw.storage.persona import PersonaBackend, claude_code_memory_dir


class TestClaudeCodeMemoryDir:
    def test_slug_encoding_for_path_with_spaces(self, tmp_path: Path) -> None:
        # Mirrors the real Claude Code convention observed in this repo
        result = claude_code_memory_dir(
            Path("/path/to/entraclaw-identity-research"),
            home=tmp_path,
        )
        expected = (
            tmp_path
            / ".claude"
            / "projects"
            / "-path-to-entraclaw-identity-research"
            / "memory"
        )
        assert result == expected

    def test_slug_encoding_for_simple_path(self, tmp_path: Path) -> None:
        result = claude_code_memory_dir(Path("/home/alice/code/myproj"), home=tmp_path)
        expected = tmp_path / ".claude" / "projects" / "-home-alice-code-myproj" / "memory"
        assert result == expected

    def test_returns_path_even_if_it_does_not_exist(self, tmp_path: Path) -> None:
        # Caller is responsible for checking .exists() — helper just resolves
        result = claude_code_memory_dir(Path("/nope"), home=tmp_path)
        assert not result.exists()


class TestPersonaBackendPushOne:
    def test_uploads_single_file_under_claude_memory_prefix(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path / "blob")
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        (mem_dir / "user_alice_role.md").write_text("# Alice\nProduct Architect")

        persona = PersonaBackend(backend, local_root=mem_dir)
        persona.push_one(mem_dir / "user_alice_role.md")

        assert (
            backend.read_text("claude_memory/user_alice_role.md")
            == "# Alice\nProduct Architect"
        )

    def test_push_one_rejects_path_outside_local_root(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path / "blob")
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        other = tmp_path / "other.md"
        other.write_text("not a memory file")

        persona = PersonaBackend(backend, local_root=mem_dir)
        with pytest.raises(ValueError, match="outside"):
            persona.push_one(other)

    def test_push_one_silently_noops_on_missing_file(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path / "blob")
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()

        persona = PersonaBackend(backend, local_root=mem_dir)
        # File does not exist — hook may fire after a rename/delete
        persona.push_one(mem_dir / "was_deleted.md")
        # No exception, no blob created
        assert backend.list("claude_memory/") == []


class TestPersonaBackendPushAll:
    def test_uploads_every_file_in_memory_dir(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path / "blob")
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        (mem_dir / "MEMORY.md").write_text("index")
        (mem_dir / "user_alice_role.md").write_text("Alice")
        (mem_dir / "feedback_cvp_tone.md").write_text("Tone notes")

        persona = PersonaBackend(backend, local_root=mem_dir)
        report = persona.push_all()

        assert report.copied == 3
        assert backend.read_text("claude_memory/MEMORY.md") == "index"
        assert backend.read_text("claude_memory/user_alice_role.md") == "Alice"

    def test_push_all_skips_existing_keys(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path / "blob")
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        (mem_dir / "MEMORY.md").write_text("local version")
        backend.write_text("claude_memory/MEMORY.md", "cloud version")

        persona = PersonaBackend(backend, local_root=mem_dir)
        report = persona.push_all()

        assert report.skipped == 1
        assert report.copied == 0
        assert backend.read_text("claude_memory/MEMORY.md") == "cloud version"

    def test_push_all_missing_local_dir_returns_empty_report(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path / "blob")
        persona = PersonaBackend(backend, local_root=tmp_path / "missing")
        report = persona.push_all()
        assert report.copied == 0
        assert report.skipped == 0


class TestPersonaBackendPullAll:
    def test_downloads_every_claude_memory_key(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path / "blob")
        backend.write_text("claude_memory/MEMORY.md", "index body")
        backend.write_text("claude_memory/user_alice_role.md", "Alice")
        backend.write_text("claude_memory/feedback_tone.md", "register")
        # These should NOT be pulled (not under claude_memory/)
        backend.write_text("interactions/2026-04-17.jsonl", "agent op data")

        mem_dir = tmp_path / "memory"
        persona = PersonaBackend(backend, local_root=mem_dir)
        report = persona.pull_all()

        assert (mem_dir / "MEMORY.md").read_text() == "index body"
        assert (mem_dir / "user_alice_role.md").read_text() == "Alice"
        assert (mem_dir / "feedback_tone.md").read_text() == "register"
        assert not (mem_dir / "interactions").exists()
        assert report.pulled == 3

    def test_pull_all_overwrites_local_with_cloud(self, tmp_path: Path) -> None:
        # Cloud is authoritative on pull
        backend = LocalBackend(tmp_path / "blob")
        backend.write_text("claude_memory/MEMORY.md", "CLOUD")
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        (mem_dir / "MEMORY.md").write_text("stale local")

        persona = PersonaBackend(backend, local_root=mem_dir)
        persona.pull_all()

        assert (mem_dir / "MEMORY.md").read_text() == "CLOUD"

    def test_pull_all_when_cloud_empty_returns_empty(self, tmp_path: Path) -> None:
        backend = LocalBackend(tmp_path / "blob")
        persona = PersonaBackend(backend, local_root=tmp_path / "mem")
        report = persona.pull_all()
        assert report.pulled == 0
