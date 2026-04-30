"""Tests for Graph API 429 rate-limit retry middleware.

The retry transport wraps httpx and automatically waits + retries
when Graph returns 429 with a Retry-After header.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from entraclaw.tools.rate_limit import RetryOn429Transport


class TestRetryOn429Transport:
    """Test the rate-limit retry transport."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_retry_on_success(self) -> None:
        """Normal 200 responses pass through without retry."""
        respx.get("https://graph.microsoft.com/v1.0/me").mock(
            return_value=httpx.Response(200, json={"id": "user-1"})
        )
        transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.get("https://graph.microsoft.com/v1.0/me")
        assert resp.status_code == 200

    @respx.mock
    @pytest.mark.asyncio
    async def test_retries_on_429_then_succeeds(self) -> None:
        """429 followed by 200 — retries once and returns the success."""
        respx.get("https://graph.microsoft.com/v1.0/me").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(200, json={"id": "user-1"}),
            ]
        )
        transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.get("https://graph.microsoft.com/v1.0/me")
        assert resp.status_code == 200
        assert resp.json()["id"] == "user-1"

    @respx.mock
    @pytest.mark.asyncio
    async def test_gives_up_after_max_retries(self) -> None:
        """If all retries hit 429, the last 429 response is returned."""
        respx.get("https://graph.microsoft.com/v1.0/me").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(429, headers={"Retry-After": "0"}),
            ]
        )
        transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport(), max_retries=3)
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.get("https://graph.microsoft.com/v1.0/me")
        assert resp.status_code == 429

    @respx.mock
    @pytest.mark.asyncio
    async def test_defaults_retry_after_when_missing(self) -> None:
        """Missing Retry-After header defaults to a short wait, still retries."""
        respx.get("https://graph.microsoft.com/v1.0/me").mock(
            side_effect=[
                httpx.Response(429),  # no Retry-After header
                httpx.Response(200, json={"ok": True}),
            ]
        )
        transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.get("https://graph.microsoft.com/v1.0/me")
        assert resp.status_code == 200

    @respx.mock
    @pytest.mark.asyncio
    async def test_caps_retry_after_at_max(self) -> None:
        """Absurdly large Retry-After values are capped."""
        respx.get("https://graph.microsoft.com/v1.0/me").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "9999"}),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        # max_wait=1 caps to 1s; Retry-After=0 for test speed
        transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport(), max_wait=1)
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.get("https://graph.microsoft.com/v1.0/me")
        assert resp.status_code == 200

    @respx.mock
    @pytest.mark.asyncio
    async def test_post_requests_also_retry(self) -> None:
        """POST requests (like sending messages) also get retry behavior."""
        respx.post("https://graph.microsoft.com/v1.0/chats/c1/messages").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(201, json={"id": "msg-1"}),
            ]
        )
        transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.post(
                "https://graph.microsoft.com/v1.0/chats/c1/messages",
                json={"body": {"content": "hi"}},
            )
        assert resp.status_code == 201

    @respx.mock
    @pytest.mark.asyncio
    async def test_non_429_errors_pass_through(self) -> None:
        """Non-429 errors (401, 404, 500) are not retried."""
        respx.get("https://graph.microsoft.com/v1.0/me").mock(return_value=httpx.Response(500))
        transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.get("https://graph.microsoft.com/v1.0/me")
        assert resp.status_code == 500


