"""Tests for environment-based configuration."""

import os
from pathlib import Path
from unittest.mock import patch

from entraclaw.config import EntraClawConfig, get_config


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
        assert cfg.log_dir == Path.home() / ".entraclaw" / "logs"
        assert cfg.audit_dir == Path.home() / ".entraclaw" / "audit"
        assert cfg.data_dir == Path.home() / ".entraclaw" / "data"

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

    def test_from_env_with_no_vars(self) -> None:
        # Remove any Openclaw env vars that might be set
        cleaned = {k: v for k, v in os.environ.items() if not k.startswith("ENTRACLAW_")}
        with patch.dict(os.environ, cleaned, clear=True):
            cfg = EntraClawConfig.from_env()
        assert cfg.tenant_id is None
        assert cfg.blueprint_app_id is None
        assert cfg.log_level == "INFO"

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
