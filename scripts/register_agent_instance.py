#!/usr/bin/env python3
"""Register this agent in the Microsoft Agent 365 registry (Agent Registry API).

Microsoft Learn ("Agent 365 observability concepts"): "Creating a Microsoft Entra identity
alone doesn't register the instance." Learn ("Create an agent identity blueprint", "Register
agents in the Agent 365 registry"): a Blueprint created through the Graph API, as Entrabot's
is, registers with an explicit call to the Agent Registry API. That is
``POST https://graph.microsoft.com/beta/copilot/agentRegistrations``, which needs
``AgentRegistration.ReadWrite.All`` (delegated or application).

Setup runs this script when the person says yes to Agent 365 integration
(``--configure-a365``), after giving the setup (Provisioner) app that permission through the
signed-in administrator's Azure CLI, as it gives the app its other permissions. Run on its
own, it never adds a permission: it checks the token and stops with what to do when the
permission is missing.

- The API is beta and documents create, get, update and delete only (no list). The
  registration's ID is kept in ``.entrabot-state.json`` (``AGENT_REGISTRATION_ID``) and is
  how a later run finds it, so a second registration is never created on the strength of a
  lookup that failed. ``--registration-id`` records one made from another computer.
- The request body is built in one place, ``entrabot.a365.registry.build_registration``.
  Learn does not say whether ``agentIdentityId`` and ``agentIdentityBlueprintId`` take app
  IDs or object IDs; this sends app IDs. Not verified against a tenant.

The Agent 365 CLI's ``a365 setup all`` also registers, but it creates its own Blueprint with
a client secret and an Azure App Service; do not run it against an Entrabot agent.

A dry run is the default: it prints the request and writes nothing. ``--apply`` writes.
``--check`` only reads; ``--delete --apply`` deletes the recorded registration.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from entrabot.a365 import registry
from entrabot.winsetup.envfile import read_env

sys.path.insert(0, str(Path(__file__).resolve().parent))
from entra_provisioning import (  # noqa: E402
    ProvisionerBootstrapError,
    clear_state,
    get_existing_graph_token,
    get_state,
    set_state,
)
from script_output import make_output_safe  # noqa: E402

REGISTRATIONS_URL = registry.REGISTRATIONS_URL
REQUIRED_ROLE = registry.REQUIRED_ROLE
STATE_KEY = registry.STATE_KEY
TIMEOUT_SECONDS = 20
RESULT_MARKER = "ENTRABOT_RESULT="
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# Exit codes. 0: done (or, with --check, an answer: present, gone or nothing recorded).
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def _request(method: str, url: str, **kwargs: Any) -> requests.Response:
    return requests.request(method, url, timeout=TIMEOUT_SECONDS, **kwargs)


def _roles(token: str) -> list[str]:
    return registry.token_roles(token)


def _setting(name: str) -> str:
    """A value from the environment, else from this folder's ``.env``."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    try:
        return read_env(ENV_FILE).get(name, "").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _env_values() -> dict[str, str]:
    try:
        values = read_env(ENV_FILE)
    except (OSError, UnicodeDecodeError):
        values = {}
    for key in ("ENTRABOT_HUMAN_USER_ID", "ENTRABOT_HUMAN_USER_IDS", "ENTRABOT_HUMAN_USER_TYPES"):
        if os.environ.get(key, "").strip():
            values[key] = os.environ[key].strip()
    return values


def build_registration(
    *, owner_ids: list[str], now: datetime, display_name: str = ""
) -> dict[str, Any]:
    """The agentRegistration body for this machine's agent, from the state file."""
    upn = (get_state("AGENT_USER_UPN") or "").strip()
    return registry.build_registration(
        agent_id=(get_state("AGENT_ID") or "").strip(),
        blueprint_app_id=(get_state("BLUEPRINT_APP_ID") or "").strip(),
        name=registry.display_name(preferred=display_name, upn=upn),
        owner_ids=owner_ids,
        created_by=(get_state("PROVISIONER_CLIENT_ID") or "").strip(),
        now=now,
    )


def register(token: str, body: dict[str, Any], *, recorded_id: str = "") -> tuple[str, str]:
    """Create or update the registration and record its ID. Returns (ID, what was done).
    Raises :class:`entrabot.a365.registry.RegistryError` with a reason that holds no token."""
    recorded = recorded_id or (get_state(STATE_KEY) or "").strip()
    registration_id, how = registry.register(token, body, recorded_id=recorded, request=_request)
    if how == "recreated":
        print(f"  Registration {recorded} is gone; created a new one.")
    set_state(STATE_KEY, registration_id)  # at once: a later failure must not lose it
    return registration_id, how


class _Run:
    """Ends the run: says what happened, and with --print-result, as one marked line."""

    def __init__(self, print_result: bool) -> None:
        self.print_result = print_result

    def end(self, code: int, outcome: str, *, registration_id: str = "", reason: str = "") -> int:
        if reason and code != EXIT_OK:
            print(f"ERROR: {reason}", file=sys.stderr)
        if self.print_result:
            result = {"outcome": outcome, "id": registration_id, "reason": reason}
            print(RESULT_MARKER + json.dumps(result))
        return code


