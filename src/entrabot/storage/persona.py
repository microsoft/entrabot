"""PersonaBackend + Claude Code memory directory helper (ADR-005 Phase 6a).

This module extends the cloud-memory backend from Phase 2/5 to cover the
Claude Code per-project auto-memory directory. Where the agent's
operational state (interaction log, summaries, cursors) lives under the
blob container root, the Claude Code persona-memory files live under the
``claude_memory/`` prefix in the same container.

The abstraction is intentionally thin: :class:`PersonaBackend` is a
convenience wrapper over an existing :class:`MemoryBackend`, adding the
three directory-level operations that the per-key protocol does not
have — ``pull_all``, ``push_all``, ``push_one``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from entrabot.storage.backend import MemoryBackend

PERSONA_PREFIX = "claude_memory"


def claude_code_memory_dir(project_root: Path, *, home: Path | None = None) -> Path:
    """Resolve the Claude Code per-project auto-memory directory.

    Claude Code stores each project's memory at
    ``~/.claude/projects/<slug>/memory`` where ``<slug>`` is the project
    absolute path with every ``/`` and space replaced by ``-``.

    The directory may not exist (a project Claude Code has never seen
    yet, or a user without Claude Code installed) — the caller is
    responsible for ``.exists()`` before reading.

    *home* is injected in tests; in production it defaults to
    :func:`Path.home`.
    """
    home_dir = home if home is not None else Path.home()
    # Replace both POSIX and Windows path separators so the Windows slug
    # matches what Claude Code itself writes (Mac/Linux test fixtures
    # pass POSIX-style absolute paths; Windows path stringification
    # uses backslashes).
    slug = str(project_root).replace("\\", "-").replace("/", "-").replace(" ", "-")
    return home_dir / ".claude" / "projects" / slug / "memory"


@dataclass
class PersonaReport:
    """Summary of a persona-sync operation."""

    copied: int = 0
    skipped: int = 0
    pulled: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)


class PersonaBackend:
    """Thin wrapper over an existing backend, scoped to ``claude_memory/``.

    The wrapped ``MemoryBackend`` owns key storage; PersonaBackend adds
    the file-tree semantics Claude Code expects. No caching — the hot
    path is exactly "one file write → one blob PUT" and "session start →
    list + fetch each key."
    """

    def __init__(self, backend: MemoryBackend, *, local_root: Path) -> None:
        self._backend = backend
        self._root = Path(local_root)

    @property
    def prefix(self) -> str:
        return f"{PERSONA_PREFIX}/"

    # ---------------------------------------------------------------- push
    def push_one(self, path: Path) -> None:
        """Upload *path* to the ``claude_memory/`` prefix.

        *path* must resolve under ``local_root`` (fail-closed against
        hook misconfiguration that could otherwise leak arbitrary
        files). Missing files are a silent no-op — a hook may fire
        after a rapid create+delete.
        """
        full = Path(path).resolve()
        try:
            rel = full.relative_to(self._root.resolve())
        except ValueError:
            raise ValueError(f"path {path} is outside persona root {self._root}") from None
        if not full.exists() or not full.is_file():
            return
        key = f"{self.prefix}{rel.as_posix()}"
        self._backend.write_text(key, full.read_text())

    def push_all(self) -> PersonaReport:
        """Upload every file in ``local_root`` that isn't already in the cloud."""
        report = PersonaReport()
        if not self._root.exists():
            return report
        for path in sorted(self._root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self._root).as_posix()
            key = f"{self.prefix}{rel}"
            try:
                if self._backend.exists(key):
                    report.skipped += 1
                    continue
                self._backend.write_text(key, path.read_text())
            except Exception as exc:  # noqa: BLE001
                report.errors.append((key, str(exc)))
                continue
            report.copied += 1
            report.keys.append(key)
        return report

    # ---------------------------------------------------------------- pull
    def pull_all(self) -> PersonaReport:
        """Download every blob under ``claude_memory/`` into ``local_root``.

        Cloud is authoritative on pull — local files are overwritten.
        The directory (and parents) are created if missing.
        """
        report = PersonaReport()
        for key in self._backend.list(self.prefix):
            if not key.startswith(self.prefix):
                continue
            rel = key[len(self.prefix) :]
            content = self._backend.read_text(key)
            if content is None:
                continue
            dst = self._root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(content)
            report.pulled += 1
            report.keys.append(key)
        return report
