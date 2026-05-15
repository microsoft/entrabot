from __future__ import annotations

from typing import Any

import pytest

from entraclaw.a365.word import (
    WordComment,
    WordCommentReply,
    WordDocument,
    WordDocumentContent,
    create_comment,
    create_document,
    get_document_content,
    reply_to_comment,
)


class FakeProvider:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.result


@pytest.mark.asyncio
async def test_get_document_content_calls_word_tool() -> None:
    provider = FakeProvider(
        {
            "content": "<p>Hello</p>",
            "comments": [{"id": "c1", "content": "Question"}],
        }
    )

    result = await get_document_content("https://contoso/doc.docx", provider=provider)

    assert result == WordDocumentContent(
        content_html="<p>Hello</p>",
        comments=[{"id": "c1", "content": "Question"}],
        raw={"content": "<p>Hello</p>", "comments": [{"id": "c1", "content": "Question"}]},
    )
    assert provider.calls[0]["server_name"] == "mcp_WordServer"
    assert provider.calls[0]["tool_name"] == "GetDocumentContent"
    assert provider.calls[0]["arguments"] == {"url": "https://contoso/doc.docx"}


@pytest.mark.asyncio
async def test_get_document_content_comments_are_copied_from_raw() -> None:
    provider = FakeProvider(
        {
            "content": "<p>Hello</p>",
            "comments": [{"id": "c1", "content": "Question"}],
        }
    )

    result = await get_document_content("https://contoso/doc.docx", provider=provider)

    result.comments.append({"id": "c2", "content": "Follow-up"})

    assert result.raw["comments"] == [{"id": "c1", "content": "Question"}]


@pytest.mark.asyncio
async def test_create_document_calls_word_tool() -> None:
    provider = FakeProvider({"url": "https://contoso/new.docx", "fileName": "new.docx"})

    result = await create_document("new.docx", "<p>Hello</p>", provider=provider)

    assert result == WordDocument(
        file_name="new.docx",
        url="https://contoso/new.docx",
        raw={"url": "https://contoso/new.docx", "fileName": "new.docx"},
    )
    assert provider.calls[0]["tool_name"] == "CreateDocument"
    assert provider.calls[0]["arguments"] == {
        "fileName": "new.docx",
        "contentInHtml": "<p>Hello</p>",
    }


@pytest.mark.asyncio
async def test_create_document_accepts_live_drive_item_shape() -> None:
    provider = FakeProvider(
        {
            "driveItem": {
                "Name": "new.docx",
                "WebUrl": "https://contoso/new.docx",
                "Id": "item-1",
            }
        }
    )

    result = await create_document("new.docx", "<p>Hello</p>", provider=provider)

    assert result == WordDocument(
        file_name="new.docx",
        url="https://contoso/new.docx",
        raw={
            "driveItem": {
                "Name": "new.docx",
                "WebUrl": "https://contoso/new.docx",
                "Id": "item-1",
            }
        },
    )


@pytest.mark.asyncio
async def test_create_document_missing_url_raises_value_error() -> None:
    provider = FakeProvider({"fileName": "new.docx"})

    with pytest.raises(ValueError, match="missing document URL"):
        await create_document("new.docx", "<p>Hello</p>", provider=provider)


@pytest.mark.asyncio
async def test_create_comment_calls_word_tool() -> None:
    provider = FakeProvider({"id": "c1", "content": "Looks wrong"})

    result = await create_comment("drive-1", "doc-1", "Looks wrong", provider=provider)

    assert result == WordComment(
        comment_id="c1",
        content="Looks wrong",
        raw={"id": "c1", "content": "Looks wrong"},
    )
    assert provider.calls[0]["tool_name"] == "AddComment"
    assert provider.calls[0]["arguments"] == {
        "driveId": "drive-1",
        "documentId": "doc-1",
        "newComment": "Looks wrong",
    }


@pytest.mark.asyncio
async def test_create_comment_missing_id_raises_value_error() -> None:
    provider = FakeProvider({"content": "Looks wrong"})

    with pytest.raises(ValueError, match="missing comment id"):
        await create_comment("drive-1", "doc-1", "Looks wrong", provider=provider)


@pytest.mark.asyncio
async def test_create_comment_accepts_a365_success_without_id() -> None:
    provider = FakeProvider(
        {
            "content": [{"type": "text", "text": "Comment added successfully."}],
            "isError": False,
        }
    )

    result = await create_comment("drive-1", "doc-1", "Looks right", provider=provider)

    assert result == WordComment(
        comment_id="",
        content="Looks right",
        raw={
            "content": [{"type": "text", "text": "Comment added successfully."}],
            "isError": False,
        },
    )


@pytest.mark.asyncio
async def test_create_comment_parses_live_word_comment_info_text() -> None:
    provider = FakeProvider(
        {
            "content": [
                {
                    "type": "text",
                    "text": "WordCommentInfo [CommentId=27CC2AEF, Content=Looks right]",
                },
                {
                    "type": "text",
                    "text": "CorrelationId: abc, TimeStamp: 2026-05-15_17:42:51",
                },
            ],
            "isError": False,
        }
    )

    result = await create_comment("drive-1", "doc-1", "Looks right", provider=provider)

    assert result.comment_id == "27CC2AEF"
    assert result.content == "Looks right"


@pytest.mark.asyncio
async def test_reply_to_comment_calls_word_tool() -> None:
    provider = FakeProvider({"id": "r1", "content": "No."})

    result = await reply_to_comment("drive-1", "doc-1", "c1", "No.", provider=provider)

    assert result == WordCommentReply(
        reply_id="r1",
        comment_id="c1",
        content="No.",
        raw={"id": "r1", "content": "No."},
    )
    assert provider.calls[0]["tool_name"] == "ReplyToComment"
    assert provider.calls[0]["arguments"] == {
        "commentId": "c1",
        "driveId": "drive-1",
        "documentId": "doc-1",
        "newComment": "No.",
    }


@pytest.mark.asyncio
async def test_reply_to_comment_missing_id_raises_value_error() -> None:
    provider = FakeProvider({"content": "No."})

    with pytest.raises(ValueError, match="missing reply id"):
        await reply_to_comment("drive-1", "doc-1", "c1", "No.", provider=provider)


@pytest.mark.asyncio
async def test_reply_to_comment_parses_live_word_comment_info_text() -> None:
    provider = FakeProvider(
        {
            "content": [
                {
                    "type": "text",
                    "text": "WordCommentInfo [CommentId=2DD36F45, Content=Reply text]",
                },
                {
                    "type": "text",
                    "text": "CorrelationId: abc, TimeStamp: 2026-05-15_17:43:03",
                },
            ],
            "isError": False,
        }
    )

    result = await reply_to_comment("drive-1", "doc-1", "c1", "Reply text", provider=provider)

    assert result.reply_id == "2DD36F45"
    assert result.comment_id == "c1"
    assert result.content == "Reply text"
