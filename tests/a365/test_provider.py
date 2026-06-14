from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from entrabot.a365.errors import A365TokenError
from entrabot.a365.provider import WorkIqProvider
from entrabot.a365.tokens import WorkIqTokenRequest


class FakeMcpClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.events: list[str] | None = None
        self.exception: Exception | None = None

    async def call_tool(self, **kwargs: Any) -> dict[str, Any]:
        if self.events is not None:
            self.events.append("mcp")
        self.calls.append(kwargs)
        if self.exception is not None:
            raise self.exception
        return {"ok": True}


@dataclass
class RecordingA365TokenProvider:
    token: str
    requests: list[WorkIqTokenRequest] = field(default_factory=list)

    async def get_token(self, request: WorkIqTokenRequest) -> str:
        self.requests.append(request)
        return self.token


class FailingA365TokenProvider:
    async def get_token(self, request: WorkIqTokenRequest) -> str:
        raise A365TokenError(request.audience, "no token")


def _manifest(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "mcpServers": [
                    {
                        "mcpServerName": "mcp_WordServer",
                        "mcpServerUniqueName": "mcp_WordServer",
                        "url": "https://example.test/workiq/word",
                        "scope": "Tools.ListInvoke.All",
                        "audience": "c2d0c2b6-8013-4346-9f8b-b81d3b754a29",
                    }
                ]
            }
        )
    )
    return path


def _provider(
    manifest_path: Path,
    mcp_client: FakeMcpClient,
) -> WorkIqProvider:
    return WorkIqProvider(
        manifest_path=manifest_path,
        token_provider=RecordingA365TokenProvider("token-123"),
        mcp_client=mcp_client,
    )


@pytest.mark.asyncio
async def test_provider_uses_manifest_audience_scope_and_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp_client = FakeMcpClient()
    token_provider = RecordingA365TokenProvider("token-123")
    monkeypatch.setattr(
        "entrabot.a365.provider.log_event",
        lambda **kwargs: kwargs,
    )
    provider = WorkIqProvider(
        manifest_path=_manifest(tmp_path / "ToolingManifest.json"),
        token_provider=token_provider,
        mcp_client=mcp_client,
    )

    result = await provider.call_tool(
        server_name="mcp_WordServer",
        tool_name="WordReplyToComment",
        arguments={"commentId": "c1"},
    )

    assert result == {"ok": True}
    assert token_provider.requests == [
        WorkIqTokenRequest(
            server_name="mcp_WordServer",
            audience="c2d0c2b6-8013-4346-9f8b-b81d3b754a29",
            scope="Tools.ListInvoke.All",
        )
    ]
    assert mcp_client.calls == [
        {
            "endpoint": "https://example.test/workiq/word",
            "server_name": "mcp_WordServer",
            "tool_name": "WordReplyToComment",
            "arguments": {"commentId": "c1"},
            "token": "token-123",
            "scope": "Tools.ListInvoke.All",
        }
    ]


@pytest.mark.asyncio
async def test_call_tool_audits_pending_before_mcp_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    audit_events: list[dict[str, Any]] = []
    mcp_client = FakeMcpClient()
    mcp_client.events = events

    def record_audit(**kwargs: Any) -> dict[str, Any]:
        events.append(f"audit:{kwargs['outcome']}")
        audit_events.append(kwargs)
        return kwargs

    monkeypatch.setattr("entrabot.a365.provider.log_event", record_audit, raising=False)

    await _provider(_manifest(tmp_path / "ToolingManifest.json"), mcp_client).call_tool(
        server_name="mcp_WordServer",
        tool_name="WordReplyToComment",
        arguments={"commentId": "c1"},
    )

    assert events[:2] == ["audit:pending", "mcp"]
    assert audit_events[0]["action"] == "a365.mcp_WordServer.WordReplyToComment"
    # Resource is always action-shaped — never derived from arguments —
    # so CodeQL can't trace LLM-controlled args into the audit sink and
    # operators see a stable handle they can grep on.
    assert audit_events[0]["resource"] == "a365.mcp_WordServer.WordReplyToComment"
    assert audit_events[0]["attribution_type"] == "agent"


