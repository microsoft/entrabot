"""Tests for environment-based configuration."""

import os
from pathlib import Path
from unittest.mock import patch

from openclaw.config import OpenclawConfig, get_config


class TestOpenclawConfig:
    def test_defaults(self) -> None:
        cfg = OpenclawConfig()
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
        assert cfg.log_dir == Path.home() / ".openclaw" / "logs"
        assert cfg.audit_dir == Path.home() / ".openclaw" / "audit"
        assert cfg.data_dir == Path.home() / ".openclaw" / "data"

    def test_from_env(self) -> None:
        env = {
            "OPENCLAW_TENANT_ID": "my-tenant",
            "OPENCLAW_BLUEPRINT_APP_ID": "my-blueprint",
            "OPENCLAW_BLUEPRINT_OBJECT_ID": "my-blueprint-obj",
            "OPENCLAW_BLUEPRINT_CERT_THUMBPRINT": "my-thumbprint",
            "OPENCLAW_AGENT_ID": "my-agent-id",
            "OPENCLAW_AGENT_OBJECT_ID": "my-agent-obj",
            "OPENCLAW_AGENT_USER_ID": "my-agent-user",
            "OPENCLAW_AGENT_USER_UPN": "agent@tenant.onmicrosoft.com",
            "OPENCLAW_HUMAN_USER_ID": "human-uid",
            "OPENCLAW_HUMAN_UPN": "human@example.com",
            "OPENCLAW_LOG_LEVEL": "DEBUG",
            "OPENCLAW_LOG_DIR": "/custom/logs",
            "OPENCLAW_AUDIT_DIR": "/custom/audit",
            "OPENCLAW_DATA_DIR": "/custom/data",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = OpenclawConfig.from_env()
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
        cleaned = {k: v for k, v in os.environ.items() if not k.startswith("OPENCLAW_")}
        with patch.dict(os.environ, cleaned, clear=True):
            cfg = OpenclawConfig.from_env()
        assert cfg.tenant_id is None
        assert cfg.blueprint_app_id is None
        assert cfg.log_level == "INFO"

    def test_frozen(self) -> None:
        cfg = OpenclawConfig()
        try:
            cfg.tenant_id = "new"  # type: ignore[misc]
            raise AssertionError("Should not allow mutation")
        except AttributeError:
            pass  # expected — frozen dataclass

    def test_get_config_shortcut(self) -> None:
        cfg = get_config()
        assert isinstance(cfg, OpenclawConfig)
