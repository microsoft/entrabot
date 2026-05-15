from __future__ import annotations

import json
from pathlib import Path

import pytest

from entraclaw.a365.catalog import WORD_SERVER_NAME
from entraclaw.a365.errors import (
    A365ManifestInvalidError,
    A365ManifestNotFoundError,
    A365ServerNotConfiguredError,
)
from entraclaw.a365.manifest import WorkIqManifest, load_manifest


def _write_manifest(path: Path, body: dict) -> Path:
    path.write_text(json.dumps(body))
    return path


def test_load_manifest_reads_word_server(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path / "ToolingManifest.json",
        {
            "mcpServers": [
                    {
                        "mcpServerName": "mcp_WordServer",
                        "mcpServerUniqueName": "mcp_WordServer",
                        "url": "https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer",
                        "scope": "McpServers.Word.All",
                        "audience": "api://word-audience",
                    }
                ]
        },
    )

    manifest = load_manifest(path)
    server = manifest.require_server(WORD_SERVER_NAME)

    assert manifest.path == path
    assert server.server_name == "mcp_WordServer"
    assert server.url == "https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer"
    assert server.scope == "McpServers.Word.All"
    assert server.audience == "api://word-audience"


def test_load_manifest_accepts_new_workiq_scope_and_url_for_any_server(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path / "ToolingManifest.json",
        {
            "mcpServers": [
                {
                    "mcpServerName": "mcp_MailTools",
                    "mcpServerUniqueName": "mcp_MailTools",
                    "url": "https://agent365.svc.cloud.microsoft/agents/servers/mcp_MailTools",
                    "scope": "Tools.ListInvoke.All",
                    "audience": "c2d0c2b6-8013-4346-9f8b-b81d3b754a29",
                    "publisher": "Microsoft",
                }
            ]
        },
    )

    manifest = load_manifest(path)
    server = manifest.require_server("mcp_MailTools")

    assert server.url == "https://agent365.svc.cloud.microsoft/agents/servers/mcp_MailTools"
    assert server.scope == "Tools.ListInvoke.All"
    assert server.audience == "c2d0c2b6-8013-4346-9f8b-b81d3b754a29"


def test_load_manifest_rejects_duplicate_server_name(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path / "ToolingManifest.json",
        {
            "mcpServers": [
                {
                    "mcpServerName": "mcp_WordServer",
                    "mcpServerUniqueName": "mcp_WordServer_unique",
                    "scope": "McpServers.Word.All",
                    "audience": "api://word-audience",
                },
                {
                    "mcpServerName": "mcp_WordServer",
                    "mcpServerUniqueName": "mcp_WordServer_unique_2",
                    "scope": "McpServers.Word.All",
                    "audience": "api://word-audience",
                },
            ]
        },
    )

    with pytest.raises(A365ManifestInvalidError) as exc_info:
        load_manifest(path)

    assert "mcp_WordServer" in str(exc_info.value)


def test_load_manifest_rejects_alias_collision(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path / "ToolingManifest.json",
        {
            "mcpServers": [
                {
                    "mcpServerName": "mcp_WordServer",
                    "mcpServerUniqueName": "mcp_SharedAlias",
                    "scope": "McpServers.Word.All",
                    "audience": "api://word-audience",
                },
                {
                    "mcpServerName": "mcp_SharedAlias",
                    "mcpServerUniqueName": "mcp_OtherServer_unique",
                    "scope": "McpServers.Other.All",
                    "audience": "api://other-audience",
                },
            ]
        },
    )

    with pytest.raises(A365ManifestInvalidError) as exc_info:
        load_manifest(path)

    assert "mcp_SharedAlias" in str(exc_info.value)


def test_load_manifest_unreadable_path_raises_invalid_error(tmp_path: Path) -> None:
    directory_path = tmp_path / "ToolingManifest.json"
    directory_path.mkdir()

    with pytest.raises(A365ManifestInvalidError) as exc_info:
        load_manifest(directory_path)

    message = str(exc_info.value)
    assert str(directory_path) in message
    assert "read" in message.lower()


def test_load_manifest_missing_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"

    with pytest.raises(A365ManifestNotFoundError) as exc_info:
        load_manifest(missing)

    assert str(missing) in str(exc_info.value)


def test_load_manifest_rejects_missing_mcp_servers(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path / "ToolingManifest.json", {"other": []})

    with pytest.raises(A365ManifestInvalidError) as exc_info:
        load_manifest(path)

    assert "mcpServers" in str(exc_info.value)


def test_load_manifest_rejects_missing_audience(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path / "ToolingManifest.json",
        {
            "mcpServers": [
                {
                    "mcpServerName": "mcp_WordServer",
                    "mcpServerUniqueName": "mcp_WordServer",
                    "scope": "McpServers.Word.All",
                }
            ]
        },
    )

    with pytest.raises(A365ManifestInvalidError) as exc_info:
        load_manifest(path)

    assert "audience" in str(exc_info.value)


def test_load_manifest_rejects_missing_server_name(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path / "ToolingManifest.json",
        {
            "mcpServers": [
                {
                    "mcpServerUniqueName": "mcp_WordServer_unique",
                    "scope": "McpServers.Word.All",
                    "audience": "api://word-audience",
                }
            ]
        },
    )

    with pytest.raises(A365ManifestInvalidError) as exc_info:
        load_manifest(path)

    assert "mcpServerName" in str(exc_info.value)


def test_load_manifest_rejects_missing_server_unique_name(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path / "ToolingManifest.json",
        {
            "mcpServers": [
                {
                    "mcpServerName": "mcp_WordServer",
                    "scope": "McpServers.Word.All",
                    "audience": "api://word-audience",
                }
            ]
        },
    )

    with pytest.raises(A365ManifestInvalidError) as exc_info:
        load_manifest(path)

    assert "mcpServerUniqueName" in str(exc_info.value)


def test_manifest_require_server_raises_for_unconfigured_server(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path / "ToolingManifest.json", {"mcpServers": []})
    manifest = WorkIqManifest(path=path, servers={})

    with pytest.raises(A365ServerNotConfiguredError):
        manifest.require_server("mcp_WordServer")
