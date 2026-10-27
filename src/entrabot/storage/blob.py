"""Azure Blob Storage client for agent memory (ADR-005, Phase 1).

Async client over httpx with Bearer-token auth. The token is sourced
from a caller-supplied ``token_provider`` callable so this module has
no opinion about how the token is acquired — production wires it to
the Agent User's storage-scope token (parallel third hop in the FIC
flow); tests pass a stub.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import httpx

from entrabot.errors import TokenExpiredError


def _check_auth(resp: httpx.Response) -> None:
    """Translate 401 into TokenExpiredError so the existing
    `_with_token_retry` pattern in mcp_server handles blob calls
    the same way it handles Graph calls. Call this at the top of
    every response-handling path."""
    if resp.status_code == 401:
        raise TokenExpiredError("Storage token expired or missing storage.azure.com scope")


# Azure Storage's list-blobs response is XML. We only need <Name>…</Name>
# from each <Blob>; a full XML parser adds a dep + attack surface for no
# real gain here. Regex is safe because Azure's response is well-formed
# and uses no nesting that'd confuse a simple pattern.
_BLOB_NAME_RE = re.compile(r"<Name>([^<]+)</Name>")

# Azure Blob Storage REST API version — required for AAD-authed calls.
# Pin explicitly so behavior is stable regardless of server-side defaults.
_API_VERSION = "2021-08-06"


class ConcurrencyError(Exception):
    """Blob write refused because the source ETag doesn't match.

    Raised from ``put(..., if_match=…)`` when Azure returns HTTP 412
    Precondition Failed. Caller should re-read the blob to get the
    current ETag and decide how to merge.
    """


class BlobStore:
    """Minimal async client over Azure Blob Storage."""

    def __init__(
        self,
        *,
        endpoint: str,
        container: str,
        token_provider: Callable[[], str],
        on_unauthorized: Callable[[], None] | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._container = container
        self._token_provider = token_provider
        self._on_unauthorized = on_unauthorized

    def _url(self, path: str) -> str:
        return f"{self._endpoint}/{self._container}/{path}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token_provider()}",
            "x-ms-version": _API_VERSION,
        }

    async def _send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs: object,
    ) -> httpx.Response:
        """Send one request with a provider token; on 401, retry once.

        The token provider may serve a cached token (#128). When Azure rejects
        it, ``on_unauthorized`` drops the cache and the request is re-sent with
        a freshly acquired token. A second 401 (or no hook) raises
        ``TokenExpiredError`` via :func:`_check_auth`.
        """
        for attempt in range(2):
            request_headers = {**self._headers(), **(headers or {})}
            async with httpx.AsyncClient() as client:
                resp = await client.request(method, url, headers=request_headers, **kwargs)
            if resp.status_code == 401 and attempt == 0 and self._on_unauthorized is not None:
                self._on_unauthorized()
                continue
            _check_auth(resp)
            return resp
        raise AssertionError("unreachable")  # pragma: no cover

    async def put(
        self,
        path: str,
        data: bytes,
        *,
        if_match: str | None = None,
    ) -> str:
        """Upload *data* to *path*. Returns the new ETag.

        When ``if_match`` is set, the write is conditional: Azure returns
        412 Precondition Failed if the blob's current ETag doesn't match,
        which we translate to ``ConcurrencyError`` so callers can retry
        with the fresh version.
        """
        headers = {"x-ms-blob-type": "BlockBlob"}
        if if_match is not None:
            headers["If-Match"] = if_match
        resp = await self._send("PUT", self._url(path), headers=headers, content=data)
        if resp.status_code == 412:
            raise ConcurrencyError(f"put({path!r}) refused: If-Match={if_match!r} is stale")
        resp.raise_for_status()
        return resp.headers.get("ETag", "")

    async def get(self, path: str) -> bytes:
        """Download *path*. Raises ``KeyError`` if the blob doesn't exist."""
        resp = await self._send("GET", self._url(path))
        if resp.status_code == 404:
            raise KeyError(path)
        resp.raise_for_status()
        return resp.content

    async def get_with_etag(self, path: str) -> tuple[bytes, str]:
        """Download *path* and its current ETag.

        Returns ``(content, etag)``. Raises ``KeyError`` on 404 so callers can
        distinguish "absent" from a transport failure. The ETag lets a caller
        do a later ``put(..., if_match=etag)`` for optimistic concurrency
        (fleet-safe cursor writes — design F5).
        """
        resp = await self._send("GET", self._url(path))
        if resp.status_code == 404:
            raise KeyError(path)
        resp.raise_for_status()
        return resp.content, resp.headers.get("ETag", "")

    async def exists(self, path: str) -> bool:
        """Probe whether *path* exists. HEAD request — doesn't pull the body."""
        resp = await self._send("HEAD", self._url(path))
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True

    async def list(self, prefix: str = "") -> list[str]:
        """Return blob names in the container under *prefix*."""
        params = {
            "restype": "container",
            "comp": "list",
            "prefix": prefix,
        }
        resp = await self._send("GET", f"{self._endpoint}/{self._container}", params=params)
        resp.raise_for_status()
        return _BLOB_NAME_RE.findall(resp.text)

    async def delete(self, path: str) -> None:
        """Delete *path*. 404 is silently accepted (idempotent delete)."""
        resp = await self._send("DELETE", self._url(path))
        if resp.status_code == 404:
            return
        resp.raise_for_status()
