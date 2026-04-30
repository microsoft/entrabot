"""Tests for entraclaw.storage.blob — Azure Blob Storage client.

Per ADR-005: async client with put/get/list/delete/exists + ETag-based
optimistic concurrency. Auth is DI'd via a token_provider callable so
the client has no knowledge of the three-hop flow — production wires
it to the Agent User storage-resource token; tests pass a stub.

Each test mocks the Azure Storage REST surface with respx.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from entraclaw.storage.blob import BlobStore

ACCOUNT = "entraclawtest123"
CONTAINER = "entraclaw-memory"
ENDPOINT = f"https://{ACCOUNT}.blob.core.windows.net"
BLOB_URL = f"{ENDPOINT}/{CONTAINER}"


def _make_store() -> BlobStore:
    """BlobStore with a stub token provider returning a static bearer."""
    return BlobStore(
        endpoint=ENDPOINT,
        container=CONTAINER,
        token_provider=lambda: "stub-token",
    )


class TestBlobPut:
    @pytest.mark.asyncio
    async def test_put_uploads_bytes_and_returns_etag(self) -> None:
        """put(path, bytes) must:
        - PUT to the correct blob URL
        - send x-ms-blob-type: BlockBlob (required for AAD-authed writes)
        - send Bearer token from the provider
        - return the ETag from the response so the caller can use it for
          later optimistic-concurrency writes
        """
        with respx.mock:
            route = respx.put(f"{BLOB_URL}/behavioral/MEMORY.md").mock(
                return_value=httpx.Response(
                    201,
                    headers={"ETag": '"0x8DC1234567890AB"'},
                )
            )
            store = _make_store()
            etag = await store.put("behavioral/MEMORY.md", b"hello world")

            assert etag == '"0x8DC1234567890AB"'
            req = route.calls.last.request
            assert req.headers["authorization"] == "Bearer stub-token"
            assert req.headers["x-ms-blob-type"] == "BlockBlob"
            assert req.content == b"hello world"


class TestBlobGet:
    @pytest.mark.asyncio
    async def test_get_returns_bytes_on_200(self) -> None:
        with respx.mock:
            respx.get(f"{BLOB_URL}/behavioral/MEMORY.md").mock(
                return_value=httpx.Response(200, content=b"hello world"),
            )
            store = _make_store()
            data = await store.get("behavioral/MEMORY.md")
            assert data == b"hello world"

    @pytest.mark.asyncio
    async def test_get_raises_keyerror_on_404(self) -> None:
        """404 means the blob doesn't exist — raise KeyError so callers can
        distinguish 'missing' from 'transport failure'."""
        with respx.mock:
            respx.get(f"{BLOB_URL}/missing.md").mock(return_value=httpx.Response(404))
            store = _make_store()
            with pytest.raises(KeyError):
                await store.get("missing.md")

    @pytest.mark.asyncio
    async def test_get_sends_bearer_token(self) -> None:
        """Token provider is called on every request — rotating tokens
        stay fresh without the caller threading them through."""
        with respx.mock:
            route = respx.get(f"{BLOB_URL}/x").mock(return_value=httpx.Response(200, content=b""))
            store = _make_store()
            await store.get("x")
            assert route.calls.last.request.headers["authorization"] == "Bearer stub-token"


class TestBlobExists:
    @pytest.mark.asyncio
    async def test_exists_true_on_200(self) -> None:
        with respx.mock:
            respx.head(f"{BLOB_URL}/present").mock(return_value=httpx.Response(200))
            store = _make_store()
            assert await store.exists("present") is True

    @pytest.mark.asyncio
    async def test_exists_false_on_404(self) -> None:
        with respx.mock:
            respx.head(f"{BLOB_URL}/missing").mock(return_value=httpx.Response(404))
            store = _make_store()
            assert await store.exists("missing") is False

    @pytest.mark.asyncio
    async def test_exists_uses_head_not_get(self) -> None:
        """HEAD avoids pulling the body — important for large blobs just
        being probed for presence."""
        with respx.mock:
            head_route = respx.head(f"{BLOB_URL}/big").mock(return_value=httpx.Response(200))
            get_route = respx.get(f"{BLOB_URL}/big").mock(
                return_value=httpx.Response(200, content=b"x" * 10**6)
            )
            store = _make_store()
            await store.exists("big")
            assert head_route.called
            assert not get_route.called


class TestBlobList:
    """Azure Storage REST list response is XML under
    GET /container?comp=list&prefix=...

    We test against the documented response shape — a minimal
    ``<EnumerationResults><Blobs><Blob><Name>…</Name></Blob>…</Blobs></EnumerationResults>``
    — so the parser stays small and obvious.
    """

    LIST_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<EnumerationResults>
  <Blobs>
    <Blob><Name>behavioral/MEMORY.md</Name><Properties/></Blob>
    <Blob><Name>behavioral/feedback_channel_discipline.md</Name><Properties/></Blob>
    <Blob><Name>behavioral/project_two_distinct_repos.md</Name><Properties/></Blob>
  </Blobs>
</EnumerationResults>
"""

    @pytest.mark.asyncio
    async def test_list_returns_names_under_prefix(self) -> None:
        with respx.mock:
            respx.get(f"{ENDPOINT}/{CONTAINER}").mock(
                return_value=httpx.Response(200, content=self.LIST_XML)
            )
            store = _make_store()
            names = await store.list("behavioral/")
            assert names == [
                "behavioral/MEMORY.md",
                "behavioral/feedback_channel_discipline.md",
                "behavioral/project_two_distinct_repos.md",
            ]

    @pytest.mark.asyncio
    async def test_list_sends_comp_list_and_prefix_params(self) -> None:
        """Graph side must see comp=list + prefix= so it actually filters."""
        captured: dict = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, content=self.LIST_XML)

        with respx.mock:
            respx.get(f"{ENDPOINT}/{CONTAINER}").mock(side_effect=handler)
            store = _make_store()
            await store.list("behavioral/")
            assert captured["params"].get("comp") == "list"
            assert captured["params"].get("prefix") == "behavioral/"
            # restype=container is the other required query param for
            # "list blobs in container" requests.
            assert captured["params"].get("restype") == "container"

    @pytest.mark.asyncio
    async def test_list_empty_returns_empty_list(self) -> None:
        empty_xml = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b"<EnumerationResults><Blobs></Blobs></EnumerationResults>"
        )
        with respx.mock:
            respx.get(f"{ENDPOINT}/{CONTAINER}").mock(
                return_value=httpx.Response(200, content=empty_xml)
            )
            store = _make_store()
            assert await store.list("nothing/") == []