def _token(run: _Run) -> tuple[str | None, int | None]:
    """The setup app's token when it holds the permission; else the run's end."""
    try:
        token = get_existing_graph_token()
    except ProvisionerBootstrapError as exc:
        return None, run.end(EXIT_FAILED, "no-access", reason=f"no setup app token: {exc}")
    if REQUIRED_ROLE not in _roles(token):
        return None, run.end(
            EXIT_FAILED,
            "permission-missing",
            reason=f"the setup (Provisioner) app does not hold {REQUIRED_ROLE}. An "
            "administrator must add that Microsoft Graph application permission to it and "
            "grant admin consent (setup does this when you answer yes to Agent 365 "
            "integration); nothing was sent.",
        )
    return token, None


def _check(run: _Run) -> int:
    recorded = (get_state(STATE_KEY) or "").strip()
    if not recorded:
        return run.end(EXIT_OK, "not-recorded")
    token, ended = _token(run)
    if ended is not None or token is None:
        return EXIT_FAILED if ended is None else ended
    try:
        found = registry.look_up(token, recorded, request=_request)
    except (registry.RegistryError, requests.RequestException) as exc:
        return run.end(EXIT_FAILED, "could-not-check", registration_id=recorded, reason=str(exc))
    print(f"Registration {recorded}: {found}")
    return run.end(EXIT_OK, found, registration_id=recorded)


def _delete(run: _Run, *, apply: bool) -> int:
    recorded = (get_state(STATE_KEY) or "").strip()
    if not recorded:
        print("No registration is recorded on this computer; nothing to delete.")
        return run.end(EXIT_OK, "not-recorded")
    if not apply:
        print(f"Dry run: --apply would delete registration {recorded}. Nothing was sent.")
        return run.end(EXIT_OK, "dry-run", registration_id=recorded)
    token, ended = _token(run)
    if ended is not None or token is None:
        return EXIT_FAILED if ended is None else ended
    try:
        done = registry.delete(token, recorded, request=_request)
    except (registry.RegistryError, requests.RequestException) as exc:
        return run.end(EXIT_FAILED, "refused", registration_id=recorded, reason=str(exc))
    clear_state(STATE_KEY)
    print(f"Registration {recorded}: {done}")
    return run.end(EXIT_OK, done, registration_id=recorded)


def main(argv: list[str] | None = None) -> int:
    make_output_safe()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="Write (default: dry run).")
    parser.add_argument(
        "--owner-id",
        action="append",
        default=[],
        help="Entra object ID of a user who owns the registration (repeatable). Default: the "
        "sponsors in .env (ENTRABOT_HUMAN_USER_IDS, members first).",
    )
    parser.add_argument(
        "--display-name",
        default="",
        help="The name the registry shows. Default: ENTRABOT_AGENT_DISPLAY_NAME from .env, "
        "else the agent's address before the @.",
    )
    parser.add_argument(
        "--registration-id",
        default="",
        help="Use this existing registration (one made from another computer of the same "
        "agent): it is read, updated and recorded here instead of a new one being created.",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="Only read the recorded registration.")
    action.add_argument(
        "--delete",
        action="store_true",
        help="Delete the recorded registration (with --apply; dry run otherwise).",
    )
    parser.add_argument(
        "--print-result",
        action="store_true",
        help=f"End with one {RESULT_MARKER}<json> line (for setup).",
    )
    args = parser.parse_args(argv)
    run = _Run(args.print_result)

    if args.check:
        return _check(run)
    if args.delete:
        return _delete(run, apply=args.apply)

    owners = [item.strip() for item in args.owner_id if item.strip()]
    if not owners:
        owners = registry.owner_ids(_env_values())
    if not owners:
        return run.end(
            EXIT_USAGE,
            "usage",
            reason="name an owner with --owner-id (a user's object ID). App-only registrations "
            "need user owners.",
        )
    if not (get_state("AGENT_ID") and get_state("BLUEPRINT_APP_ID")):
        return run.end(
            EXIT_USAGE, "usage", reason="this machine has no agent recorded yet; run setup first."
        )

    display = args.display_name.strip() or _setting("ENTRABOT_AGENT_DISPLAY_NAME")
    body = build_registration(owner_ids=owners, now=datetime.now(UTC), display_name=display)
    if not args.apply:
        print("Dry run: this is the registration that --apply would write. Nothing was sent.")
        print(json.dumps(body, indent=2))
        return run.end(EXIT_OK, "dry-run")

    token, ended = _token(run)
    if ended is not None or token is None:
        return EXIT_FAILED if ended is None else ended
    try:
        registration_id, how = register(token, body, recorded_id=args.registration_id.strip())
    except registry.RegistryError as exc:
        return run.end(EXIT_FAILED, exc.outcome, reason=str(exc))
    except requests.RequestException as exc:
        return run.end(EXIT_FAILED, "could-not-check", reason=f"Graph could not be reached ({exc})")
    print(f"Registered in the Agent 365 registry: {registration_id} ({how})")
    return run.end(EXIT_OK, how, registration_id=registration_id)


if __name__ == "__main__":
    raise SystemExit(main())
