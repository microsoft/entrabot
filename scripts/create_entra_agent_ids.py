#!/usr/bin/env python3
"""
create_entra_agent_ids.py
=========================
Creates an Agent Identity Blueprint and a per-device Agent Identity in
Microsoft Entra ID via the Graph beta API.  Stores the resulting IDs in
the local provision state file (.entrabot-state.json).

Uses the dedicated provisioner app from entra_provisioning.py — never
Azure CLI tokens (which include Directory.AccessAsUser.All and get
rejected by Agent Identity APIs).

Usage:
    python3 scripts/create_entra_agent_ids.py

Which Blueprint: the one recorded in .entrabot-state.json, where setup also records the one it
is asked to join. If that one does not exist, or can't be checked, the run stops (exit 1) before
changing anything; it never takes another Blueprint by display name. With nothing recorded, an
existing Blueprint named "EntraBot Code Agent" is used only when it is the only one.

Which Agent User: an existing one with the sign-in name this run wants (ENTRABOT_AGENT_USER_UPN,
which setup can leave in .env, or one made from _ENTRABOT_UPN_SUFFIX) is reused only when its
Agent Identity belongs to that Blueprint; one of another Blueprint stops the run before anything
is created, granted or recorded. Every lookup tells "there is none" from "could not check", and
"could not check" stops the run before the object in question is created.

Each of those stops prints one line, ENTRABOT_RESULT=<outcome> (the *_MARKER constants below), on
its own, just before its message. ENTRABOT_NEW_CHAIN=1 skips every lookup and creates a fresh
chain.

Prerequisites:
    - az login has been run
    - pip install azure-identity requests
"""

import os
import platform
import re
import socket
import sys
import time

import requests

# When ENTRABOT_NEW_CHAIN=1, skip all find_existing_* lookups and create fresh.
# Set by setup.sh --new to force a new identity chain.
_FORCE_NEW = os.environ.get("ENTRABOT_NEW_CHAIN") == "1"
_ASSIGN_TEAMS_LICENSE = os.environ.get("ENTRABOT_ASSIGN_TEAMS_LICENSE", "1") == "1"
_ASSIGN_WORK_IQ_LICENSE = os.environ.get("ENTRABOT_ASSIGN_WORK_IQ_LICENSE") == "1"

# entra_provisioning.py lives in the same directory
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from entra_provisioning import (  # noqa: E402 — sys.path insert precedes this import
    ProvisionerBootstrapError,
    build_sponsors_bind,
    get_existing_graph_token,
    get_signed_in_user_id,
    get_state,
    set_state,
)

# The repo root is one directory up; src/ contains the entrabot package.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src"))
from entrabot.graph_helpers import GRAPH_V1, graph_request, odata_escape  # noqa: E402
from entrabot.preflight import COPILOT_CAPABLE_SKUS, TEAMS_CAPABLE_SKUS  # noqa: E402

BLUEPRINT_DISPLAY_NAME = "EntraBot Code Agent"

_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_UNREADABLE = "Microsoft Graph's answer could not be read"

# How to go on after a stop, for both setup front ends.
_HOW_TO_JOIN = (
    "    macOS/Linux: ./scripts/setup.sh --use-blueprint=APP_ID",
    "    Windows:     scripts\\setup-windows.cmd -UseBlueprint APP_ID",
)
_HOW_TO_START_OVER = (
    "  To create a new agent instead: ./scripts/setup.sh --new --with-upn-suffix=NAME",
    "  (Windows: scripts\\setup-windows.cmd -NewChain -UpnSuffix NAME).",
)


# Printed on a line of their own, immediately before the message, when the run stops for one of
# these reasons: exactly one per stop. The Windows setup wizard (src/entrabot/winsetup) recognises
# these lines (its ENTRABOT_RESULT= convention, as in entra_provisioning.py), so they are never
# renamed; tests pin them.
BLUEPRINT_NOT_FOUND_MARKER = "ENTRABOT_RESULT=blueprint-not-found"
BLUEPRINT_COULD_NOT_CHECK_MARKER = "ENTRABOT_RESULT=blueprint-could-not-check"
BLUEPRINT_AMBIGUOUS_MARKER = "ENTRABOT_RESULT=blueprint-ambiguous"
AGENT_USER_WRONG_BLUEPRINT_MARKER = "ENTRABOT_RESULT=agent-user-wrong-blueprint"
AGENT_LOOKUP_COULD_NOT_CHECK_MARKER = "ENTRABOT_RESULT=agent-lookup-could-not-check"


class ProvisioningStop(RuntimeError):
    """This run can't go on without guessing. The message is for the operator; ``marker`` is
    the ENTRABOT_RESULT= line that names the outcome for the setup front ends."""

    def __init__(self, message: str, marker: str) -> None:
        super().__init__(message)
        self.marker = marker


class BlueprintLookupError(ProvisioningStop):
    """Which Blueprint this run is for can't be settled. The message is for the operator.

    Raised before anything is created or changed, in the directory or in the state file.
    """


class AgentLookupError(ProvisioningStop):
    """Which Agent Identity or Agent User this run is for can't be settled, or the Agent User
    with the wanted sign-in name belongs to another Blueprint.

    Raised before the object in question is created or reused; the message says what was not
    done.
    """


class _NoAnswer(Exception):
    """Graph gave no answer that settles the question. The message says why."""


# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------


def find_existing_blueprint(token: str) -> dict | None:
    """The Blueprint this run is for, or None when there is none yet and one may be created.

    A Blueprint recorded in the state file is the only acceptable answer: setup records there
    the one it is asked to join (``setup.sh --use-blueprint``, ``-UseBlueprint`` on Windows),
    and every run records the one it found or created. Its App ID decides; the object ID is
    looked up only when no App ID is recorded. When Graph says that Blueprint does not exist,
    or the lookup fails for any reason, this stops. It never looks for another Blueprint by
    name: display names are not unique, and the first match may be somebody else's.

    Only with nothing recorded is a Blueprint looked for by its display name. A single match
    is used. Several matches stop, listing them; a search that fails stops as well, because
    "could not check" is not "there is none", and a second Blueprint is not created on the
    strength of it.

    Raises :class:`BlueprintLookupError`.
    """
    app_id = str(get_state("BLUEPRINT_APP_ID") or "").strip()
    object_id = str(get_state("BLUEPRINT_OBJECT_ID") or "").strip()
    if app_id:
        return _recorded_blueprint_by_app_id(token, app_id, object_id)
    if object_id:
        return _recorded_blueprint_by_object_id(token, object_id)
    return _only_blueprint_named(token, BLUEPRINT_DISPLAY_NAME)


def _recorded_blueprint_by_app_id(token: str, app_id: str, stored_object_id: str) -> dict:
    _require_id(app_id, "BLUEPRINT_APP_ID")
    recorded = f"Blueprint {app_id}"
    try:
        resp = _graph_get(token, f"/applications?$filter=appId eq '{odata_escape(app_id)}'")
        if resp.status_code != 200:
            raise _NoAnswer(_http_reason(resp))
        matches = [
            app
            for app in _value_list(_json_object(resp))
            if str(app.get("appId", "")).lower() == app_id.lower()
        ]
        if matches and not matches[0].get("id"):
            raise _NoAnswer(_UNREADABLE)
    except _NoAnswer as exc:
        raise BlueprintLookupError(
            _unreadable(recorded, str(exc)), BLUEPRINT_COULD_NOT_CHECK_MARKER
        ) from exc
    if not matches:
        raise BlueprintLookupError(_not_found(recorded), BLUEPRINT_NOT_FOUND_MARKER)
    found = matches[0]
    found_object_id = str(found["id"])
    if stored_object_id and stored_object_id.lower() != found_object_id.lower():
        print(
            f"  [warn] Stored BLUEPRINT_OBJECT_ID {stored_object_id} is not the object ID of "
            f"Blueprint {app_id}, which is {found_object_id}. The App ID decides, so this run "
            f"uses {found_object_id}."
        )
    return found


