"""Microsoft Graph Files API — read, comment, list, resolve.

PR1 surface (Scenario 1: read SharePoint spec, comment for clarification):

- ``resolve_file_url`` — share URL → ``FileRef`` (drive_id, item_id, site_id, kind)
- ``list_recent_files`` — ``/me/drive/sharedWithMe`` with denylist post-filter
- ``read_file`` — auto-detect format; .md/.txt/.html raw, .docx via PDF
  conversion + ``pypdf``, .pdf via direct ``pypdf``, .xlsx/.pptx rejected
- ``add_file_comment`` — Files-only comment (beta endpoint), Word + Excel
  on OneDrive-Business / SharePoint, rejects .pptx, personal OneDrive,
  folder driveItems

PR2 (Scenario 2: author + upload + share) and PR3 (Excel reads) ship
in subsequent commits. This module's contract:

- All public functions are ``async``.
- All public functions take ``*, token: str, transport=None`` so the
  MCP wrapper supplies the token from ``acquire_agent_user_token`` and
  tests can inject a respx-driven transport.
- 429 retry handled by ``RetryOn429Transport`` (existing). Read tools
  pass ``allow_5xx_retry=True`` per D6; mutations leave it ``False``
  (fail-fast on 5xx).
- Module boundary: never imports ``tools.teams``. The chat-reply leg
  for D1 is the model's job — call ``add_file_comment`` and
  ``send_teams_message`` separately.
- Beta surface isolated to ``add_file_comment`` only — see
  ``GRAPH_BETA_HOST`` constant.
- Audit logging via ``_audit_graph_call`` async context manager (DRY).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import quote, urlparse

import httpx

from entrabot.errors import (
    FileNotFoundError,
    FileTooLargeError,
    GraphFilesError,
    MissingPermissionError,
    NoActiveSponsorChannelError,
    RequesterNotInChatError,
    RequesterNotSponsorError,
    SiteNotAllowedError,
    SponsorChannelMismatchError,
    TokenExpiredError,
    UnsupportedCommentFormatError,
    UnsupportedReadFormatError,
    UrlNotResolvableError,
)
from entrabot.tools.audit import log_event
from entrabot.tools.rate_limit import RetryOn429Transport

logger = logging.getLogger("entrabot.tools.files")

GRAPH_V1_HOST = "https://graph.microsoft.com/v1.0"
GRAPH_BETA_HOST = "https://graph.microsoft.com/beta"  # comments only; isolate

# Default budgets — overridable via env. Plan §"Failure-mode registry":
# ENTRABOT_FILES_MAX_TEXT_BYTES (200KB) caps extracted text per
# read_file; ENTRABOT_FILES_MAX_PDF_BYTES (50 MiB) refuses to download
# a PDF exceeding that size (P1).
DEFAULT_MAX_TEXT_BYTES = 200_000
DEFAULT_MAX_PDF_BYTES = 52_428_800  # 50 MiB

DriveKind = Literal["onedrive_personal", "onedrive_business", "sharepoint"]
ReadFormat = Literal["raw", "auto"]
ConflictBehavior = Literal["rename", "replace", "fail"]
ShareRole = Literal["read", "write"]

_ALLOWED_CONFLICT_BEHAVIORS = {"rename", "replace", "fail"}


# ───────────────────────────────────────────────────────────────────────
# Public dataclasses
# ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OneDriveTarget:
    """Upload target: agent's own OneDrive (no SharePoint permission needed)."""

    folder_path: str = "/"


@dataclass(frozen=True)
class SharePointTarget:
    """Upload target: named SharePoint site library (site_id denylist applied)."""

    site_id: str
    drive_id: str
    folder_path: str = "/"


@dataclass(frozen=True)
class SharePermission:
    """Result of ``share_file`` — permission metadata for V1.1 revocation."""

    permission_id: str
    role: ShareRole
    recipient_email: str
    web_url: str | None = None
    expiration_at: str | None = None


@dataclass(frozen=True)
class FileRef:
    """Stable handle to a driveItem.

    Carries ``site_id`` (eng-review A2): the resolver does the site
    lookup once and downstream tools (read, comment, share) never
    re-resolve. ``site_id`` is ``None`` for OneDrive (personal or
    business) and populated for SharePoint.
    """

    drive_id: str
    item_id: str
    name: str
    mime_type: str
    kind: DriveKind
    site_id: str | None = None
    web_url: str | None = None
    size_bytes: int | None = None


@dataclass(frozen=True)
class FileSummary:
    """One row of ``/me/drive/sharedWithMe``."""

    drive_id: str
    item_id: str
    name: str
    web_url: str
    mime_type: str
    size_bytes: int
    modified_at: str
    shared_by: str | None
    site_id: str | None = None


@dataclass(frozen=True)
class RecentFilesPage:
    """Result of ``list_recent_files``.

    ``denied_count`` (eng-review A2) is the number of files filtered
    out by the operator denylist — surfaced so the model can tell the
    user "I see N more files but my operator denied those sites."
    """

    files: list[FileSummary]
    denied_count: int


@dataclass(frozen=True)
class FileContent:
    """Result of ``read_file``."""

    drive_id: str
    item_id: str
    name: str
    mime_type: str
    text: str
    page_count: int | None
    truncated: bool


@dataclass(frozen=True)
class CommentResult:
    """Result of ``add_file_comment``.

    Files-only after eng-review A1 — there is no chat-reply leg here.
    The model orchestrates the chat reply with ``send_teams_message``
    if it wants one.
    """

    comment_id: str
    content: str
    web_url: str | None = None


# ───────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────


def _denied_sites() -> frozenset[str]:
    """Read ``ENTRABOT_FILES_DENIED_SITES`` as a normalized set.

    Separator is ``;`` (semicolon), NOT comma — Graph site IDs already
    contain commas (``{host},{site-collection},{web}``).
    """
    raw = os.environ.get("ENTRABOT_FILES_DENIED_SITES", "")
    return frozenset(s.strip() for s in raw.split(";") if s.strip())


