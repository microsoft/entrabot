"""Loader for Agent 365 ToolingManifest.json."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from entrabot.a365.config import manifest_candidates
from entrabot.a365.errors import (
    A365ManifestInvalidError,
    A365ManifestNotFoundError,
    A365ServerNotConfiguredError,
)


@dataclass(frozen=True)
class ManifestServer:
    """One configured Work IQ MCP server from ToolingManifest.json."""

    server_name: str
    unique_name: str
    url: str | None
    scope: str
    audience: str


@dataclass(frozen=True)
class WorkIqManifest:
    """Parsed Agent 365 ToolingManifest.json."""

    path: Path
    servers: dict[str, ManifestServer]

    def require_server(self, server_name: str) -> ManifestServer:
        """Return a configured server or raise with setup guidance."""
        try:
            return self.servers[server_name]
        except KeyError as exc:
            raise A365ServerNotConfiguredError(server_name) from exc


def _server_name(raw: dict[str, Any]) -> str:
    value = raw.get("mcpServerName")
    if not isinstance(value, str) or not value:
        raise A365ManifestInvalidError("mcpServerName is required")
    return value


def _required_str(raw: dict[str, Any], key: str, server_name: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise A365ManifestInvalidError(f"{key} is required for {server_name}")
    return value


def _check_unique_lookup_key(seen: set[str], key: str) -> None:
    if key in seen:
        raise A365ManifestInvalidError(f"duplicate MCP server lookup key: {key}")
    seen.add(key)


def _parse_manifest(path: Path, body: dict[str, Any]) -> WorkIqManifest:
    raw_servers = body.get("mcpServers")
    if not isinstance(raw_servers, list):
        raise A365ManifestInvalidError("mcpServers list is required")

    servers: dict[str, ManifestServer] = {}
    lookup_keys: set[str] = set()
    for raw in raw_servers:
        if not isinstance(raw, dict):
            raise A365ManifestInvalidError("each mcpServers entry must be an object")
        name = _server_name(raw)
        unique_name = _required_str(raw, "mcpServerUniqueName", name)
        for lookup_key in {name, unique_name}:
            _check_unique_lookup_key(lookup_keys, lookup_key)
        server = ManifestServer(
            server_name=name,
            unique_name=unique_name,
            url=raw.get("url") if isinstance(raw.get("url"), str) and raw.get("url") else None,
            scope=_required_str(raw, "scope", name),
            audience=_required_str(raw, "audience", name),
        )
        servers[name] = server
        servers[unique_name] = server

    return WorkIqManifest(path=path, servers=servers)


def load_manifest(path: Path | None = None) -> WorkIqManifest:
    """Load ToolingManifest.json from an explicit path or configured candidates."""
    searched = [path] if path is not None else manifest_candidates()

    for candidate in searched:
        if not candidate.exists():
            continue
        try:
            manifest_text = candidate.read_text()
        except OSError as exc:
            raise A365ManifestInvalidError(
                f"{candidate}: could not read manifest: {exc}"
            ) from exc
        try:
            body = json.loads(manifest_text)
        except json.JSONDecodeError as exc:
            raise A365ManifestInvalidError(f"{candidate}: invalid JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise A365ManifestInvalidError("manifest root must be an object")
        return _parse_manifest(candidate, body)

    raise A365ManifestNotFoundError([str(p) for p in searched])
