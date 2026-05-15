from __future__ import annotations

import httpx
import pytest
import respx

from entraclaw.a365.errors import A365ConsentMissingError, A365McpCallError
from entraclaw.a365.mcp_client import WorkIqMcpClient


@pytest.mark.asyncio
@respx.mock
async def test_call_tool_posts_mcp_request() -> None:
    route = respx.post("https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer").mock(
        return_value=httpx.Response(200, json={"result": {"content": "ok"}})
    )
    client = WorkIqMcpClient(transport=httpx.AsyncHTTPTransport())

    result = await client.call_tool(
        endpoint="https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer",
        server_name="mcp_WordServer",
        tool_name="WordGetDocumentContent",
        arguments={"url": "https://contoso.sharepoint.com/doc.docx"},
        token="token-123",
    )

    assert result == {"content": "ok"}
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer token-123"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept"] == "application/json, text/event-stream"
    assert b"WordGetDocumentContent" in request.content


@pytest.mark.asyncio
@respx.mock
async def test_call_tool_accepts_sse_message_response() -> None:
    respx.post("https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text='event: message\ndata: {"result": {"content": "ok"}}\n\n',
        )
    )
    client = WorkIqMcpClient(transport=httpx.AsyncHTTPTransport())

    result = await client.call_tool(
        endpoint="https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer",
        server_name="mcp_WordServer",
        tool_name="GetDocumentContent",
        arguments={"url": "https://contoso.sharepoint.com/doc.docx"},
        token="token-123",
    )

    assert result == {"content": "ok"}


@pytest.mark.asyncio
@respx.mock
async def test_call_tool_unwraps_mcp_text_json_content() -> None:
    respx.post("https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'event: message\n'
                'data: {"result": {"content": [{"type": "text", "text": '
                '"{\\"driveId\\":\\"drive-1\\",\\"documentId\\":\\"doc-1\\"}"}], '
                '"isError": false}}\n\n'
            ),
        )
    )
    client = WorkIqMcpClient(transport=httpx.AsyncHTTPTransport())

    result = await client.call_tool(
        endpoint="https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer",
        server_name="mcp_WordServer",
        tool_name="GetDocumentContent",
        arguments={"url": "https://contoso.sharepoint.com/doc.docx"},
        token="token-123",
    )

    assert result == {"driveId": "drive-1", "documentId": "doc-1"}


@pytest.mark.asyncio
@respx.mock
async def test_call_tool_403_maps_to_consent_missing() -> None:
    respx.post("https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer").mock(
        return_value=httpx.Response(403, json={"error": {"message": "missing consent"}})
    )
    client = WorkIqMcpClient(transport=httpx.AsyncHTTPTransport())

    with pytest.raises(A365ConsentMissingError):
        await client.call_tool(
            endpoint="https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer",
            server_name="mcp_WordServer",
            tool_name="WordReplyToComment",
            arguments={"commentId": "c1"},
            token="token-123",
            scope="McpServers.Word.All",
        )


@pytest.mark.asyncio
@respx.mock
async def test_call_tool_http_error_does_not_leak_response_body() -> None:
    respx.post("https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer").mock(
        return_value=httpx.Response(500, text="upstream echoed Authorization: Bearer token-123")
    )
    client = WorkIqMcpClient(transport=httpx.AsyncHTTPTransport())

    with pytest.raises(A365McpCallError) as exc_info:
        await client.call_tool(
            endpoint="https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer",
            server_name="mcp_WordServer",
            tool_name="WordReplyToComment",
            arguments={"commentId": "c1"},
            token="token-123",
        )

    message = str(exc_info.value)
    assert "HTTP 500" in message
    assert "token-123" not in message
    assert "upstream echoed Authorization" not in message


@pytest.mark.asyncio
@respx.mock
async def test_call_tool_non_json_success_raises_safe_error() -> None:
    respx.post("https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer").mock(
        return_value=httpx.Response(200, text="<html>not json token-123</html>")
    )
    client = WorkIqMcpClient(transport=httpx.AsyncHTTPTransport())

    with pytest.raises(A365McpCallError) as exc_info:
        await client.call_tool(
            endpoint="https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer",
            server_name="mcp_WordServer",
            tool_name="WordReplyToComment",
            arguments={"commentId": "c1"},
            token="token-123",
        )

    message = str(exc_info.value)
    assert "non-JSON" in message
    assert "HTTP 200" in message
    assert "token-123" not in message
    assert "<html>not json" not in message


@pytest.mark.asyncio
@respx.mock
async def test_call_tool_malformed_response_raises() -> None:
    respx.post("https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer").mock(
        return_value=httpx.Response(200, json={"unexpected": True})
    )
    client = WorkIqMcpClient(transport=httpx.AsyncHTTPTransport())

    with pytest.raises(A365McpCallError) as exc_info:
        await client.call_tool(
            endpoint="https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer",
            server_name="mcp_WordServer",
            tool_name="WordReplyToComment",
            arguments={"commentId": "c1"},
            token="token-123",
        )

    assert "malformed" in str(exc_info.value)
