"""Tests for ``entrabot.tools.files`` — PR1 (read + comment).

Eng-review T1/T4 budget: ~50 unit tests across 4 tools. T2 marker:
multipart-upload behaviors live in PR2 (tests for those tools come
with that PR).

Pattern (mirrors ``tests/tools/test_email.py``):

- ``respx.mock`` decorator + ``pytest.mark.asyncio``.
- Inject a respx-driven transport via ``transport=`` kwarg.
- Tests use ``make_file_ref`` from ``tests/tools/conftest.py`` (T5).
"""

from __future__ import annotations

import base64
import json
from urllib.parse import urlparse

import httpx
import pytest
import respx

from entrabot.errors import FileNotFoundError as FilesFileNotFoundError
from entrabot.errors import (
    FileTooLargeError,
    GraphFilesError,
    MissingPermissionError,
    SiteNotAllowedError,
    TokenExpiredError,
    UnsupportedCommentFormatError,
    UnsupportedReadFormatError,
    UrlNotResolvableError,
)
from entrabot.tools.files import (
    GRAPH_BETA_HOST,
    GRAPH_V1_HOST,
    OneDriveTarget,
    SharePointTarget,
    _audit_graph_call,
    _build_upload_url,
    add_file_comment,
    list_recent_files,
    read_file,
    resolve_file_url,
    upload_file,
    write_text_file,
)

TOKEN = "test-token"


class TestAuditGraphCall:
    @pytest.mark.asyncio
    async def test_uses_config_agent_id_when_credential_store_has_no_active_client(
        self, tmp_path, monkeypatch
    ) -> None:
        """Upstream Files audit calls must not depend on active_client_id keyring state."""

        class Store:
            @staticmethod
            def retrieve(*_args: object) -> None:
                return None

        audit_dir = tmp_path / "audit"
        monkeypatch.setenv("ENTRABOT_AUDIT_DIR", str(audit_dir))
        monkeypatch.setenv("ENTRABOT_AGENT_ID", "config-agent-id")
        monkeypatch.delenv("ENTRABOT_BLUEPRINT_APP_ID", raising=False)
        monkeypatch.setattr("entrabot.platform.get_credential_store", lambda: Store())

        async with _audit_graph_call(
            "read_file",
            "drive-id:item-id",
            metadata={"scope": "files.read"},
        ):
            pass

        lines = list(audit_dir.glob("*.jsonl"))[0].read_text().strip().splitlines()
        events = [json.loads(line) for line in lines]
        assert [event["outcome"] for event in events] == ["pending", "success"]
        assert {event["agent_id"] for event in events} == {"config-agent-id"}


# ───────────────────────────────────────────────────────────────────────
# Tool 1 — resolve_file_url
# ───────────────────────────────────────────────────────────────────────


