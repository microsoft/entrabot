"""Tests for the EntraClaw error hierarchy."""

import pytest

from entraclaw.errors import (
    AgentIDNotAvailable,
    AuthError,
    ChatNotFound,
    EntraClawError,
    MessageTooLong,
    RateLimitError,
    TeamsError,
    TeamsNotLicensed,
    TokenExchangeError,
    TokenExpiredError,
)


class TestErrorHierarchy:
    """Verify that error classes inherit from the expected bases."""

    def test_auth_errors_inherit_entraclaw(self) -> None:
        assert issubclass(AuthError, EntraClawError)

    def test_token_exchange_error_inherits_auth(self) -> None:
        assert issubclass(TokenExchangeError, AuthError)

    def test_agent_id_not_available_inherits_auth(self) -> None:
        assert issubclass(AgentIDNotAvailable, AuthError)

    def test_token_expired_inherits_auth(self) -> None:
        assert issubclass(TokenExpiredError, AuthError)

    def test_teams_errors_inherit_entraclaw(self) -> None:
        assert issubclass(TeamsError, EntraClawError)

    def test_teams_not_licensed_inherits_teams(self) -> None:
        assert issubclass(TeamsNotLicensed, TeamsError)

    def test_chat_not_found_inherits_teams(self) -> None:
        assert issubclass(ChatNotFound, TeamsError)

    def test_message_too_long_inherits_teams(self) -> None:
        assert issubclass(MessageTooLong, TeamsError)

    def test_rate_limit_inherits_entraclaw(self) -> None:
        assert issubclass(RateLimitError, EntraClawError)


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

    def test_catch_all_entraclaw_errors(self) -> None:
        """All custom errors can be caught with ``except EntraClawError``."""
        errors = [
            TokenExchangeError("hop1", "e", "d"),
            AgentIDNotAvailable("a"),
            TokenExpiredError("t"),
            TeamsNotLicensed("l"),
            ChatNotFound("c"),
            MessageTooLong("m"),
            RateLimitError(10),
        ]
        for err in errors:
            with pytest.raises(EntraClawError):
                raise err
