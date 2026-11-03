"""``add_word_comment`` when Work IQ reports success without a usable ID."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest


class FakeProvider:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result

    async def call_tool(self, **_: Any) -> dict[str, Any]:
        return self.result


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> Any:
    import entrabot.mcp_server as server

    monkeypatch.setattr(server, "_initialize", AsyncMock())

    async def _run(result: dict[str, Any]) -> dict[str, Any]:
        monkeypatch.setattr(
            "entrabot.a365.word.WorkIqProvider.from_env",
            classmethod(lambda cls: FakeProvider(result)),
        )
        return json.loads(await server.add_word_comment("drive-1", "doc-1", "Note"))

    return _run


@pytest.mark.asyncio
async def test_success_without_an_id_warns_instead_of_failing(run: Any) -> None:
    body = await run(
        {"content": [{"type": "text", "text": "Comment posted, format changed"}], "isError": False}
    )

    assert "error" not in body
    assert body["comment_id"] == ""
    assert "read_word_document" in body["warning"]
    assert "retry" in body["warning"].lower()


@pytest.mark.asyncio
async def test_no_warning_when_the_id_is_parsed(run: Any) -> None:
    body = await run(
        {
            "content": [
                {"type": "text", "text": "WordCommentInfo [CommentId=1549321E, Content=Note]"}
            ],
            "isError": False,
        }
    )

    assert body == {"comment_id": "1549321E", "content": "Note"}
