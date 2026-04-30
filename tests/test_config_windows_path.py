"""Tests for Windows-specific config paths, migration, and runtime guard.

Windows places per-user data under ``%LOCALAPPDATA%\\entraclaw\\`` instead of
``~/.entraclaw/``. These tests are mock-based — they patch ``sys.platform``
and ``os.environ`` so they always run, including on Mac CI.

Covers:
- ``_default_dir`` resolves to ``%LOCALAPPDATA%\\entraclaw\\<sub>`` on win32.
- ``_default_dir`` falls back to ``~/AppData/Local/entraclaw\\<sub>`` if
  ``%LOCALAPPDATA%`` is unset (rare but seen on stripped runners).
- ``migrate_legacy_data_dir`` moves content from legacy to target when
  target is empty/missing; idempotent on re-runs.
- ``check_legacy_data_dir`` runtime guard halts loud when legacy is
  non-empty AND target is empty/missing.
- Non-Windows platforms keep ``~/.entraclaw/`` unchanged; both helpers
  no-op.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from entraclaw import config


class TestDefaultDirWindows:
    def test_uses_localappdata_on_win32(self, tmp_path: Path) -> None:
        local = tmp_path / "Local"
        local.mkdir()
        with (
            patch.object(config.sys, "platform", "win32"),
            patch.dict(os.environ, {"LOCALAPPDATA": str(local)}, clear=False),
        ):
            result = config._default_dir("logs")
        assert result == local / "entraclaw" / "logs"

    def test_falls_back_to_appdata_local_when_env_missing(self, tmp_path: Path) -> None:
        env = {k: v for k, v in os.environ.items() if k != "LOCALAPPDATA"}
        with (
            patch.object(config.sys, "platform", "win32"),
            patch.object(Path, "home", return_value=tmp_path),
            patch.dict(os.environ, env, clear=True),
        ):
            result = config._default_dir("data")
        assert result == tmp_path / "AppData" / "Local" / "entraclaw" / "data"

    def test_non_windows_keeps_dotentraclaw(self, tmp_path: Path) -> None:
        with (
            patch.object(config.sys, "platform", "darwin"),
            patch.object(Path, "home", return_value=tmp_path),
        ):
            result = config._default_dir("audit")
        assert result == tmp_path / ".entraclaw" / "audit"


class TestMigrateLegacyDataDir:
    def test_no_op_on_non_windows(self, tmp_path: Path) -> None:
        legacy = tmp_path / ".entraclaw"
        legacy.mkdir()
        (legacy / "logs").mkdir()
        (legacy / "logs" / "x.log").write_text("data")
        with patch.object(config.sys, "platform", "darwin"):
            moved = config.migrate_legacy_data_dir(home=tmp_path)
        assert moved is False
        assert (legacy / "logs" / "x.log").exists()

    def test_no_op_when_legacy_missing(self, tmp_path: Path) -> None:
        local = tmp_path / "Local"
        local.mkdir()
        with (
            patch.object(config.sys, "platform", "win32"),
            patch.dict(os.environ, {"LOCALAPPDATA": str(local)}, clear=False),
        ):
            moved = config.migrate_legacy_data_dir(home=tmp_path)
        assert moved is False

    def test_no_op_when_legacy_empty(self, tmp_path: Path) -> None:
        legacy = tmp_path / ".entraclaw"
        legacy.mkdir()
        local = tmp_path / "Local"
        local.mkdir()
        with (
            patch.object(config.sys, "platform", "win32"),
            patch.dict(os.environ, {"LOCALAPPDATA": str(local)}, clear=False),
        ):
            moved = config.migrate_legacy_data_dir(home=tmp_path)
        assert moved is False

    def test_moves_legacy_to_target_when_target_missing(self, tmp_path: Path) -> None:
        legacy = tmp_path / ".entraclaw"
        (legacy / "logs").mkdir(parents=True)
        (legacy / "logs" / "a.log").write_text("alpha")
        (legacy / "data").mkdir()
        (legacy / "data" / "b.json").write_text("{}")
        local = tmp_path / "Local"
        local.mkdir()
        with (
            patch.object(config.sys, "platform", "win32"),
            patch.dict(os.environ, {"LOCALAPPDATA": str(local)}, clear=False),
        ):
            moved = config.migrate_legacy_data_dir(home=tmp_path)
        assert moved is True
        target = local / "entraclaw"
        assert (target / "logs" / "a.log").read_text() == "alpha"
        assert (target / "data" / "b.json").read_text() == "{}"

    def test_halts_when_both_dirs_have_content(self, tmp_path: Path) -> None:
        legacy = tmp_path / ".entraclaw"
        (legacy / "logs").mkdir(parents=True)
        (legacy / "logs" / "a.log").write_text("alpha")
        local = tmp_path / "Local"
        target = local / "entraclaw" / "logs"
        target.mkdir(parents=True)
        (target / "b.log").write_text("beta")
        with (
            patch.object(config.sys, "platform", "win32"),
            patch.dict(os.environ, {"LOCALAPPDATA": str(local)}, clear=False),
            pytest.raises(RuntimeError, match="two entraclaw dirs"),
        ):
            config.migrate_legacy_data_dir(home=tmp_path)

    def test_idempotent_when_target_already_populated_and_legacy_gone(self, tmp_path: Path) -> None:
        local = tmp_path / "Local"
        target = local / "entraclaw" / "logs"
        target.mkdir(parents=True)
        (target / "a.log").write_text("alpha")
        with (
            patch.object(config.sys, "platform", "win32"),
            patch.dict(os.environ, {"LOCALAPPDATA": str(local)}, clear=False),
        ):
            moved = config.migrate_legacy_data_dir(home=tmp_path)
        assert moved is False


class TestCheckLegacyDataDir:
    def test_no_op_on_non_windows(self, tmp_path: Path) -> None:
        legacy = tmp_path / ".entraclaw"
        (legacy / "logs").mkdir(parents=True)
        (legacy / "logs" / "x").write_text("y")
        with patch.object(config.sys, "platform", "linux"):
            config.check_legacy_data_dir(home=tmp_path)

    def test_passes_when_legacy_missing(self, tmp_path: Path) -> None:
        local = tmp_path / "Local"
        local.mkdir()
        with (
            patch.object(config.sys, "platform", "win32"),
            patch.dict(os.environ, {"LOCALAPPDATA": str(local)}, clear=False),
        ):
            config.check_legacy_data_dir(home=tmp_path)

    def test_passes_when_target_already_populated(self, tmp_path: Path) -> None:
        legacy = tmp_path / ".entraclaw"
        (legacy / "logs").mkdir(parents=True)
        (legacy / "logs" / "x").write_text("y")
        local = tmp_path / "Local"
        target = local / "entraclaw"
        (target / "logs").mkdir(parents=True)
        (target / "logs" / "x").write_text("y")
        with (
            patch.object(config.sys, "platform", "win32"),
            patch.dict(os.environ, {"LOCALAPPDATA": str(local)}, clear=False),
        ):
            config.check_legacy_data_dir(home=tmp_path)

    def test_raises_when_legacy_populated_and_target_empty(self, tmp_path: Path) -> None:
        legacy = tmp_path / ".entraclaw"
        (legacy / "logs").mkdir(parents=True)
        (legacy / "logs" / "x").write_text("y")
        local = tmp_path / "Local"
        local.mkdir()
        with (
            patch.object(config.sys, "platform", "win32"),
            patch.dict(os.environ, {"LOCALAPPDATA": str(local)}, clear=False),
            pytest.raises(RuntimeError, match="setup-windows.cmd --migrate"),
        ):
            config.check_legacy_data_dir(home=tmp_path)