def _recorded_blueprint_by_object_id(token: str, object_id: str) -> dict:
    _require_id(object_id, "BLUEPRINT_OBJECT_ID")
    recorded = f"the Blueprint with object ID {object_id}"
    app: dict | None = None
    try:
        resp = _graph_get(token, f"/applications/{object_id}")
        if resp.status_code == 200:
            app = _json_object(resp)
            if not app.get("appId") or not app.get("id"):
                raise _NoAnswer(_UNREADABLE)
        elif resp.status_code != 404:  # GET by ID: 404 is Graph's "no such object"
            raise _NoAnswer(_http_reason(resp))
    except _NoAnswer as exc:
        raise BlueprintLookupError(
            _unreadable(recorded, str(exc)), BLUEPRINT_COULD_NOT_CHECK_MARKER
        ) from exc
    if app is None:
        raise BlueprintLookupError(_not_found(recorded), BLUEPRINT_NOT_FOUND_MARKER)
    return app


def _only_blueprint_named(token: str, name: str) -> dict | None:
    try:
        resp = _graph_get(token, f"/applications?$filter=displayName eq '{odata_escape(name)}'")
        if resp.status_code != 200:
            raise _NoAnswer(_http_reason(resp))
        body = _json_object(resp)
        values = _value_list(body)
        if body.get("@odata.nextLink"):
            # Only a whole list says how many there are.
            raise _NoAnswer("Microsoft Graph returned more than one page of applications")
        matches = [app for app in values if app.get("displayName") == name]
        if len(matches) == 1 and not (matches[0].get("appId") and matches[0].get("id")):
            raise _NoAnswer(_UNREADABLE)
    except _NoAnswer as exc:
        raise BlueprintLookupError(
            _search_unreadable(name, str(exc)), BLUEPRINT_COULD_NOT_CHECK_MARKER
        ) from exc
    if len(matches) > 1:
        raise BlueprintLookupError(_ambiguous(name, matches), BLUEPRINT_AMBIGUOUS_MARKER)
    return matches[0] if matches else None


def _graph_get(token: str, path: str) -> requests.Response:
    """GET *path*. A request that fails before Graph answers raises :class:`_NoAnswer`."""
    try:
        return graph_request("GET", path, token)
    except requests.RequestException as exc:
        text = str(exc).strip().rstrip(".")
        detail = f"{type(exc).__name__}: {text}"[:200] if text else type(exc).__name__
        raise _NoAnswer(f"the request to Microsoft Graph failed ({detail})") from exc


def _json_object(resp: requests.Response) -> dict:
    try:
        body = resp.json()
    except ValueError as exc:
        raise _NoAnswer(_UNREADABLE) from exc
    if not isinstance(body, dict):
        raise _NoAnswer(_UNREADABLE)
    return body


def _value_list(body: dict) -> list[dict]:
    values = body.get("value")
    if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
        raise _NoAnswer(_UNREADABLE)
    return values


def _http_reason(resp: requests.Response) -> str:
    code = ""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        code = str(body["error"].get("code") or "")[:80]
    return f"Microsoft Graph answered HTTP {resp.status_code}" + (f" ({code})" if code else "")


def _answer(token: str, path: str) -> dict:
    """GET *path* and return Graph's JSON object. Anything but a readable 200 raises
    :class:`_NoAnswer`: a collection query says "none" with an empty list, never with an error."""
    resp = _graph_get(token, path)
    if resp.status_code != 200:
        raise _NoAnswer(_http_reason(resp))
    return _json_object(resp)


def _object_or_none(token: str, path: str) -> dict | None:
    """GET one object by its ID: the object, or None when Graph says there is no such object
    (404). Anything else that is not a readable 200 raises :class:`_NoAnswer`."""
    resp = _graph_get(token, path)
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise _NoAnswer(_http_reason(resp))
    return _json_object(resp)


def _same_id(one: object, other: object) -> bool:
    """Do two directory IDs name the same object? They are GUIDs, the same in either letter
    case. An empty or missing ID matches nothing."""
    first = str(one or "").strip().lower()
    return bool(first) and first == str(other or "").strip().lower()


def _this_tenant() -> str:
    tenant = str(os.environ.get("ENTRABOT_TENANT_ID") or get_state("TENANT_ID") or "").strip()
    return f"this tenant ({tenant})" if tenant else "this tenant"


def _require_id(value: str, key: str) -> None:
    if _ID.match(value):
        return
    # Reported as not found: a value that is not an ID names no Blueprint, the remedy is the same
    # (give the right App ID), and running again later would not help.
    raise BlueprintLookupError(
        "\n".join(
            [
                f"  ERROR: {key} in .entrabot-state.json is {value!r}, which is not an ID",
                "  (an ID looks like 1a2b3c4d-1234-5678-9abc-def012345678).",
                "  Nothing was created or changed, and no other Blueprint was looked for by name.",
                "  Run setup again with the Blueprint's App ID:",
                *_HOW_TO_JOIN,
            ]
        ),
        BLUEPRINT_NOT_FOUND_MARKER,
    )


def _not_found(recorded: str) -> str:
    return "\n".join(
        [
            f"  ERROR: {recorded[:1].upper()}{recorded[1:]} does not exist in {_this_tenant()}.",
            "  It is the Blueprint recorded for this machine in .entrabot-state.json, where setup",
            "  also records the one it is asked to join. The ID may be mistyped, it may belong to",
            "  a different tenant, or the Blueprint was deleted.",
            "  Nothing was created or changed, and no other Blueprint was looked for by name.",
            "  Check the ID and the tenant you are signed in to, then run setup again with the",
            "  right Blueprint App ID:",
            *_HOW_TO_JOIN,
            *_HOW_TO_START_OVER,
            "  A Blueprint created in the last few minutes may not be listed yet. If so, wait a",
            "  few minutes and run setup again.",
        ]
    )


def _unreadable(recorded: str, reason: str) -> str:
    return "\n".join(
        [
            f"  ERROR: Could not check whether {recorded} exists: {reason}.",
            "  It is the Blueprint recorded for this machine in .entrabot-state.json, where setup",
            "  also records the one it is asked to join, and the only one this run will use.",
            "  Nothing was created or changed, and no other Blueprint was looked for by name.",
            "  This is usually temporary (throttling or a network problem): run setup again in a",
            "  few minutes. If it keeps failing with HTTP 401 or 403, setup's Provisioner app is",
            "  missing a permission or its admin consent: run setup again as a tenant",
            "  administrator.",
        ]
    )


def _search_unreadable(name: str, reason: str) -> str:
    return "\n".join(
        [
            f"  ERROR: Could not look for an existing Blueprint named '{name}': {reason}.",
            "  No Blueprint is recorded for this machine, so this run looks for one by name",
            "  before it creates one, and it does not create one without an answer.",
            "  Nothing was created or changed. Run setup again in a few minutes, or name the",
            "  Blueprint to join:",
            *_HOW_TO_JOIN,
        ]
    )


def _ambiguous(name: str, matches: list[dict]) -> str:
    # Oldest first; an application without a creation date goes last.
    listed = sorted(matches, key=lambda app: str(app.get("createdDateTime") or "~"))
    return "\n".join(
        [
            f"  ERROR: {len(matches)} applications in this tenant are named '{name}', and no",
            "  Blueprint is recorded for this machine, so this run can't tell which one is",
            "  this agent's:",
            *(
                f"    App ID {app.get('appId') or 'unknown'}   "
                f"created {app.get('createdDateTime') or 'unknown'}"
                for app in listed
            ),
            "  Nothing was created or changed. To join one of them, run setup again with its",
            "  App ID:",
            *_HOW_TO_JOIN,
            *_HOW_TO_START_OVER,
        ]
    )


# What a stop in the agent lookups did not do, for the operator's message.
_NOTHING_CHANGED = "Nothing was created or changed."
_NO_IDENTITY_CREATED = (
    "No Agent Identity or Agent User was created, and no permission or licence was granted."
)
_NO_USER_CREATED = "No Agent User was created, and no permission or licence was granted."


_TRY_AGAIN = (
    "  This is usually temporary (throttling or a network problem): run setup again in a",
    "  few minutes. If it keeps failing with HTTP 401 or 403, setup's Provisioner app is",
    "  missing a permission or its admin consent: run setup again as a tenant",
    "  administrator.",
)


def _agent_unreadable(
    question: str, reason: str, consequence: str, advice: tuple[str, ...] = _TRY_AGAIN
) -> str:
    return "\n".join(
        [
            f"  ERROR: Could not check {question}: {reason}.",
            "  This run looks for the agent's existing Agent Identity and Agent User before it",
            '  creates or reuses one, and it does not go on without an answer: "could not check"',
            '  is not "there is none". Guessing could leave a second Agent Identity behind, or',
            "  take over another agent's.",
            f"  {consequence}",
            *advice,
        ]
    )