@pytest.mark.asyncio
async def test_call_tool_audits_success_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_events: list[dict[str, Any]] = []

    def record_audit(**kwargs: Any) -> dict[str, Any]:
        audit_events.append(kwargs)
        return kwargs

    monkeypatch.setattr("entrabot.a365.provider.log_event", record_audit, raising=False)

    result = await _provider(
        _manifest(tmp_path / "ToolingManifest.json"),
        FakeMcpClient(),
    ).call_tool(
        server_name="mcp_WordServer",
        tool_name="WordReplyToComment",
        arguments={"commentId": "c1"},
    )

    assert result == {"ok": True}
    assert [event["outcome"] for event in audit_events] == ["pending", "success"]


@pytest.mark.asyncio
async def test_call_tool_audits_failure_and_propagates_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_events: list[dict[str, Any]] = []
    mcp_client = FakeMcpClient()
    mcp_client.exception = RuntimeError("work iq failed")

    def record_audit(**kwargs: Any) -> dict[str, Any]:
        audit_events.append(kwargs)
        return kwargs

    monkeypatch.setattr("entrabot.a365.provider.log_event", record_audit, raising=False)

    with pytest.raises(RuntimeError, match="work iq failed"):
        await _provider(_manifest(tmp_path / "ToolingManifest.json"), mcp_client).call_tool(
            server_name="mcp_WordServer",
            tool_name="WordReplyToComment",
            arguments={"commentId": "c1"},
        )

    assert [event["outcome"] for event in audit_events] == ["pending", "failure"]


@pytest.mark.asyncio
async def test_call_tool_audits_failure_when_token_acquisition_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_events: list[dict[str, Any]] = []
    mcp_client = FakeMcpClient()

    def record_audit(**kwargs: Any) -> dict[str, Any]:
        audit_events.append(kwargs)
        return kwargs

    monkeypatch.setattr("entrabot.a365.provider.log_event", record_audit, raising=False)
    provider = WorkIqProvider(
        manifest_path=_manifest(tmp_path / "ToolingManifest.json"),
        token_provider=FailingA365TokenProvider(),
        mcp_client=mcp_client,
    )

    with pytest.raises(A365TokenError, match="no token"):
        await provider.call_tool(
            server_name="mcp_WordServer",
            tool_name="WordReplyToComment",
            arguments={"commentId": "c1"},
        )

    assert [event["outcome"] for event in audit_events] == ["pending", "failure"]
    assert mcp_client.calls == []


@pytest.mark.asyncio
async def test_call_tool_audit_metadata_records_argument_keys_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_sentinel = "SECRET-SENTINEL-VALUE"
    audit_events: list[dict[str, Any]] = []

    def record_audit(**kwargs: Any) -> dict[str, Any]:
        audit_events.append(kwargs)
        return kwargs

    monkeypatch.setattr("entrabot.a365.provider.log_event", record_audit, raising=False)

    await _provider(_manifest(tmp_path / "ToolingManifest.json"), FakeMcpClient()).call_tool(
        server_name="mcp_WordServer",
        tool_name="CreateDocument",
        arguments={"fileName": "plan.docx", "contentInHtml": secret_sentinel},
    )

    assert [event["outcome"] for event in audit_events] == ["pending", "success"]
    # User-supplied file names / URLs / paths must NEVER appear in the
    # audit resource string — CodeQL flags them as clear-text logging
    # of sensitive data. Only ID-shape values (driveId, itemId, etc.)
    # are eligible; when none are present we fall back to a stable
    # "{server}.{tool}" handle.
    assert audit_events[0]["resource"] == "a365.mcp_WordServer.CreateDocument"
    for event in audit_events:
        assert event["metadata"] == {
            "server": "mcp_WordServer",
            "tool": "CreateDocument",
            "args_keys": ["fileName", "contentInHtml"],
        }
        assert secret_sentinel not in repr(event)
        # Defence in depth: assert the file name itself is also absent
        # from the entire event (it can leak via resource OR metadata).
        assert "plan.docx" not in repr(event)


