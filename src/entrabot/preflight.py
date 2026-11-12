"""Pre-flight, smoke, and diagnose checks for setup (setup.sh and the Windows wizard).

Designed for a non-technical operator (e.g., a VP) running ``./scripts/setup.sh``
or ``scripts\\setup-windows.cmd`` once: they should either see a green confirmation
that the install works end-to-end, or a precise pointer to the broken hop. setup.sh
shells out to short Python one-liners that import from this module, and the Windows
wizard calls it directly, so the logic itself is unit-testable without a shell.

Remediation text names the setup script of the platform it runs on, and the
certificate check looks where that platform keeps the key: the OS keystore on
macOS/Linux, the certificate store (a non-exportable CNG key) on Windows.

Three entry points:

* :func:`check_teams_license_availability` — Step 2.5 of setup.sh, after
  ``az login``. Warns (never blocks) if no Teams-capable SKU has free seats.
* :func:`run_smoke_checks` — Step 8.5 of setup.sh, after MCP wiring. Verifies
  the three-hop token mint, the agent's Graph identity, and Teams scope.
* :func:`run_diagnostics` — ``./scripts/setup.sh --diagnose`` short-circuit.
  Superset of the smoke checks plus state file, cert keystore, MCP wiring, and
  :func:`check_persona_sati_mcp` (optional, never fails). An older setup key that
  setup could not switch off (:func:`check_old_setup_keys`) is a warning, listed
  only when the state file records one.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shlex
import shutil
import socket
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx

from entrabot import licensing
from entrabot.config import EntraBotConfig
from entrabot.errors import AgentIDNotAvailable, TokenExchangeError
from entrabot.tools.teams import acquire_agent_user_token
from entrabot.winsetup import oldkeys

CheckStatus = Literal["pass", "warn", "fail", "skip"]


# Which products include Teams, or Microsoft 365 Copilot, is decided in entrabot.licensing
# from each product's service plans. These name lists are its fallback for an answer that
# carries no service plans, kept importable from here for scripts/create_entra_agent_ids.py
# and scripts/assign_agent_user_licenses.py.
TEAMS_CAPABLE_SKUS: tuple[str, ...] = licensing.TEAMS_CAPABLE_SKUS
COPILOT_CAPABLE_SKUS: tuple[str, ...] = licensing.COPILOT_CAPABLE_SKUS


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
LICENSE_PURCHASE_URL = (
    "https://admin.microsoft.com/Adminportal/Home#/catalog"
    " (or any Teams-capable license: M365 Business Premium, E3, E5, etc.)"
)


def _is_windows() -> bool:
    return sys.platform == "win32"


def _setup_name() -> str:
    """The setup script's name, as someone would say it."""
    return "setup-windows.cmd" if _is_windows() else "setup.sh"


def _setup_command() -> str:
    """The setup command to run from the repository root."""
    return r"scripts\setup-windows.cmd" if _is_windows() else "./scripts/setup.sh"


def _diagnose_command() -> str:
    return f"{_setup_command()} -Diagnose" if _is_windows() else "./scripts/setup.sh --diagnose"


def default_expected_binary(project_root: Path) -> Path:
    """Where setup installs the MCP server binary for this platform."""
    if _is_windows():
        return project_root / ".venv" / "Scripts" / "entrabot-mcp.exe"
    return project_root / ".venv" / "bin" / "entrabot-mcp"


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
        product = licensing.describe(sku) if isinstance(sku, dict) else None
        if product is not None and product.teams and product.free > 0:
            available.append((product.part_number, product.free))

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
            f"No Teams-capable SKU has free seats in this tenant. {_setup_name()} will "
            "still complete, but the agent user can't sign into Teams without "
            "a license."
        ),
        remediation=(
            f"Buy a Teams-capable license at {LICENSE_PURCHASE_URL} and re-run "
            f"{_setup_name()}, or assign an existing license manually before testing."
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
        product = licensing.describe(sku) if isinstance(sku, dict) else None
        if product is not None and product.copilot and product.free > 0:
            available.append((product.part_number, product.free))

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
            f"Buy or free a Microsoft 365 Copilot license and re-run {_setup_name()}, "
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
                    f"Run {_setup_command()} end-to-end — the state file or certificate is missing."
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
                    f"Re-run {_diagnose_command()} to see which hop failed "
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
                f"the token resolves to. Re-run {_setup_name()}."
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
                f"assignment. Wait, then re-run {_diagnose_command()}. "
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
            remediation=f"Run {_setup_command()} — provisioning has not been completed.",
        )
    try:
        # UTF-8 whatever the machine's code page; a Windows editor may add a BOM.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        return Check(
            name="State file",
            status="fail",
            detail=f"{path} is unreadable: {exc}",
            remediation=f"Delete the file and re-run {_setup_command()}.",
        )

    missing = [key for key in _REQUIRED_STATE_KEYS if not data.get(key)]
    if missing:
        return Check(
            name="State file",
            status="fail",
            detail=f"Missing keys: {', '.join(missing)}",
            remediation=f"Re-run {_setup_command()} to repopulate the state file.",
        )

    return Check(
        name="State file",
        status="pass",
        detail=f"{path} has all required keys.",
    )