def _check_site_allowed(site_id: str | None) -> None:
    """Raise ``SiteNotAllowedError`` if the site is in the denylist.

    Pure local — no Graph call. ``None`` (OneDrive) is always allowed.
    """
    if site_id is None:
        return
    denied = _denied_sites()
    if site_id in denied:
        raise SiteNotAllowedError(site_id)


def _max_text_bytes() -> int:
    raw = os.environ.get("ENTRABOT_FILES_MAX_TEXT_BYTES")
    try:
        return int(raw) if raw else DEFAULT_MAX_TEXT_BYTES
    except ValueError:
        return DEFAULT_MAX_TEXT_BYTES


def _max_pdf_bytes() -> int:
    raw = os.environ.get("ENTRABOT_FILES_MAX_PDF_BYTES")
    try:
        return int(raw) if raw else DEFAULT_MAX_PDF_BYTES
    except ValueError:
        return DEFAULT_MAX_PDF_BYTES


def _share_id_from_url(url: str) -> str:
    """Encode ``url`` per Graph's ``/shares/{share-id}`` shape.

    Per https://learn.microsoft.com/en-us/graph/api/shares-get :
    base64url(url), strip ``=`` padding, prefix with ``u!``.
    """
    encoded = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii")
    return "u!" + encoded.rstrip("=")


def _classify_drive_kind(parent_ref: dict | None, drive_type: str | None) -> DriveKind:
    """Decide if a driveItem is OneDrive personal/business or SharePoint.

    Graph's ``parentReference.driveType`` is the canonical source:
    ``personal``, ``business``, ``documentLibrary`` (SharePoint).
    ``parentReference.siteId`` being present also indicates SharePoint.
    """
    parent_ref = parent_ref or {}
    site_id = parent_ref.get("siteId")
    if site_id:
        return "sharepoint"
    dt = (drive_type or parent_ref.get("driveType") or "").lower()
    if dt == "personal":
        return "onedrive_personal"
    return "onedrive_business"  # default for missing/business/other


def _extension(name: str) -> str:
    """Lowercase file extension including leading dot, or empty string."""
    name = name or ""
    idx = name.rfind(".")
    if idx < 0:
        return ""
    return name[idx:].lower()


def _contains_control_char(value: str) -> bool:
    return any(ord(char) < 0x20 for char in value)


def _validate_upload_path_part(value: str, *, label: str, allow_slash: bool) -> None:
    if _contains_control_char(value):
        raise ValueError(f"{label} contains a control character")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{label} contains a newline")
    if ":" in value:
        raise ValueError(f"{label} contains ':'")
    if "\\" in value:
        raise ValueError(f"{label} contains '\\'")
    if ".." in value:
        raise ValueError(f"{label} contains '..'")
    if "#" in value:
        raise ValueError(f"{label} contains '#'")
    if "?" in value:
        raise ValueError(f"{label} contains '?'")
    if not allow_slash and "/" in value:
        raise ValueError(f"{label} contains '/'")


def _validate_conflict_behavior(conflict_behavior: str) -> ConflictBehavior:
    if conflict_behavior not in _ALLOWED_CONFLICT_BEHAVIORS:
        raise ValueError("conflict_behavior must be one of: fail, rename, replace")
    return cast(ConflictBehavior, conflict_behavior)


def _encoded_upload_path(folder_path: str, file_name: str) -> str:
    if not file_name:
        raise ValueError("file_name is required")
    if file_name.startswith("/"):
        raise ValueError("file_name must not start with '/'")

    _validate_upload_path_part(file_name, label="file_name", allow_slash=False)
    _validate_upload_path_part(folder_path, label="folder_path", allow_slash=True)

    folder = folder_path.strip("/")
    if folder:
        segments = folder.split("/")
        if any(segment == "" for segment in segments):
            raise ValueError("folder_path contains an empty segment")
        encoded_segments = [quote(segment, safe="") for segment in segments]
    else:
        encoded_segments = []

    encoded_file_name = quote(file_name, safe="")
    return "/" + "/".join([*encoded_segments, encoded_file_name])


def _build_upload_url(
    target: OneDriveTarget | SharePointTarget,
    file_name: str,
    *,
    conflict_behavior: ConflictBehavior,
    endpoint: Literal["content", "createUploadSession"] = "content",
) -> str:
    """Build a Graph upload URL after fail-closed validation and encoding."""
    conflict_behavior = _validate_conflict_behavior(conflict_behavior)
    drive_prefix = (
        f"/drives/{quote(target.drive_id, safe='')}"
        if isinstance(target, SharePointTarget)
        else "/me/drive"
    )
    path_spec = _encoded_upload_path(target.folder_path, file_name)
    url = f"{GRAPH_V1_HOST}{drive_prefix}/root:{path_spec}:/{endpoint}"
    if endpoint == "content":
        url = f"{url}?@microsoft.graph.conflictBehavior={conflict_behavior}"
    return url


def _make_transport(*, allow_5xx_retry: bool = False) -> httpx.AsyncBaseTransport:
    """Wrap a plain httpx transport with the rate-limit retry layer."""
    return RetryOn429Transport(
        wrapped=httpx.AsyncHTTPTransport(),
        allow_5xx_retry=allow_5xx_retry,
    )


def _client(
    transport: httpx.AsyncBaseTransport | None,
    *,
    allow_5xx_retry: bool = False,
) -> httpx.AsyncClient:
    """Construct an ``httpx.AsyncClient`` honoring caller-supplied transport.

    Tests inject a respx transport directly (it already mocks the
    network) — they should pass that transport unwrapped. Production
    callers leave ``transport=None`` so we wrap the default transport
    with the rate-limit retry layer.
    """
    if transport is None:
        transport = _make_transport(allow_5xx_retry=allow_5xx_retry)
    return httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(30.0))


