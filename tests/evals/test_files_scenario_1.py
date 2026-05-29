"""Scenario 1 (read + comment) eval suite — 5 traces.

Each test loads a fixture, replays the tool sequence we expect the
LLM to use for that scenario, and rubric-grades the result. Pass
condition: every rubric dimension is True.

These tests are marked ``eval`` so CI can run them as a separate gate
(``pytest -m eval``). The plan locks PR1 merge on this gate going green.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from entrabot.errors import FilesError
from entrabot.tools.files import (
    FileRef,
    add_file_comment,
    list_recent_files,
    read_file,
    resolve_file_url,
)
from tests.evals.rubric import score_trace

TOKEN = "eval-token"


pytestmark = pytest.mark.eval


@respx.mock
@pytest.mark.asyncio
async def test_happy_path_resolve_read_comment(load_trace, install_responses):
    trace, responses = load_trace("scenario1_happy_path")
    install_responses(responses)

    # Stub pypdf so we don't depend on a real PDF body.
    actual_tools: list[str] = []
    error: Exception | None = None
    final: dict | None = None

    try:
        actual_tools.append("resolve_file_url")
        ref = await resolve_file_url(
            trace["tool_args"]["resolve_file_url"]["url"],
            token=TOKEN,
            transport=httpx.AsyncHTTPTransport(),
        )

        actual_tools.append("read_file")

        # The fixture returns a stub PDF body; pypdf would fail. Patch
        # the extractor for the test scope (acceptable: PR1's eval is
        # grading wire shape, not pypdf).
        from entrabot.tools import files as files_mod

        original = files_mod._extract_pdf_text
        files_mod._extract_pdf_text = lambda data: ("Stub spec body.", 1)
        try:
            await read_file(
                ref,
                token=TOKEN,
                transport=httpx.AsyncHTTPTransport(),
            )
        finally:
            files_mod._extract_pdf_text = original

        actual_tools.append("add_file_comment")
        result = await add_file_comment(
            ref,
            trace["tool_args"]["add_file_comment"]["content"],
            token=TOKEN,
            transport=httpx.AsyncHTTPTransport(),
        )
        final = {"comment_id": result.comment_id, "content": result.content}
    except FilesError as exc:
        error = exc

    score = score_trace(
        trace_name=trace["name"],
        expected_tools=trace["expected_tools"],
        actual_tools=actual_tools,
        expected_output_subset=trace.get("expected_output_subset"),
        actual_output=final,
        error=error,
    )
    score.assert_passed()


@respx.mock
@pytest.mark.asyncio
async def test_md_file_no_comment(load_trace, install_responses):
    trace, responses = load_trace("scenario1_md_file")
    install_responses(responses)

    actual_tools: list[str] = []
    error: Exception | None = None
    final: dict | None = None

    try:
        actual_tools.append("resolve_file_url")
        ref = await resolve_file_url(
            trace["tool_args"]["resolve_file_url"]["url"],
            token=TOKEN,
            transport=httpx.AsyncHTTPTransport(),
        )
        actual_tools.append("read_file")
        content = await read_file(ref, token=TOKEN, transport=httpx.AsyncHTTPTransport())
        final = {
            "name": content.name,
            "page_count": content.page_count,
            "truncated": content.truncated,
        }
    except FilesError as exc:
        error = exc

    score = score_trace(
        trace_name=trace["name"],
        expected_tools=trace["expected_tools"],
        actual_tools=actual_tools,
        expected_output_subset=trace.get("expected_output_subset"),
        actual_output=final,
        error=error,
    )
    score.assert_passed()


@respx.mock
@pytest.mark.asyncio
async def test_denied_site_blocks_resolve(
    load_trace, install_responses, monkeypatch: pytest.MonkeyPatch
):
    trace, responses = load_trace("scenario1_denied_site")
    monkeypatch.setenv("ENTRABOT_FILES_DENIED_SITES", trace["denied_sites"])
    install_responses(responses)

    actual_tools: list[str] = []
    error: Exception | None = None

    try:
        actual_tools.append("resolve_file_url")
        await resolve_file_url(
            trace["tool_args"]["resolve_file_url"]["url"],
            token=TOKEN,
            transport=httpx.AsyncHTTPTransport(),
        )
    except FilesError as exc:
        error = exc

    score = score_trace(
        trace_name=trace["name"],
        expected_tools=trace["expected_tools"],
        actual_tools=actual_tools,
        expected_output_subset=None,
        actual_output=None,
        error=None,
    )
    # For an expected-error trace, the rubric inverts: we want the
    # *expected* error type and no extra tool calls.
    assert error is not None
    assert type(error).__name__ == trace["expected_error"]
    score.assert_passed()


@respx.mock
@pytest.mark.asyncio
async def test_pptx_rejects_comment(load_trace, install_responses):
    trace, responses = load_trace("scenario1_pptx_reject")
    install_responses(responses)

    actual_tools: list[str] = []
    error: Exception | None = None

    try:
        actual_tools.append("resolve_file_url")
        ref = await resolve_file_url(
            trace["tool_args"]["resolve_file_url"]["url"],
            token=TOKEN,
            transport=httpx.AsyncHTTPTransport(),
        )
        actual_tools.append("add_file_comment")
        await add_file_comment(
            ref,
            trace["tool_args"]["add_file_comment"]["content"],
            token=TOKEN,
            transport=httpx.AsyncHTTPTransport(),
        )
    except FilesError as exc:
        error = exc

    score = score_trace(
        trace_name=trace["name"],
        expected_tools=trace["expected_tools"],
        actual_tools=actual_tools,
        expected_output_subset=None,
        actual_output=None,
        error=None,
    )
    assert error is not None
    assert type(error).__name__ == trace["expected_error"]
    score.assert_passed()


@respx.mock
@pytest.mark.asyncio
async def test_list_then_read(load_trace, install_responses):
    trace, responses = load_trace("scenario1_list_then_read")
    install_responses(responses)

    actual_tools: list[str] = []
    error: Exception | None = None
    final: dict | None = None

    try:
        actual_tools.append("list_recent_files")
        page = await list_recent_files(
            limit=trace["tool_args"]["list_recent_files"]["limit"],
            token=TOKEN,
            transport=httpx.AsyncHTTPTransport(),
        )
        first = page.files[0]
        ref = FileRef(
            drive_id=first.drive_id,
            item_id=first.item_id,
            name=first.name,
            mime_type=first.mime_type,
            kind="sharepoint",
            site_id=first.site_id,
            web_url=first.web_url,
            size_bytes=first.size_bytes,
        )

        actual_tools.append("read_file")
        content = await read_file(ref, token=TOKEN, transport=httpx.AsyncHTTPTransport())
        final = {
            "name": content.name,
            "page_count": content.page_count,
            "truncated": content.truncated,
        }
    except FilesError as exc:
        error = exc

    score = score_trace(
        trace_name=trace["name"],
        expected_tools=trace["expected_tools"],
        actual_tools=actual_tools,
        expected_output_subset=trace.get("expected_output_subset"),
        actual_output=final,
        error=error,
    )
    score.assert_passed()