class TestAllow5xxRetry:
    """Files PR1 extension: ``allow_5xx_retry`` opt-in for read tools (D6).

    The flag defaults to ``False`` so existing email/teams calls keep their
    fail-fast-on-5xx semantics — the regression case
    ``test_non_429_errors_pass_through`` above proves it.
    """

    @respx.mock
    @pytest.mark.asyncio
    async def test_default_does_not_retry_5xx(self) -> None:
        """REGRESSION: default ``allow_5xx_retry=False`` must NOT retry on 503."""
        call_count = 0

        def handler(request):
            nonlocal call_count
            call_count += 1
            return httpx.Response(503)

        respx.get("https://graph.microsoft.com/v1.0/me").mock(side_effect=handler)
        transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.get("https://graph.microsoft.com/v1.0/me")
        assert resp.status_code == 503
        assert call_count == 1, "default must not retry 5xx (regression for email/teams)"

    @respx.mock
    @pytest.mark.asyncio
    async def test_retries_503_then_succeeds(self, monkeypatch) -> None:
        """503 followed by 200 — retries once and returns the success."""
        from entraclaw.tools import rate_limit

        # Zero-delay backoff for test speed.
        monkeypatch.setattr(rate_limit, "RETRY_5XX_BASE_DELAYS_S", (0.0, 0.0, 0.0))

        respx.get("https://graph.microsoft.com/v1.0/me/drive/sharedWithMe").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json={"value": []}),
            ]
        )
        transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport(), allow_5xx_retry=True)
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.get("https://graph.microsoft.com/v1.0/me/drive/sharedWithMe")
        assert resp.status_code == 200
        assert resp.json() == {"value": []}

    @respx.mock
    @pytest.mark.asyncio
    async def test_retries_502_504_alongside_503(self, monkeypatch) -> None:
        """502 and 504 are also retried when allow_5xx_retry=True."""
        from entraclaw.tools import rate_limit

        monkeypatch.setattr(rate_limit, "RETRY_5XX_BASE_DELAYS_S", (0.0, 0.0, 0.0))

        respx.get("https://graph.microsoft.com/v1.0/me").mock(
            side_effect=[
                httpx.Response(502),
                httpx.Response(504),
                httpx.Response(200, json={"id": "ok"}),
            ]
        )
        transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport(), allow_5xx_retry=True)
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.get("https://graph.microsoft.com/v1.0/me")
        assert resp.status_code == 200

    @respx.mock
    @pytest.mark.asyncio
    async def test_5xx_retry_exhausted_returns_last_response(self, monkeypatch) -> None:
        """After 3 5xx retries, the last 5xx response is returned (fail clean)."""
        from entraclaw.tools import rate_limit

        monkeypatch.setattr(rate_limit, "RETRY_5XX_BASE_DELAYS_S", (0.0, 0.0, 0.0))

        respx.get("https://graph.microsoft.com/v1.0/me").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(503),
                httpx.Response(503),
                httpx.Response(503),
            ]
        )
        transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport(), allow_5xx_retry=True)
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.get("https://graph.microsoft.com/v1.0/me")
        assert resp.status_code == 503

    @respx.mock
    @pytest.mark.asyncio
    async def test_500_not_retried_even_with_flag(self, monkeypatch) -> None:
        """500 is *not* in the transient-5xx set — only 502/503/504 retry."""
        from entraclaw.tools import rate_limit

        monkeypatch.setattr(rate_limit, "RETRY_5XX_BASE_DELAYS_S", (0.0, 0.0, 0.0))

        call_count = 0

        def handler(request):
            nonlocal call_count
            call_count += 1
            return httpx.Response(500)

        respx.get("https://graph.microsoft.com/v1.0/me").mock(side_effect=handler)
        transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport(), allow_5xx_retry=True)
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.get("https://graph.microsoft.com/v1.0/me")
        assert resp.status_code == 500
        assert call_count == 1, "500 (Internal Server Error) is not transient"

    @respx.mock
    @pytest.mark.asyncio
    async def test_429_and_5xx_compose(self, monkeypatch) -> None:
        """Sequential 429 then 503 then 200 — both retry paths fire."""
        from entraclaw.tools import rate_limit

        monkeypatch.setattr(rate_limit, "RETRY_5XX_BASE_DELAYS_S", (0.0, 0.0, 0.0))

        respx.get("https://graph.microsoft.com/v1.0/me").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(503),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport(), allow_5xx_retry=True)
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.get("https://graph.microsoft.com/v1.0/me")
        assert resp.status_code == 200