class TestBlobDelete:
    @pytest.mark.asyncio
    async def test_delete_issues_delete_request(self) -> None:
        with respx.mock:
            route = respx.delete(f"{BLOB_URL}/gone").mock(
                return_value=httpx.Response(202)  # 202 Accepted is Azure's success code for delete
            )
            store = _make_store()
            await store.delete("gone")
            assert route.called

    @pytest.mark.asyncio
    async def test_delete_404_is_idempotent_no_error(self) -> None:
        """Deleting a non-existent blob should NOT raise — idempotent
        delete is a safer default (callers don't have to check exists first)."""
        with respx.mock:
            respx.delete(f"{BLOB_URL}/missing").mock(return_value=httpx.Response(404))
            store = _make_store()
            await store.delete("missing")  # must not raise

    @pytest.mark.asyncio
    async def test_delete_sends_bearer_token(self) -> None:
        with respx.mock:
            route = respx.delete(f"{BLOB_URL}/x").mock(return_value=httpx.Response(202))
            store = _make_store()
            await store.delete("x")
            assert route.calls.last.request.headers["authorization"] == "Bearer stub-token"


class TestBlobETag:
    """ETag-based optimistic concurrency for put.

    Multi-machine safety per ADR-005: caller reads a blob, notes its
    ETag, writes back with ``if_match=<etag>``. Azure returns 412
    Precondition Failed if the blob was modified in between; we
    translate to ``ConcurrencyError`` so callers can distinguish
    "stale write" from "real failure."
    """

    @pytest.mark.asyncio
    async def test_put_with_if_match_sends_header(self) -> None:
        with respx.mock:
            route = respx.put(f"{BLOB_URL}/manifest.json").mock(
                return_value=httpx.Response(201, headers={"ETag": '"new"'})
            )
            store = _make_store()
            etag = await store.put("manifest.json", b"x", if_match='"old"')
            assert etag == '"new"'
            assert route.calls.last.request.headers["if-match"] == '"old"'

    @pytest.mark.asyncio
    async def test_put_412_raises_concurrency_error(self) -> None:
        from entraclaw.storage.blob import ConcurrencyError

        with respx.mock:
            respx.put(f"{BLOB_URL}/manifest.json").mock(return_value=httpx.Response(412))
            store = _make_store()
            with pytest.raises(ConcurrencyError):
                await store.put("manifest.json", b"x", if_match='"stale"')

    @pytest.mark.asyncio
    async def test_put_without_if_match_sends_no_header(self) -> None:
        """Backwards compat: callers that don't care about concurrency
        can still call put(path, data) and get last-writer-wins."""
        with respx.mock:
            route = respx.put(f"{BLOB_URL}/x").mock(
                return_value=httpx.Response(201, headers={"ETag": '"a"'})
            )
            store = _make_store()
            await store.put("x", b"y")
            assert "if-match" not in route.calls.last.request.headers


