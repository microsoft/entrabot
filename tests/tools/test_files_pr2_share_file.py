"""Tests for share_file tool (PR2).

TDD: all tests written before implementation.
Tests cover:
  - Happy path: recipient in sponsor allowlist (table test UPN/mail/otherMails/proxyAddresses)
  - Non-sponsor rejection with NotASponsorError
  - Role variations (read, write)
  - Denylist rejection (site)
  - Federated identity detection
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from entraclaw.errors import (
    NotASponsorError,
    SiteNotAllowedError,
)
from entraclaw.tools.files import (
    FileRef,
    share_file,
)


@pytest.mark.asyncio
class TestShareFile:
    """Tests for share_file mutation tool."""

    def _make_file_ref(self, site_id=None):
        """Helper to create test FileRef."""
        return FileRef(
            drive_id="drive_123",
            item_id="item_456",
            name="spec.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            kind="sharepoint" if site_id else "onedrive_business",
            site_id=site_id,
            web_url="https://contoso.sharepoint.com/spec.docx",
            size_bytes=1024,
        )

    async def test_share_with_sponsor_by_upn(self):
        """Share succeeds when recipient is sponsor (matched by UPN)."""
        file_ref = self._make_file_ref()
        token = "mock_token_123"

        with patch("entraclaw.tools.files._get_sponsor_allowlist") as mock_sponsors:
            mock_sponsors.return_value = {
                "user@contoso.com",
                "user@other.com",
            }

            with patch("entraclaw.tools.files._client") as mock_client_ctx:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "value": [
                        {
                            "id": "perm_789",
                            "roles": ["read"],
                            "grantedTo": {"user": {"email": "user@contoso.com"}},
                            "webUrl": "https://contoso.sharepoint.com/spec.docx",
                        }
                    ]
                }

                mock_client = MagicMock()
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_ctx.return_value.__aenter__.return_value = mock_client
                mock_client_ctx.return_value.__aexit__.return_value = None

                result = await share_file(
                    file_ref=file_ref,
                    recipient_email="user@contoso.com",
                    role="read",
                    token=token,
                )

                assert result.permission_id == "perm_789"
                assert result.role == "read"
                assert result.recipient_email == "user@contoso.com"

    async def test_share_with_sponsor_by_mail(self):
        """Share succeeds when recipient is sponsor (matched by mail attribute)."""
        file_ref = self._make_file_ref()
        token = "mock_token_123"

        with patch("entraclaw.tools.files._get_sponsor_allowlist") as mock_sponsors:
            # Mock returns set of canonical addresses
            mock_sponsors.return_value = {
                "user@contoso.com",
                "alt@company.com",
            }

            with patch("entraclaw.tools.files._client") as mock_client_ctx:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "value": [
                        {
                            "id": "perm_abc",
                            "roles": ["write"],
                            "grantedTo": {"user": {"email": "alt@company.com"}},
                        }
                    ]
                }

                mock_client = MagicMock()
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_ctx.return_value.__aenter__.return_value = mock_client
                mock_client_ctx.return_value.__aexit__.return_value = None

                result = await share_file(
                    file_ref=file_ref,
                    recipient_email="alt@company.com",
                    role="write",
                    token=token,
                )

                assert result.permission_id == "perm_abc"
                assert result.role == "write"

    async def test_non_sponsor_rejection(self):
        """Non-sponsor recipient raises NotASponsorError."""
        file_ref = self._make_file_ref()
        token = "mock_token_123"

        with patch("entraclaw.tools.files._get_sponsor_allowlist") as mock_sponsors:
            mock_sponsors.return_value = {
                "allowed@contoso.com",
            }

            with pytest.raises(NotASponsorError) as exc_info:
                await share_file(
                    file_ref=file_ref,
                    recipient_email="unauthorized@gmail.com",
                    role="read",
                    token=token,
                )

            assert "unauthorized@gmail.com" in str(exc_info.value)

    async def test_role_write(self):
        """Role write is passed through to Graph."""
        file_ref = self._make_file_ref()
        token = "mock_token_123"

        with patch("entraclaw.tools.files._get_sponsor_allowlist") as mock_sponsors:
            mock_sponsors.return_value = {
                "user@contoso.com",
            }

            with patch("entraclaw.tools.files._client") as mock_client_ctx:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "value": [
                        {
                            "id": "perm_write",
                            "roles": ["write"],
                            "grantedTo": {"user": {"email": "user@contoso.com"}},
                        }
                    ]
                }

                mock_client = MagicMock()
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_ctx.return_value.__aenter__.return_value = mock_client
                mock_client_ctx.return_value.__aexit__.return_value = None

                result = await share_file(
                    file_ref=file_ref,
                    recipient_email="user@contoso.com",
                    role="write",
                    token=token,
                )

                assert result.role == "write"

    async def test_sharepoint_denylist_rejection(self):
        """SharePoint file on denylist cannot be shared."""
        file_ref = self._make_file_ref(site_id="site_denied")
        token = "mock_token_123"

        with patch("entraclaw.tools.files._check_site_allowed") as mock_check:
            mock_check.side_effect = SiteNotAllowedError("site_denied")

            with pytest.raises(SiteNotAllowedError):
                await share_file(
                    file_ref=file_ref,
                    recipient_email="user@contoso.com",
                    role="read",
                    token=token,
                )

    async def test_permission_metadata(self):
        """SharePermission includes web_url and expiration_at when available."""
        file_ref = self._make_file_ref()
        token = "mock_token_123"

        with patch("entraclaw.tools.files._get_sponsor_allowlist") as mock_sponsors:
            mock_sponsors.return_value = {
                "user@contoso.com",
            }

            with patch("entraclaw.tools.files._client") as mock_client_ctx:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "value": [
                        {
                            "id": "perm_metadata",
                            "roles": ["read"],
                            "grantedTo": {"user": {"email": "user@contoso.com"}},
                            "webUrl": "https://contoso.sharepoint.com/spec.docx",
                            "expirationDateTime": "2099-12-31T23:59:59Z",
                        }
                    ]
                }

                mock_client = MagicMock()
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_ctx.return_value.__aenter__.return_value = mock_client
                mock_client_ctx.return_value.__aexit__.return_value = None

                result = await share_file(
                    file_ref=file_ref,
                    recipient_email="user@contoso.com",
                    role="read",
                    token=token,
                )

                assert result.permission_id == "perm_metadata"
                assert result.web_url == "https://contoso.sharepoint.com/spec.docx"
                assert result.expiration_at == "2099-12-31T23:59:59Z"


@pytest.mark.asyncio
class TestGetSponsorAllowlist:
    """Regression tests for _get_sponsor_allowlist itself.

    The sibling TestShareFile class mocks _get_sponsor_allowlist whole,
    so it cannot catch signature drift between the wrapper and the
    underlying fetch_agent_identity_sponsors. These tests exercise the
    real wrapper with only the sync inner call mocked.
    """

    async def test_aggregates_all_email_identifiers(self):
        from entraclaw.identity.sponsors import AgentIdentitySponsor
        from entraclaw.tools.files import _get_sponsor_allowlist

        sponsor = AgentIdentitySponsor(
            user_id="u1",
            user_principal_name="alice@contoso.com",
            mail="alice.mail@contoso.com",
            other_mails=("alice.alt@contoso.com",),
            proxy_addresses=("alice.proxy@contoso.com",),
            federated_emails=("alice@outlook.com",),
        )

        with (
            patch("entraclaw.config.get_config") as mock_get_config,
            patch("entraclaw.identity.sponsors.fetch_agent_identity_sponsors") as mock_fetch,
        ):
            mock_get_config.return_value = MagicMock()
            mock_fetch.return_value = [sponsor]

            allowlist = await _get_sponsor_allowlist()

            # _get_sponsor_allowlist must route the /users/{id} enrichment
            # hop through the Agent User token (the Agent Identity FIC
            # token lacks User.Read.All) — see Learning #55, 2026-04-30
            # sponsor-allowlist-empty bug.
            from entraclaw.tools.teams import acquire_agent_user_token

            call_args, call_kwargs = mock_fetch.call_args
            assert call_args == (mock_get_config.return_value,)
            assert call_kwargs == {"user_token_provider": acquire_agent_user_token}
            assert allowlist == {
                "alice@contoso.com",
                "alice.mail@contoso.com",
                "alice.alt@contoso.com",
                "alice.proxy@contoso.com",
                "alice@outlook.com",
            }
