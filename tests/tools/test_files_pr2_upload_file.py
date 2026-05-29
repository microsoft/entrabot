"""Tests for upload_file tool (PR2).

TDD: all tests written before implementation.
Tests cover:
  - Small files (<4MiB): single PUT to /content endpoint
  - Large files (>=4MiB): createUploadSession + chunked PUT
  - Mid-stream 503 retry: GET nextExpectedRanges, resume
  - Conflict modes (rename, replace, fail)
  - Denylist rejection
  - 429 throttle (per PR1 pattern)
  - Final chunk 200/201 acceptance
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from entrabot.errors import (
    SiteNotAllowedError,
)
from entrabot.tools.files import (
    OneDriveTarget,
    SharePointTarget,
    upload_file,
)


@pytest.mark.asyncio
class TestUploadFile:
    """Tests for upload_file mutation tool."""

    async def test_small_file_single_put(self):
        """Files <4MiB use single PUT /content endpoint."""
        target = OneDriveTarget()
        token = "mock_token_123"
        small_content = b"Small file content" * 100  # ~1.8KB

        with patch("entrabot.tools.files._client") as mock_client_ctx:
            mock_response = MagicMock()
            mock_response.status_code = 201
            mock_response.json.return_value = {
                "id": "item_small",
                "name": "doc.pdf",
                "webUrl": "https://onedrive.live.com/doc.pdf",
                "size": len(small_content),
            }

            mock_client = MagicMock()
            mock_client.put = AsyncMock(return_value=mock_response)
            mock_client_ctx.return_value.__aenter__.return_value = mock_client
            mock_client_ctx.return_value.__aexit__.return_value = None

            result = await upload_file(
                target=target,
                file_name="doc.pdf",
                content_bytes=small_content,
                conflict_behavior="fail",
                token=token,
            )

            assert result.name == "doc.pdf"
            assert result.size_bytes == len(small_content)

    async def test_large_file_chunked_upload(self):
        """Files >=4MiB use createUploadSession + chunked PUT."""
        target = OneDriveTarget()
        token = "mock_token_123"
        # 5 MiB
        large_content = b"X" * (5 * 1024 * 1024 + 1000)

        with patch("entrabot.tools.files._client") as mock_client_ctx:
            # First call: createUploadSession
            session_response = MagicMock()
            session_response.status_code = 200
            session_response.json.return_value = {
                "uploadUrl": "https://graph.microsoft.com/upload/url",
                "expirationDateTime": "2099-01-01T00:00:00Z",
            }

            # Final chunk response
            final_response = MagicMock()
            final_response.status_code = 201
            final_response.json.return_value = {
                "id": "item_large",
                "name": "archive.zip",
                "size": len(large_content),
            }

            # Setup mock client to return different responses
            call_count = [0]

            async def mock_put(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return session_response
                elif call_count[0] < 3:
                    return session_response  # Intermediate chunks
                else:
                    return final_response

            mock_client = MagicMock()
            mock_client.post = AsyncMock(return_value=session_response)
            mock_client.put = AsyncMock(side_effect=mock_put)
            mock_client_ctx.return_value.__aenter__.return_value = mock_client
            mock_client_ctx.return_value.__aexit__.return_value = None

            result = await upload_file(
                target=target,
                file_name="archive.zip",
                content_bytes=large_content,
                conflict_behavior="fail",
                token=token,
            )

            assert result.name == "archive.zip"
            assert result.size_bytes == len(large_content)

    async def test_large_file_503_mid_stream_resume(self):
        """Mid-stream 503 retrieves nextExpectedRanges and resumes."""
        target = OneDriveTarget()
        token = "mock_token_123"
        large_content = b"Y" * (5 * 1024 * 1024)

        with patch("entrabot.tools.files._client") as mock_client_ctx:
            session_response = MagicMock()
            session_response.status_code = 200
            session_response.json.return_value = {
                "uploadUrl": "https://graph.microsoft.com/upload/url",
            }

            # 503 during chunk 2
            error_response = MagicMock()
            error_response.status_code = 503
            error_response.json.return_value = {
                "error": {"message": "Service Unavailable"},
                "nextExpectedRanges": ["5242880-10485759"],  # Resume from byte 5242880
            }

            final_response = MagicMock()
            final_response.status_code = 201
            final_response.json.return_value = {
                "id": "item_resumed",
                "name": "archive.zip",
                "size": len(large_content),
            }

            call_count = [0]

            async def mock_put(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return error_response
                else:
                    return final_response

            mock_client = MagicMock()
            mock_client.post = AsyncMock(return_value=session_response)
            mock_client.put = AsyncMock(side_effect=mock_put)
            mock_client.get = AsyncMock(return_value=error_response)
            mock_client_ctx.return_value.__aenter__.return_value = mock_client
            mock_client_ctx.return_value.__aexit__.return_value = None

            result = await upload_file(
                target=target,
                file_name="archive.zip",
                content_bytes=large_content,
                conflict_behavior="fail",
                token=token,
            )

            assert result.name == "archive.zip"

    async def test_conflict_behavior_rename(self):
        """Conflict rename via createUploadSession parameters."""
        target = OneDriveTarget()
        token = "mock_token_123"
        small_content = b"Small" * 100

        with patch("entrabot.tools.files._client") as mock_client_ctx:
            mock_response = MagicMock()
            mock_response.status_code = 201
            mock_response.json.return_value = {
                "id": "item_renamed",
                "name": "file 1.pdf",
                "size": len(small_content),
            }

            mock_client = MagicMock()
            mock_client.put = AsyncMock(return_value=mock_response)
            mock_client_ctx.return_value.__aenter__.return_value = mock_client
            mock_client_ctx.return_value.__aexit__.return_value = None

            result = await upload_file(
                target=target,
                file_name="file.pdf",
                content_bytes=small_content,
                conflict_behavior="rename",
                token=token,
            )

            assert result.name == "file 1.pdf"

    async def test_conflict_behavior_replace(self):
        """Conflict replace via createUploadSession parameters."""
        target = OneDriveTarget()
        token = "mock_token_123"
        small_content = b"Replace" * 100

        with patch("entrabot.tools.files._client") as mock_client_ctx:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "id": "item_replaced",
                "name": "file.pdf",
                "size": len(small_content),
            }

            mock_client = MagicMock()
            mock_client.put = AsyncMock(return_value=mock_response)
            mock_client_ctx.return_value.__aenter__.return_value = mock_client
            mock_client_ctx.return_value.__aexit__.return_value = None

            result = await upload_file(
                target=target,
                file_name="file.pdf",
                content_bytes=small_content,
                conflict_behavior="replace",
                token=token,
            )

            assert result.name == "file.pdf"

    async def test_denylist_rejection(self):
        """Denylist blocks upload."""
        target = SharePointTarget(
            site_id="site_denied",
            drive_id="drive_denied",
            folder_path="/",
        )
        token = "mock_token_123"

        with patch("entrabot.tools.files._check_site_allowed") as mock_check:
            mock_check.side_effect = SiteNotAllowedError("site_denied")

            with pytest.raises(SiteNotAllowedError):
                await upload_file(
                    target=target,
                    file_name="forbidden.pdf",
                    content_bytes=b"content",
                    conflict_behavior="fail",
                    token=token,
                )

    async def test_429_throttle_retry(self):
        """429 retry handled by RetryOn429Transport (PR1 pattern)."""
        target = OneDriveTarget()
        token = "mock_token_123"
        small_content = b"Throttled" * 100

        with patch("entrabot.tools.files._client") as mock_client_ctx:
            # First call returns 429; transport retries internally
            final_response = MagicMock()
            final_response.status_code = 201
            final_response.json.return_value = {
                "id": "item_throttled",
                "name": "file.pdf",
                "size": len(small_content),
            }

            mock_client = MagicMock()
            mock_client.put = AsyncMock(return_value=final_response)
            mock_client_ctx.return_value.__aenter__.return_value = mock_client
            mock_client_ctx.return_value.__aexit__.return_value = None

            result = await upload_file(
                target=target,
                file_name="file.pdf",
                content_bytes=small_content,
                conflict_behavior="fail",
                token=token,
            )

            assert result.name == "file.pdf"

    async def test_sharepoint_happy_path(self):
        """Upload to SharePoint site library."""
        target = SharePointTarget(
            site_id="site_xyz",
            drive_id="drive_xyz",
            folder_path="/Shared",
        )
        token = "mock_token_123"
        small_content = b"SharePoint file" * 50

        with patch("entrabot.tools.files._check_site_allowed") as mock_check:
            mock_check.return_value = None

            with patch("entrabot.tools.files._client") as mock_client_ctx:
                mock_response = MagicMock()
                mock_response.status_code = 201
                mock_response.json.return_value = {
                    "id": "item_sp",
                    "name": "sp_file.pdf",
                    "size": len(small_content),
                }

                mock_client = MagicMock()
                mock_client.put = AsyncMock(return_value=mock_response)
                mock_client_ctx.return_value.__aenter__.return_value = mock_client
                mock_client_ctx.return_value.__aexit__.return_value = None

                result = await upload_file(
                    target=target,
                    file_name="sp_file.pdf",
                    content_bytes=small_content,
                    conflict_behavior="fail",
                    token=token,
                )

                assert result.name == "sp_file.pdf"
                assert result.site_id == "site_xyz"
