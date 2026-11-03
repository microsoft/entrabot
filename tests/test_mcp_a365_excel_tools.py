from __future__ import annotations

import json
from typing import Any

import pytest

URL = "https://contoso-my.sharepoint.com/personal/agent/_layouts/15/Doc.aspx?sourcedoc=abc"
DRIVE_ID = "b!contoso-drive-id"
ITEM_ID = "01CONTOSOITEMID"
COMMENT_ID = "{11111111-2222-3333-4444-555555555555}"
REPLY_ID = "{66666666-7777-8888-9999-000000000000}"
PREAUTH_MARKER = "secret-preauth-marker"


class FakeProvider:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.result


@pytest.fixture
def use_provider(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Run the real wrapper and adapter against a fake Work IQ provider."""
    import entrabot.mcp_server as server

    async def fake_initialize() -> None:
        return None

    monkeypatch.setattr(server, "_initialize", fake_initialize)

    def install(result: dict[str, Any]) -> FakeProvider:
        provider = FakeProvider(result)
        monkeypatch.setattr(
            "entrabot.a365.excel.WorkIqProvider.from_env",
            classmethod(lambda cls: provider),
        )
        return provider

    return install


@pytest.mark.asyncio
async def test_read_excel_workbook_returns_only_wrapped_workbook_data(use_provider: Any) -> None:
    import entrabot.mcp_server as server

    use_provider(
        {
            "fileName": "budget.xlsx",
            "size": 1748,
            "driveId": DRIVE_ID,
            "documentId": ITEM_ID,
            "content": "### Sheet: Sheet1 ###\r\nItem\tQty",
            "comments": [
                {"CommentId": COMMENT_ID, "AnchorCell": "Item", "CommentText": "hostile comment"}
            ],
        }
    )

    body = json.loads(await server.read_excel_workbook(URL))

    assert set(body) == {"drive_id", "document_id", "size_bytes", "text", "truncated"}
    assert body["drive_id"] == DRIVE_ID
    assert body["document_id"] == ITEM_ID
    assert body["size_bytes"] == 1748
    assert body["truncated"] is False
    assert body["text"].startswith("<external_content ")
    assert body["text"].endswith("</external_content>")
    assert "hostile comment" in body["text"]
    assert COMMENT_ID in body["text"]


@pytest.mark.asyncio
async def test_read_excel_workbook_requires_url(use_provider: Any) -> None:
    import entrabot.mcp_server as server

    provider = use_provider({})

    body = json.loads(await server.read_excel_workbook("  "))

    assert body == {"error": "url is required"}
    assert provider.calls == []


@pytest.mark.asyncio
async def test_create_excel_workbook_never_exposes_download_url(use_provider: Any) -> None:
    import entrabot.mcp_server as server

    provider = use_provider(
        {
            "driveItem": json.dumps(
                {
                    "@microsoft.graph.downloadUrl": f"https://contoso/d?x={PREAUTH_MARKER}",
                    "id": ITEM_ID,
                    "name": "budget.xlsx",
                    "webUrl": URL,
                    "parentReference": {"driveId": DRIVE_ID},
                }
            ),
            "sharedWith": "",
        }
    )

    raw_body = await server.create_excel_workbook("budget", "Item,Qty\nApples,3\n")

    assert PREAUTH_MARKER not in raw_body
    assert json.loads(raw_body) == {
        "file_name": "budget.xlsx",
        "url": URL,
        "drive_id": DRIVE_ID,
        "item_id": ITEM_ID,
    }
    assert set(provider.calls[0]["arguments"]) == {"fileName", "csvContent"}


@pytest.mark.asyncio
async def test_create_excel_workbook_has_no_share_parameter() -> None:
    import inspect

    import entrabot.mcp_server as server

    assert list(inspect.signature(server.create_excel_workbook).parameters) == [
        "file_name",
        "csv_content",
    ]


@pytest.mark.asyncio
async def test_add_excel_comment_wrapper(use_provider: Any) -> None:
    import entrabot.mcp_server as server

    provider = use_provider({"id": COMMENT_ID, "content": "Hi", "cellAddress": "Sheet1!A1"})

    body = json.loads(await server.add_excel_comment(DRIVE_ID, ITEM_ID, "Sheet1!A1", "Hi"))

    assert body == {"comment_id": COMMENT_ID, "cell_address": "Sheet1!A1"}
    assert provider.calls[0]["tool_name"] == "CreateComment"


@pytest.mark.asyncio
async def test_add_excel_comment_bad_address_returns_typed_error(use_provider: Any) -> None:
    import entrabot.mcp_server as server

    provider = use_provider({"id": COMMENT_ID})

    body = json.loads(await server.add_excel_comment(DRIVE_ID, ITEM_ID, "A1", "Hi"))

    assert body["error_type"] == "ValueError"
    assert "cell_address" in body["error"]
    assert provider.calls == []


@pytest.mark.asyncio
async def test_reply_to_excel_comment_wrapper(use_provider: Any) -> None:
    import entrabot.mcp_server as server

    provider = use_provider({"id": REPLY_ID, "content": "No.", "contentType": "Plain"})

    body = json.loads(await server.reply_to_excel_comment(DRIVE_ID, ITEM_ID, COMMENT_ID, "No."))

    assert body == {"reply_id": REPLY_ID, "comment_id": COMMENT_ID}
    assert provider.calls[0]["tool_name"] == "ReplyToComment"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("read_excel_workbook", (URL,)),
        ("create_excel_workbook", ("budget.xlsx", "a,b\n")),
        ("add_excel_comment", (DRIVE_ID, ITEM_ID, "Sheet1!A1", "Hi")),
        ("reply_to_excel_comment", (DRIVE_ID, ITEM_ID, COMMENT_ID, "No.")),
    ],
)
async def test_tool_errors_become_typed_json(use_provider: Any, tool: str, args: tuple) -> None:
    import entrabot.mcp_server as server

    use_provider({"content": [{"type": "text", "text": "Error: nope"}], "isError": True})

    body = json.loads(await getattr(server, tool)(*args))

    assert body["error_type"] == "A365McpCallError"
    assert "<external_content " in body["error"]
    assert "nope" in body["error"]


@pytest.mark.asyncio
async def test_missing_manifest_becomes_typed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import entrabot.mcp_server as server
    from entrabot.a365.errors import A365ManifestNotFoundError

    async def fake_initialize() -> None:
        return None

    def raise_missing(cls: Any) -> None:
        raise A365ManifestNotFoundError(["ToolingManifest.json"])

    monkeypatch.setattr(server, "_initialize", fake_initialize)
    monkeypatch.setattr(
        "entrabot.a365.excel.WorkIqProvider.from_env",
        classmethod(raise_missing),
    )

    body = json.loads(await server.read_excel_workbook(URL))

    assert body["error_type"] == "A365ManifestNotFoundError"
