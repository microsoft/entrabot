from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from entraclaw.a365.provider import WorkIqProvider
from entraclaw.a365.tokens import WorkIqTokenRequest


class FakeMcpClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"ok": True}


@dataclass
class RecordingA365TokenProvider:
    token: str
    requests: list[WorkIqTokenRequest] = field(default_factory=list)

    async def get_token(self, request: WorkIqTokenRequest) -> str:
        self.requests.append(request)
        return self.token


def _manifest(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "mcpServers": [
                    {
                        "mcpServerName": "mcp_WordServer",
                        "mcpServerUniqueName": "mcp_WordServer",
                        "url": "https://example.test/workiq/word",
                        "scope": "Tools.ListInvoke.All",
                        "audience": "c2d0c2b6-8013-4346-9f8b-b81d3b754a29",
                    }
                ]
            }
        )
    )
    return path


@pytest.mark.asyncio
async def test_provider_uses_manifest_audience_scope_and_endpoint(tmp_path: Path) -> None:
    mcp_client = FakeMcpClient()
    token_provider = RecordingA365TokenProvider("token-123")
    provider = WorkIqProvider(
        manifest_path=_manifest(tmp_path / "ToolingManifest.json"),
        token_provider=token_provider,
        mcp_client=mcp_client,
    )

    result = await provider.call_tool(
        server_name="mcp_WordServer",
        tool_name="WordReplyToComment",
        arguments={"commentId": "c1"},
    )

    assert result == {"ok": True}
    assert token_provider.requests == [
        WorkIqTokenRequest(
            server_name="mcp_WordServer",
            audience="c2d0c2b6-8013-4346-9f8b-b81d3b754a29",
            scope="Tools.ListInvoke.All",
        )
    ]
    assert mcp_client.calls == [
        {
            "endpoint": "https://example.test/workiq/word",
            "server_name": "mcp_WordServer",
            "tool_name": "WordReplyToComment",
            "arguments": {"commentId": "c1"},
            "token": "token-123",
            "scope": "Tools.ListInvoke.All",
        }
    ]
