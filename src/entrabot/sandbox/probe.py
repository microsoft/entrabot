"""Runs inside the sandbox for :mod:`entrabot.sandbox.selftest`. Prints one JSON object."""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path


def _attempt(action) -> dict:
    try:
        value = action()
    except Exception as exc:  # noqa: BLE001 - every outcome is reported
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return {"ok": True, "value": value}


def _outside_file(home: Path) -> Path:
    # Chosen by the self-test outside the sandbox, where it can see which file exists (inside,
    # even looking is refused).
    return Path(os.environ.get("ENTRABOT_PROBE_OUTSIDE_FILE") or home / ".profile")


def main() -> int:
    import httpx

    from entrabot.config import get_config
    from entrabot.tools.teams import acquire_agent_user_token

    config = get_config()
    home = Path.home()
    repo = Path(__file__).resolve().parents[3]

    def sign_in_and_me() -> dict:
        token = acquire_agent_user_token(config)
        response = httpx.get(
            "https://graph.microsoft.com/v1.0/me?$select=id,userPrincipalName",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        response.raise_for_status()
        body = response.json()
        return {"id": body.get("id"), "userPrincipalName": body.get("userPrincipalName")}

    def read_outside() -> int:
        return len(_outside_file(home).read_bytes())

    def write_repo() -> str:
        target = repo / ".containment-probe"
        target.write_text("x")
        target.unlink()
        return str(target)

    def other_host_via_proxy() -> int:
        return httpx.get("https://example.com/", timeout=10).status_code

    def other_host_direct() -> str:
        with socket.create_connection(("1.1.1.1", 443), timeout=5):
            return "connected"

    report = {
        "containment": os.environ.get("ENTRABOT_CONTAINED", "none"),
        "whoami": _attempt(sign_in_and_me),
        "read_outside_allowed_paths": {"path": str(_outside_file(home)), **_attempt(read_outside)},
        "write_to_repository": _attempt(write_repo),
        "non_allowed_host_via_proxy": _attempt(other_host_via_proxy),
        "non_allowed_host_direct": _attempt(other_host_direct),
    }
    print(json.dumps(report, indent=2))
    expected = (
        report["whoami"]["ok"]
        and not report["read_outside_allowed_paths"]["ok"]
        and not report["write_to_repository"]["ok"]
        and not report["non_allowed_host_via_proxy"]["ok"]
        and not report["non_allowed_host_direct"]["ok"]
    )
    return 0 if expected else 1


if __name__ == "__main__":
    sys.exit(main())
