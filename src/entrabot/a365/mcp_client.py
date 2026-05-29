"""Low-level client for Microsoft-hosted Work IQ MCP servers."""

from __future__ import annotations

import json
from typing import Any

import httpx

from entrabot.a365.errors import A365ConsentMissingError, A365McpCallError


def _parse_sse_message(text: str) -> dict[str, Any]:
    """Return the first JSON ``data:`` payload from an MCP SSE response."""
    data_lines: list[str] = []
    for line in text.splitlines():
        if not line:
            if data_lines:
                break
            continue
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())
    if not data_lines:
        raise ValueError("missing SSE data payload")
    parsed = json.loads("\n".join(data_lines))
    if not isinstance(parsed, dict):
        raise ValueError("SSE data payload is not a JSON object")
    return parsed


def _unwrap_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    """Unwrap MCP ``tools/call`` text JSON content when present."""
    if result.get("isError") is True:
        return result
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return result
    first = content[0]
    if not isinstance(first, dict) or first.get("type") != "text":
        return result
    text = first.get("text")
    if not isinstance(text, str):
        return result
    try:
        parsed = json.loads(text)
    except ValueError:
        return result
    if isinstance(parsed, dict):
        return parsed
    return result


class WorkIqMcpClient:
    """HTTP wrapper for Work IQ MCP tool calls."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout
        self._transport = transport

    async def call_tool(
        self,
        *,
        endpoint: str,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        token: str,
        scope: str = "",
    ) -> dict[str, Any]:
        """Call one tool on a Work IQ MCP server."""
        payload = {
            "jsonrpc": "2.0",
            "id": "entrabot-work-iq-call",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            response = await client.post(
                endpoint,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
            )

        if response.status_code == 401:
            raise A365McpCallError(server_name, tool_name, "token rejected with 401")
        if response.status_code == 403:
            raise A365ConsentMissingError(server_name, scope or "unknown scope")
        if response.status_code >= 400:
            raise A365McpCallError(server_name, tool_name, f"HTTP {response.status_code}")

        try:
            if response.headers.get("content-type", "").lower().startswith("text/event-stream"):
                body = _parse_sse_message(response.text)
            else:
                body = response.json()
        except ValueError:
            raise A365McpCallError(
                server_name,
                tool_name,
                f"non-JSON MCP response (HTTP {response.status_code})",
            ) from None
        if not isinstance(body, dict):
            raise A365McpCallError(
                server_name,
                tool_name,
                "malformed MCP response: non-object body",
            )
        if "error" in body:
            raise A365McpCallError(server_name, tool_name, str(body["error"]))
        result = body.get("result")
        if not isinstance(result, dict):
            raise A365McpCallError(server_name, tool_name, "malformed MCP response: missing result")
        return _unwrap_tool_result(result)