def check_old_setup_keys(path: Path) -> Check | None:
    """Older setup keys that setup could not switch off in Microsoft Entra, from the state
    file's record of them (``entrabot.winsetup.oldkeys``, which setup and ``status`` read too).

    An older install kept the Provisioner's private key in a plain file, and the Provisioner
    app can manage applications across the tenant, so a certificate still listed there is
    reported: a ``warn`` naming it by the end of its thumbprint, never a failure of sign-in
    health. A record that can't be read is a ``warn`` too. None when nothing is recorded, so
    nothing is added to the report. The keys are written on Windows, but the state file is
    read wherever this runs.
    """
    finding = oldkeys.old_setup_key_check(oldkeys.read_old_setup_keys(path))
    if finding is None:
        return None
    return Check(
        name=oldkeys.CHECK_NAME, status="warn", detail=finding.detail, remediation=finding.fix
    )


_CERT_CHECK_NAME = "Blueprint cert in OS keystore"


def _check_cert_in_windows_store(config: EntraBotConfig | None) -> Check:
    """Windows: the certificate named in .env is in this user's store, in date, and
    its (non-exportable) key actually signs. Credential Manager is not involved."""
    if config is None:
        return Check(
            name=_CERT_CHECK_NAME,
            status="skip",
            detail="No configuration to check the certificate against.",
        )
    from entrabot.platform import wincert

    remediation = f"Run {_setup_command()} as the user who runs Entrabot."
    sha1 = config.blueprint_cert_sha1
    try:
        health = wincert.cert_health(sha1)
    except Exception as exc:  # noqa: BLE001 — a diagnostic must report, not crash
        return Check(
            name=_CERT_CHECK_NAME,
            status="fail",
            detail=f"Could not read the certificate store: {exc}",
            remediation=remediation,
        )
    if not health.usable:
        return Check(
            name=_CERT_CHECK_NAME, status="fail", detail=health.message, remediation=remediation
        )
    try:
        wincert.test_sign(sha1 or "")
    except wincert.WinCertError as exc:
        return Check(name=_CERT_CHECK_NAME, status="fail", detail=str(exc), remediation=remediation)
    if health.status == "expiring":
        return Check(
            name=_CERT_CHECK_NAME,
            status="warn",
            detail=health.message,
            remediation=f"Re-run {_setup_command()} to renew the certificate before it expires.",
        )
    return Check(
        name=_CERT_CHECK_NAME,
        status="pass",
        detail=f"{health.message} Its private key signs.",
    )


def _check_cert_in_keystore(config: EntraBotConfig | None = None) -> Check:
    """Default cert presence check. Tests patch this for hermeticity."""
    if _is_windows():
        return _check_cert_in_windows_store(config)
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
            remediation=f"Re-run {_setup_command()} to re-generate and store the cert.",
        )

    if not pem:
        return Check(
            name="Blueprint cert in OS keystore",
            status="fail",
            detail="No blueprint-private-key in OS keystore.",
            remediation=f"Re-run {_setup_command()} to regenerate the certificate.",
        )

    return Check(
        name="Blueprint cert in OS keystore",
        status="pass",
        detail="Blueprint private key present in OS keystore.",
    )


