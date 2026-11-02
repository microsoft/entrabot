"""The suite must never touch a developer's real agent memory, local or cloud."""

from __future__ import annotations

from pathlib import Path

from entrabot.config import get_config
from entrabot.storage.backend import LocalBackend, get_backend


def test_default_data_dir_is_under_the_test_tmp_path(tmp_path: Path) -> None:
    assert get_config().data_dir.is_relative_to(tmp_path)


def test_default_backend_is_local_even_with_cloud_memory_configured() -> None:
    assert isinstance(get_backend(), LocalBackend)
