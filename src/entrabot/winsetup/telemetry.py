"""Optional: Agent 365 telemetry (Microsoft OpenTelemetry Distro, delegated route).

Setup switches it on by itself, the same way as Word, Excel and OneDrive files
(:mod:`entrabot.winsetup.workiq`), with no Agent 365 CLI:

1. Run ``scripts/ensure_a365_observability_permissions.py`` with the setup (Provisioner)
   credential: the Agent 365 Observability service principal if the tenant lacks it, the
   Blueprint principal's tenant-wide grant of the delegated
   ``Agent365.Observability.OtelWrite`` scope, the Blueprint's inheritable permission for it,
   and the Agent Identity's grant for its Agent User. Each is read before it is written, and
   a conflicting write is settled by reading it back.
2. Check, as the agent and exactly the way the runtime asks
   (:mod:`entrabot.observability.tokens`), that its token carries the scope. Microsoft can
   take a little while to honour new grants, so the check is tried for about two minutes.
3. Only then write the runtime switches into ``.env``
   (``ENTRABOT_A365_OBSERVABILITY_ENABLED`` and ``ENTRABOT_A365_EXPORT_ENABLED``) and read
   them back.

A failure in step 1 changes nothing in ``.env``. Setup never stops for it. No token is ever
printed or logged.

Microsoft keeps the spans only when at least one person in the organization has a Microsoft
365 E7 or Microsoft Agent 365 licence assigned (Microsoft Learn, "Agent 365 observability
concepts", drop conditions). Setup cannot see that, and says so.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from entrabot.winsetup.console import Console
from entrabot.winsetup.envfile import EnvGuard, read_env, update_env
from entrabot.winsetup.envstep import Identity
from entrabot.winsetup.helpers import RESULT_MARKER, Helpers, Paths, marked_line
from entrabot.winsetup.workiq import CHECK_WAITS as WORK_IQ_CHECK_WAITS

PERMISSIONS_SCRIPT = "ensure_a365_observability_permissions.py"
# The same wait as Work IQ: about two minutes in all.
CHECK_WAITS: tuple[float, ...] = WORK_IQ_CHECK_WAITS
ENV_SWITCHES: tuple[str, ...] = (
    "ENTRABOT_A365_OBSERVABILITY_ENABLED",
    "ENTRABOT_A365_EXPORT_ENABLED",
)
PERMISSIONS_REASON = "setup could not give the agent the permission to send Agent 365 telemetry"
SWITCHES_REASON = "setup could not record the telemetry switches in .env"
LICENCE_NOTE = (
    "Agent 365 keeps the telemetry only when at least one person in your organization has a "
    "Microsoft 365 E7 or Microsoft Agent 365 licence assigned. Setup cannot check that."
)

# Run in a child, so entrabot.config never loads into the wizard. No token is printed.
TELEMETRY_SNIPPET = f"""
import json
from entrabot.config import EntraBotConfig
from entrabot.preflight import telemetry_token_scope
print({RESULT_MARKER!r} + json.dumps(telemetry_token_scope(EntraBotConfig.from_env())))
"""

Outcome = Literal["on", "pending"]


class TelemetryError(Exception):
    """Switching telemetry on failed. ``str(error)`` says which step; :attr:`reason` says why
    in plain words, for the last screen."""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason or message


def telemetry_configured(env_file: Path) -> bool:
    """Do *env_file*'s switches turn telemetry and its export on? Unreadable counts as no."""
    try:
        values = read_env(env_file)
    except (OSError, UnicodeDecodeError):
        return False
    return all(values.get(key, "").strip().lower() in ("true", "1", "yes") for key in ENV_SWITCHES)


def _honoured(helpers: Helpers) -> bool:
    """One check, as the agent. A check that could not run counts as not yet."""
    result = helpers.snippet(TELEMETRY_SNIPPET)
    raw = marked_line(result.stdout, RESULT_MARKER)
    try:
        row = json.loads(raw) if raw else None
    except ValueError:
        row = None
    if not isinstance(row, dict):
        helpers.console.debug(f"The telemetry check could not run: {result.stderr.strip()[-300:]}")
        return False
    if not row.get("ok"):
        helpers.console.debug(f"Not yet: {row.get('error')}")
    return bool(row.get("ok"))


def configure_telemetry(
    *,
    paths: Paths,
    helpers: Helpers,
    console: Console,
    identity: Identity,
    guard: EnvGuard,
    sleep: Callable[[float], None],
    waits: tuple[float, ...] = CHECK_WAITS,
) -> Outcome:
    """Switch Agent 365 telemetry on for *identity*. Returns "on" when the agent's token
    carries the write scope, "pending" when everything is set up but it does not yet. Raises
    :class:`TelemetryError` when a step failed; ``.env`` is then as it was."""
    ids = {
        "--blueprint-app-id": identity.blueprint_app_id,
        "--blueprint-object-id": identity.blueprint_object_id,
        "--agent-object-id": identity.agent_object_id,
        "--agent-user-id": identity.agent_user_id,
    }
    if not all(ids.values()):
        raise TelemetryError(
            "The agent must be created before telemetry can be switched on.",
            reason=PERMISSIONS_REASON,
        )
    granted = helpers.script(
        PERMISSIONS_SCRIPT,
        [part for pair in ids.items() for part in pair],
        capture=True,
        quiet=True,
        heartbeat="Still giving the agent the telemetry permission",
    )
    if granted.returncode != 0:
        console.show_tail(granted, lines=6)
        raise TelemetryError(
            "Giving the agent the telemetry permission failed.", reason=PERMISSIONS_REASON
        )
    console.ok("Gave the agent the permission to send Agent 365 telemetry")
    console.info("Checking that Microsoft lets the agent use it (it can take a minute)...")
    outcome: Outcome = "pending"
    if _honoured(helpers):
        outcome = "on"
    else:
        for wait in waits:
            console.detail(
                f"Still waiting for Microsoft to honour it. Checking again in {wait:.0f}s."
            )
            sleep(wait)
            if _honoured(helpers):
                outcome = "on"
                break
    try:
        update_env(paths.env_file, dict.fromkeys(ENV_SWITCHES, "true"), guard=guard)
    except OSError as exc:
        raise TelemetryError(f".env could not be written: {exc}", reason=SWITCHES_REASON) from exc
    if not telemetry_configured(paths.env_file):  # read back: what the runtime will see
        raise TelemetryError(".env does not read back as switched on.", reason=SWITCHES_REASON)
    if outcome == "on":
        console.ok("Agent 365 telemetry is switched on")
    else:
        console.warn("Agent 365 telemetry is set up, but Microsoft does not honour it yet.")
        console.detail("The agent keeps trying. What to do is at the end.")
    console.detail(LICENCE_NOTE)
    return outcome