class TestResolveFileUrl:
    @pytest.mark.asyncio
    @respx.mock
    async def test_resolves_sharepoint_url_to_file_ref(self) -> None:
        url = "https://tenant.sharepoint.com/sites/foo/spec.docx"
        encoded = "u!" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        route = respx.get(f"{GRAPH_V1_HOST}/shares/{encoded}/driveItem").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "01ABC",
                    "name": "spec.docx",
                    "webUrl": url,
                    "size": 12345,
                    "file": {
                        "mimeType": (
                            "application/vnd.openxmlformats-officedocument."
                            "wordprocessingml.document"
                        )
                    },
                    "parentReference": {
                        "driveId": "b!drive",
                        "siteId": "tenant.sharepoint.com,site-guid,web-guid",
                        "driveType": "documentLibrary",
                    },
                },
            )
        )

        ref = await resolve_file_url(url, token=TOKEN, transport=httpx.AsyncHTTPTransport())

        assert route.called
        assert ref.drive_id == "b!drive"
        assert ref.item_id == "01ABC"
        assert ref.name == "spec.docx"
        assert ref.kind == "sharepoint"
        assert ref.site_id == "tenant.sharepoint.com,site-guid,web-guid"
        assert ref.web_url == url
        assert ref.size_bytes == 12345

    @pytest.mark.asyncio
    @respx.mock
    async def test_resolves_onedrive_business_to_file_ref(self) -> None:
        url = "https://tenant-my.sharepoint.com/personal/me_tenant_com/Documents/spec.docx"
        encoded = "u!" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        respx.get(f"{GRAPH_V1_HOST}/shares/{encoded}/driveItem").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "01XYZ",
                    "name": "spec.docx",
                    "file": {"mimeType": "application/octet-stream"},
                    "parentReference": {"driveId": "b!odb", "driveType": "business"},
                },
            )
        )
        ref = await resolve_file_url(url, token=TOKEN, transport=httpx.AsyncHTTPTransport())
        assert ref.kind == "onedrive_business"
        assert ref.site_id is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_resolves_personal_onedrive(self) -> None:
        url = "https://onedrive.live.com/?cid=ABC&resid=DEF"
        encoded = "u!" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        respx.get(f"{GRAPH_V1_HOST}/shares/{encoded}/driveItem").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "01PER",
                    "name": "x.docx",
                    "file": {"mimeType": "application/octet-stream"},
                    "parentReference": {"driveId": "p!drive", "driveType": "personal"},
                },
            )
        )
        ref = await resolve_file_url(url, token=TOKEN, transport=httpx.AsyncHTTPTransport())
        assert ref.kind == "onedrive_personal"

    @pytest.mark.asyncio
    async def test_empty_url_raises_url_not_resolvable(self) -> None:
        with pytest.raises(UrlNotResolvableError):
            await resolve_file_url("", token=TOKEN)

    @pytest.mark.asyncio
    async def test_malformed_url_raises_url_not_resolvable(self) -> None:
        with pytest.raises(UrlNotResolvableError):
            await resolve_file_url("not-a-url", token=TOKEN)

    @pytest.mark.asyncio
    async def test_unsupported_scheme_raises_url_not_resolvable(self) -> None:
        with pytest.raises(UrlNotResolvableError):
            await resolve_file_url("ftp://example.com/file", token=TOKEN)

    @pytest.mark.asyncio
    @respx.mock
    async def test_404_raises_file_not_found(self) -> None:
        url = "https://tenant.sharepoint.com/sites/foo/missing.docx"
        respx.get(url__regex=r".*/shares/.*/driveItem").mock(
            return_value=httpx.Response(404, json={"error": {"message": "not found"}})
        )
        with pytest.raises(FilesFileNotFoundError):
            await resolve_file_url(url, token=TOKEN, transport=httpx.AsyncHTTPTransport())

    @pytest.mark.asyncio
    @respx.mock
    async def test_403_raises_missing_permission(self) -> None:
        url = "https://tenant.sharepoint.com/sites/foo/locked.docx"
        respx.get(url__regex=r".*/shares/.*/driveItem").mock(
            return_value=httpx.Response(403, json={"error": {"message": "forbidden"}})
        )
        with pytest.raises(MissingPermissionError):
            await resolve_file_url(url, token=TOKEN, transport=httpx.AsyncHTTPTransport())

    @pytest.mark.asyncio
    @respx.mock
    async def test_401_raises_token_expired(self) -> None:
        url = "https://tenant.sharepoint.com/sites/foo/file.docx"
        respx.get(url__regex=r".*/shares/.*/driveItem").mock(
            return_value=httpx.Response(401, json={"error": {"message": "unauth"}})
        )
        with pytest.raises(TokenExpiredError):
            await resolve_file_url(url, token=TOKEN, transport=httpx.AsyncHTTPTransport())

    @pytest.mark.asyncio
    @respx.mock
    async def test_resolved_site_in_denylist_raises_site_not_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        url = "https://tenant.sharepoint.com/sites/secret/spec.docx"
        site_id = "tenant.sharepoint.com,secret-guid,web-guid"
        monkeypatch.setenv("ENTRABOT_FILES_DENIED_SITES", site_id)
        respx.get(url__regex=r".*/shares/.*/driveItem").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "01ABC",
                    "name": "spec.docx",
                    "file": {"mimeType": "application/octet-stream"},
                    "parentReference": {
                        "driveId": "b!drive",
                        "siteId": site_id,
                        "driveType": "documentLibrary",
                    },
                },
            )
        )
        with pytest.raises(SiteNotAllowedError):
            await resolve_file_url(url, token=TOKEN, transport=httpx.AsyncHTTPTransport())

    @pytest.mark.asyncio
    @respx.mock
    async def test_500_raises_graph_files_error(self) -> None:
        url = "https://tenant.sharepoint.com/sites/foo/file.docx"
        respx.get(url__regex=r".*/shares/.*/driveItem").mock(
            return_value=httpx.Response(500, json={"error": {"message": "boom"}})
        )
        with pytest.raises(GraphFilesError):
            await resolve_file_url(url, token=TOKEN, transport=httpx.AsyncHTTPTransport())

    @pytest.mark.asyncio
    @respx.mock
    async def test_graph_returns_no_drive_id_raises_url_not_resolvable(self) -> None:
        url = "https://tenant.sharepoint.com/sites/foo/file.docx"
        respx.get(url__regex=r".*/shares/.*/driveItem").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "01ABC",
                    "name": "x.docx",
                    "file": {"mimeType": "text/plain"},
                    "parentReference": {},
                },
            )
        )
        with pytest.raises(UrlNotResolvableError):
            await resolve_file_url(url, token=TOKEN, transport=httpx.AsyncHTTPTransport())


