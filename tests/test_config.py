"""Tests for environment-based configuration."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

from entraclaw.config import EntraClawConfig, get_config


def _expected_default_root() -> Path:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / "entraclaw"
    return Path.home() / ".entraclaw"


class TestEntraClawConfig:
    def test_defaults(self) -> None:
        cfg = EntraClawConfig()
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
            "ENTRACLAW_TENANT_ID": "my-tenant",
            "ENTRACLAW_BLUEPRINT_APP_ID": "my-blueprint",
            "ENTRACLAW_BLUEPRINT_OBJECT_ID": "my-blueprint-obj",
            "ENTRACLAW_BLUEPRINT_CERT_THUMBPRINT": "my-thumbprint",
            "ENTRACLAW_AGENT_ID": "my-agent-id",
            "ENTRACLAW_AGENT_OBJECT_ID": "my-agent-obj",
            "ENTRACLAW_AGENT_USER_ID": "my-agent-user",
            "ENTRACLAW_AGENT_USER_UPN": "agent@tenant.onmicrosoft.com",
            "ENTRACLAW_HUMAN_USER_ID": "human-uid",
            "ENTRACLAW_HUMAN_UPN": "human@example.com",
            "ENTRACLAW_LOG_LEVEL": "DEBUG",
            "ENTRACLAW_LOG_DIR": "/custom/logs",
            "ENTRACLAW_AUDIT_DIR": "/custom/audit",
            "ENTRACLAW_DATA_DIR": "/custom/data",
            "ENTRACLAW_CLIENT_ID": "my-client-id",
            "ENTRACLAW_SKIP_PROVISIONING": "true",
            "ENTRACLAW_AUTHORITY": "https://login.microsoftonline.com/my-tenant",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = EntraClawConfig.from_env()
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

    def test_from_env_with_no_vars(self) -> None:
        # Remove any Entraclaw env vars that might be set
        cleaned = {k: v for k, v in os.environ.items() if not k.startswith("ENTRACLAW_")}
        with patch.dict(os.environ, cleaned, clear=True):
            cfg = EntraClawConfig.from_env()
        assert cfg.tenant_id is None
        assert cfg.blueprint_app_id is None
        assert cfg.log_level == "INFO"
        assert cfg.client_id is None
        assert cfg.skip_provisioning is False
        assert cfg.authority == "https://login.microsoftonline.com/common"

    def test_frozen(self) -> None:
        cfg = EntraClawConfig()
        try:
            cfg.tenant_id = "new"  # type: ignore[misc]
            raise AssertionError("Should not allow mutation")
        except AttributeError:
            pass  # expected — frozen dataclass

    def test_get_config_shortcut(self) -> None:
        cfg = get_config()
        assert isinstance(cfg, EntraClawConfig)

    def test_tenant_ids_parsed_from_env(self) -> None:
        """ENTRACLAW_HUMAN_USER_TENANT_IDS CSV is parsed into a list."""
        env = {
            "ENTRACLAW_HUMAN_USER_TENANT_IDS": "72f988bf-86f1-41af-91ab-2d7cd011db47,,other-tenant",
            "ENTRACLAW_HUMAN_USER_MAILS": "adrumea@microsoft.com,brandon@werner.ac,guest@other.com",
            "ENTRACLAW_HUMAN_USER_IDS": "guest-id,member-id,guest2-id",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = EntraClawConfig.from_env()
        assert cfg.human_user_tenant_ids == [
            "72f988bf-86f1-41af-91ab-2d7cd011db47",
            "",
            "other-tenant",
        ]
        assert cfg.human_user_mails == [
            "adrumea@microsoft.com",
            "brandon@werner.ac",
            "guest@other.com",
        ]

    def test_tenant_ids_empty_when_not_set(self) -> None:
        """Missing ENTRACLAW_HUMAN_USER_TENANT_IDS defaults to empty list."""
        cleaned = {k: v for k, v in os.environ.items() if not k.startswith("ENTRACLAW_")}
        with patch.dict(os.environ, cleaned, clear=True):
            cfg = EntraClawConfig.from_env()
        assert cfg.human_user_tenant_ids == []
        assert cfg.human_user_mails == []

    def test_user_types_parsed_from_env(self) -> None:
        """ENTRACLAW_HUMAN_USER_TYPES CSV is parsed preserving empty entries."""
        env = {
            "ENTRACLAW_HUMAN_USER_TYPES": "Guest,,Member",
            "ENTRACLAW_HUMAN_USER_IDS": "guest-id,member-id,member2-id",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = EntraClawConfig.from_env()
        assert cfg.human_user_types == ["Guest", "", "Member"]

    def test_user_types_empty_when_not_set(self) -> None:
        """Missing ENTRACLAW_HUMAN_USER_TYPES defaults to empty list."""
        cleaned = {k: v for k, v in os.environ.items() if not k.startswith("ENTRACLAW_")}
        with patch.dict(os.environ, cleaned, clear=True):
            cfg = EntraClawConfig.from_env()
        assert cfg.human_user_types == []

    def test_skip_provisioning_truthy_values(self) -> None:
        for val in ("true", "True", "TRUE", "1", "yes", "Yes"):
            with patch.dict(os.environ, {"ENTRACLAW_SKIP_PROVISIONING": val}, clear=False):
                cfg = EntraClawConfig.from_env()
            assert cfg.skip_provisioning is True, f"Expected True for '{val}'"

    def test_skip_provisioning_falsy_values(self) -> None:
        for val in ("false", "False", "0", "no", ""):
            with patch.dict(os.environ, {"ENTRACLAW_SKIP_PROVISIONING": val}, clear=False):
                cfg = EntraClawConfig.from_env()
            assert cfg.skip_provisioning is False, f"Expected False for '{val}'"

    def test_mode_defaults_to_auto(self) -> None:
        cfg = EntraClawConfig()
        assert cfg.mode == "auto"

    def test_mode_from_env(self) -> None:
        for val in ("bot", "delegated", "agent_user", "auto"):
            with patch.dict(os.environ, {"ENTRACLAW_MODE": val}, clear=False):
                cfg = EntraClawConfig.from_env()
            assert cfg.mode == val, f"Expected '{val}'"

    def test_mode_invalid_defaults_to_auto(self) -> None:
        with patch.dict(os.environ, {"ENTRACLAW_MODE": "invalid"}, clear=False):
            cfg = EntraClawConfig.from_env()
        assert cfg.mode == "auto"

    def test_bot_config_fields(self) -> None:
        env = {
            "ENTRACLAW_MODE": "bot",
            "ENTRACLAW_BOT_APP_ID": "bot-app-123",
            "ENTRACLAW_BOT_CERT_THUMBPRINT": "bot-cert-thumb",
            "ENTRACLAW_BOT_TUNNEL_PORT": "4000",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = EntraClawConfig.from_env()
        assert cfg.bot_app_id == "bot-app-123"
        assert cfg.bot_cert_thumbprint == "bot-cert-thumb"
        assert cfg.bot_tunnel_port == 4000

    def test_bot_tunnel_port_default(self) -> None:
        cfg = EntraClawConfig()
        assert cfg.bot_tunnel_port == 3978

    def test_bot_fields_default_none(self) -> None:
        cfg = EntraClawConfig()
        assert cfg.bot_app_id is None
        assert cfg.bot_cert_thumbprint is None


class TestBlobStorageConfig:
    """ADR-005 Phase 5 — blob endpoint, container, and keep-memory-local."""

    def test_blob_fields_default_none_and_false(self) -> None:
        cfg = EntraClawConfig()
        assert cfg.blob_endpoint is None
        assert cfg.blob_container is None
        assert cfg.keep_memory_local is False

    def test_blob_fields_from_env(self) -> None:
        env = {
            "ENTRACLAW_BLOB_ENDPOINT": "https://entclaw.blob.core.windows.net",
            "ENTRACLAW_BLOB_CONTAINER": "agent-abc-123",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = EntraClawConfig.from_env()
        assert cfg.blob_endpoint == "https://entclaw.blob.core.windows.net"
        assert cfg.blob_container == "agent-abc-123"

    def test_keep_memory_local_truthy_values(self) -> None:
        for val in ("true", "True", "1", "yes"):
            with patch.dict(os.environ, {"ENTRACLAW_KEEP_MEMORY_LOCAL": val}, clear=False):
                cfg = EntraClawConfig.from_env()
            assert cfg.keep_memory_local is True, f"Expected True for '{val}'"

    def test_keep_memory_local_falsy_values(self) -> None:
        for val in ("false", "0", "no", ""):
            with patch.dict(os.environ, {"ENTRACLAW_KEEP_MEMORY_LOCAL": val}, clear=False):
                cfg = EntraClawConfig.from_env()
            assert cfg.keep_memory_local is False, f"Expected False for '{val}'"
