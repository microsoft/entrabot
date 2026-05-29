"""Shared factories for ``tests/tools`` (eng-review T5).

Tests should construct ``FileRef`` / ``FileSummary`` / ``FileContent``
through these factories instead of repeating literals — when the
dataclass shape grows a field, a single fixture-file edit covers
every test.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from entrabot.tools.files import (
    CommentResult,
    FileContent,
    FileRef,
    FileSummary,
    RecentFilesPage,
)


@pytest.fixture
def make_file_ref() -> Callable[..., FileRef]:
    """Construct a ``FileRef`` with sensible defaults overridable per call."""

    def _make(
        *,
        drive_id: str = "drive-1",
        item_id: str = "item-1",
        name: str = "spec.docx",
        mime_type: str = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        kind: str = "sharepoint",
        site_id: str | None = "tenant.sharepoint.com,site-guid,web-guid",
        web_url: str | None = "https://tenant.sharepoint.com/sites/foo/spec.docx",
        size_bytes: int | None = 12_345,
    ) -> FileRef:
        return FileRef(
            drive_id=drive_id,
            item_id=item_id,
            name=name,
            mime_type=mime_type,
            kind=kind,  # type: ignore[arg-type]
            site_id=site_id,
            web_url=web_url,
            size_bytes=size_bytes,
        )

    return _make


@pytest.fixture
def make_file_summary() -> Callable[..., FileSummary]:
    """Construct a ``FileSummary`` row for ``list_recent_files`` tests."""

    def _make(
        *,
        drive_id: str = "drive-1",
        item_id: str = "item-1",
        name: str = "spec.docx",
        web_url: str = "https://tenant.sharepoint.com/sites/foo/spec.docx",
        mime_type: str = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        size_bytes: int = 12_345,
        modified_at: str = "2026-01-15T12:00:00Z",
        shared_by: str | None = "Test User",
        site_id: str | None = "tenant.sharepoint.com,site-guid,web-guid",
    ) -> FileSummary:
        return FileSummary(
            drive_id=drive_id,
            item_id=item_id,
            name=name,
            web_url=web_url,
            mime_type=mime_type,
            size_bytes=size_bytes,
            modified_at=modified_at,
            shared_by=shared_by,
            site_id=site_id,
        )

    return _make


@pytest.fixture
def make_file_content() -> Callable[..., FileContent]:
    """Construct a ``FileContent`` for ``read_file`` tests."""

    def _make(
        *,
        drive_id: str = "drive-1",
        item_id: str = "item-1",
        name: str = "spec.docx",
        mime_type: str = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        text: str = "Hello, spec.",
        page_count: int | None = 1,
        truncated: bool = False,
    ) -> FileContent:
        return FileContent(
            drive_id=drive_id,
            item_id=item_id,
            name=name,
            mime_type=mime_type,
            text=text,
            page_count=page_count,
            truncated=truncated,
        )

    return _make


@pytest.fixture
def make_comment_result() -> Callable[..., CommentResult]:
    """Construct a ``CommentResult`` for ``add_file_comment`` tests."""

    def _make(
        *,
        comment_id: str = "comment-1",
        content: str = "What does 'agentic' mean here?",
        web_url: str | None = None,
    ) -> CommentResult:
        return CommentResult(
            comment_id=comment_id,
            content=content,
            web_url=web_url,
        )

    return _make


@pytest.fixture
def make_recent_files_page() -> Callable[..., RecentFilesPage]:
    """Construct a ``RecentFilesPage`` for ``list_recent_files`` tests."""

    def _make(
        *,
        files: list[FileSummary] | None = None,
        denied_count: int = 0,
    ) -> RecentFilesPage:
        return RecentFilesPage(files=files or [], denied_count=denied_count)

    return _make
