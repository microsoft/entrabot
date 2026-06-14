"""Tests for the EntraBot error hierarchy."""

import pytest

from entrabot.errors import (
    AgentIDNotAvailable,
    AuthCancelledError,
    AuthError,
    AuthTimeoutError,
    ChatNotFound,
    EntraBotError,
    GraphApiError,
    InsecureKeyringBackendError,
    InvalidTransitionError,
    MessageTooLong,
    MsalAuthError,
    ProvisioningError,
    RateLimitError,
    TeamsError,
    TeamsNotLicensed,
    TokenExchangeError,
    TokenExpiredError,
    TransitionError,
    TransitionTimeoutError,
)


class TestErrorHierarchy:
    """Verify that error classes inherit from the expected bases."""

    def test_auth_errors_inherit_entrabot(self) -> None:
        assert issubclass(AuthError, EntraBotError)

    def test_token_exchange_error_inherits_auth(self) -> None:
        assert issubclass(TokenExchangeError, AuthError)

    def test_agent_id_not_available_inherits_auth(self) -> None:
        assert issubclass(AgentIDNotAvailable, AuthError)

    def test_token_expired_inherits_auth(self) -> None:
        assert issubclass(TokenExpiredError, AuthError)

    def test_teams_errors_inherit_entrabot(self) -> None:
        assert issubclass(TeamsError, EntraBotError)

    def test_teams_not_licensed_inherits_teams(self) -> None:
        assert issubclass(TeamsNotLicensed, TeamsError)

    def test_chat_not_found_inherits_teams(self) -> None:
        assert issubclass(ChatNotFound, TeamsError)

    def test_message_too_long_inherits_teams(self) -> None:
        assert issubclass(MessageTooLong, TeamsError)

    def test_rate_limit_inherits_entrabot(self) -> None:
        assert issubclass(RateLimitError, EntraBotError)

    # --- New Lane B error hierarchy tests ---

    def test_auth_timeout_inherits_auth(self) -> None:
        assert issubclass(AuthTimeoutError, AuthError)

    def test_auth_cancelled_inherits_auth(self) -> None:
        assert issubclass(AuthCancelledError, AuthError)

    def test_msal_auth_error_inherits_auth(self) -> None:
        assert issubclass(MsalAuthError, AuthError)

    def test_invalid_transition_inherits_entrabot(self) -> None:
        assert issubclass(InvalidTransitionError, EntraBotError)

    def test_transition_timeout_inherits_entrabot(self) -> None:
        assert issubclass(TransitionTimeoutError, EntraBotError)

    def test_transition_error_inherits_entrabot(self) -> None:
        assert issubclass(TransitionError, EntraBotError)

    def test_provisioning_error_inherits_entrabot(self) -> None:
        assert issubclass(ProvisioningError, EntraBotError)

    def test_graph_api_error_inherits_teams(self) -> None:
        assert issubclass(GraphApiError, TeamsError)

    def test_insecure_keyring_backend_inherits_entrabot(self) -> None:
        assert issubclass(InsecureKeyringBackendError, EntraBotError)


class TestErrorMessages:
    def test_token_exchange_error_message(self) -> None:
        err = TokenExchangeError("hop1:blueprint", "invalid_client", "Bad secret")
        assert "hop1:blueprint" in str(err)
        assert "invalid_client" in str(err)
        assert err.hop == "hop1:blueprint"
        assert err.error == "invalid_client"
        assert err.description == "Bad secret"

    def test_rate_limit_retry_after(self) -> None:
        err = RateLimitError(30)
        assert err.retry_after == 30
        assert "30" in str(err)

    def test_rate_limit_default_retry(self) -> None:
        err = RateLimitError()
        assert err.retry_after == 60

    def test_catch_all_entrabot_errors(self) -> None:
        """All custom errors can be caught with ``except EntraBotError``."""
        errors = [
            TokenExchangeError("hop1", "e", "d"),
            AgentIDNotAvailable("a"),
            TokenExpiredError("t"),
            TeamsNotLicensed("l"),
            ChatNotFound("c"),
            MessageTooLong("m"),
            RateLimitError(10),
            AuthTimeoutError("timeout"),
            AuthCancelledError("cancelled"),
            MsalAuthError(error="invalid_grant", error_description="Bad token"),
            InvalidTransitionError(from_state="a", to_state="b"),
            TransitionTimeoutError("timed out"),
            TransitionError(from_state="a", to_state="b", cause=ValueError("x")),
            ProvisioningError(),
            GraphApiError(status_code=403, message="Forbidden"),
            InsecureKeyringBackendError("keyrings.alt.file.PlaintextKeyring"),
        ]
        for err in errors:
            with pytest.raises(EntraBotError):
                raise err


class TestMsalAuthError:
    """MsalAuthError stores error and error_description."""

    def test_stores_fields(self) -> None:
        err = MsalAuthError(error="invalid_grant", error_description="Token expired")
        assert err.error == "invalid_grant"
        assert err.error_description == "Token expired"

    def test_message_includes_fields(self) -> None:
        err = MsalAuthError(error="invalid_grant", error_description="Bad token")
        assert "invalid_grant" in str(err)
        assert "Bad token" in str(err)


