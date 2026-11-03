"""Typed adapter for Work IQ Excel MCP tools.

Results deliberately carry no ``raw`` provider response. ``CreateWorkbook``
returns a driveItem with a pre-authenticated ``@microsoft.graph.downloadUrl``,
and workbook reads return author-controlled comments, so neither may reach the
model or logs outside the fields below.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from entrabot.a365.catalog import EXCEL_SERVER_NAME
from entrabot.a365.errors import A365McpCallError
from entrabot.a365.provider import WorkIqProvider
from entrabot.tools.text_limits import max_text_bytes, truncate_utf8
from entrabot.tools.worksheet_names import is_valid_worksheet_name

_MAX_TOOL_ERROR_DETAIL = 500
_XLSX_EXTENSION = ".xlsx"
_CELL_REFERENCE_RE = re.compile(r"[A-Za-z]{1,3}[1-9]\d{0,6}")


@dataclass(frozen=True)
class ExcelWorkbookContent:
    """A workbook read. ``text`` holds all workbook data inside the XPIA envelope."""

    drive_id: str
    document_id: str
    size_bytes: int | None
    text: str
    truncated: bool


@dataclass(frozen=True)
class ExcelWorkbook:
    file_name: str
    url: str
    drive_id: str
    item_id: str


@dataclass(frozen=True)
class ExcelComment:
    comment_id: str
    cell_address: str


@dataclass(frozen=True)
class ExcelCommentReply:
    reply_id: str
    comment_id: str


def _provider(provider: WorkIqProvider | None) -> WorkIqProvider:
    return provider or WorkIqProvider.from_env()


def _required(value: str, name: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _is_valid_cell_address(address: str) -> bool:
    """Check ``Sheet!A1`` or ``'My Sheet'!A1``.

    Excel allows ``!`` in a sheet name, so the reference follows the last one.
    In a quoted name a doubled quote stands for one quote. Excel forbids a name
    that starts or ends with a quote, so an unbalanced quote can't be valid.
    """
    sheet, separator, reference = address.rpartition("!")
    if not separator or not _CELL_REFERENCE_RE.fullmatch(reference):
        return False
    if len(sheet) >= 2 and sheet.startswith("'") and sheet.endswith("'"):
        sheet = sheet[1:-1].replace("''", "'")
    elif sheet.startswith("'") or sheet.endswith("'"):
        return False
    return is_valid_worksheet_name(sheet)


def _string_field(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    return value if isinstance(value, str) else ""


def _first_mcp_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            return item["text"]
    return ""


def _raise_for_tool_error(result: dict[str, Any], tool_name: str) -> None:
    """Raise with Work IQ's own error text when a tool call reports ``isError``.

    The detail reaches the model, so it is truncated first and then wrapped;
    truncating after wrapping would cut off the closing tag.
    """
    from entrabot.security.xpia import wrap_external

    if result.get("isError") is not True:
        return
    detail = _first_mcp_text(result).strip() or "Work IQ reported isError with no error detail"
    wrapped = wrap_external(
        detail[:_MAX_TOOL_ERROR_DETAIL],
        source=f"a365:{EXCEL_SERVER_NAME}.{tool_name}",
    )
    raise A365McpCallError(EXCEL_SERVER_NAME, tool_name, wrapped)


def _required_result_id(result: dict[str, Any], description: str) -> str:
    result_id = _string_field(result, "id").strip()
    if not result_id:
        raise ValueError(f"Work IQ returned malformed response: missing {description}")
    return result_id


def _workbook_file_name(file_name: str) -> str:
    clean = _required(file_name, "file_name")
    if "/" in clean or "\\" in clean:
        raise ValueError("file_name must not contain path separators")
    if clean.lower().endswith(_XLSX_EXTENSION):
        return clean
    return f"{clean}{_XLSX_EXTENSION}"


def _parse_drive_item(result: dict[str, Any]) -> dict[str, Any]:
    drive_item = result.get("driveItem")
    if isinstance(drive_item, str):
        try:
            drive_item = json.loads(drive_item)
        except ValueError:
            drive_item = None
    if not isinstance(drive_item, dict):
        raise ValueError("Work IQ returned malformed response: missing driveItem")
    return drive_item


async def get_workbook_content(
    url: str,
    *,
    provider: WorkIqProvider | None = None,
) -> ExcelWorkbookContent:
    """Read a workbook's cell text and comments through Work IQ Excel."""
    from entrabot.security.xpia import wrap_external

    clean_url = _required(url, "url")
    result = await _provider(provider).call_tool(
        server_name=EXCEL_SERVER_NAME,
        tool_name="GetDocumentContent",
        arguments={"url": clean_url},
    )
    _raise_for_tool_error(result, "GetDocumentContent")
    content = result.get("content")
    if not isinstance(content, str):
        raise ValueError("Work IQ returned malformed response: missing workbook content")
    comments = result.get("comments")
    size = result.get("size")
    # The file name, comments, and cells are all author-controlled, so all of
    # them stay inside one envelope. Comments precede the cell text so that
    # truncating a large workbook keeps them, and their IDs, readable.
    payload = json.dumps(
        {
            "file_name": _string_field(result, "fileName"),
            "comments": comments if isinstance(comments, list) else [],
            "content": content,
        },
        ensure_ascii=False,
    )
    text, truncated = truncate_utf8(payload, max_bytes=max_text_bytes())
    return ExcelWorkbookContent(
        drive_id=_string_field(result, "driveId"),
        document_id=_string_field(result, "documentId"),
        size_bytes=size if isinstance(size, int) and not isinstance(size, bool) else None,
        text=wrap_external(text, source=f"file:{clean_url}#excel", sender=None),
        truncated=truncated,
    )