# ───────────────────────────────────────────────────────────────────────
# Tool 2 — list_recent_files
# ───────────────────────────────────────────────────────────────────────


def _shared_with_me_response(items: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"value": items})


def _drive_item(
    *,
    item_id: str = "01ABC",
    drive_id: str = "b!drive",
    name: str = "spec.docx",
    web_url: str = "https://tenant.sharepoint.com/sites/foo/spec.docx",
    mime: str = "text/plain",
    size: int = 100,
    site_id: str | None = "tenant.sharepoint.com,site-guid,web-guid",
    sharer_name: str = "Alice",
    is_folder: bool = False,
) -> dict:
    facet = {} if is_folder else {"file": {"mimeType": mime}}
    return {
        "remoteItem": {
            "id": item_id,
            "name": name,
            "webUrl": web_url,
            "size": size,
            "lastModifiedDateTime": "2026-01-15T12:00:00Z",
            "parentReference": {
                "driveId": drive_id,
                "siteId": site_id,
            },
            "shared": {"sharedBy": {"user": {"displayName": sharer_name}}},
            **facet,
        }
    }


class TestListRecentFiles:
    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_sharedwithme_files(self) -> None:
        respx.get(f"{GRAPH_V1_HOST}/me/drive/sharedWithMe?$top=10").mock(
            return_value=_shared_with_me_response(
                [_drive_item(name="a.docx"), _drive_item(item_id="02", name="b.docx")]
            )
        )
        page = await list_recent_files(limit=10, token=TOKEN, transport=httpx.AsyncHTTPTransport())
        assert len(page.files) == 2
        assert page.files[0].name == "a.docx"
        assert page.denied_count == 0

    @pytest.mark.asyncio
    @respx.mock
    async def test_filters_folders(self) -> None:
        respx.get(f"{GRAPH_V1_HOST}/me/drive/sharedWithMe?$top=25").mock(
            return_value=_shared_with_me_response(
                [_drive_item(name="ok.docx"), _drive_item(name="folder", is_folder=True)]
            )
        )
        page = await list_recent_files(token=TOKEN, transport=httpx.AsyncHTTPTransport())
        assert [f.name for f in page.files] == ["ok.docx"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_denylist_increments_denied_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        denied_site = "tenant.sharepoint.com,bad-guid,web-guid"
        ok_site = "tenant.sharepoint.com,good-guid,web-guid"
        monkeypatch.setenv("ENTRABOT_FILES_DENIED_SITES", denied_site)
        respx.get(f"{GRAPH_V1_HOST}/me/drive/sharedWithMe?$top=25").mock(
            return_value=_shared_with_me_response(
                [
                    _drive_item(name="bad.docx", site_id=denied_site),
                    _drive_item(item_id="2", name="ok.docx", site_id=ok_site),
                ]
            )
        )
        page = await list_recent_files(token=TOKEN, transport=httpx.AsyncHTTPTransport())
        assert [f.name for f in page.files] == ["ok.docx"]
        assert page.denied_count == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_403_raises_missing_permission(self) -> None:
        respx.get(f"{GRAPH_V1_HOST}/me/drive/sharedWithMe?$top=25").mock(
            return_value=httpx.Response(403, json={"error": {"message": "no"}})
        )
        with pytest.raises(MissingPermissionError):
            await list_recent_files(token=TOKEN, transport=httpx.AsyncHTTPTransport())

    @pytest.mark.asyncio
    @respx.mock
    async def test_401_raises_token_expired(self) -> None:
        respx.get(f"{GRAPH_V1_HOST}/me/drive/sharedWithMe?$top=25").mock(
            return_value=httpx.Response(401, json={"error": {"message": "expired"}})
        )
        with pytest.raises(TokenExpiredError):
            await list_recent_files(token=TOKEN, transport=httpx.AsyncHTTPTransport())

    @pytest.mark.asyncio
    async def test_invalid_limit_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            await list_recent_files(limit=0, token=TOKEN)

    @pytest.mark.asyncio
    @respx.mock
    async def test_empty_response(self) -> None:
        respx.get(f"{GRAPH_V1_HOST}/me/drive/sharedWithMe?$top=25").mock(
            return_value=_shared_with_me_response([])
        )
        page = await list_recent_files(token=TOKEN, transport=httpx.AsyncHTTPTransport())
        assert page.files == []
        assert page.denied_count == 0


# ───────────────────────────────────────────────────────────────────────
# Tool 3 — read_file
# ───────────────────────────────────────────────────────────────────────


class TestReadFile:
    @pytest.mark.asyncio
    @respx.mock
    async def test_reads_md_as_raw_text(self, make_file_ref) -> None:
        ref = make_file_ref(name="spec.md", mime_type="text/markdown")
        url = f"{GRAPH_V1_HOST}/drives/{ref.drive_id}/items/{ref.item_id}/content"
        respx.get(url).mock(
            return_value=httpx.Response(
                200, content=b"# Spec\n\nHello world.", headers={"content-type": "text/markdown"}
            )
        )
        content = await read_file(ref, token=TOKEN, transport=httpx.AsyncHTTPTransport())
        assert content.text == "# Spec\n\nHello world."
        assert content.page_count is None
        assert content.truncated is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_reads_txt_as_raw_text(self, make_file_ref) -> None:
        ref = make_file_ref(name="notes.txt", mime_type="text/plain")
        url = f"{GRAPH_V1_HOST}/drives/{ref.drive_id}/items/{ref.item_id}/content"
        respx.get(url).mock(return_value=httpx.Response(200, content=b"line1\nline2"))
        content = await read_file(ref, token=TOKEN, transport=httpx.AsyncHTTPTransport())
        assert content.text == "line1\nline2"

    @pytest.mark.asyncio
    @respx.mock
    async def test_reads_html_as_raw_text(self, make_file_ref) -> None:
        ref = make_file_ref(name="page.html", mime_type="text/html")
        url = f"{GRAPH_V1_HOST}/drives/{ref.drive_id}/items/{ref.item_id}/content"
        respx.get(url).mock(return_value=httpx.Response(200, content=b"<h1>hi</h1>"))
        content = await read_file(ref, token=TOKEN, transport=httpx.AsyncHTTPTransport())
        assert "<h1>hi</h1>" in content.text

    @pytest.mark.asyncio
    @respx.mock
    async def test_truncates_to_max_text_bytes(
        self, make_file_ref, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENTRABOT_FILES_MAX_TEXT_BYTES", "10")
        ref = make_file_ref(name="big.txt", mime_type="text/plain")
        url = f"{GRAPH_V1_HOST}/drives/{ref.drive_id}/items/{ref.item_id}/content"
        respx.get(url).mock(return_value=httpx.Response(200, content=b"a" * 100))
        content = await read_file(ref, token=TOKEN, transport=httpx.AsyncHTTPTransport())
        assert content.truncated is True
        assert len(content.text.encode("utf-8")) <= 10

    @pytest.mark.asyncio
    async def test_rejects_xlsx(self, make_file_ref) -> None:
        ref = make_file_ref(
            name="data.xlsx",
            mime_type=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        )
        with pytest.raises(UnsupportedReadFormatError):
            await read_file(ref, token=TOKEN)

    @pytest.mark.asyncio
    async def test_rejects_pptx(self, make_file_ref) -> None:
        ref = make_file_ref(name="deck.pptx", mime_type="application/octet-stream")
        with pytest.raises(UnsupportedReadFormatError):
            await read_file(ref, token=TOKEN)

    @pytest.mark.asyncio
    async def test_rejects_unknown_extension(self, make_file_ref) -> None:
        ref = make_file_ref(name="weird.bin", mime_type="application/octet-stream")
        with pytest.raises(UnsupportedReadFormatError):
            await read_file(ref, token=TOKEN)

    @pytest.mark.asyncio
    async def test_rejects_extensionless_file(self, make_file_ref) -> None:
        ref = make_file_ref(name="README", mime_type="text/plain")
        with pytest.raises(UnsupportedReadFormatError):
            await read_file(ref, token=TOKEN)

    @pytest.mark.asyncio
    async def test_rejects_when_site_in_denylist(
        self, make_file_ref, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        denied = "tenant.sharepoint.com,denied,web"
        monkeypatch.setenv("ENTRABOT_FILES_DENIED_SITES", denied)
        ref = make_file_ref(name="spec.md", site_id=denied)
        with pytest.raises(SiteNotAllowedError):
            await read_file(ref, token=TOKEN)

    @pytest.mark.asyncio
    @respx.mock
    async def test_pdf_too_large_raises_before_download(
        self, make_file_ref, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENTRABOT_FILES_MAX_PDF_BYTES", "1024")
        ref = make_file_ref(
            name="huge.pdf",
            mime_type="application/pdf",
            size_bytes=10 * 1024 * 1024,
        )
        with pytest.raises(FileTooLargeError):
            await read_file(ref, token=TOKEN, transport=httpx.AsyncHTTPTransport())

    @pytest.mark.asyncio
    @respx.mock
    async def test_pdf_size_unknown_fetches_metadata_first(
        self, make_file_ref, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``size_bytes`` is None, read_file MUST query /items/{id}?$select=size first."""
        monkeypatch.setenv("ENTRABOT_FILES_MAX_PDF_BYTES", "1024")
        ref = make_file_ref(name="huge.pdf", mime_type="application/pdf", size_bytes=None)
        meta_route = respx.get(
            f"{GRAPH_V1_HOST}/drives/{ref.drive_id}/items/{ref.item_id}?$select=size"
        ).mock(return_value=httpx.Response(200, json={"size": 5 * 1024 * 1024}))
        with pytest.raises(FileTooLargeError):
            await read_file(ref, token=TOKEN, transport=httpx.AsyncHTTPTransport())
        assert meta_route.called

    @pytest.mark.asyncio
    @respx.mock
    async def test_403_on_content_raises_missing_permission(self, make_file_ref) -> None:
        ref = make_file_ref(name="spec.md")
        url = f"{GRAPH_V1_HOST}/drives/{ref.drive_id}/items/{ref.item_id}/content"
        respx.get(url).mock(return_value=httpx.Response(403, json={"error": {"message": "no"}}))
        with pytest.raises(MissingPermissionError):
            await read_file(ref, token=TOKEN, transport=httpx.AsyncHTTPTransport())

    @pytest.mark.asyncio
    @respx.mock
    async def test_401_on_content_raises_token_expired(self, make_file_ref) -> None:
        ref = make_file_ref(name="spec.md")
        url = f"{GRAPH_V1_HOST}/drives/{ref.drive_id}/items/{ref.item_id}/content"
        respx.get(url).mock(return_value=httpx.Response(401, json={"error": {"message": "no"}}))
        with pytest.raises(TokenExpiredError):
            await read_file(ref, token=TOKEN, transport=httpx.AsyncHTTPTransport())


# ───────────────────────────────────────────────────────────────────────
# Tool 4 — add_file_comment
# ───────────────────────────────────────────────────────────────────────


def _comment_response(comment_id: str = "comment-1", content: str = "Q?") -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "id": comment_id,
            "content": {"contentType": "text", "content": content},
        },
    )


class TestAddFileComment:
    @pytest.mark.asyncio
    @respx.mock
    async def test_comments_on_docx(self, make_file_ref) -> None:
        ref = make_file_ref(name="spec.docx", kind="sharepoint")
        url = f"{GRAPH_BETA_HOST}/drives/{ref.drive_id}/items/{ref.item_id}/comments"
        route = respx.post(url).mock(return_value=_comment_response("c1", "What is X?"))
        result = await add_file_comment(
            ref, "What is X?", token=TOKEN, transport=httpx.AsyncHTTPTransport()
        )
        assert route.called
        body = json.loads(route.calls.last.request.content)
        assert body["content"]["contentType"] == "text"
        assert body["content"]["content"] == "What is X?"
        assert result.comment_id == "c1"
        assert result.content == "What is X?"

    @pytest.mark.asyncio
    @respx.mock
    async def test_comments_on_xlsx(self, make_file_ref) -> None:
        ref = make_file_ref(
            name="data.xlsx",
            kind="sharepoint",
            mime_type=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        )
        url = f"{GRAPH_BETA_HOST}/drives/{ref.drive_id}/items/{ref.item_id}/comments"
        respx.post(url).mock(return_value=_comment_response())
        result = await add_file_comment(
            ref, "Q?", token=TOKEN, transport=httpx.AsyncHTTPTransport()
        )
        assert result.comment_id == "comment-1"

    @pytest.mark.asyncio
    async def test_rejects_pptx(self, make_file_ref) -> None:
        ref = make_file_ref(name="deck.pptx", kind="sharepoint")
        with pytest.raises(UnsupportedCommentFormatError):
            await add_file_comment(ref, "no", token=TOKEN)

    @pytest.mark.asyncio
    async def test_rejects_pdf(self, make_file_ref) -> None:
        ref = make_file_ref(name="doc.pdf", kind="sharepoint")
        with pytest.raises(UnsupportedCommentFormatError):
            await add_file_comment(ref, "no", token=TOKEN)

    @pytest.mark.asyncio
    async def test_rejects_md(self, make_file_ref) -> None:
        ref = make_file_ref(name="readme.md", kind="sharepoint")
        with pytest.raises(UnsupportedCommentFormatError):
            await add_file_comment(ref, "no", token=TOKEN)

    @pytest.mark.asyncio
    async def test_rejects_personal_onedrive(self, make_file_ref) -> None:
        ref = make_file_ref(name="spec.docx", kind="onedrive_personal", site_id=None)
        with pytest.raises(UnsupportedCommentFormatError):
            await add_file_comment(ref, "no", token=TOKEN)

    @pytest.mark.asyncio
    async def test_rejects_onedrive_business(self, make_file_ref) -> None:
        """Beta /comments 404s on ODB MySite drives — guard rejects upfront.

        Verified live 2026-05-04 against the agent's own ODB doc:
        POST /beta/drives/{id}/items/{id}/comments returned
        404 itemNotFound. Only real SharePoint team sites work.
        """
        ref = make_file_ref(name="spec.docx", kind="onedrive_business", site_id=None)
        with pytest.raises(UnsupportedCommentFormatError):
            await add_file_comment(ref, "no", token=TOKEN)

    @pytest.mark.asyncio
    async def test_rejects_folder(self, make_file_ref) -> None:
        ref = make_file_ref(
            name="spec.docx",
            mime_type="application/vnd.microsoft.graph.folder",
            kind="sharepoint",
        )
        with pytest.raises(UnsupportedCommentFormatError):
            await add_file_comment(ref, "no", token=TOKEN)

    @pytest.mark.asyncio
    async def test_rejects_empty_content(self, make_file_ref) -> None:
        ref = make_file_ref(name="spec.docx", kind="sharepoint")
        with pytest.raises(UnsupportedCommentFormatError):
            await add_file_comment(ref, "", token=TOKEN)

    @pytest.mark.asyncio
    async def test_rejects_when_site_in_denylist(
        self, make_file_ref, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        denied = "tenant.sharepoint.com,bad,web"
        monkeypatch.setenv("ENTRABOT_FILES_DENIED_SITES", denied)
        ref = make_file_ref(name="spec.docx", kind="sharepoint", site_id=denied)
        with pytest.raises(SiteNotAllowedError):
            await add_file_comment(ref, "Q?", token=TOKEN)

    @pytest.mark.asyncio
    @respx.mock
    async def test_403_raises_missing_permission(self, make_file_ref) -> None:
        ref = make_file_ref(name="spec.docx", kind="sharepoint")
        url = f"{GRAPH_BETA_HOST}/drives/{ref.drive_id}/items/{ref.item_id}/comments"
        respx.post(url).mock(return_value=httpx.Response(403, json={"error": {"message": "no"}}))
        with pytest.raises(MissingPermissionError):
            await add_file_comment(ref, "Q?", token=TOKEN, transport=httpx.AsyncHTTPTransport())

    @pytest.mark.asyncio
    @respx.mock
    async def test_401_raises_token_expired(self, make_file_ref) -> None:
        ref = make_file_ref(name="spec.docx", kind="sharepoint")
        url = f"{GRAPH_BETA_HOST}/drives/{ref.drive_id}/items/{ref.item_id}/comments"
        respx.post(url).mock(return_value=httpx.Response(401, json={"error": {"message": "no"}}))
        with pytest.raises(TokenExpiredError):
            await add_file_comment(ref, "Q?", token=TOKEN, transport=httpx.AsyncHTTPTransport())

    @pytest.mark.asyncio
    @respx.mock
    async def test_500_raises_graph_files_error(self, make_file_ref) -> None:
        ref = make_file_ref(name="spec.docx", kind="sharepoint")
        url = f"{GRAPH_BETA_HOST}/drives/{ref.drive_id}/items/{ref.item_id}/comments"
        respx.post(url).mock(return_value=httpx.Response(500, json={"error": {"message": "boom"}}))
        with pytest.raises(GraphFilesError):
            await add_file_comment(ref, "Q?", token=TOKEN, transport=httpx.AsyncHTTPTransport())

    @pytest.mark.asyncio
    @respx.mock
    async def test_200_response_also_accepted(self, make_file_ref) -> None:
        """Beta endpoint historically returned 200; ensure we accept both."""
        ref = make_file_ref(name="spec.docx", kind="sharepoint")
        url = f"{GRAPH_BETA_HOST}/drives/{ref.drive_id}/items/{ref.item_id}/comments"
        respx.post(url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "c2",
                    "content": {"contentType": "text", "content": "Q?"},
                },
            )
        )
        result = await add_file_comment(
            ref, "Q?", token=TOKEN, transport=httpx.AsyncHTTPTransport()
        )
        assert result.comment_id == "c2"


# ───────────────────────────────────────────────────────────────────────
# Tool 5 — upload URL construction
# ───────────────────────────────────────────────────────────────────────


def _upload_response(name: str = "report.docx", size: int = 12) -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "id": "item-1",
            "name": name,
            "webUrl": f"https://tenant.sharepoint.com/{name}",
            "size": size,
            "file": {"mimeType": "application/octet-stream"},
            "parentReference": {"driveId": "drive-1"},
        },
    )


class TestUploadUrlValidation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("target", "file_name", "conflict_behavior"),
        [
            (OneDriveTarget(), "a#b.txt", "fail"),
            (OneDriveTarget(), "a?x=y", "fail"),
            (OneDriveTarget(), "../foo.txt", "fail"),
            (OneDriveTarget(), "foo:/content", "fail"),
            (OneDriveTarget(folder_path="/legit/../../other"), "safe.txt", "fail"),
            (OneDriveTarget(), "a\nb.txt", "fail"),
            (OneDriveTarget(), "a/b.txt", "fail"),
            (OneDriveTarget(), "safe.txt", "replace&extra=x"),
        ],
    )
    async def test_write_text_file_rejects_upload_url_bypasses(
        self,
        target: OneDriveTarget,
        file_name: str,
        conflict_behavior: str,
    ) -> None:
        with pytest.raises(ValueError):
            await write_text_file(
                target,
                file_name,
                "hello",
                conflict_behavior=conflict_behavior,  # type: ignore[arg-type]
                token=TOKEN,
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("target", "file_name", "conflict_behavior"),
        [
            (OneDriveTarget(), "a#b.txt", "fail"),
            (OneDriveTarget(), "a?x=y", "fail"),
            (OneDriveTarget(), "../foo.txt", "fail"),
            (OneDriveTarget(), "foo:/content", "fail"),
            (OneDriveTarget(folder_path="/legit/../../other"), "safe.txt", "fail"),
            (OneDriveTarget(), "a\nb.txt", "fail"),
            (OneDriveTarget(), "a/b.txt", "fail"),
            (OneDriveTarget(), "safe.txt", "replace&extra=x"),
        ],
    )
    async def test_upload_file_rejects_upload_url_bypasses(
        self,
        target: OneDriveTarget,
        file_name: str,
        conflict_behavior: str,
    ) -> None:
        with pytest.raises(ValueError):
            await upload_file(
                target,
                file_name,
                b"hello",
                conflict_behavior=conflict_behavior,  # type: ignore[arg-type]
                token=TOKEN,
            )

    @pytest.mark.asyncio
    @respx.mock
    async def test_write_text_file_encodes_folder_path_segments(self) -> None:
        target = OneDriveTarget(folder_path="/Documents/Q4")
        expected_url = (
            f"{GRAPH_V1_HOST}/me/drive/root:/Documents/Q4/report.docx:/content"
            "?@microsoft.graph.conflictBehavior=replace"
        )
        route = respx.put(expected_url).mock(return_value=_upload_response("report.docx"))

        ref = await write_text_file(
            target,
            "report.docx",
            "hello",
            conflict_behavior="replace",
            token=TOKEN,
            transport=httpx.AsyncHTTPTransport(),
        )

        assert route.called
        assert ref.item_id == "item-1"

    @pytest.mark.asyncio
    @respx.mock
    async def test_upload_file_encodes_space_in_file_name(self) -> None:
        target = SharePointTarget(
            site_id="tenant.sharepoint.com,site,web",
            drive_id="drive-1",
            folder_path="/Documents/Q4",
        )
        expected_url = (
            f"{GRAPH_V1_HOST}/drives/drive-1/root:"
            "/Documents/Q4/report%20%28final%29.docx:/content"
            "?@microsoft.graph.conflictBehavior=rename"
        )
        route = respx.put(expected_url).mock(
            return_value=_upload_response("report (final).docx", size=5)
        )

        ref = await upload_file(
            target,
            "report (final).docx",
            b"hello",
            conflict_behavior="rename",
            token=TOKEN,
            transport=httpx.AsyncHTTPTransport(),
        )

        assert route.called
        assert ref.name == "report (final).docx"

    @pytest.mark.parametrize("conflict_behavior", ["rename", "replace", "fail"])
    def test_build_upload_url_encodes_segments_and_validates_conflict_behavior(
        self, conflict_behavior: str
    ) -> None:
        target = SharePointTarget(
            site_id="tenant.sharepoint.com,site,web",
            drive_id="drive-1",
            folder_path="/Docs & Plans/Q+4",
        )

        url = _build_upload_url(
            target,
            "report final.txt",
            conflict_behavior=conflict_behavior,  # type: ignore[arg-type]
        )

        parsed = urlparse(url)
        assert parsed.path.endswith(
            "/drives/drive-1/root:/Docs%20%26%20Plans/Q%2B4/report%20final.txt:/content"
        )
        assert "#" not in parsed.path
        assert ".." not in parsed.path
        assert parsed.query == f"@microsoft.graph.conflictBehavior={conflict_behavior}"

    def test_build_upload_url_encodes_drive_id_as_path_segment(self) -> None:
        target = SharePointTarget(
            site_id="tenant.sharepoint.com,site,web",
            drive_id="b!drive/root:/evil.txt:/content?x=y",
            folder_path="/Safe",
        )

        url = _build_upload_url(target, "report.docx", conflict_behavior="fail")

        parsed = urlparse(url)
        assert parsed.path.endswith("/root:/Safe/report.docx:/content")
        assert "/root:/evil.txt:/content" not in parsed.path
        assert "?" not in parsed.path
        assert "%2Froot%3A%2Fevil.txt%3A%2Fcontent%3Fx%3Dy" in parsed.path
        assert parsed.query == "@microsoft.graph.conflictBehavior=fail"
