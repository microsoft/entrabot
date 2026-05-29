"""Tests for the identity whoami function.

No bootstrap, no device-code flows — just reads config from the environment.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from entrabot.errors import GraphApiError
from entrabot.identity.sponsors import AgentIdentitySponsor
from entrabot.tools.identity import whoami
from entrabot.tools.teams import acquire_agent_user_token


class TestWhoami:
    @pytest.mark.asyncio
    async def test_returns_config_when_set(self) -> None:
        env = {
            "ENTRABOT_BLUEPRINT_APP_ID": "test-blueprint-id",
            "ENTRABOT_TENANT_ID": "test-tenant-id",
            "ENTRABOT_AGENT_ID": "test-agent-id",
            "ENTRABOT_AGENT_OBJECT_ID": "agent-object-id",
            "ENTRABOT_HUMAN_UPN": "forged@example.com",
        }
        sponsor = AgentIdentitySponsor(
            user_id="sponsor-id",
            user_principal_name="human@example.com",
            mail="human@example.com",
        )
        with (
            patch.dict(os.environ, env, clear=False),
            patch("entrabot.identity.sponsors.fetch_agent_identity_sponsors") as mock_fetch,
        ):
            mock_fetch.return_value = [sponsor]
            result = await whoami(token="fake-token")

        _, kwargs = mock_fetch.call_args
        assert kwargs["user_token_provider"] is acquire_agent_user_token
        assert result["agent_type"] == "Entra Agent Identity"
        assert result["blueprint_app_id"] == "test-blueprint-id"
        assert result["agent_id"] == "test-agent-id"
        assert result["tenant_id"] == "test-tenant-id"
        assert result["human_sponsor"] == "human@example.com"
        assert result["human_sponsor_source"] == "entra_agent_identity_sponsors"
        assert result["human_sponsor_status"] == "loaded"
        assert result["human_sponsors"] == ["human@example.com"]
        assert result["human_sponsor_user_ids"] == ["sponsor-id"]
        assert result["status"] == "authenticated"

    @pytest.mark.asyncio
    async def test_returns_plural_human_sponsors_from_entra_not_env(self) -> None:
        env = {
            "ENTRABOT_AGENT_OBJECT_ID": "agent-object-id",
            "ENTRABOT_HUMAN_UPN": "forged@example.com",
            "ENTRABOT_HUMAN_UPNS": "forged@example.com,also-forged@example.com",
            "ENTRABOT_HUMAN_USER_MAILS": "forged@example.com",
            "ENTRABOT_HUMAN_USER_ID": "forged-id",
            "ENTRABOT_HUMAN_USER_IDS": "forged-id,also-forged-id",
        }
        sponsors = [
            AgentIdentitySponsor(
                user_id="primary-id",
                user_principal_name="primary@example.com",
                mail="primary@example.com",
            ),
            AgentIdentitySponsor(
                user_id="secondary-id",
                user_principal_name="secondary@example.com",
                mail="alias@example.com",
            ),
        ]
        with (
            patch.dict(os.environ, env, clear=False),
            patch("entrabot.identity.sponsors.fetch_agent_identity_sponsors") as mock_fetch,
        ):
            mock_fetch.return_value = sponsors
            result = await whoami(token="fake-token")

        assert result["human_sponsor"] == "primary@example.com"
        assert result["human_sponsors"] == [
            "primary@example.com",
            "secondary@example.com",
            "alias@example.com",
        ]
        assert "forged@example.com" not in result["human_sponsors"]
        assert result["human_sponsor_upns"] == [
            "primary@example.com",
            "secondary@example.com",
        ]
        assert result["human_sponsor_mails"] == [
            "primary@example.com",
            "alias@example.com",
        ]
        assert result["human_sponsor_user_ids"] == ["primary-id", "secondary-id"]
        assert result["human_sponsor_count"] == 2

    @pytest.mark.asyncio
    async def test_sponsor_fetch_error_does_not_fall_back_to_env(self) -> None:
        env = {
            "ENTRABOT_AGENT_OBJECT_ID": "agent-object-id",
            "ENTRABOT_HUMAN_UPN": "forged@example.com",
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch("entrabot.identity.sponsors.fetch_agent_identity_sponsors") as mock_fetch,
        ):
            mock_fetch.side_effect = GraphApiError(403, "forbidden")
            result = await whoami(token="fake-token")

        assert result["human_sponsor"] == "unavailable"
        assert result["human_sponsors"] == []
        assert result["human_sponsor_count"] == 0
        assert result["human_sponsor_source"] == "entra_agent_identity_sponsors"
        assert result["human_sponsor_status"] == "error"
        assert "forged@example.com" not in result.values()

    @pytest.mark.asyncio
    async def test_not_authenticated_without_token(self, tmp_path: Path) -> None:
        env = {
            "ENTRABOT_BLUEPRINT_APP_ID": "bp-id",
            "ENTRABOT_TENANT_ID": "tid",
            "ENTRABOT_LOG_DIR": str(tmp_path / "logs"),
            "ENTRABOT_AUDIT_DIR": str(tmp_path / "audit"),
            "ENTRABOT_DATA_DIR": str(tmp_path / "data"),
        }
        with patch.dict(os.environ, env, clear=True):
            result = await whoami()
        assert result["status"] == "not_authenticated"

    @pytest.mark.asyncio
    async def test_defaults_when_not_configured(self) -> None:
        # Remove all Entrabot env vars
        cleaned = {k: v for k, v in os.environ.items() if not k.startswith("ENTRABOT_")}
        with patch.dict(os.environ, cleaned, clear=True):
            result = await whoami()
        assert result["agent_id"] == "not_configured"
        assert result["blueprint_app_id"] == "not_configured"
        assert result["tenant_id"] == "not_configured"
        assert result["human_sponsor"] == "not_configured"
        assert result["human_sponsors"] == []
        assert result["status"] == "not_authenticated"
