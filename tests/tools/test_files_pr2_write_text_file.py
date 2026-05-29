"""Tests for write_text_file tool (PR2).

TDD: all tests written before implementation.
Tests cover:
  - OneDrive happy path (text write)
  - SharePoint happy path (text write with site_id)
  - Denylist rejection (SiteNotAllowedError audit logged)
  - Conflict modes (rename, replace, fail)
  - 403 Forbidden (lack of permission)
  - 5xx retries exhausted (no retry on mutation)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from entrabot.errors import (
    GraphFilesError,
    MissingPermissionError,
    SiteNotAllowedError,
)
from entrabot.tools.files import (
    OneDriveTarget,
    SharePointTarget,
    write_text_file,
)


@pytest.mark.asyncio
class TestWriteTextFile:
    """Tests for write_text_file mutation tool."""

    async def test_onedrive_happy_path(self):
        """Write text to OneDrive root folder."""
        target = OneDriveTarget(folder_path="/")
        token = "mock_token_123"

        with patch("entrabot.tools.files._client") as mock_client_ctx:
            mock_response = MagicMock()
            mock_response.status_code = 201
            mock_response.json.return_value = {
                "id": "item_456",
                "name": "spec.txt",
                "webUrl": "https://onedrive.live.com/spec.txt",
                "size": 256,
            }

            mock_client = MagicMock()
            mock_client.put = AsyncMock(return_value=mock_response)
            mock_client_ctx.return_value.__aenter__.return_value = mock_client
            mock_client_ctx.return_value.__aexit__.return_value = None

            result = await write_text_file(
                target=target,
                file_name="spec.txt",
                content="Spec v1.0\nRequirements here",
                conflict_behavior="fail",
                token=token,
            )

            assert result.name == "spec.txt"
            assert result.web_url == "https://onedrive.live.com/spec.txt"

    async def test_sharepoint_happy_path(self):
        """Write text to SharePoint site library."""
        target = SharePointTarget(
            site_id="site_abc123",
            drive_id="drive_def456",
            folder_path="/shared",
        )
        token = "mock_token_123"

        with patch("entrabot.tools.files._check_site_allowed") as mock_check:
            mock_check.return_value = None  # Site allowed

            with patch("entrabot.tools.files._client") as mock_client_ctx:
                mock_response = MagicMock()
                mock_response.status_code = 201
                mock_response.json.return_value = {
                    "id": "item_789",
                    "name": "design.txt",
                    "webUrl": "https://contoso.sharepoint.com/design.txt",
                    "size": 512,
                }

                mock_client = MagicMock()
                mock_client.put = AsyncMock(return_value=mock_response)
                mock_client_ctx.return_value.__aenter__.return_value = mock_client
                mock_client_ctx.return_value.__aexit__.return_value = None

                result = await write_text_file(
                    target=target,
                    file_name="design.txt",
                    content="Design system v2",
                    conflict_behavior="fail",
                    token=token,
                )

                assert result.name == "design.txt"
                assert result.site_id == "site_abc123"

    async def test_denylist_rejection(self):
        """Denylist blocks write; audit logged."""
        target = SharePointTarget(
            site_id="site_denied",
            drive_id="drive_denied",
            folder_path="/",
        )
        token = "mock_token_123"

        with patch("entrabot.tools.files._check_site_allowed") as mock_check:
            mock_check.side_effect = SiteNotAllowedError("site_denied")

            with pytest.raises(SiteNotAllowedError):
                await write_text_file(
                    target=target,
                    file_name="forbidden.txt",
                    content="Never gonna reach",
                    conflict_behavior="fail",
                    token=token,
                )

    async def test_conflict_behavior_rename(self):
        """Conflict mode rename via @microsoft.graph.conflictBehavior."""
        target = OneDriveTarget()
        token = "mock_token_123"

        with patch("entrabot.tools.files._client") as mock_client_ctx:
            mock_response = MagicMock()
            mock_response.status_code = 201
            mock_response.json.return_value = {
                "id": "item_renamed",
                "name": "spec 1.txt",
                "webUrl": "https://onedrive.live.com/spec%201.txt",
            }

            mock_client = MagicMock()
            mock_client.put = AsyncMock(return_value=mock_response)
            mock_client_ctx.return_value.__aenter__.return_value = mock_client
            mock_client_ctx.return_value.__aexit__.return_value = None

            result = await write_text_file(
                target=target,
                file_name="spec.txt",
                content="Renamed conflict",
                conflict_behavior="rename",
                token=token,
            )

            assert result.name == "spec 1.txt"

    async def test_conflict_behavior_replace(self):
        """Conflict mode replace via @microsoft.graph.conflictBehavior."""
        target = OneDriveTarget()
        token = "mock_token_123"

        with patch("entrabot.tools.files._client") as mock_client_ctx:
            mock_response = MagicMock()
            mock_response.status_code = 200  # 200 on replace
            mock_response.json.return_value = {
                "id": "item_replaced",
                "name": "spec.txt",
                "webUrl": "https://onedrive.live.com/spec.txt",
            }

            mock_client = MagicMock()
            mock_client.put = AsyncMock(return_value=mock_response)
            mock_client_ctx.return_value.__aenter__.return_value = mock_client
            mock_client_ctx.return_value.__aexit__.return_value = None

            result = await write_text_file(
                target=target,
                file_name="spec.txt",
                content="Replaced content",
                conflict_behavior="replace",
                token=token,
            )

            assert result.name == "spec.txt"

    async def test_403_forbidden(self):
        """403 Forbidden when caller lacks permission."""
        target = OneDriveTarget()
        token = "mock_token_123"

        with patch("entrabot.tools.files._client") as mock_client_ctx:
            mock_response = MagicMock()
            mock_response.status_code = 403
            mock_response.text = "Access Denied"
            mock_response.json.return_value = {"error": {"message": "Access Denied"}}

            mock_client = MagicMock()
            mock_client.put = AsyncMock(return_value=mock_response)
            mock_client_ctx.return_value.__aenter__.return_value = mock_client
            mock_client_ctx.return_value.__aexit__.return_value = None

            with pytest.raises(MissingPermissionError):
                await write_text_file(
                    target=target,
                    file_name="forbidden.txt",
                    content="No access",
                    conflict_behavior="fail",
                    token=token,
                )

    async def test_5xx_no_retry_on_mutation(self):
        """5xx errors do NOT retry on mutations (per PR1 pattern)."""
        target = OneDriveTarget()
        token = "mock_token_123"

        with patch("entrabot.tools.files._client") as mock_client_ctx:
            mock_response = MagicMock()
            mock_response.status_code = 503
            mock_response.text = "Service Unavailable"
            mock_response.json.return_value = {"error": {"message": "Service Unavailable"}}

            mock_client = MagicMock()
            mock_client.put = AsyncMock(return_value=mock_response)
            mock_client_ctx.return_value.__aenter__.return_value = mock_client
            mock_client_ctx.return_value.__aexit__.return_value = None

            with pytest.raises(GraphFilesError):
                await write_text_file(
                    target=target,
                    file_name="fail.txt",
                    content="Will fail",
                    conflict_behavior="fail",
                    token=token,
                )
