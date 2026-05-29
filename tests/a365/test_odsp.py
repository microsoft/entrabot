from __future__ import annotations

import pytest

from entrabot.a365.odsp import (
    OdspFileContent,
    OdspFileMetadata,
    get_file_or_folder_metadata_by_url,
    read_small_binary_file,
    read_small_text_file,
)


class FakeProvider:
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def call_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        self.calls.append(
            {
                "server_name": server_name,
                "tool_name": tool_name,
                "arguments": arguments,
            }
        )
        return self.result


@pytest.mark.asyncio
async def test_get_file_or_folder_metadata_by_url_calls_odsp_tool() -> None:
    provider = FakeProvider(
        {
            "id": "item-1",
            "name": "page.loop",
            "webUrl": "https://contoso/page.loop",
            "documentLibraryId": "drive-1",
        }
    )

    result = await get_file_or_folder_metadata_by_url(
        "https://contoso/page.loop",
        provider=provider,
    )

    assert result == OdspFileMetadata(
        item_id="item-1",
        name="page.loop",
        web_url="https://contoso/page.loop",
        document_library_id="drive-1",
        raw={
            "id": "item-1",
            "name": "page.loop",
            "webUrl": "https://contoso/page.loop",
            "documentLibraryId": "drive-1",
        },
    )
    assert provider.calls == [
        {
            "server_name": "mcp_ODSPRemoteServer",
            "tool_name": "getFileOrFolderMetadataByUrl",
            "arguments": {"fileOrFolderUrl": "https://contoso/page.loop"},
        }
    ]


@pytest.mark.asyncio
async def test_read_small_text_file_calls_odsp_tool() -> None:
    provider = FakeProvider({"content": "hello"})

    result = await read_small_text_file("drive-1", "item-1", provider=provider)

    assert result == OdspFileContent(
        content="hello",
        encoding="text",
        raw={"content": "hello"},
    )
    assert provider.calls[0]["tool_name"] == "readSmallTextFile"
    assert provider.calls[0]["arguments"] == {
        "documentLibraryId": "drive-1",
        "fileId": "item-1",
    }


@pytest.mark.asyncio
async def test_get_file_metadata_reads_nested_parent_reference_drive_id() -> None:
    provider = FakeProvider(
        {
            "id": "item-1",
            "name": "page.loop",
            "webUrl": "https://contoso/page.loop",
            "parentReference": {"driveId": "drive-1"},
        }
    )

    result = await get_file_or_folder_metadata_by_url(
        "https://contoso/page.loop",
        provider=provider,
    )

    assert result.document_library_id == "drive-1"


@pytest.mark.asyncio
async def test_get_file_metadata_reads_live_pascal_case_parent_reference_drive_id() -> None:
    provider = FakeProvider(
        {
            "Id": "item-1",
            "Name": "page.loop",
            "WebUrl": "https://contoso/page.loop",
            "ParentReference": {"DriveId": "drive-1"},
        }
    )

    result = await get_file_or_folder_metadata_by_url(
        "https://contoso/page.loop",
        provider=provider,
    )

    assert result == OdspFileMetadata(
        item_id="item-1",
        name="page.loop",
        web_url="https://contoso/page.loop",
        document_library_id="drive-1",
        raw={
            "Id": "item-1",
            "Name": "page.loop",
            "WebUrl": "https://contoso/page.loop",
            "ParentReference": {"DriveId": "drive-1"},
        },
    )


@pytest.mark.asyncio
async def test_read_small_binary_file_calls_odsp_tool() -> None:
    provider = FakeProvider({"content": "SGVsbG8=", "encoding": "base64"})

    result = await read_small_binary_file("drive-1", "item-1", provider=provider)

    assert result == OdspFileContent(
        content="SGVsbG8=",
        encoding="base64",
        raw={"content": "SGVsbG8=", "encoding": "base64"},
    )
    assert provider.calls[0]["tool_name"] == "readSmallBinaryFile"


@pytest.mark.asyncio
async def test_odsp_url_is_required() -> None:
    with pytest.raises(ValueError, match="url is required"):
        await get_file_or_folder_metadata_by_url("  ", provider=FakeProvider({}))
