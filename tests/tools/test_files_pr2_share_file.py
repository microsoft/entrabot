"""Tests for the inverted-gate ``share_file`` (2026-04-30 refactor).

Authorization model:
- The REQUESTER (who asked the agent to share) MUST be in the static
  Agent Identity sponsor allowlist.
- The REQUESTER MUST be a member of the Teams chat (``chat_id``) that
  initiated the request — defends against an LLM fabricating a
  sponsor email that doesn't match the active conversation.
- The RECIPIENT is unrestricted. Sponsors may share with anyone.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from entrabot.errors import (
    RequesterNotInChatError,
    RequesterNotSponsorError,
    SiteNotAllowedError,
)
from entrabot.identity.sponsors import AgentIdentitySponsor
from entrabot.tools.files import (
    FileRef,
    share_file,
)


def _file_ref(site_id: str | None = None) -> FileRef:
    return FileRef(
        drive_id="drive_123",
        item_id="item_456",
        name="spec.docx",
        mime_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        kind="sharepoint" if site_id else "onedrive_business",
        site_id=site_id,
        web_url="https://contoso.sharepoint.com/spec.docx",
        size_bytes=1024,
    )


def _sponsor(
    user_id: str = "sponsor-uid",
    upn: str = "sponsor@contoso.com",
    mail: str | None = "sponsor@contoso.com",
) -> AgentIdentitySponsor:
    return AgentIdentitySponsor(
        user_id=user_id,
        user_principal_name=upn,
        mail=mail,
    )


def _patch_graph_invite_ok(
    permission_id: str = "perm_789",
    web_url: str = "https://contoso.sharepoint.com/spec.docx",
    expiration: str | None = None,
):
    """Helper context manager — Graph /invite returns success."""
    payload = {
        "value": [
            {
                "id": permission_id,
                "roles": ["read"],
                "webUrl": web_url,
                **({"expirationDateTime": expiration} if expiration else {}),
            }
        ]
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = payload

    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    ctx = patch("entrabot.tools.files._client")
    ctx_obj = ctx.start()
    ctx_obj.return_value.__aenter__.return_value = mock_client
    ctx_obj.return_value.__aexit__.return_value = None
    return ctx, mock_client


@pytest.mark.asyncio
class TestRequesterMustBeSponsor:
    """Gate 1 — requester_email must be in the static sponsor allowlist."""

    async def test_happy_path_sponsor_in_chat_can_share_to_anyone(self):
        sponsor = _sponsor()
        with (
            patch("entrabot.tools.files._get_sponsor_records") as mock_records,
            patch("entrabot.identity.sponsors.fetch_chat_members") as mock_members,
        ):
            mock_records.return_value = [sponsor]
            mock_members.return_value = [
                {"user_id": "sponsor-uid", "email": "sponsor@contoso.com"}
            ]
            ctx, _ = _patch_graph_invite_ok()
            try:
                result = await share_file(
                    file_ref=_file_ref(),
                    # Recipient is a non-sponsor stranger — that's fine.
                    recipient_email="stranger@example.com",
                    requester_email="sponsor@contoso.com",
                    chat_id="19:abcd@thread.v2",
                    role="read",
                    token="t",
                )
            finally:
                ctx.stop()

        assert result.permission_id == "perm_789"
        assert result.recipient_email == "stranger@example.com"

    async def test_non_sponsor_requester_rejected(self):
        sponsor = _sponsor()
        with patch("entrabot.tools.files._get_sponsor_records") as mock_records:
            mock_records.return_value = [sponsor]
            with pytest.raises(RequesterNotSponsorError) as exc_info:
                await share_file(
                    file_ref=_file_ref(),
                    recipient_email="stranger@example.com",
                    requester_email="impostor@example.com",
                    chat_id="19:abcd@thread.v2",
                    role="read",
                    token="t",
                )

        msg = str(exc_info.value)
        assert "impostor@example.com" in msg
        # Must NOT enumerate alternatives — that gives the LLM a menu (Learning #59).
        assert "sponsor@contoso.com" not in msg
        assert "Stop and ask the user" in msg

    async def test_sponsor_matched_via_decoded_b2b_ext_upn(self):
        """Requester types home address; sponsor record carries only the EXT UPN."""
        msa_sponsor = AgentIdentitySponsor(
            user_id="msa-uid",
            user_principal_name="alice_example.com#EXT#@fabrikam.onmicrosoft.com",
            mail=None,
        )
        with (
            patch("entrabot.tools.files._get_sponsor_records") as mock_records,
            patch("entrabot.identity.sponsors.fetch_chat_members") as mock_members,
        ):
            mock_records.return_value = [msa_sponsor]
            mock_members.return_value = [
                {"user_id": "msa-uid", "email": "alice@example.com"}
            ]
            ctx, _ = _patch_graph_invite_ok()
            try:
                # User typed the home address — the EXT-UPN decoder must
                # translate the sponsor record's UPN into this form.
                result = await share_file(
                    file_ref=_file_ref(),
                    recipient_email="anyone@example.com",
                    requester_email="alice@example.com",
                    chat_id="19:abcd@thread.v2",
                    role="read",
                    token="t",
                )
            finally:
                ctx.stop()

        assert result.permission_id == "perm_789"


@pytest.mark.asyncio
class TestRequesterMustBeChatMember:
    """Gate 2 — sponsor must be a MEMBER of the cited chat."""

    async def test_sponsor_not_in_chat_rejected(self):
        sponsor = _sponsor()
        with (
            patch("entrabot.tools.files._get_sponsor_records") as mock_records,
            patch("entrabot.identity.sponsors.fetch_chat_members") as mock_members,
        ):
            mock_records.return_value = [sponsor]
            # Chat exists but the sponsor isn't a member — the LLM may
            # have fabricated either the email or the chat_id.
            mock_members.return_value = [
                {"user_id": "different-user", "email": "stranger@example.com"}
            ]

            with pytest.raises(RequesterNotInChatError) as exc_info:
                await share_file(
                    file_ref=_file_ref(),
                    recipient_email="anyone@example.com",
                    requester_email="sponsor@contoso.com",
                    chat_id="19:wrong-chat@thread.v2",
                    role="read",
                    token="t",
                )

        msg = str(exc_info.value)
        assert "sponsor@contoso.com" in msg
        assert "19:wrong-chat@thread.v2" in msg

    async def test_empty_chat_members_rejected(self):
        """Graph returned 403/404 for the chat — no membership = no auth."""
        sponsor = _sponsor()
        with (
            patch("entrabot.tools.files._get_sponsor_records") as mock_records,
            patch("entrabot.identity.sponsors.fetch_chat_members") as mock_members,
        ):
            mock_records.return_value = [sponsor]
            mock_members.return_value = []

            with pytest.raises(RequesterNotInChatError):
                await share_file(
                    file_ref=_file_ref(),
                    recipient_email="anyone@example.com",
                    requester_email="sponsor@contoso.com",
                    chat_id="19:fake-chat@thread.v2",
                    role="read",
                    token="t",
                )


@pytest.mark.asyncio
class TestRequiredArguments:
    """Required-arg defaults + missing-arg rejection."""

    async def test_missing_requester_email_raises_value_error(self):
        with pytest.raises(ValueError, match="requester_email"):
            await share_file(
                file_ref=_file_ref(),
                recipient_email="anyone@example.com",
                requester_email="",
                chat_id="19:abcd@thread.v2",
                role="read",
                token="t",
            )

    async def test_missing_chat_id_raises_value_error(self):
        with pytest.raises(ValueError, match="chat_id"):
            await share_file(
                file_ref=_file_ref(),
                recipient_email="anyone@example.com",
                requester_email="sponsor@contoso.com",
                chat_id="",
                role="read",
                token="t",
            )

    async def test_missing_token_raises_value_error(self):
        with pytest.raises(ValueError, match="token"):
            await share_file(
                file_ref=_file_ref(),
                recipient_email="anyone@example.com",
                requester_email="sponsor@contoso.com",
                chat_id="19:abcd@thread.v2",
                role="read",
                token=None,
            )


@pytest.mark.asyncio
class TestRecipientUnrestricted:
    """Recipient can be ANY address — no sponsor check, no chat-member check."""

    async def test_share_with_external_recipient_succeeds(self):
        sponsor = _sponsor()
        with (
            patch("entrabot.tools.files._get_sponsor_records") as mock_records,
            patch("entrabot.identity.sponsors.fetch_chat_members") as mock_members,
        ):
            mock_records.return_value = [sponsor]
            mock_members.return_value = [
                {"user_id": "sponsor-uid", "email": "sponsor@contoso.com"}
            ]
            ctx, mock_client = _patch_graph_invite_ok()
            try:
                result = await share_file(
                    file_ref=_file_ref(),
                    recipient_email="external@gmail.com",
                    requester_email="sponsor@contoso.com",
                    chat_id="19:abcd@thread.v2",
                    role="read",
                    token="t",
                )
            finally:
                ctx.stop()

        # Recipient passed to Graph unchanged.
        post_kwargs = mock_client.post.call_args.kwargs
        assert post_kwargs["json"]["recipients"] == [{"email": "external@gmail.com"}]
        assert result.recipient_email == "external@gmail.com"


@pytest.mark.asyncio
class TestRoleAndDenylist:
    """Existing semantics that survive the gate inversion."""

    async def test_role_write_passed_through(self):
        sponsor = _sponsor()
        with (
            patch("entrabot.tools.files._get_sponsor_records") as mock_records,
            patch("entrabot.identity.sponsors.fetch_chat_members") as mock_members,
        ):
            mock_records.return_value = [sponsor]
            mock_members.return_value = [
                {"user_id": "sponsor-uid", "email": "sponsor@contoso.com"}
            ]
            ctx, mock_client = _patch_graph_invite_ok()
            try:
                result = await share_file(
                    file_ref=_file_ref(),
                    recipient_email="anyone@example.com",
                    requester_email="sponsor@contoso.com",
                    chat_id="19:abcd@thread.v2",
                    role="write",
                    token="t",
                )
            finally:
                ctx.stop()

        post_kwargs = mock_client.post.call_args.kwargs
        assert post_kwargs["json"]["roles"] == ["write"]
        assert result.role == "write"

    async def test_invite_payload_sends_invitation(self):
        """sendInvitation=True is required for cross-MySite shares.

        Without it Graph creates the permission record but doesn't add
        the recipient to the target SharePoint site's user list, so
        opening the doc returns a 500 "SharePoint Foundation" server
        error and no email lands in the recipient's inbox. Verified
        live 2026-05-04. Pin the payload so this never silently
        regresses.
        """
        sponsor = _sponsor()
        with (
            patch("entrabot.tools.files._get_sponsor_records") as mock_records,
            patch("entrabot.identity.sponsors.fetch_chat_members") as mock_members,
        ):
            mock_records.return_value = [sponsor]
            mock_members.return_value = [
                {"user_id": "sponsor-uid", "email": "sponsor@contoso.com"}
            ]
            ctx, mock_client = _patch_graph_invite_ok()
            try:
                await share_file(
                    file_ref=_file_ref(),
                    recipient_email="user@contoso.com",
                    requester_email="sponsor@contoso.com",
                    chat_id="19:abcd@thread.v2",
                    role="write",
                    token="t",
                )
            finally:
                ctx.stop()

        post_kwargs = mock_client.post.call_args.kwargs
        assert post_kwargs["json"]["sendInvitation"] is True
        assert post_kwargs["json"]["requireSignIn"] is True

    async def test_sharepoint_denylist_rejection_runs_first(self):
        """Site denylist still rejects before any sponsor lookup."""
        denied_site_id = "denied-site-id"
        with (
            patch("entrabot.tools.files._check_site_allowed") as mock_deny,
            patch("entrabot.tools.files._get_sponsor_records") as mock_records,
        ):
            mock_deny.side_effect = SiteNotAllowedError(site_id=denied_site_id)
            mock_records.return_value = []

            with pytest.raises(SiteNotAllowedError):
                await share_file(
                    file_ref=_file_ref(site_id=denied_site_id),
                    recipient_email="anyone@example.com",
                    requester_email="sponsor@contoso.com",
                    chat_id="19:abcd@thread.v2",
                    role="read",
                    token="t",
                )

            # Sponsor records should NEVER have been fetched — denylist is first.
            mock_records.assert_not_called()

    async def test_permission_metadata_includes_web_url_and_expiration(self):
        sponsor = _sponsor()
        with (
            patch("entrabot.tools.files._get_sponsor_records") as mock_records,
            patch("entrabot.identity.sponsors.fetch_chat_members") as mock_members,
        ):
            mock_records.return_value = [sponsor]
            mock_members.return_value = [
                {"user_id": "sponsor-uid", "email": "sponsor@contoso.com"}
            ]
            ctx, _ = _patch_graph_invite_ok(
                permission_id="perm_metadata",
                expiration="2099-12-31T23:59:59Z",
            )
            try:
                result = await share_file(
                    file_ref=_file_ref(),
                    recipient_email="anyone@example.com",
                    requester_email="sponsor@contoso.com",
                    chat_id="19:abcd@thread.v2",
                    role="read",
                    token="t",
                )
            finally:
                ctx.stop()

        assert result.permission_id == "perm_metadata"
        assert result.web_url == "https://contoso.sharepoint.com/spec.docx"
        assert result.expiration_at == "2099-12-31T23:59:59Z"


@pytest.mark.asyncio
class TestGetSponsorAllowlistCompat:
    """``_get_sponsor_allowlist`` is retained as a back-compat shim."""

    async def test_aggregates_all_email_identifiers(self):
        from entrabot.tools.files import _get_sponsor_allowlist

        sponsor = AgentIdentitySponsor(
            user_id="u1",
            user_principal_name="alice@contoso.com",
            mail="alice.mail@contoso.com",
            other_mails=("alice.alt@contoso.com",),
            proxy_addresses=("alice.proxy@contoso.com",),
            federated_emails=("alice@outlook.com",),
        )

        with (
            patch("entrabot.config.get_config") as mock_get_config,
            patch("entrabot.identity.sponsors.fetch_agent_identity_sponsors") as mock_fetch,
        ):
            mock_get_config.return_value = MagicMock()
            mock_fetch.return_value = [sponsor]

            allowlist = await _get_sponsor_allowlist()

            from entrabot.tools.teams import acquire_agent_user_token

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


@pytest.mark.asyncio
class TestShareFileAuditOnGateFailure:
    """share_file must emit audit events for gate failures, not only Graph failures.

    Pre-fix the audit context wrapped only the Graph /invite call, so
    Gate-1/Gate-2 rejections produced no audit log. This is a security
    visibility hole — the move to audit-first ordering closes it.
    """

    async def test_non_sponsor_failure_produces_audit_event(self, monkeypatch):
        from entrabot.tools.files import FileRef, share_file

        events: list[dict] = []

        def fake_log_event(*, action, resource, outcome, metadata):
            events.append(
                {
                    "action": action,
                    "resource": resource,
                    "outcome": outcome,
                    "metadata": dict(metadata),
                }
            )

        monkeypatch.setattr("entrabot.tools.files.log_event", fake_log_event)

        async def _empty_sponsors():
            return []

        monkeypatch.setattr(
            "entrabot.tools.files._get_sponsor_records", _empty_sponsors
        )

        ref = FileRef(
            drive_id="d-1",
            item_id="i-1",
            name="x.txt",
            mime_type="text/plain",
            kind="onedrive_business",
        )

        with pytest.raises(RequesterNotSponsorError):
            await share_file(
                file_ref=ref,
                recipient_email="bob@example.com",
                requester_email="non-sponsor@evil.com",
                chat_id="19:somechat@thread.v2",
                role="read",
                token="t",
            )

        failure_events = [e for e in events if e["outcome"] == "failure"]
        assert failure_events, (
            "share_file gate failure must emit at least one audit event "
            "with outcome=failure"
        )
        last = failure_events[-1]
        assert "RequesterNotSponsorError" in str(last["metadata"].get("error", ""))
        # And it must be tagged as a share_file action (not some unrelated audit).
        assert last["action"].endswith("share_file")