class TestInvalidTransitionError:
    """InvalidTransitionError stores from_state and to_state."""

    def test_stores_fields(self) -> None:
        err = InvalidTransitionError(from_state="delegated", to_state="agent_user")
        assert err.from_state == "delegated"
        assert err.to_state == "agent_user"

    def test_message_includes_states(self) -> None:
        err = InvalidTransitionError(from_state="delegated", to_state="agent_user")
        assert "delegated" in str(err)
        assert "agent_user" in str(err)


class TestTransitionError:
    """TransitionError stores from_state, to_state, and cause."""

    def test_stores_fields(self) -> None:
        cause = RuntimeError("something broke")
        err = TransitionError(from_state="unauthenticated", to_state="delegated", cause=cause)
        assert err.from_state == "unauthenticated"
        assert err.to_state == "delegated"
        assert err.cause is cause

    def test_message_includes_cause(self) -> None:
        cause = RuntimeError("kaboom")
        err = TransitionError(from_state="a", to_state="b", cause=cause)
        assert "kaboom" in str(err)


class TestProvisioningError:
    """ProvisioningError stores optional detail."""

    def test_with_detail(self) -> None:
        err = ProvisioningError(detail="FIC creation failed")
        assert err.detail == "FIC creation failed"
        assert "FIC creation failed" in str(err)

    def test_without_detail(self) -> None:
        err = ProvisioningError()
        assert err.detail is None
        assert "Provisioning failed" in str(err)


class TestGraphApiError:
    """GraphApiError stores status_code and message."""

    def test_stores_fields(self) -> None:
        err = GraphApiError(status_code=403, message="Forbidden")
        assert err.status_code == 403
        assert err.message == "Forbidden"

    def test_message_includes_fields(self) -> None:
        err = GraphApiError(status_code=404, message="Not Found")
        assert "404" in str(err)
        assert "Not Found" in str(err)


class TestNoActiveSponsorChannelError:
    """authorization fix: sponsor has no live binding for any chat."""

    def test_subclasses_files_error(self) -> None:
        from entrabot.errors import FilesError, NoActiveSponsorChannelError

        assert issubclass(NoActiveSponsorChannelError, FilesError)

    def test_message_includes_sponsor_and_chat(self) -> None:
        from entrabot.errors import NoActiveSponsorChannelError

        err = NoActiveSponsorChannelError(sponsor_user_id="u-1", chat_id="c-1")
        assert "u-1" in str(err)
        assert "c-1" in str(err)

    def test_stores_fields(self) -> None:
        from entrabot.errors import NoActiveSponsorChannelError

        err = NoActiveSponsorChannelError(sponsor_user_id="u-1", chat_id="c-1")
        assert err.sponsor_user_id == "u-1"
        assert err.chat_id == "c-1"


class TestExpiredSponsorChannelError:
    """authorization fix: sponsor binding exists but is past TTL."""

    def test_subclasses_files_error(self) -> None:
        from entrabot.errors import ExpiredSponsorChannelError, FilesError

        assert issubclass(ExpiredSponsorChannelError, FilesError)

    def test_message_includes_age_and_ttl(self) -> None:
        from entrabot.errors import ExpiredSponsorChannelError

        err = ExpiredSponsorChannelError(
            sponsor_user_id="u-1",
            chat_id="c-1",
            age_seconds=300,
            ttl_seconds=120,
        )
        s = str(err)
        assert "300" in s
        assert "120" in s

    def test_stores_fields(self) -> None:
        from entrabot.errors import ExpiredSponsorChannelError

        err = ExpiredSponsorChannelError(
            sponsor_user_id="u-1",
            chat_id="c-1",
            age_seconds=300,
            ttl_seconds=120,
        )
        assert err.age_seconds == 300
        assert err.ttl_seconds == 120


class TestSponsorChannelMismatchError:
    """authorization fix: LLM-supplied chat_id does not match sponsor's bound channel."""

    def test_subclasses_files_error(self) -> None:
        from entrabot.errors import FilesError, SponsorChannelMismatchError

        assert issubclass(SponsorChannelMismatchError, FilesError)

    def test_message_includes_both_chat_ids(self) -> None:
        from entrabot.errors import SponsorChannelMismatchError

        err = SponsorChannelMismatchError(
            sponsor_user_id="u-1",
            supplied_chat_id="c-supplied",
            bound_chat_id="c-bound",
        )
        s = str(err)
        assert "c-supplied" in s
        assert "c-bound" in s

    def test_stores_fields(self) -> None:
        from entrabot.errors import SponsorChannelMismatchError

        err = SponsorChannelMismatchError(
            sponsor_user_id="u-1",
            supplied_chat_id="c-supplied",
            bound_chat_id="c-bound",
        )
        assert err.supplied_chat_id == "c-supplied"
        assert err.bound_chat_id == "c-bound"
