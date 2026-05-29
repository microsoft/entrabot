"""Typed errors for Agent 365 / Work IQ integration."""

from __future__ import annotations

from entrabot.errors import EntraBotError


class A365Error(EntraBotError):
    """Base class for Agent 365 / Work IQ failures."""


class A365ManifestNotFoundError(A365Error):
    """No ToolingManifest.json could be found."""

    def __init__(self, searched_paths: list[str]) -> None:
        self.searched_paths = searched_paths
        joined = ", ".join(searched_paths)
        super().__init__(
            "Agent 365 ToolingManifest.json not found. Run "
            "`a365 develop add-mcp-servers mcp_WordServer` or set "
            f"ENTRABOT_A365_TOOLING_MANIFEST. Searched: {joined}"
        )


class A365ManifestInvalidError(A365Error):
    """ToolingManifest.json exists but has an invalid shape."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Agent 365 ToolingManifest.json is invalid: {reason}")


class A365ServerNotConfiguredError(A365Error):
    """Requested Work IQ server is not configured."""

    def __init__(self, server_name: str) -> None:
        self.server_name = server_name
        super().__init__(
            f"Work IQ server {server_name!r} is not configured. Run "
            f"`a365 develop add-mcp-servers {server_name}` and ask a Global "
            "Administrator to run `a365 setup permissions mcp`."
        )


class A365TenantNotActivatedError(A365Error):
    """Tenant or Work IQ server is disabled by admin policy."""

    def __init__(self, server_name: str, detail: str) -> None:
        self.server_name = server_name
        self.detail = detail
        super().__init__(
            f"Work IQ server {server_name!r} is not activated for this tenant: {detail}. "
            "Confirm the server is allowed in Microsoft 365 admin center."
        )


class A365ConsentMissingError(A365Error):
    """Required Work IQ permission grant is missing."""

    def __init__(self, server_name: str, scope: str) -> None:
        self.server_name = server_name
        self.scope = scope
        super().__init__(
            f"Work IQ server {server_name!r} is missing consent for {scope!r}. "
            "Ask a Global Administrator to run `a365 setup permissions mcp`."
        )


class A365TokenError(A365Error):
    """Token acquisition for a Work IQ audience failed."""

    def __init__(self, audience: str, detail: str) -> None:
        self.audience = audience
        self.detail = detail
        super().__init__(f"Could not acquire Agent 365 token for {audience!r}: {detail}")


class A365McpCallError(A365Error):
    """Microsoft-hosted Work IQ MCP call failed."""

    def __init__(self, server_name: str, tool_name: str, detail: str) -> None:
        self.server_name = server_name
        self.tool_name = tool_name
        self.detail = detail
        super().__init__(f"Work IQ MCP call {server_name}.{tool_name} failed: {detail}")
