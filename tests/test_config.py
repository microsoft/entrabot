"""Tests for environment-based configuration."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from entrabot.config import EntraBotConfig, get_config
from entrabot.errors import RemovedModeError


def _expected_default_root() -> Path:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / "entrabot"
    return Path.home() / ".entrabot"


class TestEntraBotConfig:
    def test_defaults(self) -> None:
        cfg = EntraBotConfig()
        assert cfg.tenant_id is None
        assert cfg.blueprint_app_id is None
        assert cfg.blueprint_object_id is None
        assert cfg.blueprint_cert_thumbprint is None
        assert cfg.agent_id is None
        assert cfg.agent_object_id is None
        assert cfg.agent_user_id is None
        assert cfg.agent_user_upn is None
        assert cfg.human_user_id is None
        assert cfg.human_upn is None
        assert cfg.log_level == "INFO"
        root = _expected_default_root()
        assert cfg.log_dir == root / "logs"
        assert cfg.audit_dir == root / "audit"
        assert cfg.data_dir == root / "data"
        assert cfg.client_id is None
        assert cfg.skip_provisioning is False
        assert cfg.authority == "https://login.microsoftonline.com/common"

    def test_from_env(self) -> None:
        env = {
            "ENTRABOT_TENANT_ID": "my-tenant",
            "ENTRABOT_BLUEPRINT_APP_ID": "my-blueprint",
            "ENTRABOT_BLUEPRINT_OBJECT_ID": "my-blueprint-obj",
            "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT": "my-thumbprint",
            "ENTRABOT_AGENT_ID": "my-agent-id",
            "ENTRABOT_AGENT_OBJECT_ID": "my-agent-obj",
            "ENTRABOT_AGENT_USER_ID": "my-agent-user",
            "ENTRABOT_AGENT_USER_UPN": "agent@tenant.onmicrosoft.com",
            "ENTRABOT_HUMAN_USER_ID": "human-uid",
            "ENTRABOT_HUMAN_UPN": "human@example.com",
            "ENTRABOT_LOG_LEVEL": "DEBUG",
            "ENTRABOT_LOG_DIR": "/custom/logs",
            "ENTRABOT_AUDIT_DIR": "/custom/audit",
            "ENTRABOT_DATA_DIR": "/custom/data",
            "ENTRABOT_CLIENT_ID": "my-client-id",
            "ENTRABOT_SKIP_PROVISIONING": "true",
            "ENTRABOT_AUTHORITY": "https://login.microsoftonline.com/my-tenant",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = EntraBotConfig.from_env()
        assert cfg.tenant_id == "my-tenant"
        assert cfg.blueprint_app_id == "my-blueprint"
        assert cfg.blueprint_object_id == "my-blueprint-obj"
        assert cfg.blueprint_cert_thumbprint == "my-thumbprint"
        assert cfg.agent_id == "my-agent-id"
        assert cfg.agent_object_id == "my-agent-obj"
        assert cfg.agent_user_id == "my-agent-user"
        assert cfg.agent_user_upn == "agent@tenant.onmicrosoft.com"
        assert cfg.human_user_id == "human-uid"
        assert cfg.human_upn == "human@example.com"
        assert cfg.log_level == "DEBUG"
        assert cfg.log_dir == Path("/custom/logs")
        assert cfg.audit_dir == Path("/custom/audit")
        assert cfg.data_dir == Path("/custom/data")
        assert cfg.client_id == "my-client-id"
        assert cfg.skip_provisioning is True
        assert cfg.authority == "https://login.microsoftonline.com/my-tenant"

    def test_from_env_uses_explicit_dirs_without_home(self) -> None:
        env = {
            "ENTRABOT_LOG_DIR": r"C:\entrabot\logs",
            "ENTRABOT_AUDIT_DIR": r"C:\entrabot\audit",
            "ENTRABOT_DATA_DIR": r"C:\entrabot\data",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(sys, "platform", "win32"),
            patch("entrabot.config.Path.home", side_effect=RuntimeError),
        ):
            cfg = EntraBotConfig.from_env()

        assert cfg.log_dir == Path(env["ENTRABOT_LOG_DIR"])
        assert cfg.audit_dir == Path(env["ENTRABOT_AUDIT_DIR"])
        assert cfg.data_dir == Path(env["ENTRABOT_DATA_DIR"])

    def test_from_env_with_no_vars(self) -> None:
        # Remove any Entrabot env vars that might be set
        cleaned = {k: v for k, v in os.environ.items() if not k.startswith("ENTRABOT_")}
        with patch.dict(os.environ, cleaned, clear=True):
            cfg = EntraBotConfig.from_env()
        assert cfg.tenant_id is None
        assert cfg.blueprint_app_id is None
        assert cfg.log_level == "INFO"
        assert cfg.client_id is None
        assert cfg.skip_provisioning is False
        assert cfg.authority == "https://login.microsoftonline.com/common"

    def test_frozen(self) -> None:
        cfg = EntraBotConfig()
        try:
            cfg.tenant_id = "new"  # type: ignore[misc]
            raise AssertionError("Should not allow mutation")
        except AttributeError:
            pass  # expected — frozen dataclass

    def test_get_config_shortcut(self) -> None:
        cfg = get_config()
        assert isinstance(cfg, EntraBotConfig)

    def test_tenant_ids_parsed_from_env(self) -> None:
        """ENTRABOT_HUMAN_USER_TENANT_IDS CSV is parsed into a list."""
        env = {
            "ENTRABOT_HUMAN_USER_TENANT_IDS": "72f988bf-86f1-41af-91ab-2d7cd011db47,,other-tenant",
            "ENTRABOT_HUMAN_USER_MAILS": "user1@example.com,user2@example.com,guest@other.com",
            "ENTRABOT_HUMAN_USER_IDS": "guest-id,member-id,guest2-id",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = EntraBotConfig.from_env()
        assert cfg.human_user_tenant_ids == [
            "72f988bf-86f1-41af-91ab-2d7cd011db47",
            "",
            "other-tenant",
        ]
        assert cfg.human_user_mails == [
            "user1@example.com",
            "user2@example.com",
            "guest@other.com",
        ]

    def test_tenant_ids_empty_when_not_set(self) -> None:
        """Missing ENTRABOT_HUMAN_USER_TENANT_IDS defaults to empty list."""
        cleaned = {k: v for k, v in os.environ.items() if not k.startswith("ENTRABOT_")}
        with patch.dict(os.environ, cleaned, clear=True):
            cfg = EntraBotConfig.from_env()
        assert cfg.human_user_tenant_ids == []
        assert cfg.human_user_mails == []

    def test_user_types_parsed_from_env(self) -> None:
        """ENTRABOT_HUMAN_USER_TYPES CSV is parsed preserving empty entries."""
        env = {
            "ENTRABOT_HUMAN_USER_TYPES": "Guest,,Member",
            "ENTRABOT_HUMAN_USER_IDS": "guest-id,member-id,member2-id",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = EntraBotConfig.from_env()
        assert cfg.human_user_types == ["Guest", "", "Member"]

    def test_user_types_empty_when_not_set(self) -> None:
        """Missing ENTRABOT_HUMAN_USER_TYPES defaults to empty list."""
        cleaned = {k: v for k, v in os.environ.items() if not k.startswith("ENTRABOT_")}
        with patch.dict(os.environ, cleaned, clear=True):
            cfg = EntraBotConfig.from_env()
        assert cfg.human_user_types == []

    def test_skip_provisioning_truthy_values(self) -> None:
        for val in ("true", "True", "TRUE", "1", "yes", "Yes"):
            with patch.dict(os.environ, {"ENTRABOT_SKIP_PROVISIONING": val}, clear=False):
                cfg = EntraBotConfig.from_env()
            assert cfg.skip_provisioning is True, f"Expected True for '{val}'"

    def test_skip_provisioning_falsy_values(self) -> None:
        for val in ("false", "False", "0", "no", ""):
            with patch.dict(os.environ, {"ENTRABOT_SKIP_PROVISIONING": val}, clear=False):
                cfg = EntraBotConfig.from_env()
            assert cfg.skip_provisioning is False, f"Expected False for '{val}'"

    def test_mode_defaults_to_auto(self) -> None:
        cfg = EntraBotConfig()
        assert cfg.mode == "auto"

    def test_mode_from_env(self) -> None:
        for val in ("delegated", "agent_user", "auto"):
            with patch.dict(os.environ, {"ENTRABOT_MODE": val}, clear=False):
                cfg = EntraBotConfig.from_env()
            assert cfg.mode == val, f"Expected '{val}'"

    def test_mode_invalid_defaults_to_auto(self) -> None:
        with patch.dict(os.environ, {"ENTRABOT_MODE": "invalid"}, clear=False):
            cfg = EntraBotConfig.from_env()
        assert cfg.mode == "auto"

    def test_mode_bot_raises_removed(self) -> None:
        """Bot mode was removed — it must fail loud, not silently fall back
        to auto. Honors the zero-silent-failures rule (ADR-006)."""
        with (
            patch.dict(os.environ, {"ENTRABOT_MODE": "bot"}, clear=False),
            pytest.raises(RemovedModeError, match="agent_user"),
        ):
            EntraBotConfig.from_env()


class TestBlobStorageConfig:
    """ADR-005 Phase 5 — blob endpoint, container, and keep-memory-local."""

    def test_blob_fields_default_none_and_false(self) -> None:
        cfg = EntraBotConfig()
        assert cfg.blob_endpoint is None
        assert cfg.blob_container is None
        assert cfg.keep_memory_local is False

    def test_blob_fields_from_env(self) -> None:
        env = {
            "ENTRABOT_BLOB_ENDPOINT": "https://entclaw.blob.core.windows.net",
            "ENTRABOT_BLOB_CONTAINER": "agent-abc-123",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = EntraBotConfig.from_env()
        assert cfg.blob_endpoint == "https://entclaw.blob.core.windows.net"
        assert cfg.blob_container == "agent-abc-123"

    def test_keep_memory_local_truthy_values(self) -> None:
        for val in ("true", "True", "1", "yes"):
            with patch.dict(os.environ, {"ENTRABOT_KEEP_MEMORY_LOCAL": val}, clear=False):
                cfg = EntraBotConfig.from_env()
            assert cfg.keep_memory_local is True, f"Expected True for '{val}'"

    def test_keep_memory_local_falsy_values(self) -> None:
        for val in ("false", "0", "no", ""):
            with patch.dict(os.environ, {"ENTRABOT_KEEP_MEMORY_LOCAL": val}, clear=False):
                cfg = EntraBotConfig.from_env()
            assert cfg.keep_memory_local is False, f"Expected False for '{val}'"


class TestLoadDotenv:
    """_load_dotenv honors an ENTRABOT_ENV_FILE override so a test identity
    can run from its own env file (e.g. .env.mxc-test) without disturbing
    the production .env."""

    def test_honors_env_file_override(self, tmp_path: Path) -> None:
        from entrabot.config import _load_dotenv

        env_file = tmp_path / ".env.custom"
        env_file.write_text("ENTRABOT_DOTENV_PROBE=from-custom-file\n")

        override = {"ENTRABOT_ENV_FILE": str(env_file)}
        with patch.dict(os.environ, override, clear=False):
            os.environ.pop("ENTRABOT_DOTENV_PROBE", None)
            _load_dotenv()
            try:
                assert os.environ.get("ENTRABOT_DOTENV_PROBE") == "from-custom-file"
            finally:
                os.environ.pop("ENTRABOT_DOTENV_PROBE", None)

    def test_override_does_not_clobber_existing_env(self, tmp_path: Path) -> None:
        from entrabot.config import _load_dotenv

        env_file = tmp_path / ".env.custom"
        env_file.write_text("ENTRABOT_DOTENV_PROBE=from-file\n")

        override = {
            "ENTRABOT_ENV_FILE": str(env_file),
            "ENTRABOT_DOTENV_PROBE": "already-set",
        }
        with patch.dict(os.environ, override, clear=False):
            _load_dotenv()
            assert os.environ.get("ENTRABOT_DOTENV_PROBE") == "already-set"
