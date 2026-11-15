"""whoami reports the running Entrabot version."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from entrabot import mcp_server


async def test_whoami_reports_the_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "_initialize", AsyncMock())
    monkeypatch.setattr(mcp_server, "_identity", None)
    monkeypatch.setattr(
        "entrabot.tools.identity.whoami", AsyncMock(return_value={"displayName": "Agent"})
    )
    monkeypatch.setattr("entrabot.version.installed_version", lambda: "0.2.0")
    result = json.loads(await mcp_server.whoami())
    assert result["version"] == "0.2.0"
