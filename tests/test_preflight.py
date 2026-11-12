"""Tests for entrabot.preflight.

Covers the three VP-readiness deliverables:
  1. License preflight (Graph /subscribedSkus check).
  2. Post-setup smoke checks (token mint, /me, /me/chats).
  3. --diagnose mode (full health check including state file + MCP config).

All Graph calls are mocked via respx. Token minting is mocked at the
function boundary so we never hit Entra in tests.
"""

from __future__ import annotations

import json
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from entrabot.config import EntraBotConfig
from entrabot.errors import AgentIDNotAvailable, TokenExchangeError
from entrabot.preflight import (
    COPILOT_CAPABLE_SKUS,
    TEAMS_CAPABLE_SKUS,
    Check,
    check_copilot_license_availability,
    check_mcp_configs,
    check_old_setup_keys,
    check_persona_sati_mcp,
    check_state_file,
    check_teams_license_availability,
    format_report,
    overall_exit_code,
    run_diagnostics,
    run_smoke_checks,
)


@pytest.fixture(autouse=True)
def _not_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remediation text names the platform's own setup script. These tests assert the
    macOS/Linux wording, so pin it; ``TestWindows`` turns Windows on explicitly."""
    monkeypatch.setattr("entrabot.preflight._is_windows", lambda: False)


# ─── Phase 1: license preflight ──────────────────────────────────────────────


class TestCheckTeamsLicenseAvailability:
    """check_teams_license_availability: queries /subscribedSkus, classifies."""

    SUBSCRIBED_SKUS_URL = "https://graph.microsoft.com/v1.0/subscribedSkus"

    def test_pass_when_teams_capable_sku_has_seats(self) -> None:
        with respx.mock:
            respx.get(self.SUBSCRIBED_SKUS_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "skuId": "sku-1",
                                "skuPartNumber": "ENTERPRISEPREMIUM",
                                "prepaidUnits": {"enabled": 5},
                                "consumedUnits": 2,
                            }
                        ]
                    },
                )
            )
            result = check_teams_license_availability("fake-token")

        assert result.status == "pass"
        assert "ENTERPRISEPREMIUM" in result.detail

    def test_warn_when_no_teams_capable_sku_available(self) -> None:
        with respx.mock:
            respx.get(self.SUBSCRIBED_SKUS_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "skuId": "sku-1",
                                "skuPartNumber": "POWER_BI_STANDARD",
                                "prepaidUnits": {"enabled": 5},
                                "consumedUnits": 0,
                            }
                        ]
                    },
                )
            )
            result = check_teams_license_availability("fake-token")

        assert result.status == "warn"
        assert result.remediation == (
            "Buy a Teams-capable license at "
            "https://admin.microsoft.com/Adminportal/Home#/catalog "
            "(or any Teams-capable license: M365 Business Premium, E3, E5, etc.)"
            " and re-run setup.sh, or assign an existing license manually before testing."
        )

    def test_warn_when_teams_sku_exists_but_all_seats_consumed(self) -> None:
        with respx.mock:
            respx.get(self.SUBSCRIBED_SKUS_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "skuId": "sku-1",
                                "skuPartNumber": "SPE_E3",
                                "prepaidUnits": {"enabled": 1},
                                "consumedUnits": 1,
                            }
                        ]
                    },
                )
            )
            result = check_teams_license_availability("fake-token")

        assert result.status == "warn"

    def test_copilot_add_on_does_not_satisfy_teams_requirement(self) -> None:
        with respx.mock:
            respx.get(self.SUBSCRIBED_SKUS_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "skuId": "copilot-sku",
                                "skuPartNumber": "MICROSOFT_365_COPILOT",
                                "prepaidUnits": {"enabled": 2},
                                "consumedUnits": 0,
                            }
                        ]
                    },
                )
            )
            result = check_teams_license_availability("fake-token")

        assert result.status == "warn"

    def test_skip_when_graph_unreachable(self) -> None:
        with respx.mock:
            respx.get(self.SUBSCRIBED_SKUS_URL).mock(
                return_value=httpx.Response(401, json={"error": "unauthorized"})
            )
            result = check_teams_license_availability("fake-token")

        assert result.status == "skip"

    @staticmethod
    def _with_plans(part: str, plans: list[str], *, free: int = 2) -> dict:
        return {
            "skuId": f"id-{part}",
            "skuPartNumber": part,
            "appliesTo": "User",
            "prepaidUnits": {"enabled": free},
            "consumedUnits": 0,
            "servicePlans": [
                {"servicePlanName": name, "appliesTo": "User", "provisioningStatus": "Success"}
                for name in plans
            ],
        }

    def _check(self, *skus: dict) -> Check:
        with respx.mock:
            respx.get(self.SUBSCRIBED_SKUS_URL).mock(
                return_value=httpx.Response(200, json={"value": list(skus)})
            )
            return check_teams_license_availability("fake-token")

    def test_a_product_with_the_teams_plan_counts_whatever_its_part_number(self) -> None:
        # Microsoft 365 Business Basic: not on the old name list under this part number.
        result = self._check(
            self._with_plans("SMB_BUSINESS_ESSENTIALS", ["TEAMS1", "EXCHANGE_S_STANDARD"])
        )
        assert result.status == "pass" and "SMB_BUSINESS_ESSENTIALS (2 free)" in result.detail

    def test_a_no_teams_suite_does_not_count_though_plans_are_named_after_teams(self) -> None:
        plans = ["MESH_AVATARS_FOR_TEAMS", "CDS_O365_P2", "EXCHANGE_S_ENTERPRISE"]
        assert self._check(self._with_plans("Microsoft_365_E3_(no_Teams)", plans)).status == "warn"

    def test_the_e5_extra_features_licence_does_not_count(self) -> None:
        # It was on the old name list, and includes no Teams.
        result = self._check(self._with_plans("M365_E5_SUITE_COMPONENTS", ["CLIPCHAMP"]))
        assert result.status == "warn"

    def test_a_shared_device_licence_does_not_count(self) -> None:
        result = self._check(self._with_plans("MCOCAP", ["TEAMS1", "MCOEV"]))
        assert result.status == "warn"


class TestCheckCopilotLicenseAvailability:
    SUBSCRIBED_SKUS_URL = "https://graph.microsoft.com/v1.0/subscribedSkus"

    def test_pass_when_copilot_sku_has_seats(self) -> None:
        with respx.mock:
            respx.get(self.SUBSCRIBED_SKUS_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "skuId": "copilot-sku",
                                "skuPartNumber": "MICROSOFT_365_COPILOT",
                                "prepaidUnits": {"enabled": 2},
                                "consumedUnits": 0,
                            }
                        ]
                    },
                )
            )
            result = check_copilot_license_availability("fake-token")

        assert result.status == "pass"
        assert "MICROSOFT_365_COPILOT" in result.detail

    def test_pass_when_copilot_sku_uses_tenant_part_number_casing(self) -> None:
        with respx.mock:
            respx.get(self.SUBSCRIBED_SKUS_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "skuId": "copilot-sku",
                                "skuPartNumber": "Microsoft_365_Copilot",
                                "prepaidUnits": {"enabled": 2},
                                "consumedUnits": 0,
                            }
                        ]
                    },
                )
            )
            result = check_copilot_license_availability("fake-token")

        assert result.status == "pass"
        assert "Microsoft_365_Copilot" in result.detail

    def test_warn_when_copilot_sku_is_missing_even_if_teams_is_available(self) -> None:
        with respx.mock:
            respx.get(self.SUBSCRIBED_SKUS_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "skuId": "teams-sku",
                                "skuPartNumber": "SPE_E3",
                                "prepaidUnits": {"enabled": 5},
                                "consumedUnits": 0,
                            }
                        ]
                    },
                )
            )
            result = check_copilot_license_availability("fake-token")

        assert result.status == "warn"
        assert "Microsoft 365 Copilot" in result.detail

    def test_skip_when_graph_returns_non_200(self) -> None:
        with respx.mock:
            respx.get(self.SUBSCRIBED_SKUS_URL).mock(
                return_value=httpx.Response(401, json={"error": "unauthorized"})
            )
            result = check_copilot_license_availability("fake-token")

        assert result.status == "skip"

    def test_skip_when_graph_unreachable(self) -> None:
        transport = httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("boom"))
        )

        result = check_copilot_license_availability("fake-token", transport=transport)

        assert result.status == "skip"

    def test_copilot_under_another_part_number_counts_by_its_plans(self) -> None:
        # "Microsoft Copilot for Microsoft 365" (M365_Copilot) is not on the name list.
        sku = {
            "skuId": "copilot-sku",
            "skuPartNumber": "M365_Copilot",
            "prepaidUnits": {"enabled": 2},
            "consumedUnits": 1,
            "servicePlans": [{"servicePlanName": "M365_COPILOT_APPS", "appliesTo": "User"}],
        }
        with respx.mock:
            respx.get(self.SUBSCRIBED_SKUS_URL).mock(
                return_value=httpx.Response(200, json={"value": [sku]})
            )
            result = check_copilot_license_availability("fake-token")

        assert result.status == "pass" and "M365_Copilot (1 free)" in result.detail


# ─── Phase 2: smoke checks ───────────────────────────────────────────────────


def _config_complete() -> EntraBotConfig:
    return EntraBotConfig(
        tenant_id="00000000-0000-0000-0000-000000000001",
        blueprint_app_id="00000000-0000-0000-0000-000000000002",
        blueprint_cert_thumbprint="A" * 40,
        agent_id="00000000-0000-0000-0000-000000000003",
        agent_user_id="00000000-0000-0000-0000-000000000004",
        agent_user_upn="agent-test@example.com",
    )


class TestRunSmokeChecks:
    ME_URL = "https://graph.microsoft.com/v1.0/me"
    CHATS_URL = "https://graph.microsoft.com/v1.0/me/chats"

    def test_all_pass(self) -> None:
        config = _config_complete()
        with (
            patch("entrabot.preflight.acquire_agent_user_token", return_value="tok-123"),
            respx.mock,
        ):
            respx.get(self.ME_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": config.agent_user_id,
                        "userPrincipalName": config.agent_user_upn,
                        "displayName": "Agent Test",
                    },
                )
            )
            respx.get(url__startswith=self.CHATS_URL).mock(
                return_value=httpx.Response(200, json={"value": []})
            )
            checks = run_smoke_checks(config)

        assert [c.status for c in checks] == ["pass", "pass", "pass"]
        assert {c.name for c in checks} == {
            "Token mint (three-hop)",
            "Graph /me identity",
            "Teams scope (/me/chats)",
        }

    def test_token_mint_fails_short_circuits(self) -> None:
        config = _config_complete()
        with patch(
            "entrabot.preflight.acquire_agent_user_token",
            side_effect=TokenExchangeError(
                hop="hop1", error="invalid_client", description="bad assertion"
            ),
        ):
            checks = run_smoke_checks(config)

        # First check fails, the rest are skip.
        assert checks[0].status == "fail"
        assert "hop1" in checks[0].detail or "invalid_client" in checks[0].detail
        assert checks[1].status == "skip"
        assert checks[2].status == "skip"

    def test_token_mint_unconfigured(self) -> None:
        config = _config_complete()
        with patch(
            "entrabot.preflight.acquire_agent_user_token",
            side_effect=AgentIDNotAvailable("missing fields"),
        ):
            checks = run_smoke_checks(config)
        assert checks[0].status == "fail"
        assert "setup.sh" in (checks[0].remediation or "")

    def test_me_upn_mismatch_fails(self) -> None:
        config = _config_complete()
        with patch("entrabot.preflight.acquire_agent_user_token", return_value="tok"), respx.mock:
            respx.get(self.ME_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": config.agent_user_id,
                        "userPrincipalName": "someone-else@example.com",
                    },
                )
            )
            respx.get(url__startswith=self.CHATS_URL).mock(
                return_value=httpx.Response(200, json={"value": []})
            )
            checks = run_smoke_checks(config)

        me_check = next(c for c in checks if c.name == "Graph /me identity")
        assert me_check.status == "fail"
        assert "someone-else@example.com" in me_check.detail

    def test_chats_403_replication_lag(self) -> None:
        config = _config_complete()
        with patch("entrabot.preflight.acquire_agent_user_token", return_value="tok"), respx.mock:
            respx.get(self.ME_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": config.agent_user_id,
                        "userPrincipalName": config.agent_user_upn,
                    },
                )
            )
            respx.get(url__startswith=self.CHATS_URL).mock(
                return_value=httpx.Response(403, json={"error": "forbidden"})
            )
            checks = run_smoke_checks(config)

        chats = next(c for c in checks if c.name.startswith("Teams scope"))
        assert chats.status == "fail"
        assert "10-15" in (chats.remediation or "") or "license" in (chats.remediation or "")


# ─── Phase 3: diagnostics (state + MCP) ──────────────────────────────────────


class TestCheckStateFile:
    def test_pass_when_required_keys_present(self, tmp_path: Path) -> None:
        state = tmp_path / ".entrabot-state.json"
        state.write_text(
            json.dumps(
                {
                    "BLUEPRINT_APP_ID": "x",
                    "AGENT_ID": "y",
                    "AGENT_USER_ID": "z",
                }
            )
        )
        result = check_state_file(state)
        assert result.status == "pass"

    def test_fail_when_state_missing(self, tmp_path: Path) -> None:
        result = check_state_file(tmp_path / "missing.json")
        assert result.status == "fail"
        assert "setup.sh" in (result.remediation or "")

    def test_fail_when_keys_incomplete(self, tmp_path: Path) -> None:
        state = tmp_path / ".entrabot-state.json"
        state.write_text(json.dumps({"BLUEPRINT_APP_ID": "x"}))
        result = check_state_file(state)
        assert result.status == "fail"
        assert "AGENT_ID" in result.detail or "AGENT_USER_ID" in result.detail


class TestCheckMcpConfigs:
    def test_pass_when_both_configs_point_at_binary(self, tmp_path: Path) -> None:
        binary = tmp_path / "bin" / "entrabot-mcp"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)

        claude_cfg = tmp_path / ".mcp.json"
        claude_cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "entrabot": {
                            "type": "stdio",
                            "command": str(binary),
                            "args": [],
                        }
                    }
                }
            )
        )
        copilot_cfg = tmp_path / "copilot" / "mcp-config.json"
        copilot_cfg.parent.mkdir()
        copilot_cfg.write_text(claude_cfg.read_text())

        checks = check_mcp_configs(
            expected_binary=binary,
            claude_path=claude_cfg,
            copilot_path=copilot_cfg,
        )
        statuses = {c.name: c.status for c in checks}
        assert statuses["Claude Code MCP config"] == "pass"
        assert statuses["Copilot CLI MCP config"] == "pass"

    def test_fail_when_binary_drifts(self, tmp_path: Path) -> None:
        claude_cfg = tmp_path / ".mcp.json"
        claude_cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "entrabot": {
                            "type": "stdio",
                            "command": "/old/stale/path/entrabot-mcp",
                            "args": [],
                        }
                    }
                }
            )
        )
        binary = tmp_path / "bin" / "entrabot-mcp"
        binary.parent.mkdir()
        binary.write_text("")

        checks = check_mcp_configs(
            expected_binary=binary,
            claude_path=claude_cfg,
            copilot_path=tmp_path / "missing.json",
        )
        statuses = {c.name: c.status for c in checks}
        assert statuses["Claude Code MCP config"] == "fail"
        # Copilot missing → warn (not every dev installs Copilot)
        assert statuses["Copilot CLI MCP config"] == "warn"

    def test_relative_command_path_resolves_against_config_parent(self, tmp_path: Path) -> None:
        """The Claude .mcp.json deliberately writes a relative path."""
        binary = tmp_path / ".venv" / "bin" / "entrabot-mcp"
        binary.parent.mkdir(parents=True)
        binary.write_text("")
        claude_cfg = tmp_path / ".mcp.json"
        claude_cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "entrabot": {
                            "type": "stdio",
                            "command": ".venv/bin/entrabot-mcp",
                            "args": [],
                        }
                    }
                }
            )
        )
        copilot_cfg = tmp_path / "copilot.json"
        copilot_cfg.write_text(claude_cfg.read_text())

        checks = check_mcp_configs(
            expected_binary=binary,
            claude_path=claude_cfg,
            copilot_path=copilot_cfg,
        )
        statuses = {c.name: c.status for c in checks}
        assert statuses["Claude Code MCP config"] == "pass"
        assert statuses["Copilot CLI MCP config"] == "pass"

    def test_copilot_drift_is_warn_not_fail(self, tmp_path: Path) -> None:
        """Copilot pointing at another worktree is non-fatal — common in dev."""
        claude_cfg = tmp_path / ".mcp.json"
        binary = tmp_path / "bin" / "entrabot-mcp"
        binary.parent.mkdir()
        binary.write_text("")
        claude_cfg.write_text(json.dumps({"mcpServers": {"entrabot": {"command": str(binary)}}}))
        copilot_cfg = tmp_path / "copilot.json"
        copilot_cfg.write_text(
            json.dumps(
                {"mcpServers": {"entrabot": {"command": "/other/worktree/.venv/bin/entrabot-mcp"}}}
            )
        )

        checks = check_mcp_configs(
            expected_binary=binary,
            claude_path=claude_cfg,
            copilot_path=copilot_cfg,
        )
        statuses = {c.name: c.status for c in checks}
        assert statuses["Claude Code MCP config"] == "pass"
        assert statuses["Copilot CLI MCP config"] == "warn"

    def test_warn_when_claude_missing_and_copilot_missing(self, tmp_path: Path) -> None:
        binary = tmp_path / "entrabot-mcp"
        binary.write_text("")
        checks = check_mcp_configs(
            expected_binary=binary,
            claude_path=tmp_path / "absent-claude.json",
            copilot_path=tmp_path / "absent-copilot.json",
        )
        # Claude missing is a fail (it's the project root file we own).
        statuses = {c.name: c.status for c in checks}
        assert statuses["Claude Code MCP config"] == "fail"
        assert statuses["Copilot CLI MCP config"] == "warn"


# ─── Optional persona-sati check ─────────────────────────────────────────────

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / ".mcp.json.example"
PERSONA_CHECK = "persona-sati MCP (optional)"
PLACEHOLDER_CMD = "<absolute path to the client launcher from your persona-sati install>"
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="Windows has no executable bit")
LEAKABLE = ("jdoe", "s3cret", "tok3n", "abc123", "hunter2")


def _write_mcp(tmp_path: Path, persona: object, entrabot_env: dict[str, str] | None = None) -> Path:
    servers: dict[str, object] = {
        "entrabot": {"type": "stdio", "command": "entrabot-mcp", "env": entrabot_env or {}}
    }
    if persona is not None:
        servers["persona-sati"] = persona
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}))
    return path


def _launcher(directory: Path, mode: int = 0o755) -> Path:
    """Stand-in launcher; the .cmd suffix keeps shutil.which happy on Windows too."""
    directory.mkdir(exist_ok=True)
    path = directory / "launcher.cmd"
    path.write_text("#!/bin/sh\n")
    path.chmod(mode)
    return path


def _text(check: Check) -> str:
    return f"{check.detail} {check.remediation or ''}"


@pytest.fixture
def listening_port() -> Iterator[int]:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        yield server.getsockname()[1]


@pytest.fixture
def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]  # released on return, so nothing listens here


class TestCheckPersonaSatiMcp:
    @pytest.mark.parametrize(
        ("kind", "field"),
        [("stdio", {"url": "https://persona.example/sse"}), ("sse", {"command": "launcher"})],
        ids=["stdio-with-only-a-url", "sse-with-only-a-command"],
    )
    def test_fields_that_do_not_match_the_declared_type_warn(
        self, tmp_path: Path, kind: str, field: dict[str, str]
    ) -> None:
        launcher = _launcher(tmp_path / "bin")
        entry = {"type": kind, **field}
        if "command" in entry:
            entry["command"] = str(launcher)

        result = check_persona_sati_mcp(_write_mcp(tmp_path, entry))

        assert result.status == "warn"
        assert kind in result.detail

    @pytest.mark.parametrize("kind", ["http", "streamable-http", "streamable_http"])
    def test_every_http_transport_uses_the_url(self, tmp_path: Path, kind: str) -> None:
        entry = {"type": kind, "url": "https://persona.example/mcp"}

        result = check_persona_sati_mcp(_write_mcp(tmp_path, entry))

        assert result.status == "pass"

    def test_a_missing_type_defaults_to_stdio_like_the_runtime(self, tmp_path: Path) -> None:
        launcher = _launcher(tmp_path / "bin")

        result = check_persona_sati_mcp(_write_mcp(tmp_path, {"command": str(launcher)}))

        assert result.status == "pass"

    def test_every_ipv4_loopback_address_is_treated_as_local(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        contacted: list[tuple[str, int]] = []

        def _refuse(address: tuple[str, int], *args: object, **kwargs: object) -> None:
            contacted.append(address)
            raise ConnectionRefusedError

        monkeypatch.setattr(socket, "create_connection", _refuse)
        entry = {"type": "sse", "url": "http://127.0.0.2:8100/sse"}

        result = check_persona_sati_mcp(_write_mcp(tmp_path, entry))

        assert contacted == [("127.0.0.2", 8100)]
        assert result.status == "warn"
        assert "nothing is listening" in result.detail

    def test_bare_commands_use_the_path_their_own_entry_sets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        launcher = _launcher(tmp_path / "bin")
        monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
        persona = {"type": "stdio", "command": launcher.name, "env": {"PATH": str(launcher.parent)}}
        env = {"PATH": str(launcher.parent), "PERSONA_SATI_MCP_TOKEN_COMMAND": launcher.name}

        result = check_persona_sati_mcp(_write_mcp(tmp_path, persona, env))

        assert result.status == "pass"
        assert str(tmp_path / "bin") not in result.detail  # env values stay out of the report

    def test_a_command_only_on_this_process_path_warns_if_the_entry_overrides_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        launcher = _launcher(tmp_path / "bin")
        monkeypatch.setenv("PATH", str(launcher.parent))
        persona = {
            "type": "stdio",
            "command": launcher.name,
            "env": {"PATH": str(tmp_path / "elsewhere")},
        }

        result = check_persona_sati_mcp(_write_mcp(tmp_path, persona))

        assert result.status == "warn"
        assert str(tmp_path / "elsewhere") not in _text(result)

    def test_remediation_says_to_remove_the_env_vars_too(self, tmp_path: Path) -> None:
        # The server reads these on its own, so deleting only the entry isn't standalone.
        result = check_persona_sati_mcp(
            _write_mcp(tmp_path, {"type": "stdio", "command": str(tmp_path / "missing")})
        )

        assert result.status == "warn"
        assert "PERSONA_SATI_MCP_URL" in result.remediation
        assert "PERSONA_SATI_MCP_TOKEN_COMMAND" in result.remediation

    def test_example_file_is_valid_json_and_not_on_localhost(self) -> None:
        entry = json.loads(EXAMPLE_CONFIG.read_text())["mcpServers"]["persona-sati"]
        assert entry["type"] == "stdio"
        assert not any(h in json.dumps(entry) for h in ("localhost", "127.0.0.1", "::1", "8100"))

    def test_unedited_example_warns_and_never_fails(self) -> None:
        result = check_persona_sati_mcp(EXAMPLE_CONFIG)
        assert (result.name, result.status) == (PERSONA_CHECK, "warn")
        assert "placeholder" in result.detail
        assert "PERSONA_SATI_MCP_URL" in result.detail
        assert "PERSONA_SATI_MCP_TOKEN_COMMAND" in result.detail
        assert overall_exit_code([result]) == 0

    @pytest.mark.parametrize(
        "content",
        [None, "{not json", "[]", '{"mcpServers": {}}', '{"mcpServers": {"persona-sati": "x"}}'],
        ids=["missing-file", "unparsable", "wrong-shape", "no-entry", "entry-not-an-object"],
    )
    def test_skip_when_there_is_no_usable_entry(self, tmp_path: Path, content: str | None) -> None:
        path = tmp_path / ".mcp.json"
        if content is not None:
            path.write_text(content)
        result = check_persona_sati_mcp(path)
        assert (result.name, result.status) == (PERSONA_CHECK, "skip")
        assert "optional" in result.detail

    @pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative-to-config-dir"])
    def test_stdio_launcher_passes(self, tmp_path: Path, relative: bool) -> None:
        launcher = _launcher(tmp_path / "bin")
        command = "bin/launcher.cmd" if relative else str(launcher)
        result = check_persona_sati_mcp(_write_mcp(tmp_path, {"type": "stdio", "command": command}))
        assert result.status == "pass"
        assert command in result.detail

    @pytest.mark.parametrize(
        ("entry", "expect"),
        [
            ({"type": "stdio", "command": PLACEHOLDER_CMD}, "unfilled placeholder"),
            ({"type": "stdio", "command": "/no/such/launcher"}, "not found"),
            ({"type": "stdio"}, "stdio entry has no command"),
            ({"type": "sse"}, "sse entry has no url"),
        ],
        ids=["placeholder", "missing-file", "no-command", "no-url"],
    )
    def test_unusable_entry_warns(self, tmp_path: Path, entry: dict[str, str], expect: str) -> None:
        result = check_persona_sati_mcp(_write_mcp(tmp_path, entry))
        assert result.status == "warn"
        assert expect in result.detail

    @POSIX_ONLY
    @pytest.mark.parametrize("where", ["entry-command", "token-command"])
    def test_non_executable_file_warns(self, tmp_path: Path, where: str) -> None:
        dud = str(_launcher(tmp_path / "dud", mode=0o644))
        good = str(_launcher(tmp_path / "good"))
        entry = {"type": "stdio", "command": dud if where == "entry-command" else good}
        env = {"PERSONA_SATI_MCP_TOKEN_COMMAND": dud} if where == "token-command" else None
        result = check_persona_sati_mcp(_write_mcp(tmp_path, entry, env))
        assert result.status == "warn"
        assert "not executable" in result.detail

    @pytest.mark.parametrize("kind", ["sse", "http"])
    def test_loopback_url_with_a_listener_passes(
        self, tmp_path: Path, listening_port: int, kind: str
    ) -> None:
        entry = {"type": kind, "url": f"http://127.0.0.1:{listening_port}/sse"}
        result = check_persona_sati_mcp(_write_mcp(tmp_path, entry))
        assert result.status == "pass"
        assert f"127.0.0.1:{listening_port}" in result.detail

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
    def test_loopback_url_without_a_listener_warns_not_fails(
        self, tmp_path: Path, closed_port: int, host: str
    ) -> None:
        entry = {"type": "sse", "url": f"http://{host}:{closed_port}/sse"}
        result = check_persona_sati_mcp(_write_mcp(tmp_path, entry))
        assert result.status == "warn"
        assert "nothing is listening" in result.detail
        assert "does not mean a hosted" in result.detail
        assert "client launcher" in result.remediation

    def test_remote_url_passes_without_contacting_the_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("a non-loopback host must never be contacted")

        monkeypatch.setattr(socket, "create_connection", _boom)
        entry = {"type": "http", "url": "https://persona.example/sse"}
        result = check_persona_sati_mcp(_write_mcp(tmp_path, entry))
        assert result.status == "pass"
        assert result.detail == "https://persona.example:443 is a remote host (not contacted)."

    @pytest.mark.parametrize("host", ["persona.example", "127.0.0.1"], ids=["remote", "loopback"])
    def test_details_never_leak_userinfo_query_headers_or_env(
        self, tmp_path: Path, closed_port: int, host: str
    ) -> None:
        entry = {
            "type": "sse",
            "url": f"https://jdoe:s3cret@{host}:{closed_port}/sse/tok3n?key=abc123#hunter2",
            "headers": {"Authorization": "Bearer abc123"},
            "env": {"TOKEN": "hunter2"},
        }
        result = check_persona_sati_mcp(_write_mcp(tmp_path, entry, {"OTHER": "s3cret"}))
        assert f"{host}:{closed_port}" in result.detail
        assert not [secret for secret in LEAKABLE if secret in _text(result)]

    @pytest.mark.parametrize(
        "url",
        [
            "not a url",
            "ftp://persona.example/sse",
            "<your persona-sati base URL>",
            "http://h:s3cret/",
        ],
        ids=["no-scheme", "wrong-scheme", "placeholder", "bad-port"],
    )
    def test_invalid_url_warns_without_echoing_it(self, tmp_path: Path, url: str) -> None:
        result = check_persona_sati_mcp(_write_mcp(tmp_path, {"type": "sse", "url": url}))
        assert result.status == "warn"
        assert url not in _text(result)
        assert "s3cret" not in _text(result)

    @pytest.mark.parametrize(
        ("env", "reason"),
        [
            ({"PERSONA_SATI_MCP_URL": "<your persona-sati base URL>"}, "placeholder"),
            ({"PERSONA_SATI_MCP_TOKEN_COMMAND": "<path to your token command>"}, "placeholder"),
            ({"PERSONA_SATI_MCP_TOKEN_COMMAND": "/no/such/token-cmd"}, "not found"),
        ],
        ids=["url-placeholder", "token-placeholder", "token-missing"],
    )
    def test_entrabot_env_problems_warn(
        self, tmp_path: Path, env: dict[str, str], reason: str
    ) -> None:
        entry = {"type": "stdio", "command": str(_launcher(tmp_path / "bin"))}
        result = check_persona_sati_mcp(_write_mcp(tmp_path, entry, env))
        assert result.status == "warn"
        assert next(iter(env)) in result.detail
        assert reason in result.detail
        assert "/no/such/token-cmd" not in _text(result)  # env values are never echoed

    def test_filled_in_entrabot_env_passes(self, tmp_path: Path) -> None:
        launcher = str(_launcher(tmp_path / "bin"))
        env = {
            "PERSONA_SATI_MCP_URL": "https://persona.example",
            "PERSONA_SATI_MCP_TOKEN_COMMAND": launcher,
        }
        result = check_persona_sati_mcp(_write_mcp(tmp_path, {"command": launcher}, env))
        assert result.status == "pass"


class TestPersonaTokenCommandForms:
    """The check accepts every form of PERSONA_SATI_MCP_TOKEN_COMMAND the running server
    accepts (``mcp_server._persona_token_words``): a program with arguments, and on Windows
    a script or an unquoted path with spaces. It used to treat the whole value as one
    executable, so a working setup was reported as "not found"."""

    ENTRY = {"type": "sse", "url": "https://persona.example/sse"}

    def _check(self, tmp_path: Path, command: str, extra: dict[str, str] | None = None) -> Check:
        env = {"PERSONA_SATI_MCP_TOKEN_COMMAND": command, **(extra or {})}
        return check_persona_sati_mcp(_write_mcp(tmp_path, self.ENTRY, env))

    @POSIX_ONLY
    def test_a_program_with_arguments_passes(self, tmp_path: Path) -> None:
        launcher = _launcher(tmp_path / "bin")

        result = self._check(tmp_path, f"{launcher} --audience api://persona --tenant t1")

        assert result.status == "pass", result.detail

    @POSIX_ONLY
    def test_a_quoted_program_with_spaces_in_its_path_passes(self, tmp_path: Path) -> None:
        launcher = _launcher(tmp_path / "my tools")

        result = self._check(tmp_path, f'"{launcher}" --audience "api://persona sati"')

        assert result.status == "pass", result.detail

    @POSIX_ONLY
    def test_an_unquoted_path_with_spaces_and_no_arguments_still_passes(
        self, tmp_path: Path
    ) -> None:
        # The whole value is tried as one program first, as the server does.
        launcher = _launcher(tmp_path / "my tools")

        assert self._check(tmp_path, str(launcher)).status == "pass"

    @POSIX_ONLY
    def test_a_bare_name_with_arguments_is_found_on_the_entrys_own_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        launcher = _launcher(tmp_path / "bin")
        monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))

        result = self._check(tmp_path, f"{launcher.name} --flag", {"PATH": str(launcher.parent)})

        assert result.status == "pass", result.detail

    def test_a_missing_program_with_arguments_warns_without_echoing_them(
        self, tmp_path: Path
    ) -> None:
        result = self._check(tmp_path, "/no/such/token-cli --client-secret hunter2")

        assert result.status == "warn"
        assert "PERSONA_SATI_MCP_TOKEN_COMMAND" in result.detail
        assert "not found" in result.detail
        assert "hunter2" not in _text(result)
        assert "/no/such/token-cli" not in _text(result)

    @POSIX_ONLY
    def test_an_unbalanced_quote_warns(self, tmp_path: Path) -> None:
        launcher = _launcher(tmp_path / "bin")

        assert self._check(tmp_path, f'{launcher} --scope "api://persona').status == "warn"

    @POSIX_ONLY
    def test_posix_does_not_guess_at_an_unquoted_path_with_spaces_and_arguments(
        self, tmp_path: Path
    ) -> None:
        # The server splits by shell rules here, so the program would be ".../my".
        launcher = _launcher(tmp_path / "my tools")

        assert self._check(tmp_path, f"{launcher} --flag").status == "warn"

    @POSIX_ONLY  # Windows rules, simulated on a POSIX host with real files
    @pytest.mark.parametrize("name", ["get-token.ps1", "get-token.py", "get-token.cmd"])
    def test_on_windows_a_script_is_a_program_even_without_an_executable_bit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        script = tmp_path / "bin" / name
        script.parent.mkdir()
        script.write_text("", encoding="utf-8")
        script.chmod(0o644)
        monkeypatch.setattr("entrabot.preflight._is_windows", lambda: True)

        result = self._check(tmp_path, f"{script} -Audience api://persona")

        assert result.status == "pass", result.detail

    @POSIX_ONLY
    def test_on_windows_an_unquoted_path_with_spaces_and_arguments_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script = tmp_path / "Program Files" / "persona" / "token.cmd"
        script.parent.mkdir(parents=True)
        script.write_text("", encoding="utf-8")
        monkeypatch.setattr("entrabot.preflight._is_windows", lambda: True)

        result = self._check(tmp_path, f'{script} --flag "a b"')

        assert result.status == "pass", result.detail

    @POSIX_ONLY
    def test_on_windows_a_program_that_does_not_exist_still_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("entrabot.preflight._is_windows", lambda: True)

        result = self._check(tmp_path, f"{tmp_path}/nowhere/token.ps1 --flag")

        assert result.status == "warn"
        assert "not found" in result.detail

    def test_checking_never_runs_the_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("the token command must not be run by a health check")

        for name in ("run", "check_output", "Popen", "call"):
            monkeypatch.setattr(subprocess, name, _boom)
        launcher = _launcher(tmp_path / "bin")

        self._check(tmp_path, f"{launcher} --flag")

    def test_checking_never_imports_the_server(self) -> None:
        # Importing entrabot.mcp_server builds the server, which runs the token command and
        # contacts persona-sati. A health check must do neither.
        import subprocess

        code = (
            "import sys, entrabot.preflight; "
            "sys.exit(1 if 'entrabot.mcp_server' in sys.modules else 0)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
        )
        assert result.returncode == 0, result.stderr


class TestRunDiagnostics:
    def test_aggregates_all_checks(self, tmp_path: Path) -> None:
        # Just smoke-test that run_diagnostics returns a superset of smoke checks
        # plus state + mcp. We mock everything so this is a structural test.
        config = _config_complete()
        state = tmp_path / ".entrabot-state.json"
        state.write_text(
            json.dumps(
                {
                    "BLUEPRINT_APP_ID": "x",
                    "AGENT_ID": "y",
                    "AGENT_USER_ID": "z",
                }
            )
        )
        with (
            patch("entrabot.preflight.acquire_agent_user_token", return_value="tok"),
            patch(
                "entrabot.preflight._check_cert_in_keystore",
                return_value=Check(
                    name="Blueprint cert in OS keystore",
                    status="pass",
                    detail="present",
                ),
            ),
            respx.mock,
        ):
            respx.get("https://graph.microsoft.com/v1.0/me").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": config.agent_user_id,
                        "userPrincipalName": config.agent_user_upn,
                    },
                )
            )
            respx.get(url__startswith="https://graph.microsoft.com/v1.0/me/chats").mock(
                return_value=httpx.Response(200, json={"value": []})
            )

            checks = run_diagnostics(
                config,
                state_path=state,
                expected_binary=tmp_path / "bin" / "entrabot-mcp",
                claude_mcp_path=tmp_path / "absent.json",
                copilot_mcp_path=tmp_path / "absent2.json",
            )

        names = [c.name for c in checks]
        assert "State file" in names
        assert "Blueprint cert in OS keystore" in names
        assert "Token mint (three-hop)" in names
        assert "Graph /me identity" in names
        assert "Teams scope (/me/chats)" in names
        assert "Claude Code MCP config" in names
        assert PERSONA_CHECK in names


# ─── older setup keys ────────────────────────────────────────────────────────
#
# An older install kept the Provisioner's private key in a plain file. When setup could not take
# that key's certificate off the Provisioner app (tenant-wide Graph permissions), it recorded it
# in the state file: PROVISIONER_CERT_REVOKE_PENDING (lowercase SHA-1 thumbprints) and
# PROVISIONER_CERT_REVOKE_PENDING_EXPIRES (their expiry dates, in the same order). Setup retries
# on every run; until then the health check says so.

PENDING = "PROVISIONER_CERT_REVOKE_PENDING"
PENDING_EXPIRES = "PROVISIONER_CERT_REVOKE_PENDING_EXPIRES"
OLD_KEY_SHA1 = "c3f1a2b4d5e6f708192a3b4c5d6e7f8091a2b3c4"
THIS_COMPUTERS_KEY_SHA1 = "77aa88bb99cc00dd11ee22ff33aa44bb55cc66dd"
OLD_KEYS_CHECK = "Older setup keys"


def _set_up_state(tmp_path: Path, **values: str) -> Path:
    state = tmp_path / ".entrabot-state.json"
    state.write_text(
        json.dumps(
            {
                "BLUEPRINT_APP_ID": "x",
                "AGENT_ID": "y",
                "AGENT_USER_ID": "z",
                "PROVISIONER_CLIENT_ID": "prov-app",
                "PROVISIONER_CERT_THUMBPRINT": THIS_COMPUTERS_KEY_SHA1,
                **values,
            }
        ),
        encoding="utf-8",
    )
    return state


def _pending(tmp_path: Path) -> Path:
    return _set_up_state(tmp_path, **{PENDING: OLD_KEY_SHA1, PENDING_EXPIRES: "2027-02-01"})


class TestCheckOldSetupKeys:
    def test_nothing_recorded_adds_nothing(self, tmp_path: Path) -> None:
        assert check_old_setup_keys(_set_up_state(tmp_path)) is None
        assert check_old_setup_keys(tmp_path / "missing.json") is None

    def test_a_pending_key_is_a_warning_that_names_it_and_where_to_remove_it(
        self, tmp_path: Path
    ) -> None:
        check = check_old_setup_keys(_pending(tmp_path))

        assert check is not None
        assert check.name == OLD_KEYS_CHECK and check.status == "warn"
        assert (
            "An older setup key for this computer could not be switched off in Microsoft Entra: "
            "the certificate whose thumbprint ends in B3C4 (expires 2027-02-01)." in check.detail
        )
        remediation = check.remediation or ""
        assert (
            "Microsoft Entra admin center (https://entra.microsoft.com): App registrations > "
            "'EntraBot Agent ID Provisioner' (application ID prov-app) > Certificates & secrets "
            "> Certificates." in remediation
        )
        assert remediation.endswith("Setup will try again the next time it runs.")

    @pytest.mark.parametrize("broken", ["an entry", "the file"])
    def test_a_record_that_cannot_be_read_is_a_warning_that_says_so(
        self, tmp_path: Path, broken: str
    ) -> None:
        if broken == "an entry":
            state = _set_up_state(tmp_path, **{PENDING: "not-a-thumbprint"})
        else:
            state = tmp_path / ".entrabot-state.json"
            state.write_text("{half written", encoding="utf-8")

        check = check_old_setup_keys(state)

        assert check is not None and check.status == "warn"
        assert check.detail.startswith(
            "Setup could not read its record of older setup keys for this computer."
        )
        assert "can't account for" in (check.remediation or "")

    def test_it_is_a_warning_not_a_failed_health_check(self, tmp_path: Path) -> None:
        check = check_old_setup_keys(_pending(tmp_path))
        assert check is not None
        assert overall_exit_code([check]) == 0
        rendered = format_report([check], color=False)
        assert rendered.startswith("[ WARN ] Older setup keys: An older setup key")
        assert "→ Remove it by hand in the Microsoft Entra admin center" in rendered

    def test_no_full_thumbprint_is_reported(self, tmp_path: Path) -> None:
        check = check_old_setup_keys(_pending(tmp_path))
        assert check is not None
        text = (format_report([check], color=False) + repr(check)).lower()
        for full in (OLD_KEY_SHA1, THIS_COMPUTERS_KEY_SHA1):
            assert full not in text and full[:36] not in text

    @pytest.mark.parametrize("windows", [False, True])
    def test_it_is_checked_on_every_platform(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: bool
    ) -> None:
        # The keys are only written on Windows, but a state file can travel; the check reads
        # what is there wherever it runs.
        monkeypatch.setattr("entrabot.preflight._is_windows", lambda: windows)
        assert check_old_setup_keys(_pending(tmp_path)) is not None

    def _diagnose(self, state: Path) -> list[Check]:
        with (
            patch(
                "entrabot.preflight.acquire_agent_user_token",
                side_effect=AgentIDNotAvailable("no cert"),
            ),
            patch(
                "entrabot.preflight._check_cert_in_keystore",
                return_value=Check(name="Blueprint cert in OS keystore", status="pass", detail="x"),
            ),
        ):
            return run_diagnostics(
                _config_complete(),
                state_path=state,
                expected_binary=state.parent / "bin" / "entrabot-mcp",
                claude_mcp_path=state.parent / "absent.json",
                copilot_mcp_path=state.parent / "absent2.json",
            )

    def test_diagnostics_report_it_right_after_the_state_file(self, tmp_path: Path) -> None:
        names = [check.name for check in self._diagnose(_pending(tmp_path))]
        assert names[:3] == ["State file", OLD_KEYS_CHECK, "Blueprint cert in OS keystore"]

    def test_diagnostics_with_nothing_pending_print_nothing_new(self, tmp_path: Path) -> None:
        names = [check.name for check in self._diagnose(_set_up_state(tmp_path))]
        assert OLD_KEYS_CHECK not in names
        assert names[:2] == ["State file", "Blueprint cert in OS keystore"]


# ─── format_report rendering ─────────────────────────────────────────────────


class TestFormatReport:
    def test_renders_status_glyphs_and_remediation(self) -> None:
        checks = [
            Check(name="X", status="pass", detail="ok"),
            Check(
                name="Y",
                status="fail",
                detail="boom",
                remediation="re-run setup.sh",
            ),
            Check(name="Z", status="warn", detail="careful", remediation="buy SKU"),
            Check(name="Q", status="skip", detail="cannot run"),
        ]
        rendered = format_report(checks, color=False)

        assert "PASS" in rendered or "✓" in rendered
        assert "FAIL" in rendered or "✗" in rendered
        assert "WARN" in rendered
        assert "re-run setup.sh" in rendered
        assert "buy SKU" in rendered

    def test_teams_capable_skus_constant_exposed(self) -> None:
        # Sanity check that we exposed the SKU list consumers can import.
        assert "ENTERPRISEPREMIUM" in TEAMS_CAPABLE_SKUS
        assert "SPE_E3" in TEAMS_CAPABLE_SKUS

    def test_copilot_capable_skus_constant_exposed(self) -> None:
        assert COPILOT_CAPABLE_SKUS == (
            "MICROSOFT_365_COPILOT",
            "Microsoft_365_Copilot",
        )


# ─── Windows ─────────────────────────────────────────────────────────────────


class TestWindows:
    """On Windows the key is a CNG key found by the SHA-1 in .env, the setup script is
    ``scripts\\setup-windows.cmd``, and the MCP binary is under ``.venv\\Scripts``."""

    SHA1 = "0123456789ABCDEF0123456789ABCDEF01234567"

    @pytest.fixture(autouse=True)
    def _windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from entrabot import preflight

        monkeypatch.setattr(preflight, "_is_windows", lambda: True)

        def no_keyring() -> None:
            raise AssertionError("Credential Manager must not be used on Windows")

        monkeypatch.setattr("entrabot.platform.get_credential_store", no_keyring)

    def _config(self, **overrides: object) -> EntraBotConfig:
        values: dict[str, object] = {
            "tenant_id": "t",
            "blueprint_app_id": "b",
            "blueprint_cert_thumbprint": "x" * 43,
            "blueprint_cert_sha1": self.SHA1,
        }
        values.update(overrides)
        return EntraBotConfig(**values)  # type: ignore[arg-type]

    def _health(self, status: str, message: str = "") -> object:
        from entrabot.platform import wincert

        return wincert.CertHealth(
            status=status, days_left=9, not_after=None, message=message or f"cert is {status}"
        )

    def _cert_check(
        self,
        monkeypatch: pytest.MonkeyPatch,
        status: str,
        *,
        sign_error: Exception | None = None,
        config: EntraBotConfig | None = None,
    ) -> Check:
        from entrabot import preflight
        from entrabot.platform import wincert

        signed: list[str] = []

        def test_sign(sha1: str) -> None:
            signed.append(sha1)
            if sign_error:
                raise sign_error

        monkeypatch.setattr(wincert, "cert_health", lambda sha1: self._health(status))
        monkeypatch.setattr(wincert, "test_sign", test_sign)
        result = preflight._check_cert_in_keystore(config or self._config())
        self.signed = signed
        return result

    def test_present_in_date_and_signing_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = self._cert_check(monkeypatch, "ok")
        assert result.status == "pass"
        assert self.signed == [self.SHA1]
        assert self.SHA1 not in result.detail

    def test_expiring_is_a_warning_with_what_to_do(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = self._cert_check(monkeypatch, "expiring")
        assert result.status == "warn"
        assert "setup-windows.cmd" in (result.remediation or "")

    @pytest.mark.parametrize("status", ["missing", "expired", "no_private_key", "not_yet_valid"])
    def test_unusable_certificate_fails_without_trying_to_sign(
        self, monkeypatch: pytest.MonkeyPatch, status: str
    ) -> None:
        result = self._cert_check(monkeypatch, status)
        assert result.status == "fail"
        assert result.detail == f"cert is {status}"
        assert "setup-windows.cmd" in (result.remediation or "")
        assert "setup.sh" not in (result.remediation or "")
        assert self.signed == []

    def test_a_key_that_cannot_sign_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from entrabot.platform import wincert

        result = self._cert_check(
            monkeypatch, "ok", sign_error=wincert.WinCertError("the key is not usable here")
        )
        assert result.status == "fail"
        assert "the key is not usable here" in result.detail

    def test_store_error_is_a_failure_not_a_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from entrabot import preflight
        from entrabot.platform import wincert

        def broken(sha1: str) -> None:
            raise wincert.WinCertError("could not open the certificate store")

        monkeypatch.setattr(wincert, "cert_health", broken)
        result = preflight._check_cert_in_keystore(self._config())
        assert result.status == "fail" and "certificate store" in result.detail

    def test_no_config_is_a_skip(self) -> None:
        from entrabot import preflight

        assert preflight._check_cert_in_keystore().status == "skip"

    def test_run_diagnostics_hands_the_config_to_the_certificate_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from entrabot import preflight

        seen: list[object] = []

        def record(config: EntraBotConfig | None = None) -> Check:
            seen.append(config)
            return Check(name="Blueprint cert in OS keystore", status="pass", detail="ok")

        monkeypatch.setattr(preflight, "_check_cert_in_keystore", record)
        monkeypatch.setattr(
            preflight,
            "acquire_agent_user_token",
            lambda config: (_ for _ in ()).throw(AgentIDNotAvailable("no cert")),
        )
        config = self._config()
        run_diagnostics(
            config,
            state_path=tmp_path / "missing.json",
            expected_binary=tmp_path / "x",
            claude_mcp_path=tmp_path / "a.json",
            copilot_mcp_path=tmp_path / "b.json",
        )
        assert seen == [config]

    def test_remediation_names_the_windows_setup_script(self, tmp_path: Path) -> None:
        missing = check_state_file(tmp_path / "nope.json")
        assert "scripts\\setup-windows.cmd" in (missing.remediation or "")
        assert "setup.sh" not in (missing.remediation or "")

        with patch(
            "entrabot.preflight.acquire_agent_user_token",
            side_effect=TokenExchangeError(hop="Hop 1", error="invalid_client", description="x"),
        ):
            token_check = run_smoke_checks(self._config())[0]
        assert "setup-windows.cmd -Diagnose" in (token_check.remediation or "")
        assert "setup.sh" not in (token_check.remediation or "")

        mcp = check_mcp_configs(
            expected_binary=tmp_path / "x.exe",
            claude_path=tmp_path / "absent.json",
            copilot_path=tmp_path / "absent2.json",
        )
        for check in mcp:
            assert "setup.sh" not in (check.remediation or "")

    def test_default_expected_binary(self, tmp_path: Path) -> None:
        from entrabot import preflight

        assert preflight.default_expected_binary(tmp_path) == (
            tmp_path / ".venv" / "Scripts" / "entrabot-mcp.exe"
        )

    def test_mcp_command_matches_whatever_its_case_or_exe_suffix(self, tmp_path: Path) -> None:
        expected = tmp_path / ".venv" / "Scripts" / "entrabot-mcp.exe"
        expected.parent.mkdir(parents=True)
        expected.write_bytes(b"")
        claude = tmp_path / ".mcp.json"
        for command in (
            str(expected),
            str(expected).upper(),
            str(expected)[: -len(".exe")],
            ".venv/Scripts/entrabot-mcp.exe",
        ):
            claude.write_text(
                json.dumps({"mcpServers": {"entrabot": {"command": command}}}), encoding="utf-8"
            )
            checks = check_mcp_configs(
                expected_binary=expected, claude_path=claude, copilot_path=tmp_path / "none.json"
            )
            assert checks[0].status == "pass", (command, checks[0])


class TestPosixExpectedBinary:
    def test_default_expected_binary(self, tmp_path: Path) -> None:
        from entrabot import preflight

        assert preflight.default_expected_binary(tmp_path) == (
            tmp_path / ".venv" / "bin" / "entrabot-mcp"
        )

    def test_case_still_matters_off_windows(self, tmp_path: Path) -> None:
        from entrabot import preflight

        assert preflight._comparable("/Repo/.venv/bin/entrabot-mcp") != preflight._comparable(
            "/repo/.venv/bin/entrabot-mcp"
        )


class TestFileEncodings:
    """These files are UTF-8 whatever the machine's ANSI code page is, and a Windows
    editor may have put a byte-order mark in front."""

    def test_state_file_with_a_bom_is_read(self, tmp_path: Path) -> None:
        state = tmp_path / ".entrabot-state.json"
        body = json.dumps({"BLUEPRINT_APP_ID": "x", "AGENT_ID": "y", "AGENT_USER_ID": "z"})
        state.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
        assert check_state_file(state).status == "pass"

    def test_mcp_config_with_a_non_ascii_path_under_a_windows_code_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.locale_codec import force_locale_codec

        home = tmp_path / "José Łukasz"
        expected = home / ".venv" / "bin" / "entrabot-mcp"
        expected.parent.mkdir(parents=True)
        expected.write_bytes(b"")
        claude = home / ".mcp.json"
        claude.write_bytes(
            json.dumps(
                {"mcpServers": {"entrabot": {"command": str(expected)}}}, ensure_ascii=False
            ).encode("utf-8")
        )
        force_locale_codec(monkeypatch)

        checks = check_mcp_configs(
            expected_binary=expected, claude_path=claude, copilot_path=tmp_path / "none.json"
        )
        assert checks[0].status == "pass", checks[0]
