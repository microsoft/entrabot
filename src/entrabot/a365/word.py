"""Typed adapter for Work IQ Word MCP tools."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from entrabot.a365.catalog import WORD_SERVER_NAME
from entrabot.a365.provider import WorkIqProvider


@dataclass(frozen=True)
class WordDocumentContent:
    content_html: str
    comments: list[dict[str, Any]]
    raw: dict[str, Any]


@dataclass(frozen=True)
class WordDocument:
    file_name: str
    url: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class WordComment:
    comment_id: str
    content: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class WordCommentReply:
    reply_id: str
    comment_id: str
    content: str
    raw: dict[str, Any]


def _provider(provider: WorkIqProvider | None) -> WorkIqProvider:
    return provider or WorkIqProvider.from_env()


def _required(value: str, name: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _is_successful_mcp_text_result(result: dict[str, Any]) -> bool:
    content = result.get("content")
    return result.get("isError") is False and isinstance(content, list) and len(content) > 0


def _string_field_or_default(result: dict[str, Any], key: str, default: str) -> str:
    value = result.get(key)
    return value if isinstance(value, str) else default


def _first_mcp_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            return item["text"]
    return ""


def _parse_word_comment_info(result: dict[str, Any]) -> tuple[str, str]:
    text = _first_mcp_text(result)
    match = re.search(r"WordCommentInfo \[CommentId=([^,\]]+), Content=(.*)\]", text)
    if not match:
        return "", ""
    return match.group(1).strip(), match.group(2).strip()


def _first_nested_string(raw: dict[str, Any], *paths: tuple[str, ...]) -> str:
    for path in paths:
        current: Any = raw
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if isinstance(current, str) and current:
            return current
    return ""


async def get_document_content(
    url: str,
    *,
    provider: WorkIqProvider | None = None,
) -> WordDocumentContent:
    """Read Word document content and comments through Work IQ Word."""
    result = await _provider(provider).call_tool(
        server_name=WORD_SERVER_NAME,
        tool_name="GetDocumentContent",
        arguments={"url": _required(url, "url")},
    )
    comments = result.get("comments")
    return WordDocumentContent(
        content_html=str(result.get("content") or result.get("contentInHtml") or ""),
        comments=list(comments) if isinstance(comments, list) else [],
        raw=result,
    )


async def create_document(
    file_name: str,
    content_html: str,
    *,
    provider: WorkIqProvider | None = None,
) -> WordDocument:
    """Create a Word document through Work IQ Word."""
    result = await _provider(provider).call_tool(
        server_name=WORD_SERVER_NAME,
        tool_name="CreateDocument",
        arguments={
            "fileName": _required(file_name, "file_name"),
            "contentInHtml": _required(content_html, "content_html"),
        },
    )
    document_url = _first_nested_string(
        result,
        ("url",),
        ("webUrl",),
        ("WebUrl",),
        ("driveItem", "WebUrl"),
        ("driveItem", "webUrl"),
    )
    if not document_url.strip():
        raise ValueError("Work IQ returned malformed response: missing document URL")
    return WordDocument(
        file_name=_first_nested_string(
            result,
            ("fileName",),
            ("name",),
            ("Name",),
            ("driveItem", "Name"),
            ("driveItem", "name"),
        )
        or file_name,
        url=document_url,
        raw=result,
    )


async def create_comment(
    drive_id: str,
    document_id: str,
    content: str,
    *,
    provider: WorkIqProvider | None = None,
) -> WordComment:
    """Create a top-level Word comment through Work IQ Word."""
    result = await _provider(provider).call_tool(
        server_name=WORD_SERVER_NAME,
        tool_name="AddComment",
        arguments={
            "driveId": _required(drive_id, "drive_id"),
            "documentId": _required(document_id, "document_id"),
            "newComment": _required(content, "content"),
        },
    )
    parsed_comment_id, parsed_content = _parse_word_comment_info(result)
    comment_id = str(result.get("id") or result.get("commentId") or parsed_comment_id or "")
    if not comment_id.strip() and not _is_successful_mcp_text_result(result):
        raise ValueError("Work IQ returned malformed response: missing comment id")
    return WordComment(
        comment_id=comment_id,
        content=_string_field_or_default(result, "content", parsed_content or content),
        raw=result,
    )


async def reply_to_comment(
    drive_id: str,
    document_id: str,
    comment_id: str,
    content: str,
    *,
    provider: WorkIqProvider | None = None,
) -> WordCommentReply:
    """Reply to an existing Word comment through Work IQ Word."""
    result = await _provider(provider).call_tool(
        server_name=WORD_SERVER_NAME,
        tool_name="ReplyToComment",
        arguments={
            "commentId": _required(comment_id, "comment_id"),
            "driveId": _required(drive_id, "drive_id"),
            "documentId": _required(document_id, "document_id"),
            "newComment": _required(content, "content"),
        },
    )
    parsed_reply_id, parsed_content = _parse_word_comment_info(result)
    reply_id = str(result.get("id") or result.get("replyId") or parsed_reply_id or "")
    if not reply_id.strip():
        raise ValueError("Work IQ returned malformed response: missing reply id")
    return WordCommentReply(
        reply_id=reply_id,
        comment_id=comment_id,
        content=_string_field_or_default(result, "content", parsed_content or content),
        raw=result,
    )
