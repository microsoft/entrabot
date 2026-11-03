#!/usr/bin/env python3
"""Inspect local Agent 365 Work IQ setup for Entrabot.

This script is a discovery helper, not runtime code. It prints the configured
Work IQ server metadata from ToolingManifest.json and validates that Word and
Excel are present with the scope and audience the runtime loader requires.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

REQUIRED_SERVERS = ("mcp_WordServer", "mcp_ExcelServer")


def _manifest_candidates() -> list[Path]:
    configured = os.environ.get("ENTRABOT_A365_TOOLING_MANIFEST")
    paths: list[Path] = []
    if configured:
        paths.append(Path(configured).expanduser())
    paths.extend([Path("ToolingManifest.json"), Path(".a365/ToolingManifest.json")])
    return paths


def _load_manifest() -> tuple[Path, dict[str, Any]]:
    for path in _manifest_candidates():
        if path.exists():
            return path, json.loads(path.read_text())
    raise FileNotFoundError("ToolingManifest.json not found")


def _validate_server(servers: list[dict[str, Any]], name: str) -> int:
    """Return 0 if ``name`` is usable, otherwise print why and return an exit code."""
    matches = [
        server
        for server in servers
        if server.get("mcpServerName") == name or server.get("mcpServerUniqueName") == name
    ]
    if not matches:
        print(f"ERROR: {name} is not configured", file=sys.stderr)
        return 3
    required = ("mcpServerUniqueName", "scope", "audience")
    missing = [field for field in required if not matches[0].get(field)]
    if missing:
        print(f"ERROR: {name} is missing {' and '.join(missing)}", file=sys.stderr)
        return 4
    print(f"{name} configured")
    return 0


async def smoke_read_word(url: str) -> int:
    from entrabot.a365.word import get_document_content

    content = await get_document_content(url)
    print(f"content_html bytes: {len(content.content_html.encode('utf-8'))}")
    print(f"comments bytes: {len(content.comments.encode('utf-8'))}")
    return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "read-word":
        import asyncio

        return asyncio.run(smoke_read_word(sys.argv[2]))

    try:
        path, manifest = _load_manifest()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    servers = manifest.get("mcpServers")
    if not isinstance(servers, list):
        print("ERROR: manifest has no mcpServers list", file=sys.stderr)
        return 2

    print(f"manifest: {path}")
    for server in servers:
        name = server.get("mcpServerName") or server.get("mcpServerUniqueName")
        print(
            f"- {name}: url={server.get('url')} "
            f"scope={server.get('scope')} audience={server.get('audience')}"
        )

    for name in REQUIRED_SERVERS:
        code = _validate_server(servers, name)
        if code:
            return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
