"""Tests for environment-based configuration."""

import os
from pathlib import Path
from unittest.mock import patch

from openclaw.config import OpenclawConfig, get_config


class TestOpenclawConfig:
    def test_defaults(self) -> None:
        cfg = OpenclawConfig()
        assert cfg.tenant_id is None
        assert cfg.client_id is None
        assert cfg.client_secret is None
        assert cfg.agent_user_id is None
        assert cfg.agent_upn is None
        assert cfg.agent_password is None
        assert cfg.human_user_id is None
        assert cfg.human_upn is None
        assert cfg.log_level == "INFO"
        assert cfg.log_dir == Path.home() / ".openclaw" / "logs"
        assert cfg.audit_dir == Path.home() / ".openclaw" / "audit"
        assert cfg.data_dir == Path.home() / ".openclaw" / "data"

    def test_from_env(self) -> None:
        env = {
            "OPENCLAW_TENANT_ID": "my-tenant",
            "OPENCLAW_CLIENT_ID": "my-client",
            "OPENCLAW_CLIENT_SECRET": "my-secret",
            "OPENCLAW_AGENT_USER_ID": "agent-uid",
            "OPENCLAW_AGENT_UPN": "agent@example.com",
            "OPENCLAW_AGENT_PASSWORD": "agent-pass",
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
        assert cfg.client_id == "my-client"
        assert cfg.client_secret == "my-secret"
        assert cfg.agent_user_id == "agent-uid"
        assert cfg.agent_upn == "agent@example.com"
        assert cfg.agent_password == "agent-pass"
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
        assert cfg.agent_upn is None
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
