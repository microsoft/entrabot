"""Pre-flight, smoke, and diagnose checks for setup.sh.

Designed for a non-technical operator (e.g., a VP) running ``./scripts/setup.sh``
once: they should either see a green confirmation that the install works end-to-end,
or a precise pointer to the broken hop. setup.sh shells out to short Python
one-liners that import from this module, so the logic itself is unit-testable
without bash.

Three entry points:

* :func:`check_teams_license_availability` — Step 2.5 of setup.sh, after
  ``az login``. Warns (never blocks) if no Teams-capable SKU has free seats.
* :func:`run_smoke_checks` — Step 8.5 of setup.sh, after MCP wiring. Verifies
  the three-hop token mint, the agent's Graph identity, and Teams scope.
* :func:`run_diagnostics` — ``./scripts/setup.sh --diagnose`` short-circuit.
  Superset of the smoke checks plus state file, cert keystore, and MCP wiring.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from entrabot.config import EntraBotConfig
from entrabot.errors import AgentIDNotAvailable, TokenExchangeError
from entrabot.tools.teams import acquire_agent_user_token

CheckStatus = Literal["pass", "warn", "fail", "skip"]


# Teams-capable SKUs. The single source of truth — ``scripts/create_entra_agent_ids.py``
# imports this constant. Union of every SKU part number we have observed assigning
# Teams to a user, including M365 E3/E5, Business Premium, Developer Pack, and
# the standalone Teams SKUs.
TEAMS_CAPABLE_SKUS: tuple[str, ...] = (
    "ENTERPRISEPREMIUM",  # M365 E5 / Office 365 E5
    "ENTERPRISEPACK",  # Office 365 E3
    "SPE_E3",  # M365 E3
    "SPE_E5",  # M365 E5 (alternate part number)
    "SPE_F1",  # M365 F1 (frontline)
    "STANDARDPACK",  # Office 365 E1
    "DEVELOPERPACK",  # Office 365 E3 Developer
    "DEVELOPERPACK_E5",  # M365 E5 Developer
    "M365_BUSINESS_PREMIUM",  # M365 Business Premium
    "O365_BUSINESS_PREMIUM",  # O365 Business Premium (legacy name)
    "Microsoft_Teams_Enterprise",
    "Microsoft_Teams_Essentials",
    "TEAMS_ESSENTIALS_AAD",
    "TEAMS_EXPLORATORY",
    "TEAMS_PREMIUM",
    "M365_E5_SUITE_COMPONENTS",
)

COPILOT_CAPABLE_SKUS: tuple[str, ...] = (
    "MICROSOFT_365_COPILOT",
    "Microsoft_365_Copilot",
)


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
LICENSE_PURCHASE_URL = (
    "https://admin.microsoft.com/Adminportal/Home#/catalog"
    " (or any Teams-capable license: M365 Business Premium, E3, E5, etc.)"
)


@dataclass(frozen=True)
class Check:
    """Result of a single pre-flight or smoke check."""

    name: str
    status: CheckStatus
    detail: str
    remediation: str | None = None


# ─── Phase 1: license preflight ──────────────────────────────────────────────


def check_teams_license_availability(
    token: str, *, transport: httpx.BaseTransport | None = None
) -> Check:
    """Query ``/subscribedSkus`` and report whether a Teams seat is free.

    Returns ``warn`` (never ``fail``) when no Teams-capable SKU is available —
    the goal is to warn the VP early, not block the install.
    """
    url = f"{GRAPH_BASE}/subscribedSkus"
    try:
        with httpx.Client(transport=transport, timeout=15.0) as client:
            resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        return Check(
            name="Teams license availability",
            status="skip",
            detail=f"Graph unreachable: {exc}",
        )

    if resp.status_code != 200:
        return Check(
            name="Teams license availability",
            status="skip",
            detail=f"Graph returned {resp.status_code}",
        )

    skus = resp.json().get("value", []) or []
    available: list[tuple[str, int]] = []
    for sku in skus:
        part = sku.get("skuPartNumber", "")
        if part not in TEAMS_CAPABLE_SKUS:
            continue
        prepaid = (sku.get("prepaidUnits") or {}).get("enabled", 0) or 0
        consumed = sku.get("consumedUnits", 0) or 0
        free = prepaid - consumed
        if free > 0:
            available.append((part, free))

    if available:
        descs = ", ".join(f"{part} ({free} free)" for part, free in available)
        return Check(
            name="Teams license availability",
            status="pass",
            detail=f"Teams-capable SKUs available: {descs}",
        )

    return Check(
        name="Teams license availability",
        status="warn",
        detail=(
            "No Teams-capable SKU has free seats in this tenant. setup.sh will "
            "still complete, but the agent user can't sign into Teams without "
            "a license."
        ),
        remediation=(
            f"Buy a Teams-capable license at {LICENSE_PURCHASE_URL} and re-run "
            "setup.sh, or assign an existing license manually before testing."
        ),
    )


def check_copilot_license_availability(
    token: str, *, transport: httpx.BaseTransport | None = None
) -> Check:
    """Query ``/subscribedSkus`` and report whether a Copilot seat is free."""
    url = f"{GRAPH_BASE}/subscribedSkus"
    try:
        with httpx.Client(transport=transport, timeout=15.0) as client:
            resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        return Check(
            name="Microsoft 365 Copilot license availability",
            status="skip",
            detail=f"Graph unreachable: {exc}",
        )

    if resp.status_code != 200:
        return Check(
            name="Microsoft 365 Copilot license availability",
            status="skip",
            detail=f"Graph returned {resp.status_code}",
        )

    available: list[tuple[str, int]] = []
    for sku in resp.json().get("value", []) or []:
        part = sku.get("skuPartNumber", "")
        if part not in COPILOT_CAPABLE_SKUS:
            continue
        prepaid = (sku.get("prepaidUnits") or {}).get("enabled", 0) or 0
        consumed = sku.get("consumedUnits", 0) or 0
        free = prepaid - consumed
        if free > 0:
            available.append((part, free))

    if available:
        descs = ", ".join(f"{part} ({free} free)" for part, free in available)
        return Check(
            name="Microsoft 365 Copilot license availability",
            status="pass",
            detail=f"Copilot-capable SKUs available: {descs}",
        )

    return Check(
        name="Microsoft 365 Copilot license availability",
        status="warn",
        detail=(
            "No Microsoft 365 Copilot SKU has free seats in this tenant. Work IQ "
            "MCP servers require Microsoft 365 Copilot for the calling Agent User."
        ),
        remediation=(
            "Buy or free a Microsoft 365 Copilot license and re-run setup.sh, "
            "or assign it manually before testing Work IQ Word."
        ),
    )


# ─── Phase 2: smoke checks ───────────────────────────────────────────────────


def _token_mint_check(config: EntraBotConfig) -> tuple[Check, str | None]:
    try:
        token = acquire_agent_user_token(config)
    except AgentIDNotAvailable as exc:
        return (
            Check(
                name="Token mint (three-hop)",
                status="fail",
                detail=f"Agent identity not bootstrapped: {exc}",
                remediation=(
                    "Run ./scripts/setup.sh end-to-end — the state file or certificate is missing."
                ),
            ),
            None,
        )
    except TokenExchangeError as exc:
        return (
            Check(
                name="Token mint (three-hop)",
                status="fail",
                detail=f"{exc.hop} failed — {exc.error}: {exc.description}",
                remediation=(
                    "Re-run ./scripts/setup.sh --diagnose to see which hop failed "
                    "and consult docs/troubleshooting/setup-and-authentication.md."
                ),
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001 — last-ditch safety net
        return (
            Check(
                name="Token mint (three-hop)",
                status="fail",
                detail=f"Unexpected error: {exc}",
            ),
            None,
        )
    return (
        Check(
            name="Token mint (three-hop)",
            status="pass",
            detail="Acquired Agent User token via Blueprint -> Agent Identity -> Agent User.",
        ),
        token,
    )


def _me_identity_check(
    config: EntraBotConfig,
    token: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Check:
    url = f"{GRAPH_BASE}/me"
    try:
        with httpx.Client(transport=transport, timeout=15.0) as client:
            resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        return Check(
            name="Graph /me identity",
            status="fail",
            detail=f"Graph /me unreachable: {exc}",
        )

    if resp.status_code != 200:
        return Check(
            name="Graph /me identity",
            status="fail",
            detail=f"Graph /me returned {resp.status_code}: {resp.text[:200]}",
            remediation=(
                "The Agent User token doesn't carry user scope. Verify the "
                "third-hop user_fic grant in scripts/create_entra_agent_ids.py."
            ),
        )

    body = resp.json() or {}
    upn = body.get("userPrincipalName", "")
    if config.agent_user_upn and upn != config.agent_user_upn:
        return Check(
            name="Graph /me identity",
            status="fail",
            detail=(f"Token belongs to {upn!r}, expected {config.agent_user_upn!r}."),
            remediation=(
                "The .env state file points at a different agent user than "
                "the token resolves to. Re-run setup.sh."
            ),
        )

    return Check(
        name="Graph /me identity",
        status="pass",
        detail=f"Token resolves to {upn} (expected).",
    )


def _chats_scope_check(token: str, *, transport: httpx.BaseTransport | None = None) -> Check:
    url = f"{GRAPH_BASE}/me/chats?$top=1"
    try:
        with httpx.Client(transport=transport, timeout=15.0) as client:
            resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        return Check(
            name="Teams scope (/me/chats)",
            status="fail",
            detail=f"Graph unreachable: {exc}",
        )

    if resp.status_code == 200:
        return Check(
            name="Teams scope (/me/chats)",
            status="pass",
            detail="Agent user has Teams chat scope.",
        )

    if resp.status_code in (401, 403):
        return Check(
            name="Teams scope (/me/chats)",
            status="fail",
            detail=f"Graph returned {resp.status_code} for /me/chats.",
            remediation=(
                "Teams replication can take 10-15 minutes after license "
                "assignment. Wait, then re-run ./scripts/setup.sh --diagnose. "
                "If the failure persists, check that the agent user has a "
                "Teams-capable license assigned."
            ),
        )

    return Check(
        name="Teams scope (/me/chats)",
        status="fail",
        detail=f"Graph returned {resp.status_code}: {resp.text[:200]}",
    )


def run_smoke_checks(
    config: EntraBotConfig, *, transport: httpx.BaseTransport | None = None
) -> list[Check]:
    """Run the post-setup smoke battery: token + /me + /me/chats.

    Short-circuits on token mint failure — the remaining checks return ``skip``.
    """
    checks: list[Check] = []
    token_check, token = _token_mint_check(config)
    checks.append(token_check)
    if token is None:
        checks.append(
            Check(
                name="Graph /me identity",
                status="skip",
                detail="Skipped — token mint failed.",
            )
        )
        checks.append(
            Check(
                name="Teams scope (/me/chats)",
                status="skip",
                detail="Skipped — token mint failed.",
            )
        )
        return checks

    checks.append(_me_identity_check(config, token, transport=transport))
    checks.append(_chats_scope_check(token, transport=transport))
    return checks


# ─── Phase 3: diagnostics extras ─────────────────────────────────────────────


_REQUIRED_STATE_KEYS = ("BLUEPRINT_APP_ID", "AGENT_ID", "AGENT_USER_ID")


def check_state_file(path: Path) -> Check:
    if not path.exists():
        return Check(
            name="State file",
            status="fail",
            detail=f"{path} does not exist.",
            remediation="Run ./scripts/setup.sh — provisioning has not been completed.",
        )
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return Check(
            name="State file",
            status="fail",
            detail=f"{path} is unreadable: {exc}",
            remediation="Delete the file and re-run ./scripts/setup.sh.",
        )

    missing = [key for key in _REQUIRED_STATE_KEYS if not data.get(key)]
    if missing:
        return Check(
            name="State file",
            status="fail",
            detail=f"Missing keys: {', '.join(missing)}",
            remediation="Re-run ./scripts/setup.sh to repopulate the state file.",
        )

    return Check(
        name="State file",
        status="pass",
        detail=f"{path} has all required keys.",
    )


def _check_cert_in_keystore() -> Check:
    """Default cert presence check. Tests patch this for hermeticity."""
    try:
        from entrabot.platform import get_credential_store
    except Exception as exc:  # pragma: no cover — import failures are platform-level
        return Check(
            name="Blueprint cert in OS keystore",
            status="skip",
            detail=f"Could not load credential store: {exc}",
        )
    try:
        store = get_credential_store()
        pem = store.retrieve("entrabot", "blueprint-private-key")
    except Exception as exc:  # noqa: BLE001
        return Check(
            name="Blueprint cert in OS keystore",
            status="fail",
            detail=f"Keystore error: {exc}",
            remediation="Re-run ./scripts/setup.sh to re-generate and store the cert.",
        )

    if not pem:
        return Check(
            name="Blueprint cert in OS keystore",
            status="fail",
            detail="No blueprint-private-key in OS keystore.",
            remediation="Re-run ./scripts/setup.sh to regenerate the certificate.",
        )

    return Check(
        name="Blueprint cert in OS keystore",
        status="pass",
        detail="Blueprint private key present in OS keystore.",
    )


def _read_mcp_command(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    servers = data.get("mcpServers") or {}
    entry = servers.get("entrabot") or {}
    cmd = entry.get("command")
    if isinstance(cmd, str):
        return cmd
    return None


def _paths_equivalent(observed: str, expected: Path, config_path: Path) -> bool:
    """Return True if ``observed`` (as written in MCP config) resolves to ``expected``.

    The Claude .mcp.json deliberately writes a relative path like
    ``.venv/bin/entrabot-mcp`` so the file is portable. Resolve relative
    paths against the config file's parent, then compare to the absolute
    expected binary.
    """
    obs_path = Path(observed)
    if not obs_path.is_absolute():
        obs_path = (config_path.parent / obs_path).resolve()
    else:
        obs_path = obs_path.resolve()
    try:
        return obs_path == expected.resolve()
    except (OSError, RuntimeError):
        return obs_path == expected


def check_mcp_configs(
    *,
    expected_binary: Path,
    claude_path: Path,
    copilot_path: Path,
) -> list[Check]:
    """Verify Claude Code's ``.mcp.json`` and Copilot CLI's ``mcp-config.json``.

    Claude Code config is mandatory — its absence is a fail. Copilot CLI is
    optional (not every developer installs it) — absence is a warn.
    """
    expected = str(expected_binary)
    checks: list[Check] = []

    # Claude Code
    if not claude_path.exists():
        checks.append(
            Check(
                name="Claude Code MCP config",
                status="fail",
                detail=f"{claude_path} not found.",
                remediation="Re-run ./scripts/setup.sh to write .mcp.json.",
            )
        )
    else:
        cmd = _read_mcp_command(claude_path)
        if cmd is None:
            checks.append(
                Check(
                    name="Claude Code MCP config",
                    status="fail",
                    detail=f"{claude_path} has no entrabot entry.",
                    remediation="Re-run ./scripts/setup.sh to write .mcp.json.",
                )
            )
        elif not _paths_equivalent(cmd, expected_binary, claude_path):
            checks.append(
                Check(
                    name="Claude Code MCP config",
                    status="fail",
                    detail=(f"Entry points at {cmd!r}, expected {expected!r}. Worktree drift?"),
                    remediation=(
                        "Re-run ./scripts/setup.sh from the main checkout to rewrite the MCP entry."
                    ),
                )
            )
        else:
            checks.append(
                Check(
                    name="Claude Code MCP config",
                    status="pass",
                    detail=f"{claude_path} -> {expected}",
                )
            )

    # Copilot CLI
    if not copilot_path.exists():
        checks.append(
            Check(
                name="Copilot CLI MCP config",
                status="warn",
                detail=f"{copilot_path} not found (Copilot CLI not installed?).",
                remediation=(
                    "Install Copilot CLI and re-run setup.sh to wire it up, "
                    "or ignore if you only use Claude Code."
                ),
            )
        )
    else:
        cmd = _read_mcp_command(copilot_path)
        if cmd is None:
            checks.append(
                Check(
                    name="Copilot CLI MCP config",
                    status="warn",
                    detail=f"{copilot_path} has no entrabot entry.",
                    remediation="Re-run ./scripts/setup.sh.",
                )
            )
        elif not _paths_equivalent(cmd, expected_binary, copilot_path):
            # Copilot drift is a warn, not a fail: it's common for developers
            # to have multiple checkouts/worktrees, and the global Copilot
            # config can legitimately point at a different one.
            checks.append(
                Check(
                    name="Copilot CLI MCP config",
                    status="warn",
                    detail=(
                        f"Entry points at {cmd!r}, expected {expected!r}. "
                        "Possibly pointing at another checkout/worktree."
                    ),
                    remediation=(
                        "Re-run ./scripts/setup.sh from the checkout you want "
                        "Copilot to use, or ignore if intentional."
                    ),
                )
            )
        else:
            checks.append(
                Check(
                    name="Copilot CLI MCP config",
                    status="pass",
                    detail=f"{copilot_path} -> {expected}",
                )
            )

    return checks


def run_diagnostics(
    config: EntraBotConfig,
    *,
    state_path: Path,
    expected_binary: Path,
    claude_mcp_path: Path,
    copilot_mcp_path: Path,
    transport: httpx.BaseTransport | None = None,
) -> list[Check]:
    """Full health check for ``./scripts/setup.sh --diagnose``."""
    checks: list[Check] = []
    checks.append(check_state_file(state_path))
    checks.append(_check_cert_in_keystore())
    checks.extend(run_smoke_checks(config, transport=transport))
    checks.extend(
        check_mcp_configs(
            expected_binary=expected_binary,
            claude_path=claude_mcp_path,
            copilot_path=copilot_mcp_path,
        )
    )
    return checks


# ─── Reporting ───────────────────────────────────────────────────────────────


_GLYPHS_COLOR = {
    "pass": "\033[32m✓ PASS\033[0m",
    "warn": "\033[33m⚠ WARN\033[0m",
    "fail": "\033[31m✗ FAIL\033[0m",
    "skip": "\033[90m· SKIP\033[0m",
}
_GLYPHS_PLAIN = {
    "pass": "[ PASS ]",
    "warn": "[ WARN ]",
    "fail": "[ FAIL ]",
    "skip": "[ SKIP ]",
}


def format_report(checks: Iterable[Check], *, color: bool = True) -> str:
    glyphs = _GLYPHS_COLOR if color else _GLYPHS_PLAIN
    lines: list[str] = []
    for check in checks:
        lines.append(f"{glyphs[check.status]} {check.name}: {check.detail}")
        if check.remediation and check.status in ("fail", "warn"):
            lines.append(f"        → {check.remediation}")
    return "\n".join(lines)


def overall_exit_code(checks: Iterable[Check]) -> int:
    """Return 0 if no checks failed, 1 otherwise. Warns are non-blocking."""
    return 1 if any(c.status == "fail" for c in checks) else 0


__all__ = [
    "Check",
    "COPILOT_CAPABLE_SKUS",
    "TEAMS_CAPABLE_SKUS",
    "check_copilot_license_availability",
    "check_mcp_configs",
    "check_state_file",
    "check_teams_license_availability",
    "format_report",
    "overall_exit_code",
    "run_diagnostics",
    "run_smoke_checks",
]