@asynccontextmanager
async def _audit_graph_call(
    verb: str,
    resource: str,
    *,
    metadata: dict | None = None,
) -> AsyncIterator[None]:
    """C3: single audit-log point for every Graph Files call.

    Emits ``outcome="pending"`` before the body runs and ``"success"``
    or ``"failure"`` after. Replaces nine ad-hoc ``log_event`` blocks.
    """
    log_event(
        action=f"files.{verb}",
        resource=resource,
        outcome="pending",
        metadata=metadata or {},
    )
    try:
        yield
    except Exception as exc:
        log_event(
            action=f"files.{verb}",
            resource=resource,
            outcome="failure",
            metadata={**(metadata or {}), "error": type(exc).__name__, "message": str(exc)},
        )
        raise
    else:
        log_event(
            action=f"files.{verb}",
            resource=resource,
            outcome="success",
            metadata=metadata or {},
        )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _raise_for_files_error(resp: httpx.Response, *, target: str, scope: str) -> None:
    """Map a non-2xx Graph response onto the FilesError hierarchy."""
    status = resp.status_code
    if status == 401:
        raise TokenExpiredError(
            "Agent User token expired during Files Graph call — re-acquire via three-hop flow"
        )
    if status == 404:
        raise FileNotFoundError(target)
    if status == 403:
        raise MissingPermissionError(scope)
    try:
        body = resp.json()
        msg = json.dumps(body)
    except Exception:
        msg = resp.text or f"HTTP {status}"
    raise GraphFilesError(status, msg)


# ───────────────────────────────────────────────────────────────────────
# Tool 1 — resolve_file_url
# ───────────────────────────────────────────────────────────────────────