@pytest.mark.asyncio
async def test_call_tool_fails_closed_when_audit_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from entrabot.errors import InsecureKeyringBackendError

    mcp_client = FakeMcpClient()

    def raise_insecure(**_kwargs: Any) -> None:
        raise InsecureKeyringBackendError(
            "keyrings.alt.file.PlaintextKeyring",
            ("keyring.backends.macOS.Keyring",),
        )

    monkeypatch.setattr("entrabot.a365.provider.log_event", raise_insecure, raising=False)

    with pytest.raises(InsecureKeyringBackendError):
        await _provider(_manifest(tmp_path / "ToolingManifest.json"), mcp_client).call_tool(
            server_name="mcp_WordServer",
            tool_name="CreateDocument",
            arguments={"fileName": "plan.docx"},
        )

    assert mcp_client.calls == []


@pytest.mark.asyncio
async def test_url_and_path_args_never_leak_into_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-supplied URLs / paths / file names must never appear in the audit
    resource string or metadata. CodeQL flags them as clear-text logging of
    sensitive data (data-flow source = LLM-controlled tool arg, sink = audit
    file write + logger.info). Only ID-shape values (driveId, itemId, etc.)
    are eligible to surface in the resource handle.
    """
    audit_events: list[dict[str, Any]] = []

    def record_audit(**kwargs: Any) -> dict[str, Any]:
        audit_events.append(kwargs)
        return kwargs

    monkeypatch.setattr("entrabot.a365.provider.log_event", record_audit, raising=False)

    user_url = "https://contoso.sharepoint.com/sites/Secret/Documents/Plan.docx"
    user_path = "/Documents/Q4/Plan.docx"
    user_filename = "Q4-strategy.docx"

    await _provider(_manifest(tmp_path / "ToolingManifest.json"), FakeMcpClient()).call_tool(
        server_name="mcp_WordServer",
        tool_name="GetDocumentMetadata",
        arguments={
            "fileOrFolderUrl": user_url,
            "url": user_url,
            "webUrl": user_url,
            "filePath": user_path,
            "path": user_path,
            "fileName": user_filename,
        },
    )

    assert audit_events, "expected at least one audit event"
    for event in audit_events:
        rendered = repr(event)
        for leak in (user_url, user_path, user_filename, "contoso", "Plan.docx", "Q4-strategy"):
            assert leak not in rendered, f"audit event leaked user data: {leak!r}"
        # Resource must fall back to the stable {server}.{tool} handle
        # when no ID-shape key is present in arguments.
        assert event["resource"] == "a365.mcp_WordServer.GetDocumentMetadata"


@pytest.mark.asyncio
async def test_id_shape_args_do_not_surface_in_audit_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even ID-shape values (driveId, itemId, commentId) are NOT surfaced
    in the audit resource. CodeQL's taint analysis treats every entry in
    `arguments` (an MCP tool args dict) as potentially-sensitive, so any
    flow from `arguments` into log_event re-triggers the alert. The
    correct fix is to never let `arguments` data reach log_event at all —
    operators correlate audit entries by action + timestamp; deeper
    detail lives in the Graph API server-side logs.
    """
    audit_events: list[dict[str, Any]] = []

    def record_audit(**kwargs: Any) -> dict[str, Any]:
        audit_events.append(kwargs)
        return kwargs

    monkeypatch.setattr("entrabot.a365.provider.log_event", record_audit, raising=False)

    await _provider(_manifest(tmp_path / "ToolingManifest.json"), FakeMcpClient()).call_tool(
        server_name="mcp_WordServer",
        tool_name="WordReplyToComment",
        arguments={
            "driveId": "b!1234",
            "itemId": "01ABCDEF",
            "commentId": "c1",
        },
    )

    resource = audit_events[0]["resource"]
    # Resource is action-shaped only — no argument values leak in.
    assert resource == "a365.mcp_WordServer.WordReplyToComment"
    for leak in ("b!1234", "01ABCDEF", "c1"):
        assert leak not in repr(audit_events[0])
