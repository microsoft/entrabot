"""Guard against re-introducing the failed PTY-supervisor / terminal-watcher modules.

Phase 3 of the Copilot-CLI watcher plan deleted ``src/entrabot/supervise.py`` and
``src/entrabot/watch.py`` (along with their ``entrabot-supervise`` / ``entrabot-watch``
console scripts) once ``wait_for_sponsor_dm`` shipped in PR #46. The wait-tool is the
only sanctioned wake mechanism — any new code that imports the removed modules is a
regression toward the failed approach. This test fails loudly if it happens.

See ``docs/runbooks/hard-won-learnings.md`` Learning #49 for why the long-blocking
MCP tool replaces the PTY supervisor.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"

FORBIDDEN_IMPORTS = (
    "from entrabot.supervise",
    "import entrabot.supervise",
    "from entrabot.watch ",
    "from entrabot.watch\n",
    "import entrabot.watch",
)

FORBIDDEN_SCRIPTS = ("entrabot-supervise", "entrabot-watch")


def _python_files() -> list[Path]:
    return [p for p in SRC_ROOT.rglob("*.py") if p.is_file()]


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_source_does_not_import_removed_modules(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for needle in FORBIDDEN_IMPORTS:
        assert needle not in text, (
            f"{path.relative_to(REPO_ROOT)} imports a removed module ({needle!r}). "
            "Use wait_for_sponsor_dm instead — see Learning #49."
        )


def test_pyproject_does_not_advertise_removed_console_scripts() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for script in FORBIDDEN_SCRIPTS:
        assert script not in text, (
            f"pyproject.toml still advertises removed console script {script!r}. "
            "Phase 3 of the Copilot-CLI watcher plan removed it."
        )


def test_removed_modules_not_present_on_disk() -> None:
    for relpath in ("src/entrabot/supervise.py", "src/entrabot/watch.py"):
        assert not (REPO_ROOT / relpath).exists(), (
            f"{relpath} reappeared. The PTY-supervisor / terminal-watcher path "
            "was retired in favor of wait_for_sponsor_dm — see Learning #49."
        )
