"""Composable Agent 365 Work IQ provider."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from entrabot.a365.catalog import get_server
from entrabot.a365.manifest import load_manifest
from entrabot.a365.mcp_client import WorkIqMcpClient
from entrabot.a365.tokens import (
    A365TokenProvider,
    EntrabotA365TokenProvider,
    WorkIqTokenRequest,
)
from entrabot.tools.audit import log_event

_SENSITIVE_RESOURCE_KEYS = frozenset(
    {
        "content",
        "contentInHtml",
        "newComment",
        "password",
        "secret",
        "token",
    }
)

_RESOURCE_KEY_PRIORITY = (
    "fileOrFolderUrl",
    "url",
    "webUrl",
    "filePath",
    "path",
    "fileName",
    "driveId",
    "documentLibraryId",
    "documentId",
    "fileId",
    "itemId",
    "commentId",
)


def _safe_resource_parts(arguments: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    for key in _RESOURCE_KEY_PRIORITY:
        # Defense in depth for future priority-list edits: never log sensitive values.
        if key in _SENSITIVE_RESOURCE_KEYS:
            continue
        value = arguments.get(key)
        if isinstance(value, str) and value:
            parts.append(f"{key}={value}")
    return parts


def _audit_resource(
    *,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    parts = _safe_resource_parts(arguments)
    if parts:
        return " ".join(parts)
    return f"{server_name}.{tool_name}"


class WorkIqProvider:
    """High-level boundary for Work IQ MCP calls."""

    def __init__(
        self,
        *,
        manifest_path: Path | None = None,
        token_provider: A365TokenProvider | None = None,
        mcp_client: WorkIqMcpClient | None = None,
    ) -> None:
        self._manifest = load_manifest(manifest_path)
        self._token_provider = token_provider or EntrabotA365TokenProvider()
        self._mcp_client = mcp_client or WorkIqMcpClient()

    @classmethod
    def from_env(cls) -> WorkIqProvider:
        """Build provider from environment and local ToolingManifest.json."""
        return cls()

    async def call_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Call one Work IQ tool by server and tool name."""
        catalog_server = get_server(server_name)
        manifest_server = self._manifest.require_server(server_name)
        action = f"a365.{server_name}.{tool_name}"
        resource = _audit_resource(
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
        )
        metadata = {
            "server": server_name,
            "tool": tool_name,
            "args_keys": list(arguments.keys()),
        }
        log_event(
            action=action,
            resource=resource,
            outcome="pending",
            attribution_type="agent",
            metadata=metadata,
        )
        try:
            token = await self._token_provider.get_token(
                WorkIqTokenRequest(
                    server_name=server_name,
                    audience=manifest_server.audience,
                    scope=manifest_server.scope,
                )
            )
            result = await self._mcp_client.call_tool(
                endpoint=manifest_server.url or catalog_server.default_endpoint,
                server_name=server_name,
                tool_name=tool_name,
                arguments=arguments,
                token=token,
                scope=manifest_server.scope,
            )
        except Exception:
            log_event(
                action=action,
                resource=resource,
                outcome="failure",
                attribution_type="agent",
                metadata=metadata,
            )
            raise
        log_event(
            action=action,
            resource=resource,
            outcome="success",
            attribution_type="agent",
            metadata=metadata,
        )
        return result