def _wrong_blueprint(upn: str, agent_identity: str, theirs: str, ours: str) -> str:
    lines = [
        f"  ERROR: The Agent User {upn} belongs to a different Blueprint",
        "  from the one this run is for:",
        f"    its Blueprint:         {theirs}",
        f"    this run's Blueprint:  {ours or 'a new one, which this run would create'}",
        f"    its Agent Identity:    {agent_identity}",
        "  This run looks for an existing Agent User by that sign-in name before it creates one,",
        "  and it does not take over another Blueprint's agent. The name is the one",
        "  --agent-user-upn or --with-upn-suffix asked for (-AgentUserUpn or -UpnSuffix on",
        "  Windows), or the one an earlier setup left in this machine's .env; with none of",
        "  those, it is entrabot-agent@<domain>.",
        f"  {_NOTHING_CHANGED}",
        "  To use that agent on this machine, join its Blueprint instead:",
        f"    macOS/Linux: ./scripts/setup.sh --use-blueprint={theirs}",
        f"    Windows:     scripts\\setup-windows.cmd -UseBlueprint {theirs}",
    ]
    if ours:
        lines += [
            f"  To stay with Blueprint {ours}, give its Agent User a different name:",
            f"    macOS/Linux: ./scripts/setup.sh --use-blueprint={ours} --with-upn-suffix=NAME",
            f"    Windows:     scripts\\setup-windows.cmd -UseBlueprint {ours} -UpnSuffix NAME",
            "  or name one of its Agent Users exactly with --agent-user-upn=UPN (-AgentUserUpn).",
        ]
    else:
        lines += list(_HOW_TO_START_OVER)
    return "\n".join(lines)


def ensure_blueprint_principal(token: str, app_id: str) -> None:
    """Ensure the BlueprintPrincipal (SP) exists — it is NOT auto-created."""
    resp = graph_request(
        "GET",
        f"/servicePrincipals?$filter=appId eq '{app_id}'",
        token,
    )
    if resp.status_code == 200:
        sps = resp.json().get("value", [])
        if sps:
            print(f"  Blueprint SP already exists: {sps[0].get('id')}")
            return

    print("  Creating BlueprintPrincipal...")
    sp_body = {"appId": app_id}
    for attempt in range(4):
        sp_resp = graph_request(
            "POST",
            "/servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal",
            token,
            json_body=sp_body,
            base_url=GRAPH_V1,
        )
        if sp_resp.status_code in (200, 201):
            sp_data = sp_resp.json()
            print(f"  BlueprintPrincipal created: {sp_data.get('id', 'unknown')}")
            return
        if attempt < 3:
            wait = (attempt + 1) * 10
            print(
                f"  SP creation returned {sp_resp.status_code}, "
                f"retrying in {wait}s (app may still be propagating)..."
            )
            time.sleep(wait)
    print(f"  WARNING: Failed to create BlueprintPrincipal after retries: {sp_resp.status_code}")
    print(f"  Response: {sp_resp.text[:300]}")


def create_blueprint(token: str) -> tuple[str, str]:
    """Create or find the Agent Identity Blueprint. Returns (app_id, object_id).

    Raises :class:`BlueprintLookupError`, before anything is created or recorded, when the
    Blueprint this run is for can't be settled (see :func:`find_existing_blueprint`).
    """
    return record_blueprint(token, blueprint_for_this_run(token))


def blueprint_for_this_run(token: str) -> dict | None:
    """The existing Blueprint this run is for, or None when this run creates one. Changes
    nothing. Raises :class:`BlueprintLookupError` (see :func:`find_existing_blueprint`)."""
    print("\n--- Creating Agent Identity Blueprint ---\n")

    if _FORCE_NEW:
        print("  [--new] Skipping existing Blueprint lookup — creating fresh")
        return None
    existing = find_existing_blueprint(token)
    if existing:
        name = existing.get("displayName", BLUEPRINT_DISPLAY_NAME)
        print(f"  [skip] Blueprint already exists: {name}")
        print(f"         App ID:    {existing['appId']}")
        print(f"         Object ID: {existing['id']}")
    return existing


def record_blueprint(token: str, existing: dict | None) -> tuple[str, str]:
    """Record *existing* (from :func:`blueprint_for_this_run`) in the state file, or create a
    new Blueprint when it is None; either way make sure its BlueprintPrincipal exists. Returns
    (app_id, object_id)."""
    if existing:
        app_id = existing["appId"]
        obj_id = existing["id"]
        set_state("BLUEPRINT_APP_ID", app_id)
        set_state("BLUEPRINT_OBJECT_ID", obj_id)
        # Always ensure BlueprintPrincipal exists — previous run may have crashed
        ensure_blueprint_principal(token, app_id)
        return app_id, obj_id

    body: dict = {
        "displayName": BLUEPRINT_DISPLAY_NAME,
        "description": "Agent Identity Blueprint for EntraBot device agents",
    }
    sponsors_bind = build_sponsors_bind()
    if sponsors_bind:
        body["sponsors@odata.bind"] = sponsors_bind

    resp = graph_request(
        "POST",
        "/applications/microsoft.graph.agentIdentityBlueprint",
        token,
        json_body=body,
        base_url=GRAPH_V1,
    )
    if resp.status_code not in (200, 201):
        resp_text = resp.text
        if "Directory.AccessAsUser.All" in resp_text:
            print("  ERROR: Agent APIs reject tokens with Directory.AccessAsUser.All")
            print("  The provisioner app token still has issues.")
            print(f"  Check admin consent for: {get_state('PROVISIONER_CLIENT_ID')}")
            sys.exit(1)
        print(f"  ERROR: Failed to create blueprint: {resp.status_code}")
        print(f"  Response: {resp_text[:300]}")
        sys.exit(1)

    data = resp.json()
    app_id = data["appId"]
    obj_id = data["id"]
    print(f"  [new] Blueprint created: {BLUEPRINT_DISPLAY_NAME}")
    print(f"        App ID:    {app_id}")
    print(f"        Object ID: {obj_id}")

    set_state("BLUEPRINT_APP_ID", app_id)
    set_state("BLUEPRINT_OBJECT_ID", obj_id)

    ensure_blueprint_principal(token, app_id)

    return app_id, obj_id


# ---------------------------------------------------------------------------
# Agent Identity
# ---------------------------------------------------------------------------


def _agent_display_name() -> str:
    """Generate a display name for the agent identity based on hostname."""
    try:
        hostname = socket.gethostname().split(".")[0]
    except Exception:
        hostname = platform.node() or "unknown"
    return f"EntraBot Agent - {hostname}"


def find_existing_agent_identity(
    token: str,
    display_name: str,
    blueprint_app_id: str,
    stored_app_id: str | None = None,
) -> dict | None:
    """Find an existing Agent Identity under ``blueprint_app_id``.

    Agent Identity display names include the machine hostname
    (``EntraBot Agent - <host>``) — in a tenant that has multiple
    EntraBot Blueprints, multiple Agent Identity SPs will share the
    same display name, each parented by a different Blueprint.

    Graph does not support ``$filter=agentIdentityBlueprintId eq ...``
    on service principals, so we filter the result set in Python. Any
    candidate whose ``agentIdentityBlueprintId`` doesn't match our
    target Blueprint is rejected — otherwise this function can cross-
    contaminate state and point the agent at the wrong Blueprint's
    identity, which is a latent source of "why is my three-hop flow
    trying to use the old Blueprint?" bugs.

    ``stored_app_id`` is still honoured as a fast path, but only when
    the fetched SP is also scoped to ``blueprint_app_id``.

    None means Graph has said there is none. A lookup that fails, or an answer that can't be
    read, raises :class:`AgentLookupError` instead: "could not check" is not "there is none",
    and the caller would create a second Agent Identity for this machine on the strength of it.
    """
    blueprint = f"Blueprint {blueprint_app_id}"
    stored = str(stored_app_id or "").strip()
    if stored and not _ID.match(stored):
        print(f"  [warn] stored AGENT_ID {stored!r} is not an ID; ignoring it and re-discovering.")
        stored = ""
    if stored:
        try:
            recorded = _value_list(
                _answer(token, f"/servicePrincipals?$filter=appId eq '{odata_escape(stored)}'")
            )
            for sp in recorded:
                if _same_id(sp.get("agentIdentityBlueprintId"), blueprint_app_id):
                    return _with_ids(sp)
        except _NoAnswer as exc:
            question = (
                f"whether the Agent Identity recorded for this machine (AGENT_ID {stored}) "
                f"belongs to {blueprint}"
            )
            raise AgentLookupError(
                _agent_unreadable(question, str(exc), _NO_IDENTITY_CREATED),
                AGENT_LOOKUP_COULD_NOT_CHECK_MARKER,
            ) from exc
        if recorded:
            print(
                f"  [warn] stored AGENT_ID={stored} is parented by a "
                f"different Blueprint; ignoring and re-discovering."
            )

    try:
        body = _answer(
            token, f"/servicePrincipals?$filter=displayName eq '{odata_escape(display_name)}'"
        )
        for sp in _value_list(body):
            if sp.get("displayName") == display_name and _same_id(
                sp.get("agentIdentityBlueprintId"), blueprint_app_id
            ):
                return _with_ids(sp)
        if body.get("@odata.nextLink"):
            # Only a whole list says there is none.
            raise _NoAnswer("Microsoft Graph returned more than one page of service principals")
    except _NoAnswer as exc:
        question = (
            f"whether this machine already has an Agent Identity under {blueprint} "
            f"(one named '{display_name}')"
        )
        raise AgentLookupError(
            _agent_unreadable(question, str(exc), _NO_IDENTITY_CREATED),
            AGENT_LOOKUP_COULD_NOT_CHECK_MARKER,
        ) from exc
    return None


