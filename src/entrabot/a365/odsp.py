"""Typed adapter for Work IQ OneDrive/SharePoint MCP tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from entrabot.a365.catalog import ODSP_SERVER_NAME
from entrabot.a365.errors import A365McpCallError
from entrabot.a365.provider import WorkIqProvider

_MAX_TOOL_ERROR_DETAIL = 500


@dataclass(frozen=True)
class OdspFileMetadata:
    item_id: str
    name: str
    web_url: str
    document_library_id: str
    raw: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class OdspFileContent:
    content: str
    encoding: str
    raw: dict[str, Any] = field(repr=False)


def _provider(provider: WorkIqProvider | None) -> WorkIqProvider:
    return provider or WorkIqProvider.from_env()


def _required(value: str, name: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _first_string(raw: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


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
        source=f"a365:{ODSP_SERVER_NAME}.{tool_name}",
    )
    raise A365McpCallError(ODSP_SERVER_NAME, tool_name, wrapped)


def _file_content(result: dict[str, Any]) -> str:
    """Return the file body, refusing an MCP content list that isn't one."""
    content = result.get("content")
    if content is None:
        return ""
    if not isinstance(content, str):
        raise ValueError("Work IQ returned malformed response: file content is not a string")
    return content


async def get_file_or_folder_metadata_by_url(
    url: str,
    *,
    provider: WorkIqProvider | None = None,
) -> OdspFileMetadata:
    """Read OneDrive/SharePoint file metadata through Work IQ ODSP.

    ``name`` is author-controlled, so it comes back inside the XPIA envelope.
    """
    from entrabot.security.xpia import wrap_external

    clean_url = _required(url, "url")
    result = await _provider(provider).call_tool(
        server_name=ODSP_SERVER_NAME,
        tool_name="getFileOrFolderMetadataByUrl",
        arguments={"fileOrFolderUrl": clean_url},
    )
    _raise_for_tool_error(result, "getFileOrFolderMetadataByUrl")
    name = _first_string(result, "name", "Name", "fileName")
    return OdspFileMetadata(
        item_id=_first_string(result, "id", "Id", "fileId", "itemId"),
        name=wrap_external(name, source=f"file:{clean_url}", sender=None) if name else "",
        web_url=_first_string(result, "webUrl", "WebUrl", "webURL", "url"),
        document_library_id=_first_nested_string(
            result,
            ("documentLibraryId",),
            ("driveId",),
            ("documentLibraryID",),
            ("parentReference", "driveId"),
            ("ParentReference", "DriveId"),
        ),
        raw=result,
    )


async def read_small_text_file(
    document_library_id: str,
    file_id: str,
    *,
    provider: WorkIqProvider | None = None,
) -> OdspFileContent:
    """Read a small text file from OneDrive/SharePoint through Work IQ ODSP."""
    from entrabot.security.xpia import wrap_external

    clean_file_id = _required(file_id, "file_id")
    result = await _provider(provider).call_tool(
        server_name=ODSP_SERVER_NAME,
        tool_name="readSmallTextFile",
        arguments={
            "documentLibraryId": _required(document_library_id, "document_library_id"),
            "fileId": clean_file_id,
        },
    )
    _raise_for_tool_error(result, "readSmallTextFile")
    raw_content = _file_content(result)
    # XPIA envelope: file bodies from OneDrive/SharePoint are model-facing
    # external content. ``raw`` (the provider's response) stays intact for
    # audit; ``encoding`` is metadata about the model-facing wrap.
    return OdspFileContent(
        content=wrap_external(
            raw_content,
            source=f"a365:{clean_file_id}",
            sender=None,
        ),
        encoding="text",
        raw=result,
    )


async def read_small_binary_file(
    document_library_id: str,
    file_id: str,
    *,
    provider: WorkIqProvider | None = None,
) -> OdspFileContent:
    """Read a small binary file from OneDrive/SharePoint through Work IQ ODSP."""
    result = await _provider(provider).call_tool(
        server_name=ODSP_SERVER_NAME,
        tool_name="readSmallBinaryFile",
        arguments={
            "documentLibraryId": _required(document_library_id, "document_library_id"),
            "fileId": _required(file_id, "file_id"),
        },
    )
    _raise_for_tool_error(result, "readSmallBinaryFile")
    return OdspFileContent(
        content=_file_content(result),
        encoding=str(result.get("encoding") or "base64"),
        raw=result,
    )
