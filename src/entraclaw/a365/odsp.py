"""Typed adapter for Work IQ OneDrive/SharePoint MCP tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from entraclaw.a365.catalog import ODSP_SERVER_NAME
from entraclaw.a365.provider import WorkIqProvider


@dataclass(frozen=True)
class OdspFileMetadata:
    item_id: str
    name: str
    web_url: str
    document_library_id: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class OdspFileContent:
    content: str
    encoding: str
    raw: dict[str, Any]


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


async def get_file_or_folder_metadata_by_url(
    url: str,
    *,
    provider: WorkIqProvider | None = None,
) -> OdspFileMetadata:
    """Read OneDrive/SharePoint file metadata through Work IQ ODSP."""
    result = await _provider(provider).call_tool(
        server_name=ODSP_SERVER_NAME,
        tool_name="getFileOrFolderMetadataByUrl",
        arguments={"fileOrFolderUrl": _required(url, "url")},
    )
    return OdspFileMetadata(
        item_id=_first_string(result, "id", "Id", "fileId", "itemId"),
        name=_first_string(result, "name", "Name", "fileName"),
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
    result = await _provider(provider).call_tool(
        server_name=ODSP_SERVER_NAME,
        tool_name="readSmallTextFile",
        arguments={
            "documentLibraryId": _required(document_library_id, "document_library_id"),
            "fileId": _required(file_id, "file_id"),
        },
    )
    return OdspFileContent(
        content=str(result.get("content") or ""),
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
    return OdspFileContent(
        content=str(result.get("content") or ""),
        encoding=str(result.get("encoding") or "base64"),
        raw=result,
    )