async def create_workbook(
    file_name: str,
    csv_content: str,
    *,
    provider: WorkIqProvider | None = None,
) -> ExcelWorkbook:
    """Create a workbook from CSV in the root of the Agent User's OneDrive."""
    _required(csv_content, "csv_content")
    arguments = {
        "fileName": _workbook_file_name(file_name),
        # Unstripped: leading and trailing whitespace can be meaningful in CSV.
        "csvContent": csv_content,
    }
    # Never send CreateWorkbook's optional ``shareWith``: it would share the
    # file without the sponsor, channel, and membership checks in ``share_file``.
    result = await _provider(provider).call_tool(
        server_name=EXCEL_SERVER_NAME,
        tool_name="CreateWorkbook",
        arguments=arguments,
    )
    _raise_for_tool_error(result, "CreateWorkbook")
    drive_item = _parse_drive_item(result)
    url = _string_field(drive_item, "webUrl")
    item_id = _string_field(drive_item, "id")
    if not url or not item_id:
        raise ValueError("Work IQ returned malformed response: missing workbook URL or id")
    parent = drive_item.get("parentReference")
    return ExcelWorkbook(
        file_name=_string_field(drive_item, "name") or arguments["fileName"],
        url=url,
        drive_id=_string_field(parent, "driveId") if isinstance(parent, dict) else "",
        item_id=item_id,
    )


async def create_comment(
    drive_id: str,
    document_id: str,
    cell_address: str,
    content: str,
    *,
    provider: WorkIqProvider | None = None,
) -> ExcelComment:
    """Create a comment on one cell through Work IQ Excel."""
    clean_address = _required(cell_address, "cell_address")
    if not _is_valid_cell_address(clean_address):
        raise ValueError("cell_address must look like Sheet1!A1")
    arguments = {
        "cellAddress": clean_address,
        "driveId": _required(drive_id, "drive_id"),
        "documentId": _required(document_id, "document_id"),
        "content": _required(content, "content"),
    }
    result = await _provider(provider).call_tool(
        server_name=EXCEL_SERVER_NAME,
        tool_name="CreateComment",
        arguments=arguments,
    )
    _raise_for_tool_error(result, "CreateComment")
    return ExcelComment(
        comment_id=_required_result_id(result, "comment id"),
        cell_address=clean_address,
    )


async def reply_to_comment(
    drive_id: str,
    document_id: str,
    comment_id: str,
    content: str,
    *,
    provider: WorkIqProvider | None = None,
) -> ExcelCommentReply:
    """Reply to an existing workbook comment through Work IQ Excel."""
    clean_comment_id = _required(comment_id, "comment_id")
    arguments = {
        "commentId": clean_comment_id,
        "driveId": _required(drive_id, "drive_id"),
        "documentId": _required(document_id, "document_id"),
        "newComment": _required(content, "content"),
    }
    result = await _provider(provider).call_tool(
        server_name=EXCEL_SERVER_NAME,
        tool_name="ReplyToComment",
        arguments=arguments,
    )
    _raise_for_tool_error(result, "ReplyToComment")
    return ExcelCommentReply(
        reply_id=_required_result_id(result, "reply id"),
        comment_id=clean_comment_id,
    )