def _read_mcp_command(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return None
    if not isinstance(data, dict):
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
        resolved_expected = expected.resolve()
    except (OSError, RuntimeError):
        resolved_expected = expected
    return _comparable(obs_path) == _comparable(resolved_expected)


def _comparable(path: Path | str) -> str:
    """A path in the form two spellings of the same file agree on.

    Windows paths don't distinguish case or slash direction, and ``entrabot-mcp``
    runs ``entrabot-mcp.exe``. Elsewhere the path is compared as written.
    """
    text = str(path)
    if _is_windows():
        text = text.replace("\\", "/").lower()
        if text.endswith(".exe"):
            text = text[: -len(".exe")]
    return text


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
                remediation=f"Re-run {_setup_command()} to write .mcp.json.",
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
                    remediation=f"Re-run {_setup_command()} to write .mcp.json.",
                )
            )
        elif not _paths_equivalent(cmd, expected_binary, claude_path):
            checks.append(
                Check(
                    name="Claude Code MCP config",
                    status="fail",
                    detail=(f"Entry points at {cmd!r}, expected {expected!r}. Worktree drift?"),
                    remediation=(
                        f"Re-run {_setup_command()} from the main checkout to rewrite "
                        "the MCP entry."
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
                    f"Install Copilot CLI and re-run {_setup_name()} to wire it up, "
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
                    remediation=f"Re-run {_setup_command()}.",
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
                        f"Re-run {_setup_command()} from the checkout you want "
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


_TOKEN_ENV = "PERSONA_SATI_MCP_TOKEN_COMMAND"
_URL_TRANSPORTS = frozenset({"sse", "http", "streamable-http", "streamable_http"})


def _is_placeholder(value: object) -> bool:
    text = value.strip() if isinstance(value, str) else ""
    return text.startswith("<") and text.endswith(">")


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback  # all of 127.0.0.0/8, and ::1
    except ValueError:
        return False


def _find_program(command: str, base: Path, env: object, *, any_file: bool = False) -> str | None:
    """The runnable file ``command`` names, or None.

    Relative paths resolve against ``base``. Bare names are looked up on the ``PATH`` in
    the entry's own ``env``, because that's what the host runs them with, or on this
    process's ``PATH`` if the entry doesn't set one. ``any_file`` also accepts a file that
    isn't marked executable: Windows has no executable bit, and there ``which`` goes by
    ``PATHEXT``, which never lists ``.ps1`` and may not list ``.py``.
    """
    bare = "/" not in command and "\\" not in command
    path = env.get("PATH") if isinstance(env, dict) else None
    path = path if isinstance(path, str) else None
    target = command if bare else str(base / command)
    found = shutil.which(target, path=path)
    if found is None and any_file:
        found = shutil.which(target, mode=os.F_OK, path=path)
    return found


def _is_executable(command: str, base: Path, env: object) -> bool:
    """True if ``command`` is a runnable file (see :func:`_find_program`)."""
    return _find_program(command, base, env) is not None


def _windows_leading_parts(command: str) -> list[str]:
    r"""Every leading part of a Windows command line that could be its program, longest
    first: ``C:\Program Files\x\t.cmd --flag`` gives that whole text, then
    ``C:\Program Files\x\t.cmd``, then ``C:\Program``. A quoted part counts only when
    it is one quoted word, and is returned without its quotes. An unbalanced quote gives
    nothing: that is not something to guess at.
    """
    ends: list[int] = []
    quoted = False
    for index, char in enumerate(command):
        if char == '"':
            quoted = not quoted
        elif char.isspace() and not quoted and index > 0 and not command[index - 1].isspace():
            ends.append(index)
    if quoted:
        return []
    ends.append(len(command))
    parts: list[str] = []
    for end in reversed(ends):
        candidate = command[:end].strip()
        if '"' in candidate:
            if not (candidate.count('"') == 2 and candidate[0] == candidate[-1] == '"'):
                continue
            candidate = candidate[1:-1]
        if candidate:
            parts.append(candidate)
    return parts


def _token_command_runs(command: str, base: Path, env: object) -> bool:
    """True if the server could start ``PERSONA_SATI_MCP_TOKEN_COMMAND``.

    This follows ``entrabot.mcp_server._persona_token_words``, which decides what the
    server actually starts: the whole value as one program first (so a path with spaces
    needs no quoting), otherwise a program followed by arguments, split by the platform's
    rules. On POSIX that is shell quoting. On Windows the program is the longest leading
    part that names a file, and a script (``.ps1``, ``.py``) counts, because the server
    hands those to their interpreter.

    The rule is restated here rather than imported: importing ``entrabot.mcp_server``
    builds the server, which runs this very command and contacts persona-sati, and a
    health check must do neither. Nothing here runs the command.
    """
    windows = _is_windows()
    if _find_program(command, base, env, any_file=windows) is not None:
        return True
    if windows:
        return any(
            _find_program(part, base, env, any_file=True) is not None
            for part in _windows_leading_parts(command)
        )
    try:
        words = shlex.split(command)
    except ValueError:  # an unbalanced quote
        return False
    return bool(words) and _find_program(words[0], base, env) is not None


def check_persona_sati_mcp(claude_path: Path) -> Check:
    """Check the optional ``persona-sati`` entry in Claude Code's ``.mcp.json``.

    Never ``fail``: no entry is ``skip``, misconfiguration is ``warn``. Only a loopback URL
    is contacted (one 1s TCP connect). Details show only the command path and the URL's
    scheme, host and port, never env values, headers, userinfo, URL paths or queries.
    """
    name = "persona-sati MCP (optional)"
    try:
        servers = json.loads(claude_path.read_text(encoding="utf-8-sig")).get("mcpServers") or {}
        entry, entrabot = servers.get("persona-sati"), servers.get("entrabot")
    except (OSError, ValueError, AttributeError):  # missing, unparsable, or wrong shape
        entry = entrabot = None
    if not isinstance(entry, dict):
        detail = "No persona-sati entry in the MCP config (optional; the agent runs standalone)."
        return Check(name=name, status="skip", detail=detail)

    env = entrabot.get("env") if isinstance(entrabot, dict) else None
    env = env if isinstance(env, dict) else {}
    url, command = entry.get("url"), entry.get("command")
    # Follow the declared transport, as the runtime does, so an entry whose fields don't
    # match it can't pass. Only an entry that declares nothing is inferred from its fields.
    declared = entry.get("type") or entry.get("transport")
    kind = str(declared).lower() if declared else ("sse" if isinstance(url, str) else "stdio")
    problems: list[str] = []
    ok = ""
    if kind in _URL_TRANSPORTS and not isinstance(url, str):
        problems.append(f"{kind} entry has no url")
    elif kind in _URL_TRANSPORTS:
        try:
            parts = urlsplit(url)
            scheme, host, port = parts.scheme, parts.hostname, parts.port
        except ValueError:  # malformed; never echo it, the message can quote the bad part
            scheme = host = port = None
        if scheme not in ("http", "https") or not host:
            problems.append("url is not a valid http(s) URL (unfilled placeholder?)")
        else:
            port = port or (443 if scheme == "https" else 80)
            shown = f"{scheme}://{f'[{host}]' if ':' in host else host}:{port}"
            if not _is_loopback(host):
                ok = f"{shown} is a remote host (not contacted)."
            else:
                try:
                    socket.create_connection((host, port), timeout=1.0).close()
                except OSError:
                    problems.append(
                        f"nothing is listening on {shown} "
                        "(this does not mean a hosted persona-sati service is down)"
                    )
                else:
                    ok = f"{shown} is accepting connections."
    elif kind != "stdio":
        problems.append("entry has an unsupported transport type")
    elif not isinstance(command, str) or not command:
        problems.append("stdio entry has no command")
    elif _is_placeholder(command):  # hosts exec the command as written, unstripped
        problems.append(f"command {command} is an unfilled placeholder")
    elif not _is_executable(command, claude_path.parent, entry.get("env")):
        problems.append(f"command {command} was not found or is not executable")
    else:
        ok = f"stdio launcher {command} is present and executable."

    token = str(env.get(_TOKEN_ENV) or "").strip()  # run directly, so it must be runnable
    for var in ("PERSONA_SATI_MCP_URL", _TOKEN_ENV):
        if _is_placeholder(env.get(var)):
            problems.append(f"{var} in the entrabot env is an unfilled placeholder")
    runnable = _token_command_runs(token, claude_path.parent, env) if token else False
    if token and not _is_placeholder(token) and not runnable:
        problems.append(f"{_TOKEN_ENV} in the entrabot env was not found or is not executable")

    if not problems:
        return Check(name=name, status="pass", detail=ok)
    return Check(
        name=name,
        status="warn",
        detail="; ".join(problems) + ".",
        remediation=(
            "persona-sati is optional: to run standalone, delete this entry and remove "
            f"PERSONA_SATI_MCP_URL and {_TOKEN_ENV} from the entrabot entry's env. If it is "
            "hosted, use the client launcher from your persona-sati install, per its own setup "
            "documentation."
        ),
    )


def run_diagnostics(
    config: EntraBotConfig,
    *,
    state_path: Path,
    expected_binary: Path,
    claude_mcp_path: Path,
    copilot_mcp_path: Path,
    transport: httpx.BaseTransport | None = None,
) -> list[Check]:
    """Full health check for ``./scripts/setup.sh --diagnose`` and the Windows wizard.

    ``expected_binary`` is the MCP server setup installed; see
    :func:`default_expected_binary`.
    """
    checks: list[Check] = []
    checks.append(check_state_file(state_path))
    old_setup_keys = check_old_setup_keys(state_path)
    if old_setup_keys is not None:
        checks.append(old_setup_keys)
    checks.append(_check_cert_in_keystore(config))
    checks.extend(run_smoke_checks(config, transport=transport))
    checks.extend(
        check_mcp_configs(
            expected_binary=expected_binary,
            claude_path=claude_mcp_path,
            copilot_path=copilot_mcp_path,
        )
    )
    checks.append(check_persona_sati_mcp(claude_mcp_path))
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
    "check_old_setup_keys",
    "check_persona_sati_mcp",
    "check_state_file",
    "check_teams_license_availability",
    "default_expected_binary",
    "format_report",
    "overall_exit_code",
    "run_diagnostics",
    "run_smoke_checks",
]
