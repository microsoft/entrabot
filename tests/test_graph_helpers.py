"""Tests for entrabot.graph_helpers — shared Graph API utilities.

TDD: these tests were written before the implementation.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _resp(status: int, body: dict | None = None, *, text: str = "", headers: dict | None = None):
    """Build a minimal object that quacks like requests.Response."""
    ns = SimpleNamespace(
        status_code=status,
        text=text or str(body or {}),
        headers=headers or {},
    )
    ns.json = lambda: body if body is not None else {}
    return ns


# ---------------------------------------------------------------------------
# odata_escape
# ---------------------------------------------------------------------------


class TestOdataEscape:
    def test_no_quotes(self):
        from entrabot.graph_helpers import odata_escape

        assert odata_escape("hello") == "hello"

    def test_single_quotes_doubled(self):
        from entrabot.graph_helpers import odata_escape

        assert odata_escape("it's a test") == "it''s a test"

    def test_multiple_quotes(self):
        from entrabot.graph_helpers import odata_escape

        assert odata_escape("a'b'c") == "a''b''c"

    def test_empty_string(self):
        from entrabot.graph_helpers import odata_escape

        assert odata_escape("") == ""


# ---------------------------------------------------------------------------
# graph_request
# ---------------------------------------------------------------------------


class TestGraphRequest:
    def test_basic_get(self):
        from entrabot.graph_helpers import graph_request

        mock_resp = _resp(200, {"value": []})
        with patch("entrabot.graph_helpers.requests") as mock_requests:
            mock_requests.request.return_value = mock_resp
            result = graph_request("GET", "/users", "fake-token")

        assert result.status_code == 200
        mock_requests.request.assert_called_once()
        args, kwargs = mock_requests.request.call_args
        assert args == ("GET", "https://graph.microsoft.com/beta/users")
        assert kwargs["headers"]["Authorization"] == "Bearer fake-token"

    def test_post_with_json(self):
        from entrabot.graph_helpers import graph_request

        mock_resp = _resp(201, {"id": "123"})
        body = {"displayName": "Test"}
        with patch("entrabot.graph_helpers.requests") as mock_requests:
            mock_requests.request.return_value = mock_resp
            result = graph_request("POST", "/applications", "tok", json_body=body)

        assert result.status_code == 201
        _, kwargs = mock_requests.request.call_args
        assert kwargs["json"] == body

    def test_retry_on_429(self):
        from entrabot.graph_helpers import graph_request

        throttled = _resp(429, headers={"Retry-After": "1"})
        ok = _resp(200, {"value": []})
        with (
            patch("entrabot.graph_helpers.requests") as mock_requests,
            patch("entrabot.graph_helpers.time") as mock_time,
        ):
            mock_requests.request.side_effect = [throttled, ok]
            result = graph_request("GET", "/users", "tok")

        assert result.status_code == 200
        assert mock_requests.request.call_count == 2
        mock_time.sleep.assert_called_once_with(1)

    def test_retry_on_503(self):
        from entrabot.graph_helpers import graph_request

        error = _resp(503, headers={})
        ok = _resp(200, {"value": []})
        with (
            patch("entrabot.graph_helpers.requests") as mock_requests,
            patch("entrabot.graph_helpers.time") as mock_time,
        ):
            mock_requests.request.side_effect = [error, ok]
            result = graph_request("GET", "/me", "tok")

        assert result.status_code == 200
        mock_time.sleep.assert_called_once_with(10)  # default retry-after

    def test_no_retry_when_disabled(self):
        from entrabot.graph_helpers import graph_request

        error = _resp(503)
        with patch("entrabot.graph_helpers.requests") as mock_requests:
            mock_requests.request.return_value = error
            result = graph_request("GET", "/me", "tok", retry=False)

        assert result.status_code == 503
        assert mock_requests.request.call_count == 1

    def test_custom_base_url(self):
        from entrabot.graph_helpers import graph_request

        mock_resp = _resp(200, {})
        with patch("entrabot.graph_helpers.requests") as mock_requests:
            mock_requests.request.return_value = mock_resp
            graph_request(
                "GET",
                "/oauth2PermissionGrants",
                "tok",
                base_url="https://graph.microsoft.com/v1.0",
            )

        args, _ = mock_requests.request.call_args
        assert args[1] == "https://graph.microsoft.com/v1.0/oauth2PermissionGrants"

    def test_timeout_passed(self):
        from entrabot.graph_helpers import graph_request

        mock_resp = _resp(200, {})
        with patch("entrabot.graph_helpers.requests") as mock_requests:
            mock_requests.request.return_value = mock_resp
            graph_request("GET", "/me", "tok", timeout=60)

        _, kwargs = mock_requests.request.call_args
        assert kwargs["timeout"] == 60


# ---------------------------------------------------------------------------
# graph_collection_values
# ---------------------------------------------------------------------------


class TestGraphCollectionValues:
    def test_single_page(self):
        from entrabot.graph_helpers import graph_collection_values

        page = _resp(200, {"value": [{"id": "a"}, {"id": "b"}]})
        with patch("entrabot.graph_helpers.requests") as mock_requests:
            mock_requests.request.return_value = page
            result = graph_collection_values("/users", "tok")

        assert result == [{"id": "a"}, {"id": "b"}]

    def test_paginated(self):
        from entrabot.graph_helpers import graph_collection_values

        page1 = _resp(
            200,
            {
                "value": [{"id": "a"}],
                "@odata.nextLink": "https://graph.microsoft.com/beta/users?$skip=1",
            },
        )
        page2 = _resp(200, {"value": [{"id": "b"}]})
        with patch("entrabot.graph_helpers.requests") as mock_requests:
            mock_requests.request.side_effect = [page1, page2]
            result = graph_collection_values("/users", "tok")

        assert result == [{"id": "a"}, {"id": "b"}]
        assert mock_requests.request.call_count == 2

    @pytest.mark.parametrize(
        "next_link",
        [
            "https://attacker.com/beta/users?$skip=1",
            "https://graph.microsoft.com.attacker.com/beta/users?$skip=1",
            "http://graph.microsoft.com/beta/users?$skip=1",
            # Userinfo smuggling — these "look" like a Graph host to humans
            # scanning logs but route to the userinfo's authority. _is_graph_url
            # already rejects these (PR #67), but explicit coverage locks in the
            # contract so a future caller can't accidentally relax it.
            "https://attacker.com@graph.microsoft.com/beta/users?$skip=1",
            "https://user:pwd@graph.microsoft.com/beta/users?$skip=1",
        ],
    )
    def test_rejects_untrusted_next_link_before_sending_token(self, next_link, caplog):
        from entrabot.graph_helpers import UnsafeGraphNextLinkError, graph_collection_values

        page1 = _resp(
            200,
            {
                "value": [{"id": "a"}],
                "@odata.nextLink": next_link,
            },
        )
        with (
            patch("entrabot.graph_helpers.requests") as mock_requests,
            pytest.raises(UnsafeGraphNextLinkError, match="unsafe @odata.nextLink"),
            caplog.at_level("WARNING", logger="entrabot.graph_helpers"),
        ):
            mock_requests.request.return_value = page1
            graph_collection_values("/users", "tok")

        assert mock_requests.request.call_count == 1
        assert "unsafe @odata.nextLink" in caplog.text
        assert next_link not in caplog.text

    @pytest.mark.parametrize(
        "host",
        [
            "graph.microsoft.us",
            "dod-graph.microsoft.us",
            "microsoftgraph.chinacloudapi.cn",
        ],
    )
    def test_accepts_sovereign_cloud_next_links(self, host):
        from entrabot.graph_helpers import graph_collection_values

        page1 = _resp(
            200,
            {
                "value": [{"id": "a"}],
                "@odata.nextLink": f"https://{host}/v1.0/users?$skiptoken=abc",
            },
        )
        page2 = _resp(200, {"value": [{"id": host}]})
        with patch("entrabot.graph_helpers.requests") as mock_requests:
            mock_requests.request.side_effect = [page1, page2]
            result = graph_collection_values("/users", "tok")

        assert result == [{"id": "a"}, {"id": host}]
        assert mock_requests.request.call_count == 2

    def test_accepts_commercial_graph_next_link_and_fetches_second_page(self):
        from entrabot.graph_helpers import graph_collection_values

        next_link = "https://graph.microsoft.com/v1.0/users?$skiptoken=abc"
        page1 = _resp(200, {"value": [{"id": "a"}], "@odata.nextLink": next_link})
        page2 = _resp(200, {"value": [{"id": "b"}]})
        with patch("entrabot.graph_helpers.requests") as mock_requests:
            mock_requests.request.side_effect = [page1, page2]
            result = graph_collection_values("/users", "tok")

        assert result == [{"id": "a"}, {"id": "b"}]
        assert mock_requests.request.call_args_list[1].args == ("GET", next_link)
        assert mock_requests.request.call_args_list[1].kwargs["headers"][
            "Authorization"
        ] == "Bearer tok"

    def test_error_raises(self):
        from entrabot.graph_helpers import graph_collection_values

        error = _resp(403, text="Forbidden")
        with (
            patch("entrabot.graph_helpers.requests") as mock_requests,
            pytest.raises(RuntimeError, match="List users.*403"),
        ):
            mock_requests.request.return_value = error
            graph_collection_values("/users", "tok", action="List users")

    def test_custom_base_url(self):
        from entrabot.graph_helpers import graph_collection_values

        page = _resp(200, {"value": [{"id": "a"}]})
        with patch("entrabot.graph_helpers.requests") as mock_requests:
            mock_requests.request.return_value = page
            graph_collection_values(
                "/oauth2PermissionGrants",
                "tok",
                base_url="https://graph.microsoft.com/v1.0",
            )

        args, _ = mock_requests.request.call_args
        assert args[1].startswith("https://graph.microsoft.com/v1.0/")


# ---------------------------------------------------------------------------
# resolve_user_by_email
# ---------------------------------------------------------------------------


class TestResolveUserByEmail:
    def test_found_by_upn(self):
        from entrabot.graph_helpers import resolve_user_by_email

        resp_found = _resp(200, {"value": [{"id": "uid-1", "displayName": "Alice"}]})
        with patch("entrabot.graph_helpers.requests") as mock_requests:
            mock_requests.get.return_value = resp_found
            oid, name = resolve_user_by_email("tok", "alice@example.com")

        assert oid == "uid-1"
        assert name == "Alice"
        # Should have been called with UPN filter first
        first_call_url = mock_requests.get.call_args_list[0][0][0]
        assert "userPrincipalName" in first_call_url

    def test_found_by_mail_fallback(self):
        from entrabot.graph_helpers import resolve_user_by_email

        not_found = _resp(200, {"value": []})
        found = _resp(200, {"value": [{"id": "uid-2", "displayName": "Bob"}]})
        with patch("entrabot.graph_helpers.requests") as mock_requests:
            mock_requests.get.side_effect = [not_found, found]
            oid, name = resolve_user_by_email("tok", "bob@example.com")

        assert oid == "uid-2"

    def test_found_with_consistency_level_retry(self):
        from entrabot.graph_helpers import resolve_user_by_email

        not_found = _resp(200, {"value": []})
        bad_request = _resp(400, text="ConsistencyLevel required")
        found = _resp(200, {"value": [{"id": "uid-3", "displayName": "Carol"}]})
        with patch("entrabot.graph_helpers.requests") as mock_requests:
            # First UPN try → empty, then mail try → 400, retry with ConsistencyLevel → found
            mock_requests.get.side_effect = [not_found, bad_request, found]
            oid, name = resolve_user_by_email("tok", "carol@example.com")

        assert oid == "uid-3"

    def test_not_found_raises(self):
        from entrabot.graph_helpers import resolve_user_by_email

        not_found = _resp(200, {"value": []})
        with (
            patch("entrabot.graph_helpers.requests") as mock_requests,
            pytest.raises(LookupError, match="Could not resolve"),
        ):
            mock_requests.get.return_value = not_found
            resolve_user_by_email("tok", "nobody@example.com")

    def test_display_name_falls_back_to_email(self):
        from entrabot.graph_helpers import resolve_user_by_email

        resp = _resp(200, {"value": [{"id": "uid-4", "displayName": None}]})
        with patch("entrabot.graph_helpers.requests") as mock_requests:
            mock_requests.get.return_value = resp
            _, name = resolve_user_by_email("tok", "noname@example.com")

        assert name == "noname@example.com"

    def test_quotes_escaped_in_email(self):
        from entrabot.graph_helpers import resolve_user_by_email

        resp = _resp(200, {"value": [{"id": "uid-5", "displayName": "O'Brien"}]})
        with patch("entrabot.graph_helpers.requests") as mock_requests:
            mock_requests.get.return_value = resp
            oid, _ = resolve_user_by_email("tok", "o'brien@example.com")

        assert oid == "uid-5"
        first_call_url = mock_requests.get.call_args_list[0][0][0]
        assert "''" in first_call_url  # OData escaped


# ---------------------------------------------------------------------------
# require_ok
# ---------------------------------------------------------------------------


class TestRequireOk:
    def test_success_codes_pass(self):
        from entrabot.graph_helpers import require_ok

        for code in (200, 201, 204):
            require_ok(_resp(code), "test")  # should not raise

    def test_failure_raises(self):
        from entrabot.graph_helpers import require_ok

        with pytest.raises(RuntimeError, match="delete user.*403"):
            require_ok(_resp(403, text="Forbidden"), "delete user")
