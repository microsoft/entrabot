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
from pathlib import Path
from unittest.mock import patch

import httpx
import respx

from entrabot.config import EntraBotConfig
from entrabot.errors import AgentIDNotAvailable, TokenExchangeError
from entrabot.preflight import (
    COPILOT_CAPABLE_SKUS,
    TEAMS_CAPABLE_SKUS,
    Check,
    check_copilot_license_availability,
    check_mcp_configs,
    check_state_file,
    check_teams_license_availability,
    format_report,
    run_diagnostics,
    run_smoke_checks,
)

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
                {
                    "mcpServers": {
                        "entrabot": {"command": "/other/worktree/.venv/bin/entrabot-mcp"}
                    }
                }
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
