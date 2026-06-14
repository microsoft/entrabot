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
        # The audit resource is intentionally derived ONLY from server_name +
        # tool_name (both come from the manifest, not user input). We do not
        # surface anything from `arguments` into the audit record: tool args
        # may contain customer data (URLs, document titles, message bodies)
        # that has no business in an audit handle, and CodeQL flags any flow
        # from arguments → audit as clear-text logging of sensitive
        # information. Operators correlate audit entries by action +
        # timestamp; deeper resource detail (which document, which comment)
        # lives in the Graph API server-side logs.
        action = f"a365.{server_name}.{tool_name}"
        resource = action
        # Only log the argument KEYS (never values) so operators can see
        # which fields were exercised without recording any user content.
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
