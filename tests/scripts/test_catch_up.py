"""Tests for scripts/catch_up.py: it reads chats from the configured backend."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

import entrabot.config as config_module
from entrabot.storage.backend import BlobBackend, get_backend
from entrabot.tools.poll_state import WATCHED_CHATS_KEY

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "catch_up.py"
_spec = importlib.util.spec_from_file_location("catch_up", _SCRIPT)
catch_up = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(catch_up)

GRAPH = "https://graph.microsoft.com/v1.0"
CHAT_A = "19:chat-a@thread.v2"
OLD_CHAT = "19:old-agent-chat@thread.v2"
DECOY_CHAT = "19:decoy-in-home-directory@thread.v2"


@pytest.fixture
def graph():
    """Answer the inbox and any chat, recording which chats were read."""
    chats_read: list[str] = []

    def chat_messages(request: httpx.Request, chat_id: str) -> httpx.Response:
        chats_read.append(chat_id)
        return httpx.Response(200, json={"value": []})

    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=rf"{GRAPH}/chats/(?P<chat_id>[^/]+)/messages").mock(
            side_effect=chat_messages
        )
        router.get(f"{GRAPH}/me/messages").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        yield chats_read


@pytest.fixture(autouse=True)
def _no_argv_or_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "argv", ["catch_up.py"])
    monkeypatch.setattr(catch_up, "acquire_agent_user_token", lambda _config: "fake-token")


@pytest.fixture(autouse=True)
def _decoy_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point home at a decoy for EVERY test here, including any regression.

    This script used to hardcode ``~/.entrabot/data``, so a test run could read a
    developer's real agent data. Each test now sees decoy files in a throwaway home
    instead, and a regression shows up as a decoy chat being requested.
    """
    home = tmp_path / "decoy-home"
    data = home / ".entrabot" / "data"
    data.mkdir(parents=True)
    (data / "watched_chats").write_text(f"{DECOY_CHAT}\n")
    (data / "chat_id").write_text(f"{DECOY_CHAT}\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert Path.home() == home, "refusing to run against the real home directory"


class TestChatsComeFromTheBackend:
    async def test_cloud_list_is_used(self, fake_cloud_store, graph) -> None:
        BlobBackend(fake_cloud_store).write_text(WATCHED_CHATS_KEY, f"{CHAT_A}\n")
        await catch_up.main()
        assert graph == [CHAT_A]

    async def test_stale_local_file_is_ignored_in_cloud_mode(
        self, fake_cloud_store, graph, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
        (tmp_path / "watched_chats").write_text(f"{OLD_CHAT}\n")
        await catch_up.main()
        assert graph == []

    async def test_local_mode_honors_the_configured_data_dir(
        self, graph, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
        (tmp_path / "watched_chats").write_text(f"{CHAT_A}\n")
        await catch_up.main()
        assert graph == [CHAT_A]

    async def test_legacy_default_chat_goes_through_the_backend_first(
        self, graph, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
        (tmp_path / "watched_chats").write_text(f"{CHAT_A}\n")
        (tmp_path / "chat_id").write_text("19:legacy-default@thread.v2\n")
        await catch_up.main()
        assert graph == ["19:legacy-default@thread.v2", CHAT_A]

    async def test_malformed_legacy_default_chat_is_not_requested(
        self, graph, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
        (tmp_path / "chat_id").write_text("../../users/someone\n")
        await catch_up.main()
        assert graph == []

    async def test_unreadable_legacy_default_chat_does_not_stop_the_script(
        self, fake_cloud_store, graph, capsys
    ) -> None:
        real_get = fake_cloud_store.get

        async def get(path: str) -> bytes:
            if path == "chat_id":
                raise OSError("simulated blob outage")
            return await real_get(path)

        BlobBackend(fake_cloud_store).write_text(WATCHED_CHATS_KEY, f"{CHAT_A}\n")
        with patch.object(fake_cloud_store, "get", get):
            await catch_up.main()
        assert graph == [CHAT_A]
        assert "chat_id" in capsys.readouterr().err


class TestHomeDirectory:
    async def test_home_directory_is_never_read(
        self, graph, monkeypatch, tmp_path, fake_cloud_store
    ) -> None:
        await catch_up.main()
        assert DECOY_CHAT not in graph

    def test_tests_here_cannot_see_the_real_home(self, tmp_path: Path) -> None:
        assert Path.home().is_relative_to(tmp_path)


class TestUnreadableList:
    async def test_prints_an_error_still_shows_the_inbox_and_exits_nonzero(
        self, fake_cloud_store, graph, capsys, monkeypatch, tmp_path
    ) -> None:
        """An outage must not look like "no chats", and must not read a local copy."""
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
        (tmp_path / "watched_chats").write_text(f"{OLD_CHAT}\n")

        with (
            patch.object(fake_cloud_store, "get_with_etag", side_effect=OSError("outage")),
            pytest.raises(SystemExit) as exit_info,
        ):
            await catch_up.main()

        captured = capsys.readouterr()
        assert exit_info.value.code == 1
        assert "watched-chat list" in captured.err
        assert "INBOX" in captured.out
        assert graph == []


class TestDotenvStillDrivesTheBackend:
    def test_cloud_settings_in_dotenv_select_the_blob_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The chat list now depends on ``.env``: if the blob settings in it were
        missed, the script would silently read an empty local store on a cloud
        deployment. ``entrabot.config`` finds ``.env`` from its own location, not the
        working directory, and ``get_backend`` reads the environment at call time."""
        fake_root = tmp_path / "project"
        (fake_root / "src" / "entrabot").mkdir(parents=True)
        (fake_root / ".env").write_text(
            "ENTRABOT_BLOB_ENDPOINT=https://fake-account.invalid\n"
            "ENTRABOT_BLOB_CONTAINER=fake-container\n"
        )
        for name in ("ENTRABOT_BLOB_ENDPOINT", "ENTRABOT_BLOB_CONTAINER"):
            # Set then delete so monkeypatch restores the original absence, even
            # though _load_dotenv writes os.environ directly.
            monkeypatch.setenv(name, "placeholder")
            monkeypatch.delenv(name)
        monkeypatch.delenv("ENTRABOT_KEEP_MEMORY_LOCAL", raising=False)
        monkeypatch.setattr(
            config_module, "__file__", str(fake_root / "src" / "entrabot" / "config.py")
        )
        monkeypatch.chdir(tmp_path)

        config_module._load_dotenv()

        assert isinstance(get_backend(), BlobBackend)

    def test_script_imports_config_and_hardcodes_no_path_to_the_list(self) -> None:
        """Importing entrabot.config is what loads ``.env``."""
        source = _SCRIPT.read_text()
        assert "from entrabot.config import get_config" in source
        assert '"watched_chats"' not in source