class TestBlobAuth:
    """All blob methods must translate 401 into TokenExpiredError so
    existing _with_token_retry in mcp_server picks them up for free —
    same pattern as every Graph-side tool."""

    @pytest.mark.asyncio
    async def test_get_401_raises_token_expired(self) -> None:
        from entraclaw.errors import TokenExpiredError

        with respx.mock:
            respx.get(f"{BLOB_URL}/x").mock(return_value=httpx.Response(401))
            store = _make_store()
            with pytest.raises(TokenExpiredError):
                await store.get("x")

    @pytest.mark.asyncio
    async def test_put_401_raises_token_expired(self) -> None:
        from entraclaw.errors import TokenExpiredError

        with respx.mock:
            respx.put(f"{BLOB_URL}/x").mock(return_value=httpx.Response(401))
            store = _make_store()
            with pytest.raises(TokenExpiredError):
                await store.put("x", b"y")

    @pytest.mark.asyncio
    async def test_head_401_raises_token_expired(self) -> None:
        from entraclaw.errors import TokenExpiredError

        with respx.mock:
            respx.head(f"{BLOB_URL}/x").mock(return_value=httpx.Response(401))
            store = _make_store()
            with pytest.raises(TokenExpiredError):
                await store.exists("x")

    @pytest.mark.asyncio
    async def test_list_401_raises_token_expired(self) -> None:
        from entraclaw.errors import TokenExpiredError

        with respx.mock:
            respx.get(f"{ENDPOINT}/{CONTAINER}").mock(return_value=httpx.Response(401))
            store = _make_store()
            with pytest.raises(TokenExpiredError):
                await store.list("")

    @pytest.mark.asyncio
    async def test_delete_401_raises_token_expired(self) -> None:
        from entraclaw.errors import TokenExpiredError

        with respx.mock:
            respx.delete(f"{BLOB_URL}/x").mock(return_value=httpx.Response(401))
            store = _make_store()
            with pytest.raises(TokenExpiredError):
                await store.delete("x")


class TestBlobRoundTrip:
    """put → get round-trip semantics: what you put is what you get back."""

    @pytest.mark.asyncio
    async def test_round_trip(self) -> None:
        stored: dict[str, bytes] = {}

        def put_handler(request):
            path = str(request.url).rsplit(f"/{CONTAINER}/", 1)[1]
            stored[path] = request.read()
            return httpx.Response(201, headers={"ETag": '"e1"'})

        def get_handler(request):
            path = str(request.url).rsplit(f"/{CONTAINER}/", 1)[1]
            if path in stored:
                return httpx.Response(200, content=stored[path])
            return httpx.Response(404)

        with respx.mock:
            respx.put(url__regex=rf"{BLOB_URL}/.*").mock(side_effect=put_handler)
            respx.get(url__regex=rf"{BLOB_URL}/.*").mock(side_effect=get_handler)

            store = _make_store()
            payload = b'{"updated_at": "2026-04-17T20:00:00Z"}'
            await store.put("manifest.json", payload)
            got = await store.get("manifest.json")

            assert got == payload
