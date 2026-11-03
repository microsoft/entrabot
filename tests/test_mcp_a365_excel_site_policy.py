"""Denylist enforcement and transport errors in the Work IQ Excel MCP tools."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from entrabot.tools.files import GRAPH_V1_HOST

URL = "https://contoso.sharepoint.com/sites/finance/budget.xlsx"
DRIVE_ID = "b!contoso-drive-id"
ITEM_ID = "01CONTOSOITEMID"
COMMENT_ID = "{11111111-2222-3333-4444-555555555555}"
SITE_GUID = "11111111-2222-3333-4444-555555555555"
COMPOSITE = f"contoso.sharepoint.com,{SITE_GUID},66666666-7777-8888-9999-000000000000"
OTHER_SITE = "99999999-0000-0000-0000-000000000000"
ITEM_LOOKUP = rf"^{GRAPH_V1_HOST}/drives/[^/]+/items/[^/]+\?"
SHARES = f"{GRAPH_V1_HOST}/shares/"

CONTENT_RESULT = {
    "fileName": "budget.xlsx",
    "size": 1,
    "driveId": DRIVE_ID,
    "documentId": ITEM_ID,
    "content": "secret cells",
    "comments": [],
}


class FakeProvider:
    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def use_provider(monkeypatch: pytest.MonkeyPatch) -> Any:
    import entrabot.mcp_server as server

    monkeypatch.setattr(server, "_initialize", AsyncMock())
    monkeypatch.setattr(server, "_ensure_valid_token", AsyncMock())
    monkeypatch.setitem(server._state, "token", "test-token")

    def install(result: dict[str, Any] | Exception) -> FakeProvider:
        provider = FakeProvider(result)
        monkeypatch.setattr(
            "entrabot.a365.excel.WorkIqProvider.from_env",
            classmethod(lambda cls: provider),
        )
        return provider

    return install


def _shared_item(site_id: str | None = SITE_GUID) -> httpx.Response:
    parent: dict[str, Any] = {"driveId": DRIVE_ID, "driveType": "documentLibrary"}
    if site_id:
        parent["siteId"] = site_id
    return httpx.Response(
        200,
        json={"id": ITEM_ID, "name": "budget.xlsx", "parentReference": parent},
    )


def _lookup(site_id: str | None) -> httpx.Response:
    parent = {"siteId": site_id} if site_id else {}
    return httpx.Response(200, json={"id": ITEM_ID, "parentReference": parent})


READ = ("read_excel_workbook", (URL), CONTENT_RESULT)
COMMENT = (
    "add_excel_comment",
    (DRIVE_ID, ITEM_ID, "Sheet1!A1", "Note"),
    {"id": COMMENT_ID, "content": "Note"},
)
REPLY = (
    "reply_to_excel_comment",
    (DRIVE_ID, ITEM_ID, COMMENT_ID, "Note"),
    {"id": COMMENT_ID, "content": "Note"},
)


def _args(args: Any) -> tuple[Any, ...]:
    return args if isinstance(args, tuple) else (args,)


class TestEmptyDenylist:
    @pytest.mark.asyncio
    @respx.mock
    @pytest.mark.parametrize(("tool", "args", "result"), [READ, COMMENT, REPLY])
    async def test_no_graph_round_trip(
        self, use_provider: Any, monkeypatch: pytest.MonkeyPatch, tool: str, args: Any, result: Any
    ) -> None:
        import entrabot.mcp_server as server

        monkeypatch.delenv("ENTRABOT_FILES_DENIED_SITES", raising=False)
        graph = respx.route(url__startswith=GRAPH_V1_HOST).mock(return_value=httpx.Response(500))
        provider = use_provider(result)

        body = json.loads(await getattr(server, tool)(*_args(args)))

        assert "error" not in body
        assert graph.call_count == 0
        assert len(provider.calls) == 1


class TestDeniedSite:
    @pytest.mark.asyncio
    @respx.mock
    async def test_read_is_refused_before_work_iq(
        self, use_provider: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import entrabot.mcp_server as server

        monkeypatch.setenv("ENTRABOT_FILES_DENIED_SITES", COMPOSITE)
        respx.get(url__startswith=SHARES).mock(return_value=_shared_item(SITE_GUID))
        respx.get(url__regex=ITEM_LOOKUP).mock(return_value=_lookup(SITE_GUID))
        provider = use_provider(CONTENT_RESULT)

        body = json.loads(await server.read_excel_workbook(URL))

        assert body["error_type"] == "SiteNotAllowedError"
        assert "secret cells" not in json.dumps(body)
        assert provider.calls == []

    @pytest.mark.asyncio
    @respx.mock
    @pytest.mark.parametrize(("tool", "args", "result"), [COMMENT, REPLY])
    async def test_writes_are_refused_before_work_iq(
        self, use_provider: Any, monkeypatch: pytest.MonkeyPatch, tool: str, args: Any, result: Any
    ) -> None:
        import entrabot.mcp_server as server

        monkeypatch.setenv("ENTRABOT_FILES_DENIED_SITES", COMPOSITE)
        respx.get(url__regex=ITEM_LOOKUP).mock(return_value=_lookup(SITE_GUID))
        provider = use_provider(result)

        body = json.loads(await getattr(server, tool)(*args))

        assert body["error_type"] == "SiteNotAllowedError"
        assert provider.calls == []


class TestAllowedSite:
    @pytest.mark.asyncio
    @respx.mock
    async def test_read_proceeds_for_another_site(
        self, use_provider: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import entrabot.mcp_server as server

        monkeypatch.setenv("ENTRABOT_FILES_DENIED_SITES", COMPOSITE)
        respx.get(url__startswith=SHARES).mock(return_value=_shared_item(OTHER_SITE))
        lookup = respx.get(url__regex=ITEM_LOOKUP).mock(return_value=_lookup(OTHER_SITE))
        provider = use_provider(CONTENT_RESULT)

        body = json.loads(await server.read_excel_workbook(URL))

        assert body["drive_id"] == DRIVE_ID
        assert lookup.call_count == 1
        assert len(provider.calls) == 1

    @pytest.mark.asyncio
    @respx.mock
    @pytest.mark.parametrize(("tool", "args", "result"), [COMMENT, REPLY])
    async def test_writes_proceed_for_another_site(
        self, use_provider: Any, monkeypatch: pytest.MonkeyPatch, tool: str, args: Any, result: Any
    ) -> None:
        import entrabot.mcp_server as server

        monkeypatch.setenv("ENTRABOT_FILES_DENIED_SITES", COMPOSITE)
        respx.get(url__regex=ITEM_LOOKUP).mock(return_value=_lookup(OTHER_SITE))
        provider = use_provider(result)

        body = json.loads(await getattr(server, tool)(*args))

        assert "error" not in body
        assert len(provider.calls) == 1


class TestFailsClosed:
    @pytest.mark.asyncio
    @respx.mock
    @pytest.mark.parametrize(("tool", "args", "result"), [COMMENT, REPLY])
    async def test_missing_site_blocks_writes(
        self, use_provider: Any, monkeypatch: pytest.MonkeyPatch, tool: str, args: Any, result: Any
    ) -> None:
        import entrabot.mcp_server as server

        monkeypatch.setenv("ENTRABOT_FILES_DENIED_SITES", COMPOSITE)
        respx.get(url__regex=ITEM_LOOKUP).mock(return_value=_lookup(None))
        provider = use_provider(result)

        body = json.loads(await getattr(server, tool)(*args))

        assert body["error_type"] == "GraphFilesError"
        assert provider.calls == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_unresolvable_url_blocks_the_read(
        self, use_provider: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import entrabot.mcp_server as server

        monkeypatch.setenv("ENTRABOT_FILES_DENIED_SITES", COMPOSITE)
        respx.get(url__startswith=SHARES).mock(return_value=httpx.Response(404, json={}))
        provider = use_provider(CONTENT_RESULT)

        body = json.loads(await server.read_excel_workbook(URL))

        assert body["error_type"] == "FileNotFoundError"
        assert provider.calls == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_graph_timeout_blocks_the_read_and_returns_an_error(
        self, use_provider: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import entrabot.mcp_server as server

        monkeypatch.setenv("ENTRABOT_FILES_DENIED_SITES", COMPOSITE)
        respx.get(url__startswith=SHARES).mock(side_effect=httpx.ReadTimeout("timed out"))
        provider = use_provider(CONTENT_RESULT)

        body = json.loads(await server.read_excel_workbook(URL))

        assert body["error_type"] == "ReadTimeout"
        assert provider.calls == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_blank_ids_reach_the_adapter_validation(
        self, use_provider: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import entrabot.mcp_server as server

        monkeypatch.setenv("ENTRABOT_FILES_DENIED_SITES", COMPOSITE)
        graph = respx.route(url__startswith=GRAPH_V1_HOST).mock(return_value=httpx.Response(500))
        provider = use_provider({"id": COMMENT_ID})

        body = json.loads(await server.add_excel_comment(" ", ITEM_ID, "Sheet1!A1", "Note"))

        assert body["error_type"] == "ValueError"
        assert graph.call_count == 0
        assert provider.calls == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_create_workbook_has_no_site_check(
        self, use_provider: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import entrabot.mcp_server as server

        monkeypatch.setenv("ENTRABOT_FILES_DENIED_SITES", COMPOSITE)
        graph = respx.route(url__startswith=GRAPH_V1_HOST).mock(return_value=httpx.Response(500))
        provider = use_provider(
            {
                "driveItem": {
                    "id": ITEM_ID,
                    "name": "new.xlsx",
                    "webUrl": "https://contoso/new.xlsx",
                    "parentReference": {"driveId": DRIVE_ID},
                }
            }
        )

        body = json.loads(await server.create_excel_workbook("new", "a,b"))

        assert body["item_id"] == ITEM_ID
        assert graph.call_count == 0
        assert len(provider.calls) == 1


class TestTransportErrors:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("read_excel_workbook", (URL,)),
            ("create_excel_workbook", ("new", "a,b")),
            ("add_excel_comment", (DRIVE_ID, ITEM_ID, "Sheet1!A1", "Note")),
            ("reply_to_excel_comment", (DRIVE_ID, ITEM_ID, COMMENT_ID, "Note")),
        ],
    )
    async def test_timeout_becomes_a_typed_error(
        self, use_provider: Any, monkeypatch: pytest.MonkeyPatch, tool: str, args: Any
    ) -> None:
        import entrabot.mcp_server as server

        monkeypatch.delenv("ENTRABOT_FILES_DENIED_SITES", raising=False)
        use_provider(httpx.ReadTimeout("timed out"))

        body = json.loads(await getattr(server, tool)(*args))

        assert body["error_type"] == "ReadTimeout"
        assert "timed out" in body["error"]