def _with_ids(found: dict) -> dict:
    """*found*, an Agent Identity Graph returned, when it has the IDs this run records."""
    if not found.get("id") or not found.get("appId"):
        raise _NoAnswer(_UNREADABLE)
    return found


def create_agent_identity(token: str, blueprint_app_id: str) -> tuple[str, str]:
    """Create or find the Agent Identity. Returns (agent_app_id, agent_object_id)."""
    print("\n--- Creating Agent Identity ---\n")

    display_name = _agent_display_name()
    stored_app_id = get_state("AGENT_ID")

    if _FORCE_NEW:
        print("  [--new] Skipping existing Agent Identity lookup — creating fresh")
        existing = None
    else:
        existing = find_existing_agent_identity(
            token,
            display_name,
            blueprint_app_id,
            stored_app_id=stored_app_id,
        )
    if existing:
        agent_id = existing.get("appId", "")
        agent_obj_id = existing.get("id", "")
        print(f"  [skip] Agent Identity already exists: {display_name} (appId={agent_id})")
        set_state("AGENT_ID", agent_id)
        set_state("AGENT_OBJECT_ID", agent_obj_id)
        return agent_id, agent_obj_id

    sponsor_id = get_signed_in_user_id()
    if sponsor_id:
        print(f"  Sponsor (current user): {sponsor_id}")
    else:
        print("  WARNING: Could not get current user for sponsorship")

    body: dict = {
        "displayName": display_name,
        "agentIdentityBlueprintId": blueprint_app_id,
    }
    if sponsor_id:
        body["sponsors@odata.bind"] = [f"https://graph.microsoft.com/v1.0/users/{sponsor_id}"]

    for attempt in range(3):
        resp = graph_request(
            "POST",
            "/servicePrincipals/microsoft.graph.agentIdentity",
            token,
            json_body=body,
            base_url=GRAPH_V1,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            agent_id = data.get("appId", "")
            agent_obj_id = data.get("id", "")
            if not agent_id:
                print("  ERROR: Graph response did not include appId")
                sys.exit(1)
            print(f"  [new] Agent Identity created: {display_name} (appId={agent_id})")
            set_state("AGENT_ID", agent_id)
            set_state("AGENT_OBJECT_ID", agent_obj_id)
            return agent_id, agent_obj_id
        elif resp.status_code == 403:
            print("  ERROR: Permission denied creating Agent Identity")
            print(f"  Response: {resp.text[:300]}")
            sys.exit(1)
        elif attempt < 2:
            wait = 10 * (attempt + 1)
            print(f"  Returned {resp.status_code}, retrying in {wait}s...")
            time.sleep(wait)
        else:
            print(f"  ERROR: Failed after retries ({resp.status_code})")
            print(f"  Response: {resp.text[:300]}")
            sys.exit(1)

    # Unreachable, but satisfies type checker
    sys.exit(1)


# ---------------------------------------------------------------------------
# Agent User
# ---------------------------------------------------------------------------

# Microsoft Graph SP object ID — needed for oauth2PermissionGrant.
# Resolved at runtime because it varies per tenant.
MS_GRAPH_API_APP_ID = "00000003-0000-0000-c000-000000000000"

# Azure Storage well-known app ID — the target SP for user_impersonation grants.
# Resolved at runtime per tenant like the Graph SP above.
AZURE_STORAGE_APP_ID = "e406a681-f3d4-42a8-90b6-c2b029497af1"
AZURE_STORAGE_SCOPE = "user_impersonation"


def _agent_user_upn(token: str) -> str:
    """Generate the UPN for the Agent User.

    Queries the tenant's verified domains via Graph API (using the Provisioner
    token) and picks the best domain. Prefers custom domains over .onmicrosoft.com.

    Previous approach (extracting from signed-in user UPN) failed for guest
    accounts like user@example.com — the domain is outlook.com, not the
    tenant's verified domain.
    """
    # Setup sets a suffix only when no UPN was asked for, so a UPN alongside one is just what an
    # earlier run left in .env, and the suffix is what this run wants.
    explicit_upn = os.environ.get("ENTRABOT_AGENT_USER_UPN", "").strip()
    if explicit_upn and not os.environ.get("_ENTRABOT_UPN_SUFFIX", "").strip():
        print(f"  Using explicit Agent User UPN: {explicit_upn}")
        return explicit_upn

    # Query verified domains via Provisioner token (not az CLI — Learning #1)
    resp = requests.get(
        "https://graph.microsoft.com/v1.0/domains?$select=id,isDefault,isVerified",
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code == 200:
        domains = resp.json().get("value", [])
        # Prefer custom domain (not .onmicrosoft.com)
        custom = [
            d["id"] for d in domains if d.get("isVerified") and ".onmicrosoft.com" not in d["id"]
        ]
        # When --new, add a unique suffix to avoid UPN collision
        upn_suffix = os.environ.get("_ENTRABOT_UPN_SUFFIX", "")
        agent_name = f"entrabot-agent-{upn_suffix}" if upn_suffix else "entrabot-agent"
        if custom:
            print(f"  Using custom verified domain: {custom[0]}")
            return f"{agent_name}@{custom[0]}"
        # Fall back to default domain (including .onmicrosoft.com)
        default = [d["id"] for d in domains if d.get("isDefault")]
        if default:
            print(f"  Using default domain: {default[0]}")
            return f"{agent_name}@{default[0]}"

    # Last resort
    print("  WARNING: Could not determine verified domain for Agent User UPN")
    print(f"  Graph API returned: {resp.status_code} {resp.text[:200]}")
    return "entrabot-agent@unknown.onmicrosoft.com"


def _resolve_graph_sp_object_id(token: str) -> str | None:
    """Get the object ID of the Microsoft Graph service principal in this tenant."""
    return _resolve_sp_object_id_by_app_id(token, MS_GRAPH_API_APP_ID)


def _resolve_sp_object_id_by_app_id(token: str, app_id: str) -> str | None:
    """Get the object ID of a service principal by its appId in this tenant."""
    resp = graph_request(
        "GET",
        f"/servicePrincipals?$filter=appId eq '{app_id}'&$select=id",
        token,
    )
    if resp.status_code == 200:
        values = resp.json().get("value", [])
        if values:
            return values[0].get("id")
    return None


def find_existing_agent_user(token: str, agent_identity_obj_id: str) -> dict | None:
    """Find an existing Agent User linked to the given Agent Identity.

    The stored AGENT_USER_ID is only trusted if the fetched user's
    ``identityParentId`` matches ``agent_identity_obj_id`` — otherwise
    state from a previous Blueprint's chain can silently win and the
    caller ends up pointing at a user parented by a different Agent
    Identity. Seen in the wild on 2026-04-19 when a name-based lookup
    elsewhere mis-assigned AGENT_ID across Blueprints; defense in
    depth here keeps the damage from propagating.

    None means Graph has said there is none: the stored user is gone (404) or parented
    elsewhere, and no user is parented by ``agent_identity_obj_id``. A lookup that fails, or an
    answer that can't be read, raises :class:`AgentLookupError` before the caller creates one.
    """
    stored_user_id = str(get_state("AGENT_USER_ID") or "").strip()
    if stored_user_id and not _ID.match(stored_user_id):
        print(f"  [warn] Stored AGENT_USER_ID {stored_user_id!r} is not an ID; ignoring it.")
        stored_user_id = ""
    if stored_user_id:
        try:
            user = _object_or_none(token, f"/users/{stored_user_id}")
            if user is not None and not user.get("id"):
                raise _NoAnswer(_UNREADABLE)
        except _NoAnswer as exc:
            question = (
                f"whether the Agent User recorded for this machine (AGENT_USER_ID "
                f"{stored_user_id}) belongs to Agent Identity {agent_identity_obj_id}"
            )
            raise AgentLookupError(
                _agent_unreadable(question, str(exc), _NO_USER_CREATED),
                AGENT_LOOKUP_COULD_NOT_CHECK_MARKER,
            ) from exc
        if user is None:
            print(f"  [warn] Stored AGENT_USER_ID not found: {stored_user_id}")
        elif _same_id(user.get("identityParentId"), agent_identity_obj_id):
            return user
        else:
            print(
                f"  [warn] stored AGENT_USER_ID={stored_user_id} is parented by a "
                f"different Agent Identity; ignoring and re-discovering."
            )

    # Search by identityParentId (the Agent Identity's object ID)
    try:
        body = _answer(
            token, f"/users?$filter=identityParentId eq '{odata_escape(agent_identity_obj_id)}'"
        )
        values = _value_list(body)
        if values:
            if not values[0].get("id"):
                raise _NoAnswer(_UNREADABLE)
            return values[0]
        if body.get("@odata.nextLink"):
            raise _NoAnswer("Microsoft Graph returned more than one page of users")
    except _NoAnswer as exc:
        question = f"whether Agent Identity {agent_identity_obj_id} already has an Agent User"
        raise AgentLookupError(
            _agent_unreadable(question, str(exc), _NO_USER_CREATED),
            AGENT_LOOKUP_COULD_NOT_CHECK_MARKER,
        ) from exc
    return None


def find_existing_agent_user_by_upn(token: str, upn: str) -> dict | None:
    """Find an Agent User in the tenant by its UPN (tenant-wide).

    An Agent User's UPN is globally unique in Entra. We check BEFORE any
    creation so a re-run on a different machine (with a different
    hostname-suffixed Agent Identity display name) doesn't try to mint
    a second Agent Identity + Agent User and collide with the existing
    UPN — the failure mode that produced the orphan 966d16f3-... on
    2026-04-16.

    Returns the user dict (including ``identityParentId``) so the caller
    can derive the parent Agent Identity already in use; whether that may be
    reused is :func:`agent_identity_to_reuse`'s question.

    None means Graph has said no user has that name. A lookup that fails, or an
    answer that can't be read, raises :class:`AgentLookupError` instead: read as
    "none", it led to a second Agent Identity and then a collision on the name.
    """
    path = (
        f"/users?$filter=userPrincipalName eq '{odata_escape(upn)}'"
        "&$select=id,userPrincipalName,displayName,identityParentId,"
        "mailNickname,accountEnabled"
    )
    try:
        body = _answer(token, path)
        for user in _value_list(body):
            if str(user.get("userPrincipalName") or "").lower() == upn.lower():
                if not user.get("id"):
                    raise _NoAnswer(_UNREADABLE)
                return user
        if body.get("@odata.nextLink"):
            raise _NoAnswer("Microsoft Graph returned more than one page of users")
    except _NoAnswer as exc:
        raise AgentLookupError(
            _agent_unreadable(
                f"whether an Agent User named {upn} exists", str(exc), _NOTHING_CHANGED
            ),
            AGENT_LOOKUP_COULD_NOT_CHECK_MARKER,
        ) from exc
    return None


def agent_identity_to_reuse(token: str, user: dict, blueprint_app_id: str) -> dict:
    """The parent Agent Identity of *user*, an Agent User found by the sign-in name this run
    wants, when it belongs to the Blueprint this run is for. *blueprint_app_id* is that
    Blueprint's App ID, or empty when this run is about to create one.

    An Agent User belongs to one Agent Identity, and that to one Blueprint, for good. Reusing one
    of another Blueprint would put another agent on this machine: its consents, app role and
    licence granted again, and its IDs recorded next to this run's Blueprint. So this stops when
    it belongs to another Blueprint, and when which one can't be read. The Blueprint is the
    parent's ``agentIdentityBlueprintId`` (the Blueprint's App ID, the property that also scopes
    :func:`find_existing_agent_identity`), read with ``GET /servicePrincipals/{id}``.

    Raises :class:`AgentLookupError`, before anything is created, granted or recorded.
    """
    upn = str(user.get("userPrincipalName") or "")
    parent_id = str(user.get("identityParentId") or "").strip()
    advice = _TRY_AGAIN
    try:
        if not parent_id:
            # An ordinary account can have the name too, and waiting would not change that.
            advice = (
                "  If that is an ordinary account and not an agent's, give this agent a different",
                "  name: --with-upn-suffix=NAME (-UpnSuffix NAME on Windows). If it is an agent's",
                "  Agent User, run setup again in a few minutes.",
            )
            raise _NoAnswer("Microsoft Graph's answer names no parent Agent Identity for it")
        if not _ID.match(parent_id):
            raise _NoAnswer(_UNREADABLE)
        parent = _object_or_none(token, f"/servicePrincipals/{parent_id}")
        if parent is None:
            raise _NoAnswer(
                f"its parent Agent Identity {parent_id} was not found (Microsoft Graph answered"
                " HTTP 404; one created in the last few minutes may not be listed yet)"
            )
        theirs = str(parent.get("agentIdentityBlueprintId") or "").strip()
        if not theirs:
            raise _NoAnswer(
                f"Microsoft Graph's answer names no Blueprint for its parent Agent Identity "
                f"{parent_id}"
            )
        if not parent.get("appId"):
            raise _NoAnswer(_UNREADABLE)
    except _NoAnswer as exc:
        question = f"which Blueprint the Agent User {upn} belongs to"
        raise AgentLookupError(
            _agent_unreadable(question, str(exc), _NOTHING_CHANGED, advice),
            AGENT_LOOKUP_COULD_NOT_CHECK_MARKER,
        ) from exc
    if not _same_id(theirs, blueprint_app_id):
        raise AgentLookupError(
            _wrong_blueprint(upn, parent_id, theirs, blueprint_app_id),
            AGENT_USER_WRONG_BLUEPRINT_MARKER,
        )
    return parent


def create_agent_user(
    token: str,
    agent_identity_obj_id: str,
) -> tuple[str, str]:
    """Create or find the Agent User. Returns (user_object_id, user_upn)."""
    print("\n--- Creating Agent User ---\n")

    if _FORCE_NEW:
        print("  [--new] Skipping existing Agent User lookup — creating fresh")
        existing = None
    else:
        existing = find_existing_agent_user(token, agent_identity_obj_id)
    if existing:
        user_id = existing.get("id", "")
        upn = existing.get("userPrincipalName", "")
        print(f"  [skip] Agent User already exists: {upn} ({user_id})")
        set_state("AGENT_USER_ID", user_id)
        set_state("AGENT_USER_UPN", upn)
        return user_id, upn

    upn = _agent_user_upn(token)
    upn_suffix = os.environ.get("_ENTRABOT_UPN_SUFFIX", "")
    mail_nick = f"entrabot-agent-{upn_suffix}" if upn_suffix else "entrabot-agent"
    display = f"EntraBot Agent ({upn_suffix})" if upn_suffix else "EntraBot Agent"
    body = {
        "@odata.type": "microsoft.graph.agentUser",
        "displayName": display,
        "userPrincipalName": upn,
        "identityParentId": agent_identity_obj_id,
        "mailNickname": mail_nick,
        "accountEnabled": True,
    }

    for attempt in range(3):
        resp = graph_request("POST", "/users", token, json_body=body)
        if resp.status_code in (200, 201):
            data = resp.json()
            user_id = data.get("id", "")
            actual_upn = data.get("userPrincipalName", upn)
            print(f"  [new] Agent User created: {actual_upn} ({user_id})")
            set_state("AGENT_USER_ID", user_id)
            set_state("AGENT_USER_UPN", actual_upn)
            # Wait for Entra propagation (Learning #8: 30-120s for new principals)
            print("  Waiting 30s for Agent User to propagate in Entra...")
            time.sleep(30)
            return user_id, actual_upn
        elif resp.status_code == 403:
            print("  ERROR: Permission denied creating Agent User")
            print("  Ensure AgentIdUser.ReadWrite.IdentityParentedBy is granted")
            print(f"  Response: {resp.text[:300]}")
            sys.exit(1)
        elif attempt < 2:
            wait = 10 * (attempt + 1)
            print(f"  Returned {resp.status_code}, retrying in {wait}s...")
            time.sleep(wait)
        else:
            print(f"  ERROR: Failed after retries ({resp.status_code})")
            print(f"  Response: {resp.text[:300]}")
            sys.exit(1)

    sys.exit(1)


def grant_agent_user_consent(
    token: str,
    agent_identity_obj_id: str,
    agent_user_obj_id: str,
) -> None:
    """Grant the Agent Identity permission to act as the Agent User for Graph APIs.

    Creates an oAuth2PermissionGrant so the Agent Identity can get delegated
    tokens (Chat.Create, ChatMessage.Send, etc.) as the Agent User.
    """
    print("\n--- Granting Agent User consent ---\n")

    graph_sp_id = _resolve_graph_sp_object_id(token)
    if not graph_sp_id:
        print("  ERROR: Could not resolve Microsoft Graph SP object ID")
        print("  Consent grant will need to be done manually")
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    from datetime import UTC, datetime

    # Scopes the Agent Identity can request when impersonating the Agent User:
    #   Chat.*, ChatMessage.Send  — Teams chat send/read/manage (the original use case)
    #   User.Read                 — basic profile lookup (self only)
    #   User.ReadBasic.All        — read basic profile of OTHER users in the tenant
    #                               (mail/UPN/identities). Required for share_file
    #                               sponsor email allowlist (Learning #57, 2026-04-30):
    #                               /sponsors nav-collection returns sparse user objects
    #                               with all email fields null, so /users/{id} enrichment
    #                               is mandatory and needs at minimum User.ReadBasic.All.
    #   Files.ReadWrite           — OneDrive file ops (e.g., upload PDFs, share links)
    #   Files.Read.All            — SharedWithMe + cross-drive reads (Files MCP V1 PR1)
    #   Sites.Read.All            — SharePoint site/driveItem reads (Files MCP V1 PR1)
    #   Sites.ReadWrite.All       — POST /beta/.../comments on SharePoint files (Files MCP V1 PR1)
    #   Mail.Read                 — read Agent User's mailbox (inbound email)
    #   Mail.Send                 — send email from Agent User (e.g., daily activity summary)
    scopes = (
        "Chat.Create Chat.ReadWrite ChatMessage.Send User.Read User.ReadBasic.All "
        "Files.ReadWrite Files.Read.All Sites.Read.All Sites.ReadWrite.All "
        "Mail.Read Mail.Send"
    )
    required_scopes = set(scopes.split())

    # Check if consent already exists (v1.0 API)
    check_url = (
        "https://graph.microsoft.com/v1.0/oauth2PermissionGrants"
        f"?$filter=clientId eq '{agent_identity_obj_id}'"
        f" and principalId eq '{agent_user_obj_id}'"
    )
    resp = requests.get(check_url, headers=headers)
    if resp.status_code == 200:
        existing = resp.json().get("value", [])
        if existing:
            grant = existing[0]
            existing_scopes = set((grant.get("scope") or "").split())
            missing = required_scopes - existing_scopes
            if not missing:
                print(f"  [skip] Consent already granted (scope: {grant.get('scope', '')})")
                return
            # Existing consent is missing some required scopes — PATCH to add them.
            merged = sorted(existing_scopes | required_scopes)
            patch_url = f"https://graph.microsoft.com/v1.0/oauth2PermissionGrants/{grant['id']}"
            patch_resp = requests.patch(
                patch_url,
                headers=headers,
                json={"scope": " ".join(merged)},
            )
            if patch_resp.status_code in (200, 204):
                print(f"  [updated] Added missing scopes: {sorted(missing)}")
                print(f"           Full scope set: {' '.join(merged)}")
                return
            print(
                f"  ERROR: Failed to patch consent ({patch_resp.status_code}): "
                f"{patch_resp.text[:200]}"
            )
            print("  Falling through to create new grant...")

    body = {
        "clientId": agent_identity_obj_id,
        "consentType": "Principal",
        "principalId": agent_user_obj_id,
        "resourceId": graph_sp_id,
        "scope": scopes,
        "startTime": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    # oAuth2PermissionGrants is a v1.0 API — use the full URL, not graph_request()
    # which prepends the beta base URL.
    # Retry on "Principal not found" — Agent User may still be propagating (Learning #8)
    for attempt in range(4):
        resp = requests.post(
            "https://graph.microsoft.com/v1.0/oauth2PermissionGrants",
            headers=headers,
            json=body,
        )
        if resp.status_code in (200, 201):
            print(f"  [new] Consent granted: {scopes}")
            return

        # Retry on propagation-related errors
        resp_text = resp.text
        is_propagation = (
            "Principal was not found" in resp_text
            or "does not exist" in resp_text
            or resp.status_code == 404
        )
        if is_propagation and attempt < 3:
            wait = 15 * (attempt + 1)
            print(f"  Agent User not yet propagated — waiting {wait}s (attempt {attempt + 1}/4)...")
            time.sleep(wait)
            continue

        print(f"  ERROR: Consent grant failed ({resp.status_code})")
        print(f"  Response: {resp_text[:400]}")
        print("")
        print("  This is a BLOCKING error — hop 3 of the three-hop flow will fail")
        print("  without this consent grant. Check that the provisioner has")
        print("  DelegatedPermissionGrant.ReadWrite.All permission.")
        sys.exit(1)


def grant_agent_identity_app_permissions(
    token: str,
    agent_identity_obj_id: str,
) -> None:
    """Grant Application.Read.All to the Agent Identity service principal.

    The Agent Identity needs this app permission to call the sponsors API
    (GET /servicePrincipals/{id}/microsoft.graph.agentIdentity/sponsors)
    which is required for sponsor-gated flows like wait_for_sponsor_dm.

    This is an app role assignment (application permission), not a delegated
    permission grant — the Agent Identity calls Graph as itself, not on behalf
    of the Agent User.
    """
    print("\n--- Granting Agent Identity app permissions ---\n")

    graph_sp_id = _resolve_graph_sp_object_id(token)
    if not graph_sp_id:
        print("  ERROR: Could not resolve Microsoft Graph SP object ID")
        print("  Application.Read.All grant will need to be done manually")
        return

    # Application.Read.All app role ID (well-known across all tenants)
    APPLICATION_READ_ALL_ROLE_ID = "9a5d68dd-52b0-4cc2-bd40-abcf44ac3a30"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Check if already granted
    check_url = (
        f"https://graph.microsoft.com/v1.0/servicePrincipals/{agent_identity_obj_id}"
        "/appRoleAssignments"
    )
    resp = requests.get(check_url, headers=headers)
    if resp.status_code == 200:
        existing = resp.json().get("value", [])
        for assignment in existing:
            if assignment.get("appRoleId") == APPLICATION_READ_ALL_ROLE_ID:
                print("  [skip] Application.Read.All already granted")
                return

    # Grant the app role
    body = {
        "principalId": agent_identity_obj_id,
        "resourceId": graph_sp_id,
        "appRoleId": APPLICATION_READ_ALL_ROLE_ID,
    }
    resp = requests.post(
        f"https://graph.microsoft.com/v1.0/servicePrincipals/{agent_identity_obj_id}"
        "/appRoleAssignments",
        headers=headers,
        json=body,
    )
    if resp.status_code in (200, 201):
        print("  [ok] Application.Read.All granted to Agent Identity")
    elif resp.status_code == 409:
        print("  [skip] Application.Read.All already granted (409 conflict)")
    else:
        print(f"  WARNING: Failed to grant Application.Read.All ({resp.status_code})")
        print(f"  Response: {resp.text[:300]}")
        print("  The sponsors API (wait_for_sponsor_dm) will fail with 403.")
        print("  Grant manually: New-MgServicePrincipalAppRoleAssignment")


def grant_agent_user_storage_consent(
    token: str,
    agent_identity_obj_id: str,
    agent_user_obj_id: str,
) -> None:
    """Grant the Agent Identity permission to request Azure Storage tokens as the Agent User.

    Creates/updates an oAuth2PermissionGrant with `user_impersonation` on the
    Azure Storage service principal so the third hop of the three-hop flow can
    exchange a `user_fic` grant for a `idtyp=user` token with
    `https://storage.azure.com/.default` audience (ADR-005).
    """
    print("\n--- Granting Agent User storage consent ---\n")

    storage_sp_id = _resolve_sp_object_id_by_app_id(token, AZURE_STORAGE_APP_ID)
    if not storage_sp_id:
        print("  WARN: Azure Storage SP not found in tenant — cannot grant consent.")
        print("  This is non-fatal; cloud-hosted memory (ADR-005) will not work")
        print("  until the grant is added manually or --keep-memory-local is set.")
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    from datetime import UTC, datetime

    check_url = (
        "https://graph.microsoft.com/v1.0/oauth2PermissionGrants"
        f"?$filter=clientId eq '{agent_identity_obj_id}'"
        f" and principalId eq '{agent_user_obj_id}'"
        f" and resourceId eq '{storage_sp_id}'"
    )
    resp = requests.get(check_url, headers=headers)
    if resp.status_code == 200:
        existing = resp.json().get("value", [])
        if existing:
            grant = existing[0]
            existing_scopes = set((grant.get("scope") or "").split())
            if AZURE_STORAGE_SCOPE in existing_scopes:
                print(f"  [skip] Storage consent already granted ({AZURE_STORAGE_SCOPE})")
                return
            merged = sorted(existing_scopes | {AZURE_STORAGE_SCOPE})
            patch_url = f"https://graph.microsoft.com/v1.0/oauth2PermissionGrants/{grant['id']}"
            patch_resp = requests.patch(
                patch_url,
                headers=headers,
                json={"scope": " ".join(merged)},
            )
            if patch_resp.status_code in (200, 204):
                print(f"  [updated] Added storage scope: {AZURE_STORAGE_SCOPE}")
                return
            print(
                f"  WARN: Failed to patch storage consent ({patch_resp.status_code}): "
                f"{patch_resp.text[:200]}"
            )
            print("  Falling through to create new grant...")

    body = {
        "clientId": agent_identity_obj_id,
        "consentType": "Principal",
        "principalId": agent_user_obj_id,
        "resourceId": storage_sp_id,
        "scope": AZURE_STORAGE_SCOPE,
        "startTime": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    for attempt in range(4):
        resp = requests.post(
            "https://graph.microsoft.com/v1.0/oauth2PermissionGrants",
            headers=headers,
            json=body,
        )
        if resp.status_code in (200, 201):
            print(f"  [new] Storage consent granted: {AZURE_STORAGE_SCOPE}")
            return

        resp_text = resp.text
        is_propagation = (
            "Principal was not found" in resp_text
            or "does not exist" in resp_text
            or resp.status_code == 404
        )
        if is_propagation and attempt < 3:
            wait = 15 * (attempt + 1)
            print(
                f"  Storage SP/User not yet propagated — "
                f"waiting {wait}s (attempt {attempt + 1}/4)..."
            )
            time.sleep(wait)
            continue

        print(f"  WARN: Storage consent grant failed ({resp.status_code})")
        print(f"  Response: {resp_text[:400]}")
        print("  Cloud-hosted memory (ADR-005) will not work until this is resolved.")
        print("  Re-run this script or set ENTRABOT_KEEP_MEMORY_LOCAL=true to bypass.")
        return


# ---------------------------------------------------------------------------
# License assignment
# ---------------------------------------------------------------------------
# License SKU constants are imported from entrabot.preflight — single source of
# truth so setup.sh's preflight checks and actual assignment use the same lists.


def _get_available_skus(token: str) -> list[dict]:
    """Get all subscribed SKUs with available licenses."""
    resp = graph_request("GET", "/subscribedSkus", token)
    if resp.status_code != 200:
        print(f"  WARNING: Could not list subscribed SKUs ({resp.status_code})")
        return []

    skus = resp.json().get("value", [])
    available = []
    for sku in skus:
        enabled = sku.get("prepaidUnits", {}).get("enabled", 0)
        consumed = sku.get("consumedUnits", 0)
        remaining = enabled - consumed
        if remaining > 0:
            available.append(
                {
                    "skuId": sku["skuId"],
                    "skuPartNumber": sku.get("skuPartNumber", ""),
                    "displayName": sku.get("skuPartNumber", sku["skuId"]),
                    "remaining": remaining,
                    "total": enabled,
                }
            )
    return available


def _set_usage_location(token: str, user_id: str, location: str = "US") -> bool:
    """Set the usageLocation on a user (required before license assignment)."""
    resp = graph_request(
        "PATCH",
        f"/users/{user_id}",
        token,
        json_body={"usageLocation": location},
    )
    return resp.status_code in (200, 204)


def _check_existing_licenses(token: str, user_id: str) -> list[str]:
    """Check what licenses are already assigned to the user."""
    resp = graph_request("GET", f"/users/{user_id}?$select=assignedLicenses", token)
    if resp.status_code == 200:
        return [lic.get("skuId", "") for lic in resp.json().get("assignedLicenses", [])]
    return []


def _get_all_sku_part_numbers(token: str) -> dict[str, str] | None:
    resp = graph_request("GET", "/subscribedSkus", token)
    if resp.status_code != 200:
        print(f"  WARNING: Could not resolve subscribed SKU names ({resp.status_code})")
        return None
    return {
        sku["skuId"]: sku.get("skuPartNumber", sku["skuId"])
        for sku in resp.json().get("value", [])
    }


def _ensure_usage_location(token: str, agent_user_id: str) -> bool:
    print("  Setting usageLocation on Agent User (waiting for Entra replication)...")
    for attempt in range(5):
        if _set_usage_location(token, agent_user_id):
            return True
        wait = 5 * (attempt + 1)
        print(f"  Agent User not ready yet, retrying in {wait}s...")
        time.sleep(wait)
    print("  WARNING: Could not set usageLocation after retries")
    print("  The Agent User may not have replicated to M365 yet.")
    print("  Re-run setup.sh in a few minutes to assign the license.")
    return False


def _assign_license(token: str, agent_user_id: str, sku: dict, label: str) -> bool:
    print(f"  Assigning {sku['displayName']} to Agent User...")
    for attempt in range(3):
        resp = graph_request(
            "POST",
            f"/users/{agent_user_id}/assignLicense",
            token,
            json_body={
                "addLicenses": [{"skuId": sku["skuId"]}],
                "removeLicenses": [],
            },
        )
        if resp.status_code in (200, 201):
            print(f"  [done] {label} license assigned: {sku['displayName']}")
            return True
        if attempt < 2:
            wait = 10 * (attempt + 1)
            print(f"  License assignment returned {resp.status_code}, retrying in {wait}s...")
            time.sleep(wait)

    print(f"  WARNING: {label} license assignment failed after retries ({resp.status_code})")
    print(f"  Response: {resp.text[:300]}")
    print("  Re-run setup.sh in a few minutes or assign manually in the Entra admin center")
    return False


def assign_license_to_agent_user(
    token: str,
    agent_user_id: str,
    *,
    assign_teams: bool = _ASSIGN_TEAMS_LICENSE,
    assign_work_iq: bool = _ASSIGN_WORK_IQ_LICENSE,
) -> None:
    """Assign only the requested licenses to the Agent User.

    Teams presence needs a Teams-capable SKU. Work IQ MCP servers need
    Microsoft 365 Copilot as a distinct license and are intentionally opt-in.
    """
    print("\n--- License Assignment ---\n")

    if not assign_teams and not assign_work_iq:
        print("  [skip] License assignment disabled for this run")
        return

    existing_sku_ids = _check_existing_licenses(token, agent_user_id)
    sku_id_to_name = _get_all_sku_part_numbers(token)
    if existing_sku_ids and sku_id_to_name is None:
        print("  WARNING: Could not resolve existing Agent User license SKU names.")
        print("  Skipping automatic license assignment to avoid duplicate assignment.")
        print("  Re-run setup.sh after Graph /subscribedSkus is available.")
        return
    sku_id_to_name = sku_id_to_name or {}
    existing_names = [sku_id_to_name.get(sid, sid) for sid in existing_sku_ids]
    has_teams = any(name in TEAMS_CAPABLE_SKUS for name in existing_names)
    has_copilot = any(name in COPILOT_CAPABLE_SKUS for name in existing_names)

    if existing_sku_ids:
        if has_teams:
            teams_name = next(n for n in existing_names if n in TEAMS_CAPABLE_SKUS)
            print(f"  [skip] Agent User already has Teams-capable license: {teams_name}")
        else:
            print(f"  Agent User has {len(existing_sku_ids)} license(s) but none include Teams:")
            for name in existing_names:
                print(f"    - {name}")
            print("  Will assign a Teams-capable license...")
        if has_copilot:
            copilot_name = next(n for n in existing_names if n in COPILOT_CAPABLE_SKUS)
            print(f"  [skip] Agent User already has Work IQ license: {copilot_name}")

    if (not assign_teams or has_teams) and (not assign_work_iq or has_copilot):
        if assign_teams and assign_work_iq:
            print("  [skip] Agent User already has Teams and Work IQ licenses")
        elif assign_teams:
            print("  [skip] Agent User already has Teams-capable license")
        elif assign_work_iq:
            print("  [skip] Agent User already has Work IQ license")
        return

    all_skus = _get_available_skus(token)
    if not all_skus:
        print("  ERROR: No subscribed SKUs found in this tenant, or no available licenses.")
        print("  Purchase M365 licenses (E3/E5/Teams Enterprise) at https://admin.microsoft.com")
        print("  Then re-run setup.sh to assign a license to the Agent User.")
        return

    if not _ensure_usage_location(token, agent_user_id):
        return

    assigned_teams = False
    assigned_copilot = False
    if assign_teams and not has_teams:
        teams_skus = [s for s in all_skus if s["skuPartNumber"] in TEAMS_CAPABLE_SKUS]
        if not teams_skus:
            print("  No Teams-capable licenses found with available seats.")
            print("  Available SKUs in this tenant:")
            for i, sku in enumerate(all_skus, 1):
                print(
                    f"    {i}. {sku['displayName']}"
                    f" ({sku['remaining']}/{sku['total']} available)"
                )
            print("")
            print("  To assign a license to the Agent User, either:")
            print("  - Purchase a Teams-capable license (E3/E5/Teams Enterprise)")
            print("  - Or assign one manually in the Entra admin center")
        else:
            chosen = teams_skus[0]
            if len(teams_skus) == 1:
                print(
                    f"  Found 1 Teams-capable license: {chosen['displayName']}"
                    f" ({chosen['remaining']}/{chosen['total']} available)"
                )
            else:
                print("  Teams-capable licenses available; auto-selecting first available:")
                for i, sku in enumerate(teams_skus, 1):
                    print(
                        f"    {i}. {sku['displayName']}"
                        f" ({sku['remaining']}/{sku['total']} available)"
                    )
            if _assign_license(token, agent_user_id, chosen, "Teams-capable"):
                set_state("AGENT_USER_LICENSE_SKU", chosen["skuPartNumber"])
                assigned_teams = True

    if assign_work_iq and not has_copilot:
        copilot_skus = [s for s in all_skus if s["skuPartNumber"] in COPILOT_CAPABLE_SKUS]
        if not copilot_skus:
            print("  No Microsoft 365 Copilot licenses found with available seats.")
            print("  Work IQ MCP servers require Microsoft 365 Copilot for the Agent User.")
            print("  Purchase or free a Copilot seat, then re-run setup.sh.")
            return
        chosen = copilot_skus[0]
        print(
            f"  Found Work IQ license: {chosen['displayName']}"
            f" ({chosen['remaining']}/{chosen['total']} available)"
        )
        if _assign_license(token, agent_user_id, chosen, "Work IQ"):
            set_state("AGENT_USER_WORK_IQ_LICENSE_SKU", chosen["skuPartNumber"])
            assigned_copilot = True

    if assigned_teams and assigned_copilot:
        print("  Teams/mailbox and Work IQ provisioning can take 10-15 minutes")
    elif assigned_teams:
        print("  Teams/mailbox provisioning can take 10-15 minutes")
    elif assigned_copilot:
        print("  Work IQ provisioning can take 10-15 minutes")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 60)
    print("EntraBot — Entra Agent Identity Provisioning")
    print("=" * 60)

    try:
        token = get_existing_graph_token()
    except ProvisionerBootstrapError as exc:
        print(f"ERROR: {exc}")
        return 1

    try:
        _provision(token)
    except ProvisioningStop as exc:
        print(exc.marker)  # alone on its line, for the setup front ends
        print(exc)
        return 1
    return 0


def _provision(token: str) -> None:
    """Find or create the chain, then grant and license it. Raises :class:`ProvisioningStop`.

    The Blueprint, and whether an Agent User with the wanted sign-in name may be reused, are
    settled before anything is created or recorded.
    """
    blueprint = blueprint_for_this_run(token)

    # Check tenant-wide for an existing Agent User by UPN BEFORE provisioning
    # anything new. If the UPN already exists (from a prior run on another
    # machine, or a state-file loss), reuse the Agent User + its parent
    # Agent Identity rather than creating duplicates that would then
    # collide on the unique-UPN constraint -- but only when that Agent
    # Identity belongs to this run's Blueprint (agent_identity_to_reuse).
    #
    # When _FORCE_NEW (setup.sh --new), skip this check entirely and use a
    # unique UPN suffix so the new Agent User doesn't collide.
    reuse: tuple[dict, dict] | None = None
    if _FORCE_NEW:
        _suffix = os.environ.get("_ENTRABOT_UPN_SUFFIX", "")
        if _suffix:
            print(f"  [--new] Will use UPN suffix: {_suffix}")
        else:
            print("  [--new] WARNING: No UPN suffix set — may collide with existing Agent User")
    else:
        print("\n--- Looking for an existing Agent User ---\n")
        intended_upn = _agent_user_upn(token)
        existing_user = find_existing_agent_user_by_upn(token, intended_upn)
        if existing_user:
            wanted = str(blueprint["appId"]) if blueprint else ""  # empty: about to be created
            reuse = (existing_user, agent_identity_to_reuse(token, existing_user, wanted))
        else:
            print(f"  No Agent User is named {intended_upn} yet.")

    blueprint_app_id, blueprint_obj_id = record_blueprint(token, blueprint)

    if reuse:
        existing_user, sp = reuse
        agent_user_id = existing_user["id"]
        agent_user_upn = existing_user["userPrincipalName"]
        agent_obj_id = existing_user["identityParentId"]
        agent_id = sp["appId"]
        parent_name = sp.get("displayName", "?")
        print("\n--- Reusing existing Agent User ---\n")
        print(f"  UPN:            {agent_user_upn}")
        print(f"  User Object ID: {agent_user_id}")
        print(f"  Parent Agent Identity: {parent_name}")
        print(f"    appId:        {agent_id}")
        print(f"    objId:        {agent_obj_id}")
        print("  (Skipping Agent Identity + Agent User creation — already provisioned.)")
        set_state("AGENT_ID", agent_id)
        set_state("AGENT_OBJECT_ID", agent_obj_id)
        set_state("AGENT_USER_ID", agent_user_id)
        set_state("AGENT_USER_UPN", agent_user_upn)
    else:
        agent_id, agent_obj_id = create_agent_identity(token, blueprint_app_id)
        agent_user_id, agent_user_upn = create_agent_user(token, agent_obj_id)

    grant_agent_identity_app_permissions(token, agent_obj_id)
    grant_agent_user_consent(token, agent_obj_id, agent_user_id)
    grant_agent_user_storage_consent(token, agent_obj_id, agent_user_id)
    assign_license_to_agent_user(token, agent_user_id)

    print("\n--- Summary ---\n")
    print(f"  Blueprint App ID:    {blueprint_app_id}")
    print(f"  Blueprint Object ID: {blueprint_obj_id}")
    print(f"  Agent App ID:        {agent_id}")
    print(f"  Agent Object ID:     {agent_obj_id}")
    print(f"  Agent User ID:       {agent_user_id}")
    print(f"  Agent User UPN:      {agent_user_upn}")
    print(f"  Agent Display Name:  {_agent_display_name()}")
    print("")


if __name__ == "__main__":
    sys.exit(main())
