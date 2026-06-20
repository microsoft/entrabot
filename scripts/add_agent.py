#!/usr/bin/env python3
"""Provision an ADDITIONAL agent — a new Agent Identity + Agent User under the EXISTING Blueprint.

``create_entra_agent_ids.py`` converges to a single agent per device (it reuses the host's Agent
Identity and the first user under it). This script instead mints a *distinct*, independently-named
agent that reuses the shared Blueprint + cert — so a second agent that "goes by a different name"
gets its own Teams identity without re-provisioning the chain.

It reuses that module's building blocks; the only thing that makes the result distinct is the
suffix, which makes the Agent Identity display name and the Agent User UPN unique (so the
find-existing lookups miss and fresh objects are created). Idempotent: re-running with the same
suffix reuses what's already there.

Driven by environment (NOT ``ENTRABOT_NEW_CHAIN`` — that would fork a new Blueprint):
  _ENTRABOT_UPN_SUFFIX          required — unique slug, e.g. ``nemo`` (drives identity + UPN)
  ENTRABOT_AGENT_DISPLAY_NAME   optional — friendly Teams display name, e.g. ``Nemo``

Prints the new identity as one ``AGENT_JSON={...}`` line for the caller to capture.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import create_entra_agent_ids as C  # noqa: E402


def main() -> int:
    suffix = os.environ.get("_ENTRABOT_UPN_SUFFIX", "").strip()
    if not suffix:
        print("ERROR: _ENTRABOT_UPN_SUFFIX is required (the agent's unique slug).", file=sys.stderr)
        return 2
    if os.environ.get("ENTRABOT_NEW_CHAIN") == "1":
        print("ERROR: ENTRABOT_NEW_CHAIN must NOT be set — this reuses the existing Blueprint.",
              file=sys.stderr)
        return 2
    # An inherited ENTRABOT_AGENT_USER_UPN (the *other* agent's, from a loaded .env) would make
    # _agent_user_upn() reuse it and collide. A new agent's UPN must derive from the suffix.
    os.environ.pop("ENTRABOT_AGENT_USER_UPN", None)

    print("=" * 60)
    print(f"EntraBot — additional agent '{suffix}' (reusing existing Blueprint)")
    print("=" * 60)

    try:
        token = C.get_existing_graph_token()
    except C.ProvisionerBootstrapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # The provisioning helpers set_state() into the shared .entrabot-state.json (designed for the
    # single per-host agent). An additional agent's identity belongs in its OWN per-agent .env
    # (the caller captures it from AGENT_JSON below), so snapshot + restore the shared state to
    # keep it pointing at the primary agent instead of drifting to whatever was provisioned last.
    from pathlib import Path

    state_file = Path(__file__).resolve().parent.parent / ".entrabot-state.json"
    state_snapshot = state_file.read_bytes() if state_file.is_file() else None
    try:
        blueprint_app_id, _ = C.create_blueprint(token)  # non-force → reuse existing
        agent_id, agent_obj_id = C.create_agent_identity(token, blueprint_app_id)  # unique → new
        agent_user_id, agent_user_upn = C.create_agent_user(token, agent_obj_id)  # new → new user

        C.grant_agent_identity_app_permissions(token, agent_obj_id)
        C.grant_agent_user_consent(token, agent_obj_id, agent_user_id)
        C.grant_agent_user_storage_consent(token, agent_obj_id, agent_user_id)
        C.assign_license_to_agent_user(token, agent_user_id)
    finally:
        if state_snapshot is not None:
            state_file.write_bytes(state_snapshot)

    result = {
        "ENTRABOT_AGENT_ID": agent_id,
        "ENTRABOT_AGENT_OBJECT_ID": agent_obj_id,
        "ENTRABOT_AGENT_USER_ID": agent_user_id,
        "ENTRABOT_AGENT_USER_UPN": agent_user_upn,
    }
    print("\n--- Additional agent ready ---")
    print(f"  Display name: {os.environ.get('ENTRABOT_AGENT_DISPLAY_NAME') or '(default)'}")
    print(f"  UPN:          {agent_user_upn}")
    print("AGENT_JSON=" + json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
