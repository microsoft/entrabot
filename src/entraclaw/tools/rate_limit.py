"""Graph API 429 rate-limit retry transport for httpx.

Wraps an ``httpx.AsyncBaseTransport`` and automatically retries requests
that receive a 429 response, respecting the ``Retry-After`` header.

Usage in teams.py (or any module making Graph calls)::

    transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.get(...)

Files Graph API extension (PR1, eng-review T3): the optional
``allow_5xx_retry`` flag turns this into a generalized "Graph
transient failure" retry transport — when ``True``, the transport
retries 502/503/504 with exponential jitter (200/800/2400ms) on top of
the existing 429 behavior. Default stays ``False`` so existing email
and Teams calls keep their fail-fast-on-5xx semantics.
"""

from __future__ import annotations

import asyncio
import logging
import random

import httpx

logger = logging.getLogger("entraclaw.tools.rate_limit")

DEFAULT_RETRY_AFTER = 5  # seconds when Retry-After header is missing
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_WAIT = 120  # cap on Retry-After to avoid absurd waits

# Per-attempt base delays for 5xx retries (when allow_5xx_retry=True).
# Three attempts, exponential backoff. Jitter adds 0-30% per attempt.
RETRY_5XX_BASE_DELAYS_S = (0.2, 0.8, 2.4)
RETRY_5XX_STATUS_CODES = (502, 503, 504)


class RetryOn429Transport(httpx.AsyncBaseTransport):
    """Async httpx transport that retries on 429 with Retry-After backoff.

    When ``allow_5xx_retry=True`` the transport additionally retries on
    transient 5xx (502/503/504) with exponential jitter — used by Files
    read tools per D6. Mutations leave the default ``False`` so 5xx
    surfaces to the caller (fail fast).
    """

    def __init__(
        self,
        wrapped: httpx.AsyncBaseTransport,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_wait: int = DEFAULT_MAX_WAIT,
        *,
        allow_5xx_retry: bool = False,
    ) -> None:
        self._wrapped = wrapped
        self._max_retries = max_retries
        self._max_wait = max_wait
        self._allow_5xx_retry = allow_5xx_retry

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._wrapped.handle_async_request(request)

        retries = 0
        five_xx_retries = 0
        while retries < self._max_retries:
            if response.status_code == 429:
                retries += 1
                retry_after = int(response.headers.get("Retry-After", str(DEFAULT_RETRY_AFTER)))
                retry_after = min(retry_after, self._max_wait)

                logger.warning(
                    "Graph API 429 — retry %d/%d after %ds",
                    retries,
                    self._max_retries,
                    retry_after,
                )
                await asyncio.sleep(retry_after)
                response = await self._wrapped.handle_async_request(request)
                continue

            if (
                self._allow_5xx_retry
                and response.status_code in RETRY_5XX_STATUS_CODES
                and five_xx_retries < len(RETRY_5XX_BASE_DELAYS_S)
            ):
                base_delay = RETRY_5XX_BASE_DELAYS_S[five_xx_retries]
                jitter = base_delay * random.uniform(0.0, 0.3)
                delay = min(base_delay + jitter, self._max_wait)
                five_xx_retries += 1
                retries += 1

                logger.warning(
                    "Graph API %d — 5xx retry %d/%d after %.2fs",
                    response.status_code,
                    five_xx_retries,
                    len(RETRY_5XX_BASE_DELAYS_S),
                    delay,
                )
                await asyncio.sleep(delay)
                response = await self._wrapped.handle_async_request(request)
                continue

            break

        return response
