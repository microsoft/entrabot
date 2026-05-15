#!/usr/bin/env python3
"""Inspect local Agent 365 Work IQ setup for Entraclaw.

This script is a discovery helper, not runtime code. It prints the configured
Work IQ server metadata from ToolingManifest.json and validates that Word is
present before implementation begins.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def _manifest_candidates() -> list[Path]:
    configured = os.environ.get("ENTRACLAW_A365_TOOLING_MANIFEST")
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


async def smoke_read_word(url: str) -> int:
    from entraclaw.a365.word import get_document_content

    content = await get_document_content(url)
    print(f"content_html bytes: {len(content.content_html.encode('utf-8'))}")
    print(f"comments: {len(content.comments)}")
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

    word = [
        server
        for server in servers
        if server.get("mcpServerName") == "mcp_WordServer"
        or server.get("mcpServerUniqueName") == "mcp_WordServer"
    ]
    if not word:
        print("ERROR: mcp_WordServer is not configured", file=sys.stderr)
        return 3

    print("mcp_WordServer configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
