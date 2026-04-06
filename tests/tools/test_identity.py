"""Tests for the identity whoami function.

No bootstrap, no device-code flows — just reads config from the environment.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from openclaw.tools.identity import whoami


class TestWhoami:
    @pytest.mark.asyncio
    async def test_returns_config_when_set(self) -> None:
        env = {
            "OPENCLAW_CLIENT_ID": "test-client-id",
            "OPENCLAW_TENANT_ID": "test-tenant-id",
            "OPENCLAW_AGENT_UPN": "agent@example.com",
            "OPENCLAW_HUMAN_UPN": "human@example.com",
            "OPENCLAW_HUMAN_USER_ID": "human-uid",
        }
        with patch.dict(os.environ, env, clear=False):
            result = await whoami(token="fake-token")
        assert result["agent_id"] == "test-client-id"
        assert result["agent_upn"] == "agent@example.com"
        assert result["tenant_id"] == "test-tenant-id"
        assert result["human_upn"] == "human@example.com"
        assert result["human_user_id"] == "human-uid"
        assert result["status"] == "authenticated"

    @pytest.mark.asyncio
    async def test_not_authenticated_without_token(self) -> None:
        env = {
            "OPENCLAW_CLIENT_ID": "cid",
            "OPENCLAW_TENANT_ID": "tid",
        }
        with patch.dict(os.environ, env, clear=False):
            result = await whoami()
        assert result["status"] == "not_authenticated"

    @pytest.mark.asyncio
    async def test_defaults_when_not_configured(self) -> None:
        # Remove all Openclaw env vars
        cleaned = {k: v for k, v in os.environ.items() if not k.startswith("OPENCLAW_")}
        with patch.dict(os.environ, cleaned, clear=True):
            result = await whoami()
        assert result["agent_id"] == "not_configured"
        assert result["agent_upn"] == "not_configured"
        assert result["tenant_id"] == "not_configured"
        assert result["human_upn"] == "not_configured"
        assert result["status"] == "not_authenticated"
