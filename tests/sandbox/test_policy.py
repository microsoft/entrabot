"""What the contained server may touch, derived from configuration and the code."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from entrabot.config import EntraBotConfig
from entrabot.sandbox.policy import (
    BASE_HOSTS,
    build_policy,
    host_allowed,
)


def _config(tmp_path: Path, **overrides: object) -> EntraBotConfig:
    values: dict[str, object] = {
        "log_dir": tmp_path / "home" / ".entrabot" / "logs",
        "audit_dir": tmp_path / "home" / ".entrabot" / "audit",
        "data_dir": tmp_path / "home" / ".entrabot" / "data",
    }
    values.update(overrides)
    return EntraBotConfig(**values)  # type: ignore[arg-type]


def _policy(tmp_path: Path, env: dict[str, str] | None = None, **config: object):
    return build_policy(
        _config(tmp_path, **config),
        project_root=tmp_path / "repo",
        venv=tmp_path / "repo" / ".venv",
        home=tmp_path / "home",
        cache_dir=tmp_path / "home" / "Library" / "Caches" / "entrabot",
        env=env or {},
    )


class TestHosts:
    def test_the_hosts_the_code_calls(self, tmp_path: Path) -> None:
        hosts = set(_policy(tmp_path).allowed_hosts)
        assert {
            "login.microsoftonline.com",  # three-hop sign-in, OIDC discovery
            "graph.microsoft.com",
            "agent365.svc.cloud.microsoft",  # Work IQ and Agent 365 telemetry
            "*.sharepoint.com",  # Graph file downloads redirect here
        } <= hosts
        assert set(BASE_HOSTS) <= hosts

    def test_the_blob_endpoint_only_with_cloud_memory(self, tmp_path: Path) -> None:
        assert not any("blob" in h for h in _policy(tmp_path).allowed_hosts)
        policy = _policy(
            tmp_path,
            blob_endpoint="https://acct.blob.core.windows.net",
            blob_container="memory",
        )
        assert {"acct.blob.core.windows.net"} <= set(policy.allowed_hosts)

    def test_persona_sati_and_extra_hosts_from_the_environment(self, tmp_path: Path) -> None:
        policy = _policy(
            tmp_path,
            env={
                "PERSONA_SATI_MCP_URL": "https://mind.example.test/mcp",
                "ENTRABOT_SANDBOX_EXTRA_HOSTS": "proxy.corp.example, *.corp.example",
            },
        )
        assert {"mind.example.test", "proxy.corp.example", "*.corp.example"} <= set(
            policy.allowed_hosts
        )

    @pytest.mark.parametrize(
        ("host", "allowed"),
        [
            ("graph.microsoft.com", True),
            ("GRAPH.microsoft.com", True),
            ("contoso.sharepoint.com", True),
            ("contoso-my.sharepoint.com", True),
            ("sharepoint.com", False),  # a wildcard covers subdomains only
            ("evilsharepoint.com", False),
            ("graph.microsoft.com.evil.example", False),
            ("contoso.sharepoint.com.evil.example", False),
            ("graph.microsoft.com.", True),  # the fully qualified form
            ("example.com", False),
            ("", False),
        ],
    )
    def test_matching(self, host: str, allowed: bool) -> None:
        assert host_allowed(host, ("graph.microsoft.com", "*.sharepoint.com")) is allowed


class TestPaths:
    def test_read_only_repo_and_venv_read_write_state_only(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path)
        assert tmp_path / "repo" in policy.read_only
        assert tmp_path / "repo" / ".venv" in policy.read_only
        assert tmp_path / "home" / ".entrabot" in policy.read_write
        assert tmp_path / "home" / "Library" / "Caches" / "entrabot" in policy.read_write
        assert not any(path == tmp_path / "repo" for path in policy.read_write)
        assert policy.home == tmp_path / "home"

    def test_moved_state_directories_are_writable(self, tmp_path: Path) -> None:
        elsewhere = tmp_path / "state" / "logs"
        policy = _policy(tmp_path, log_dir=elsewhere)
        assert elsewhere in policy.read_write

    def test_the_temp_directory_is_inside_the_state(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path)
        assert policy.temp_dir.is_relative_to(tmp_path / "home" / ".entrabot")

    def test_extra_read_paths_from_the_environment(self, tmp_path: Path) -> None:
        extra = os.pathsep.join(["/opt/tools", "/srv/x"])  # ":" here, ";" on Windows
        policy = _policy(tmp_path, env={"ENTRABOT_SANDBOX_EXTRA_READ": extra})
        assert Path("/opt/tools") in policy.read_only and Path("/srv/x") in policy.read_only