async def resolve_file_url(
    url: str,
    *,
    token: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FileRef:
    """Resolve a SharePoint / OneDrive / shared-link URL to a ``FileRef``.

    Uses ``GET /shares/{share-id}/driveItem`` (eng-review A3) — one
    call covers SharePoint URLs, OneDrive personal/business URLs, and
    shared-link URLs, and the response includes ``parentReference.siteId``
    and ``parentReference.driveId`` for free.

    Errors:
    - ``UrlNotResolvableError`` — empty / malformed URL
    - ``FileNotFoundError`` — Graph 404
    - ``SiteNotAllowedError`` — resolved site is in the operator denylist
    - ``MissingPermissionError`` — Graph 403
    """
    if not url or not isinstance(url, str):
        raise UrlNotResolvableError(str(url), "empty or non-string URL")
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        raise UrlNotResolvableError(url, "no scheme or hostname")
    if parsed.scheme not in ("http", "https"):
        raise UrlNotResolvableError(url, f"unsupported scheme {parsed.scheme!r}")

    share_id = _share_id_from_url(url.strip())
    request_url = f"{GRAPH_V1_HOST}/shares/{share_id}/driveItem"

    async with _audit_graph_call("resolve_file_url", url):
        async with _client(transport, allow_5xx_retry=True) as client:
            resp = await client.get(request_url, headers=_bearer(token))

        if resp.status_code != 200:
            _raise_for_files_error(resp, target=url, scope="Files.Read")

        data = resp.json()
        parent_ref = data.get("parentReference") or {}
        drive_id = parent_ref.get("driveId") or ""
        item_id = data.get("id") or ""
        if not drive_id or not item_id:
            raise UrlNotResolvableError(url, "Graph returned a driveItem without drive_id/item_id")

        site_id = parent_ref.get("siteId") or None
        kind = _classify_drive_kind(parent_ref, parent_ref.get("driveType"))
        # Drop the {site}/-style suffix that Graph sometimes adds.
        if site_id and "," in site_id:
            # Graph returns "{tenantHost},{siteCollectionId},{siteId}".
            # Keep the full triple — that's the canonical site identifier.
            pass

        if site_id:
            _check_site_allowed(site_id)

        mime = ((data.get("file") or {}).get("mimeType")) or "application/octet-stream"
        return FileRef(
            drive_id=drive_id,
            item_id=item_id,
            name=str(data.get("name") or ""),
            mime_type=mime,
            kind=kind,
            site_id=site_id,
            web_url=data.get("webUrl"),
            size_bytes=data.get("size"),
        )


# ───────────────────────────────────────────────────────────────────────
# Tool 2 — list_recent_files
# ───────────────────────────────────────────────────────────────────────


def _summary_from_drive_item(item: dict) -> FileSummary | None:
    """Build a ``FileSummary`` from a ``/sharedWithMe`` row.

    Returns ``None`` for items without a remoteItem facet (folders /
    non-file shares).
    """
    remote = item.get("remoteItem") or item
    parent_ref = remote.get("parentReference") or {}
    drive_id = parent_ref.get("driveId") or ""
    item_id = remote.get("id") or ""
    file_facet = remote.get("file") or {}
    if not drive_id or not item_id or not file_facet:
        return None  # folder or non-file share

    shared_by = None
    shared_facet = remote.get("shared") or item.get("shared") or {}
    sharer = (shared_facet.get("sharedBy") or {}).get("user") or {}
    shared_by = sharer.get("displayName") or sharer.get("email")

    site_id = parent_ref.get("siteId") or None
    return FileSummary(
        drive_id=drive_id,
        item_id=item_id,
        name=str(remote.get("name") or ""),
        web_url=str(remote.get("webUrl") or ""),
        mime_type=str(file_facet.get("mimeType") or "application/octet-stream"),
        size_bytes=int(remote.get("size") or 0),
        modified_at=str(remote.get("lastModifiedDateTime") or ""),
        shared_by=shared_by,
        site_id=site_id,
    )


async def list_recent_files(
    limit: int = 25,
    *,
    token: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RecentFilesPage:
    """List files recently shared with the Agent User.

    Calls ``/me/drive/sharedWithMe`` and post-filters with the
    operator denylist; ``denied_count`` (eng-review A2) is surfaced on
    the result so the model can tell the user "N more files exist on
    sites my operator denied."
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    request_url = f"{GRAPH_V1_HOST}/me/drive/sharedWithMe?$top={limit}"

    files: list[FileSummary] = []
    denied_count = 0

    async with _audit_graph_call("list_recent_files", "me/drive/sharedWithMe"):
        async with _client(transport, allow_5xx_retry=True) as client:
            resp = await client.get(request_url, headers=_bearer(token))

        if resp.status_code != 200:
            _raise_for_files_error(resp, target="sharedWithMe", scope="Files.Read.All")

        denied = _denied_sites()
        for raw in resp.json().get("value", []):
            summary = _summary_from_drive_item(raw)
            if summary is None:
                continue
            if summary.site_id and summary.site_id in denied:
                denied_count += 1
                continue
            files.append(summary)

    return RecentFilesPage(files=files, denied_count=denied_count)


# ───────────────────────────────────────────────────────────────────────
# Tool 3 — read_file
# ───────────────────────────────────────────────────────────────────────


# Extensions read raw as text/markdown.
_RAW_TEXT_EXTENSIONS: frozenset[str] = frozenset({".md", ".txt", ".html", ".htm"})

# Extensions rejected with a hint to the right tool / chat fallback.
_EXCEL_EXTENSIONS: frozenset[str] = frozenset({".xlsx", ".xls", ".xlsm"})
_PPT_EXTENSIONS: frozenset[str] = frozenset({".pptx", ".ppt"})


def _extract_pdf_text(data: bytes) -> tuple[str, int]:
    """Return ``(text, page_count)`` from PDF bytes via pypdf.

    ``pypdf`` is in the V1 dep set; if a future caller needs an
    alternative, swap here. Intentionally lazy-imported so tests that
    don't read PDFs don't pay the import cost.
    """
    from io import BytesIO

    import pypdf

    reader = pypdf.PdfReader(BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return ("\n\n".join(pages), len(pages))


def _truncate(text: str, *, max_bytes: int) -> tuple[str, bool]:
    """Truncate ``text`` so its UTF-8 encoding is at most ``max_bytes``."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated, True


async def _check_size_or_raise(
    *,
    client: httpx.AsyncClient,
    file_ref: FileRef,
    token: str,
) -> int:
    """GET /items/{id} for ``size``. Raises ``FileTooLargeError`` if over the cap."""
    if file_ref.size_bytes is not None:
        size = file_ref.size_bytes
    else:
        meta_url = (
            f"{GRAPH_V1_HOST}/drives/{file_ref.drive_id}/items/{file_ref.item_id}?$select=size"
        )
        resp = await client.get(meta_url, headers=_bearer(token))
        if resp.status_code != 200:
            _raise_for_files_error(
                resp,
                target=f"{file_ref.drive_id}:{file_ref.item_id}",
                scope="Files.Read",
            )
        size = int(resp.json().get("size") or 0)
    cap = _max_pdf_bytes()
    if size > cap:
        raise FileTooLargeError(size, cap)
    return size


async def read_file(
    file_ref: FileRef,
    *,
    as_format: ReadFormat = "auto",
    token: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FileContent:
    """Read a file's contents as text.

    Format policy (eng-review A2: ``file_ref`` carries ``site_id`` →
    one denylist check, no re-resolve):

    - ``.md`` / ``.txt`` / ``.html`` / ``.htm`` → fetch raw, decode, return text
    - ``.docx`` → ``GET /content?format=pdf``, extract via ``pypdf``
    - ``.pdf`` → fetch raw, extract via ``pypdf`` (size-checked first, P1)
    - ``.xlsx`` / ``.xls`` → reject (use ``read_workbook_range`` — PR3)
    - ``.pptx`` / ``.ppt`` → reject (paste content into chat instead)
    - everything else → reject

    Raises ``FileTooLargeError`` if the file exceeds
    ``ENTRABOT_FILES_MAX_PDF_BYTES`` (default 50 MiB) — checked
    *before* the body download. Raises ``SiteNotAllowedError`` if
    ``file_ref.site_id`` is in the operator denylist.
    """
    _check_site_allowed(file_ref.site_id)

    ext = _extension(file_ref.name)
    resource = f"{file_ref.drive_id}:{file_ref.item_id}"

    if ext in _EXCEL_EXTENSIONS:
        raise UnsupportedReadFormatError(ext, "Use read_workbook_range for Excel data (PR3).")
    if ext in _PPT_EXTENSIONS:
        raise UnsupportedReadFormatError(
            ext,
            "PowerPoint reading is not supported in V1; ask the user "
            "to paste slide content into chat.",
        )
    if ext not in _RAW_TEXT_EXTENSIONS and ext not in {".pdf", ".docx"}:
        raise UnsupportedReadFormatError(
            ext or "(no extension)",
            "Only .md/.txt/.html, .pdf, and .docx are supported in V1.",
        )

    async with (
        _audit_graph_call(
            "read_file",
            resource,
            metadata={"name": file_ref.name, "extension": ext, "as_format": as_format},
        ),
        _client(transport, allow_5xx_retry=True) as client,
    ):
        page_count: int | None = None

        if ext in _RAW_TEXT_EXTENSIONS:
            content_url = (
                f"{GRAPH_V1_HOST}/drives/{file_ref.drive_id}/items/{file_ref.item_id}/content"
            )
            resp = await client.get(content_url, headers=_bearer(token))
            if resp.status_code not in (200, 302):
                _raise_for_files_error(resp, target=resource, scope="Files.Read")
            # httpx auto-follows redirects; resp.content is the file body.
            text = resp.content.decode("utf-8", errors="replace")

        elif ext == ".pdf":
            # P1: refuse to download PDFs over the size cap.
            await _check_size_or_raise(client=client, file_ref=file_ref, token=token)
            content_url = (
                f"{GRAPH_V1_HOST}/drives/{file_ref.drive_id}/items/{file_ref.item_id}/content"
            )
            resp = await client.get(content_url, headers=_bearer(token))
            if resp.status_code != 200:
                _raise_for_files_error(resp, target=resource, scope="Files.Read")
            text, page_count = _extract_pdf_text(resp.content)

        else:  # .docx via PDF conversion
            content_url = (
                f"{GRAPH_V1_HOST}/drives/{file_ref.drive_id}"
                f"/items/{file_ref.item_id}/content?format=pdf"
            )
            resp = await client.get(content_url, headers=_bearer(token))
            if resp.status_code != 200:
                _raise_for_files_error(resp, target=resource, scope="Files.Read")
            pdf_bytes = resp.content
            cap = _max_pdf_bytes()
            if len(pdf_bytes) > cap:
                raise FileTooLargeError(len(pdf_bytes), cap)
            text, page_count = _extract_pdf_text(pdf_bytes)

        text, truncated = _truncate(text, max_bytes=_max_text_bytes())

        return FileContent(
            drive_id=file_ref.drive_id,
            item_id=file_ref.item_id,
            name=file_ref.name,
            mime_type=file_ref.mime_type,
            text=text,
            page_count=page_count,
            truncated=truncated,
        )


# ───────────────────────────────────────────────────────────────────────
# Tool 4 — add_file_comment
# ───────────────────────────────────────────────────────────────────────


_COMMENT_SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".docx", ".xlsx"})


def _check_comment_target_allowed(file_ref: FileRef) -> None:
    """Apply comment-tool guards: site allowlist, format, kind, folder.

    Raises:
        SiteNotAllowedError: site is in the operator denylist
        UnsupportedCommentFormatError: extension, kind, or driveItem type
            does not support comments

    Rejected kinds:
    - ``onedrive_personal`` — Microsoft does not GA personal-OneDrive
      comments on any surface.
    - ``onedrive_business`` — beta ``/drives/{id}/items/{id}/comments``
      returns ``404 itemNotFound`` on MySite drives (POST and GET
      both); only real SharePoint team sites work. Verified live
      2026-05-04 against the agent's own ODB doc.
    """
    _check_site_allowed(file_ref.site_id)

    if (file_ref.mime_type or "").lower() in (
        "folder",
        "application/vnd.microsoft.graph.folder",
    ):
        raise UnsupportedCommentFormatError("cannot comment on a folder driveItem")

    ext = _extension(file_ref.name)
    if ext not in _COMMENT_SUPPORTED_EXTENSIONS:
        raise UnsupportedCommentFormatError(
            f"file extension {ext or '(none)'} does not support comments — "
            "only .docx and .xlsx files can receive document comments"
        )

    if file_ref.kind == "onedrive_personal":
        raise UnsupportedCommentFormatError(
            "comments on personal OneDrive files are not GA in Graph; "
            "ask the user to share the file from a SharePoint team site"
        )

    if file_ref.kind == "onedrive_business":
        raise UnsupportedCommentFormatError(
            "comments on OneDrive-for-Business (MySite) files are not "
            "supported by the Graph beta /comments endpoint — it returns "
            "404 itemNotFound on POST and GET. Move the file to a "
            "SharePoint team site, or share it from one."
        )


async def add_file_comment(
    file_ref: FileRef,
    content: str,
    *,
    token: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CommentResult:
    """Add a document comment through the legacy Graph beta endpoint; for Word UI
    comments and replies, use the Agent 365 Work IQ Word tools
    (`read_word_document`, `add_word_comment`, `reply_to_word_comment`).

    This tool is retained for compatibility with the existing Files surface.
    It is NOT the production path for Word UI comments. Use the Agent 365
    Work IQ Word tools (`read_word_document`, `add_word_comment`,
    `reply_to_word_comment`) for Word document comments and replies.

    Files-only after eng-review A1 — there is no chat-reply leg here.
    The model orchestrates the chat reply via ``send_teams_message``.

    Reject conditions (eng-review A5, raise ``UnsupportedCommentFormatError``):

    - File extension not in ``{.docx, .xlsx}`` (rejects .pptx, .pdf, .md)
    - ``file_ref.kind == "onedrive_personal"`` (Microsoft does not GA
      personal-OneDrive comments)
    - ``file_ref.mime_type`` indicates a folder

    Legacy endpoint:
    ``POST /beta/drives/{drive-id}/items/{item-id}/comments``
    """
    if not content or not isinstance(content, str):
        raise UnsupportedCommentFormatError("content is empty or non-string")

    _check_comment_target_allowed(file_ref)
    ext = _extension(file_ref.name)

    resource = f"{file_ref.drive_id}:{file_ref.item_id}"
    request_url = f"{GRAPH_BETA_HOST}/drives/{file_ref.drive_id}/items/{file_ref.item_id}/comments"
    payload = {"content": {"contentType": "text", "content": content}}

    async with _audit_graph_call(
        "add_file_comment",
        resource,
        metadata={"extension": ext, "kind": file_ref.kind, "site_id": file_ref.site_id},
    ):
        async with _client(transport, allow_5xx_retry=False) as client:
            resp = await client.post(
                request_url,
                json=payload,
                headers={**_bearer(token), "Content-Type": "application/json"},
            )

        if resp.status_code not in (200, 201):
            _raise_for_files_error(resp, target=resource, scope="Files.ReadWrite")

        body = resp.json()
        comment_content = (body.get("content") or {}).get("content") or content
        return CommentResult(
            comment_id=str(body.get("id") or ""),
            content=str(comment_content),
            web_url=body.get("webUrl"),
        )


# ───────────────────────────────────────────────────────────────────────
# PR2: Author / Upload / Share Tools
# ───────────────────────────────────────────────────────────────────────


# Implementation template for write_text_file


async def write_text_file(
    target: OneDriveTarget | SharePointTarget,
    file_name: str,
    content: str,
    conflict_behavior: ConflictBehavior = "fail",
    token: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FileRef:
    """Write text to a file (create or update per conflict_behavior).

    Args:
        target: OneDrive or SharePoint upload target
        file_name: Name of file to create/overwrite
        content: Text content to write
        conflict_behavior: rename / replace / fail
        token: Optional pre-fetched token; else refreshed
        transport: Optional test transport

    Returns:
        FileRef to the written file

    Raises:
        SiteNotAllowedError: Site on operator denylist
        GraphFilesError: 403 Forbidden, 5xx, or other Graph errors
    """
    if not token:
        raise ValueError("token is required")

    # For SharePoint, check site allowed
    if isinstance(target, SharePointTarget):
        _check_site_allowed(target.site_id)
        drive_id = target.drive_id
        site_id = target.site_id
    else:
        drive_id = None  # Fetch from /me/drive
        site_id = None

    # Resource identifier for audit
    resource = f"{drive_id or 'me/drive'}:{target.folder_path}/{file_name}"

    url = _build_upload_url(target, file_name, conflict_behavior=conflict_behavior)

    async with _audit_graph_call(
        "write_text_file",
        resource,
        metadata={
            "file_name": file_name,
            "conflict_behavior": conflict_behavior,
            "site_id": site_id,
        },
    ):
        async with _client(transport, allow_5xx_retry=False) as client:
            # Write text content as UTF-8 bytes
            resp = await client.put(
                url,
                content=content.encode("utf-8"),
                headers=_bearer(token),
            )

        if resp.status_code not in (200, 201):
            _raise_for_files_error(resp, target=resource, scope="Files.ReadWrite")

        body = resp.json()
        return FileRef(
            drive_id=str(body.get("parentReference", {}).get("driveId") or ""),
            item_id=str(body.get("id") or ""),
            name=str(body.get("name") or file_name),
            mime_type=str(body.get("file", {}).get("mimeType") or "text/plain"),
            kind="sharepoint" if site_id else "onedrive_business",
            site_id=site_id,
            web_url=body.get("webUrl"),
            size_bytes=int(body.get("size") or 0),
        )


async def upload_file(
    target: OneDriveTarget | SharePointTarget,
    file_name: str,
    content_bytes: bytes,
    conflict_behavior: ConflictBehavior = "fail",
    token: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FileRef:
    """Upload binary file with automatic chunking for large files.

    Args:
        target: OneDrive or SharePoint upload target
        file_name: Name of file to create/upload
        content_bytes: File content bytes
        conflict_behavior: rename / replace / fail
        token: Optional pre-fetched token; else refreshed
        transport: Optional test transport

    Returns:
        FileRef to the uploaded file

    Raises:
        SiteNotAllowedError: Site on operator denylist
        GraphFilesError: 403 Forbidden, 429 throttle, 5xx, or other errors
    """
    if not token:
        raise ValueError("token is required")

    # Check site allowed (SharePoint only)
    if isinstance(target, SharePointTarget):
        _check_site_allowed(target.site_id)
        drive_id = target.drive_id
        site_id = target.site_id
    else:
        drive_id = None
        site_id = None

    resource = f"{drive_id or 'me/drive'}:{target.folder_path}/{file_name}"

    # Decide single-PUT vs chunked based on file size
    CHUNK_SIZE = 5 * 1024 * 1024  # 5 MiB per chunk
    use_chunked = len(content_bytes) >= CHUNK_SIZE

    if not use_chunked:
        url = _build_upload_url(target, file_name, conflict_behavior=conflict_behavior)

        async with _audit_graph_call(
            "upload_file",
            resource,
            metadata={
                "file_name": file_name,
                "conflict_behavior": conflict_behavior,
                "site_id": site_id,
                "size_bytes": len(content_bytes),
            },
        ):
            async with _client(transport, allow_5xx_retry=False) as client:
                resp = await client.put(
                    url,
                    content=content_bytes,
                    headers=_bearer(token),
                )

            if resp.status_code not in (200, 201):
                _raise_for_files_error(resp, target=resource, scope="Files.ReadWrite")

            body = resp.json()
            return FileRef(
                drive_id=str(body.get("parentReference", {}).get("driveId") or ""),
                item_id=str(body.get("id") or ""),
                name=str(body.get("name") or file_name),
                mime_type=str(body.get("file", {}).get("mimeType") or "application/octet-stream"),
                kind="sharepoint" if site_id else "onedrive_business",
                site_id=site_id,
                web_url=body.get("webUrl"),
                size_bytes=int(body.get("size") or len(content_bytes)),
            )
    else:
        # Large file: createUploadSession + chunked upload
        return await _upload_chunked_session(
            target=target,
            file_name=file_name,
            content_bytes=content_bytes,
            conflict_behavior=conflict_behavior,
            token=token,
            transport=transport,
            resource=resource,
            site_id=site_id,
        )


async def _upload_chunked_session(
    target: OneDriveTarget | SharePointTarget,
    file_name: str,
    content_bytes: bytes,
    conflict_behavior: ConflictBehavior,
    token: str,
    transport: httpx.AsyncBaseTransport | None,
    resource: str,
    site_id: str | None,
) -> FileRef:
    """Chunked upload via createUploadSession + resumable PUT."""
    create_url = _build_upload_url(
        target,
        file_name,
        conflict_behavior=conflict_behavior,
        endpoint="createUploadSession",
    )

    payload = {
        "item": {
            "@microsoft.graph.conflictBehavior": conflict_behavior,
        }
    }

    async with _client(transport, allow_5xx_retry=False) as client:
        resp = await client.post(
            create_url,
            json=payload,
            headers={**_bearer(token), "Content-Type": "application/json"},
        )

    if resp.status_code != 200:
        _raise_for_files_error(resp, target=resource, scope="Files.ReadWrite")

    session_data = resp.json()
    upload_url = session_data.get("uploadUrl")
    if not upload_url:
        raise GraphFilesError(500, "No uploadUrl in createUploadSession response")

    # Upload chunks
    CHUNK_SIZE = 5 * 1024 * 1024
    total_bytes = len(content_bytes)

    async with _audit_graph_call(
        "upload_file",
        resource,
        metadata={
            "file_name": file_name,
            "size_bytes": total_bytes,
            "chunked": True,
        },
    ):
        offset = 0
        while offset < total_bytes:
            chunk_end = min(offset + CHUNK_SIZE, total_bytes)
            chunk = content_bytes[offset:chunk_end]
            is_last = chunk_end == total_bytes

            content_range = f"bytes {offset}-{chunk_end - 1}/{total_bytes}"

            retry_count = 0
            max_retries = 3

            while retry_count < max_retries:
                async with _client(transport, allow_5xx_retry=False) as client:
                    resp = await client.put(
                        upload_url,
                        content=chunk,
                        headers={
                            **_bearer(token),
                            "Content-Length": str(len(chunk)),
                            "Content-Range": content_range,
                        },
                    )

                # 200/201 (intermediate/final success)
                if resp.status_code in (200, 201):
                    if is_last:
                        body = resp.json()
                        return FileRef(
                            drive_id=str(body.get("parentReference", {}).get("driveId") or ""),
                            item_id=str(body.get("id") or ""),
                            name=str(body.get("name") or file_name),
                            mime_type=str(
                                body.get("file", {}).get("mimeType") or "application/octet-stream"
                            ),
                            kind="sharepoint" if site_id else "onedrive_business",
                            site_id=site_id,
                            web_url=body.get("webUrl"),
                            size_bytes=int(body.get("size") or total_bytes),
                        )
                    else:
                        # Intermediate chunk accepted; move to next
                        offset = chunk_end
                        break

                # 5xx or 429: retry with exponential backoff
                elif resp.status_code in (503, 504, 502, 429):
                    retry_count += 1
                    if retry_count >= max_retries:
                        _raise_for_files_error(resp, target=resource, scope="Files.ReadWrite")
                    # Exponential backoff: 1s, 2s, 4s
                    import asyncio

                    await asyncio.sleep(2 ** (retry_count - 1))
                    continue

                # Other errors: fail immediately
                else:
                    _raise_for_files_error(resp, target=resource, scope="Files.ReadWrite")

        # Should never reach here
        raise GraphFilesError(500, "Upload loop exited without final response")


async def _get_sponsor_records() -> list[Any]:
    """Fetch the Agent Identity sponsor records (with user_id + emails).

    Two-stage strategy (mirrors the email-allowlist logic from before
    the 2026-04-30 share_file gate inversion):

    **Stage 1:** ``fetch_agent_identity_sponsors`` enumerates sponsor
    user IDs via the Agent Identity FIC token, then enriches each via
    ``/users/{id}`` using the Agent User token. Needs ``User.ReadBasic.All``
    delegated.

    **Stage 2:** When the enrichment hop returns 403 because the scope
    wasn't granted, sponsors come back with only ``user_id`` and empty
    email identifiers. We patch the returned sponsor records by scanning
    chat members of all watched chats — any chat member whose ``user_id``
    matches a sponsor's ``user_id`` contributes their email back to the
    sponsor record.

    Returns the full sponsor records (not just emails) because
    ``share_file`` now needs to match by ``user_id`` for the chat
    membership check, in addition to email-based authorization.
    """
    import asyncio
    import logging

    from entrabot.config import get_config
    from entrabot.identity.sponsors import (
        AgentIdentitySponsor,
        fetch_agent_identity_sponsors,
        fetch_watched_chat_members,
    )
    from entrabot.tools.teams import acquire_agent_user_token

    config = get_config()
    sponsors: list[AgentIdentitySponsor] = await asyncio.to_thread(
        fetch_agent_identity_sponsors,
        config,
        user_token_provider=acquire_agent_user_token,
    )

    unenriched_user_ids = {
        s.user_id.lower()
        for s in sponsors
        if s.user_id and not s.email_identifiers()
    }
    if unenriched_user_ids:
        try:
            members = await asyncio.to_thread(fetch_watched_chat_members, config)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "chat-members sponsor email fallback failed: %s", exc
            )
            members = []

        member_email_by_user_id: dict[str, str] = {}
        for member in members:
            mid = (member.get("user_id") or "").strip().lower()
            email = (member.get("email") or "").strip().lower()
            if mid and email and mid in unenriched_user_ids:
                member_email_by_user_id[mid] = email

        # Patch each unenriched sponsor with the chat-members email by
        # rebuilding the dataclass with ``other_mails`` populated.
        if member_email_by_user_id:
            patched: list[AgentIdentitySponsor] = []
            for sponsor in sponsors:
                key = sponsor.user_id.lower()
                fallback_email = member_email_by_user_id.get(key)
                if fallback_email and not sponsor.email_identifiers():
                    patched.append(
                        AgentIdentitySponsor(
                            user_id=sponsor.user_id,
                            user_principal_name=sponsor.user_principal_name,
                            mail=sponsor.mail or fallback_email,
                            other_mails=tuple(
                                sorted(set(sponsor.other_mails) | {fallback_email})
                            ),
                            proxy_addresses=sponsor.proxy_addresses,
                            federated_emails=sponsor.federated_emails,
                        )
                    )
                else:
                    patched.append(sponsor)
            sponsors = patched

    return sponsors


async def _get_sponsor_allowlist() -> set[str]:
    """Return the union of all email-shaped identifiers across sponsors.

    Retained for back-compat with existing callers and tests. Internally
    delegates to ``_get_sponsor_records``.
    """
    sponsors = await _get_sponsor_records()
    canonical: set[str] = set()
    for sponsor in sponsors:
        canonical.update(sponsor.email_identifiers())
    return canonical


async def share_file(
    file_ref: FileRef,
    recipient_email: str,
    *,
    requester_email: str,
    chat_id: str,
    role: ShareRole = "read",
    token: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SharePermission:
    """Share a file. The REQUESTER is sponsor-gated; the recipient is not.

    Authorization model (2026-04-30 inverted gate):
    Only Agent Identity sponsors are authorized to direct the agent to
    share files. A sponsor may share with anyone they choose. The
    recipient is passed straight through to Graph ``/invite``.

    Two checks are enforced before the Graph call:

    1. **Requester is a sponsor.** ``requester_email`` is matched
       against the static sponsor allowlist (Entra-configured sponsors
       on the Agent Identity object). All email forms are accepted —
       UPN, mail, otherMails, proxyAddresses, federated identities, and
       decoded B2B EXT UPN home addresses.

    2. **Requester is a member of ``chat_id``.** The sponsor's
       ``user_id`` MUST appear in the chat's member list. This catches
       an LLM fabricating a sponsor email that doesn't match the
       conversation it's actually in. Both ``requester_email`` and
       ``chat_id`` are required parameters; the LLM cannot bypass the
       chat-context binding by omitting it.

    Args:
        file_ref: ``FileRef`` from ``resolve_file_url`` or ``read_file``.
        recipient_email: Address to share with (any address allowed —
            sponsors may share with non-sponsors).
        requester_email: Email of the human (sponsor) who asked the
            agent to share. Required. The LLM should derive this from
            the active conversation context — never use the agent's own
            address.
        chat_id: Teams chat ID that initiated this share request.
            Required. Used to verify the requester is genuinely in the
            conversation we're acting on.
        role: ``read`` or ``write``.
        token: Optional pre-fetched token; else refreshed.
        transport: Optional test transport for the Graph ``/invite``
            call. Sponsor-record fetching uses its own transport.

    Returns:
        ``SharePermission`` with ``permission_id`` and metadata.

    Raises:
        RequesterNotSponsorError: ``requester_email`` not in sponsor allowlist.
        RequesterNotInChatError: requester is a sponsor but not a member of ``chat_id``.
        SiteNotAllowedError: (SharePoint only) site on operator denylist.
        GraphFilesError: 403 Forbidden, 5xx, or other Graph errors.
    """
    if not token:
        raise ValueError("token is required")
    if not requester_email:
        raise ValueError("requester_email is required")
    if not chat_id:
        raise ValueError("chat_id is required")

    if file_ref.site_id:
        _check_site_allowed(file_ref.site_id)

    resource = f"{file_ref.drive_id}:{file_ref.item_id}"
    audit_metadata: dict = {
        "requester_email": requester_email,
        "chat_id": chat_id,
        "supplied_chat_id": chat_id,
        "bound_chat_id": "",
        "recipient_email": recipient_email,
        "role": role,
        "site_id": file_ref.site_id,
    }

    # Audit-first ordering: every share_file invocation — including
    # gate rejections — emits a pending + (success|failure) audit pair.
    # Before this refactor only Graph /invite failures were audited;
    # Gate 1 / Gate 2 rejections were security-invisible (audit-first prep).
    async with _audit_graph_call(
        "share_file",
        resource,
        metadata=audit_metadata,
    ):
        # Gate 1: requester must be in the static sponsor allowlist.
        sponsors = await _get_sponsor_records()
        requester_lower = requester_email.strip().lower()
        matched_sponsor = next(
            (
                s
                for s in sponsors
                if any(email.lower() == requester_lower for email in s.email_identifiers())
            ),
            None,
        )
        if matched_sponsor is None or not matched_sponsor.user_id:
            raise RequesterNotSponsorError(requester=requester_email)

        # Gate 3 (authorization fix): matched sponsor must be actively engaged in
        # the cited chat — the server must have successfully pushed a
        # recent (within TTL) inbound message from this sponsor in this
        # chat. Defends Chain A confused-deputy: attacker in chat A
        # cannot get the agent to share a file in chat B's authority
        # context even when the sponsor is a genuine member of B.
        # See docs/runbooks/hard-won-learnings.md Learning #67.
        from entrabot.identity.active_channel import get_bindings

        binding = get_bindings().lookup(matched_sponsor.user_id)
        if binding is None:
            raise NoActiveSponsorChannelError(
                sponsor_user_id=matched_sponsor.user_id,
                chat_id=chat_id,
            )
        # Surface the bound chat in the audit metadata for forensic
        # visibility on the success path. (Failure path: the raised
        # exception's repr already includes both chat_ids.)
        audit_metadata["bound_chat_id"] = binding.chat_id
        if binding.chat_id != chat_id:
            raise SponsorChannelMismatchError(
                sponsor_user_id=matched_sponsor.user_id,
                supplied_chat_id=chat_id,
                bound_chat_id=binding.chat_id,
            )

        # Gate 2 (defense-in-depth): matched sponsor must be a member of the cited chat.
        import asyncio

        from entrabot.config import get_config
        from entrabot.identity.sponsors import fetch_chat_members

        members = await asyncio.to_thread(fetch_chat_members, get_config(), chat_id)
        matched_user_id = matched_sponsor.user_id.lower()
        if not any(
            (m.get("user_id") or "").strip().lower() == matched_user_id for m in members
        ):
            raise RequesterNotInChatError(requester=requester_email, chat_id=chat_id)

        request_url = f"{GRAPH_V1_HOST}/drives/{file_ref.drive_id}/items/{file_ref.item_id}/invite"

        payload = {
            "recipients": [
                {
                    "email": recipient_email,
                }
            ],
            "roles": [role],
            "requireSignIn": True,
            # sendInvitation=True is required for cross-MySite shares: without
            # it Graph creates the permission record but never registers the
            # recipient in the target SharePoint site's user list, so the
            # recipient gets a 500 "something went wrong" page when opening
            # the doc, and the share never surfaces in their Outlook /
            # OneDrive "shared with me" view (because no invitation email
            # is sent). Verified live 2026-05-04 against an agent-owned
            # ODB doc shared to a tenant user — without sendInvitation,
            # SP returned a "SharePoint Foundation" 500 server error;
            # with sendInvitation, the share appears and opens normally.
            "sendInvitation": True,
        }

        async with _client(transport, allow_5xx_retry=False) as client:
            resp = await client.post(
                request_url,
                json=payload,
                headers={**_bearer(token), "Content-Type": "application/json"},
            )

        if resp.status_code not in (200, 201):
            _raise_for_files_error(resp, target=resource, scope="Files.ReadWrite")

        body = resp.json()

        # POST /invite returns array of permission objects
        perms = body.get("value", [])
        if perms:
            perm = perms[0]
            return SharePermission(
                permission_id=str(perm.get("id") or ""),
                role=role,
                recipient_email=recipient_email,
                web_url=perm.get("webUrl"),
                expiration_at=perm.get("expirationDateTime"),
            )
        else:
            raise GraphFilesError(500, "No permission returned from invite endpoint")


__all__ = [
    "GRAPH_V1_HOST",
    "GRAPH_BETA_HOST",
    "DEFAULT_MAX_PDF_BYTES",
    "DEFAULT_MAX_TEXT_BYTES",
    "OneDriveTarget",
    "SharePointTarget",
    "SharePermission",
    "FileRef",
    "FileSummary",
    "RecentFilesPage",
    "FileContent",
    "CommentResult",
    "resolve_file_url",
    "list_recent_files",
    "read_file",
    "add_file_comment",
    "write_text_file",
    "upload_file",
    "share_file",
]
