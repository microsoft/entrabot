from __future__ import annotations

import json

import pytest


@pytest.mark.asyncio
async def test_read_word_document_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    import entraclaw.mcp_server as server
    from entraclaw.a365.word import WordDocumentContent

    async def fake_initialize() -> None:
        return None

    async def fake_read(url: str) -> WordDocumentContent:
        assert url == "https://contoso/doc.docx"
        return WordDocumentContent(
            content_html="<p>Hello</p>",
            comments=[{"id": "c1"}],
            raw={"content": "<p>Hello</p>", "comments": [{"id": "c1"}]},
        )

    monkeypatch.setattr(server, "_initialize", fake_initialize)
    monkeypatch.setattr("entraclaw.a365.word.get_document_content", fake_read)

    body = json.loads(await server.read_word_document("https://contoso/doc.docx"))

    assert body["content_html"] == "<p>Hello</p>"
    assert body["comments"] == [{"id": "c1"}]


@pytest.mark.asyncio
async def test_reply_to_word_comment_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    import entraclaw.mcp_server as server
    from entraclaw.a365.word import WordCommentReply

    async def fake_initialize() -> None:
        return None

    async def fake_reply(
        drive_id: str,
        document_id: str,
        comment_id: str,
        content: str,
    ) -> WordCommentReply:
        assert (drive_id, document_id, comment_id, content) == ("d", "doc", "c", "No.")
        return WordCommentReply(reply_id="r1", comment_id="c", content="No.", raw={"id": "r1"})

    monkeypatch.setattr(server, "_initialize", fake_initialize)
    monkeypatch.setattr("entraclaw.a365.word.reply_to_comment", fake_reply)

    body = json.loads(await server.reply_to_word_comment("d", "doc", "c", "No."))

    assert body == {
        "reply_id": "r1",
        "comment_id": "c",
        "content": "No.",
        "raw": {"id": "r1"},
    }


@pytest.mark.asyncio
async def test_get_a365_file_metadata_by_url_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    import entraclaw.mcp_server as server
    from entraclaw.a365.odsp import OdspFileMetadata

    async def fake_initialize() -> None:
        return None

    async def fake_metadata(url: str) -> OdspFileMetadata:
        assert url == "https://contoso/page.loop"
        return OdspFileMetadata(
            item_id="item-1",
            name="page.loop",
            web_url="https://contoso/page.loop",
            document_library_id="drive-1",
            raw={"id": "item-1"},
        )

    monkeypatch.setattr(server, "_initialize", fake_initialize)
    monkeypatch.setattr("entraclaw.a365.odsp.get_file_or_folder_metadata_by_url", fake_metadata)

    body = json.loads(await server.get_a365_file_metadata_by_url("https://contoso/page.loop"))

    assert body == {
        "item_id": "item-1",
        "name": "page.loop",
        "web_url": "https://contoso/page.loop",
        "document_library_id": "drive-1",
        "raw": {"id": "item-1"},
    }


@pytest.mark.asyncio
async def test_read_a365_text_file_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    import entraclaw.mcp_server as server
    from entraclaw.a365.odsp import OdspFileContent

    async def fake_initialize() -> None:
        return None

    async def fake_read(document_library_id: str, file_id: str) -> OdspFileContent:
        assert (document_library_id, file_id) == ("drive-1", "item-1")
        return OdspFileContent(content="hello", encoding="text", raw={"content": "hello"})

    monkeypatch.setattr(server, "_initialize", fake_initialize)
    monkeypatch.setattr("entraclaw.a365.odsp.read_small_text_file", fake_read)

    body = json.loads(await server.read_a365_text_file("drive-1", "item-1"))

    assert body == {
        "content": "hello",
        "encoding": "text",
        "raw": {"content": "hello"},
    }


@pytest.mark.asyncio
async def test_read_a365_binary_file_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    import entraclaw.mcp_server as server
    from entraclaw.a365.odsp import OdspFileContent

    async def fake_initialize() -> None:
        return None

    async def fake_read(document_library_id: str, file_id: str) -> OdspFileContent:
        assert (document_library_id, file_id) == ("drive-1", "item-1")
        return OdspFileContent(content="SGVsbG8=", encoding="base64", raw={"content": "SGVsbG8="})

    monkeypatch.setattr(server, "_initialize", fake_initialize)
    monkeypatch.setattr("entraclaw.a365.odsp.read_small_binary_file", fake_read)

    body = json.loads(await server.read_a365_binary_file("drive-1", "item-1"))

    assert body == {
        "content": "SGVsbG8=",
        "encoding": "base64",
        "raw": {"content": "SGVsbG8="},
    }
