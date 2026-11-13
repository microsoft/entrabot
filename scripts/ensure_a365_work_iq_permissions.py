#!/usr/bin/env python3
"""Ensure Microsoft Agent 365 Work IQ MCP tenant resources are usable.

With Entrabot's Provisioner token (never an Azure CLI token), this script gives the agent's
Blueprint what Word, Excel and OneDrive files need, which is what ``a365 setup permissions mcp``
does:

1. the resource service principals and the Blueprint-wide OAuth2 permission grants, and
2. inheritable permissions on the Blueprint (Microsoft Learn, "Configure inheritable
   permissions for agent identity blueprints"), which let the Blueprint's agents use those
   grants. With the grants alone, the agent's token request fails with AADSTS65001.

The Windows setup wizard runs it instead of Microsoft's Agent 365 CLI. ``setup.sh`` runs it
before the CLI's own permission step, where an existing entry that already covers these
scopes (the CLI sets every scope and role) is left alone, so it changes nothing.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from entra_provisioning import (  # noqa: E402
    ProvisionerBootstrapError,
    get_existing_graph_token,
    get_state,
)

# The repo root is one directory up; src/ contains the entrabot package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from entrabot.a365.catalog import (  # noqa: E402
    AGENT_365_TOOLS_APP_ID,
    WORK_IQ_WORD_APP_ID,
    granted_scopes,
)
from entrabot.graph_helpers import odata_escape as _odata_escape  # noqa: E402

GRAPH_V1 = "https://graph.microsoft.com/v1.0"

# The resource apps, from the package's catalog: the one source of truth for the manifest
# setup writes and the permissions it grants.
A365_AGENT_TOOLS_APP_ID = AGENT_365_TOOLS_APP_ID
A365_WORD_MCP_APP_ID = WORK_IQ_WORD_APP_ID


class ResponseLike(Protocol):
    status_code: int
    text: str

    def json(self) -> dict[str, Any]: ...


RequestFn = Callable[..., ResponseLike]
SleepFn = Callable[[float], None]


class A365PermissionError(RuntimeError):
    """Raised when A365 Work IQ permissions cannot be made ready."""


@dataclass(frozen=True)
class RequiredResource:
    app_id: str
    display_name: str
    scope: str


RESOURCE_NAMES = {
    A365_AGENT_TOOLS_APP_ID: "Agent 365 tools",
    A365_WORD_MCP_APP_ID: "Work IQ Word MCP",
}

REQUIRED_RESOURCES = tuple(
    RequiredResource(app_id=app_id, display_name=RESOURCE_NAMES.get(app_id, app_id), scope=scope)
    for app_id, scopes in granted_scopes().items()
    for scope in scopes
)
# How long to wait between tries when Entra cannot yet read something written moments ago.
RETRY_WAITS: tuple[float, ...] = (5, 10, 15, 20)


def _scopes_by_resource() -> list[tuple[str, str, tuple[str, ...]]]:
    """``(app ID, name, scopes)`` for each resource app, in the order they are listed.

    Entra keeps one grant per client and resource, so every scope of a resource belongs in
    the same write. Writing them one at a time meant patching a grant created moments
    earlier, which Entra could not always read yet (404 on a new agent).
    """
    grouped: dict[str, list[str]] = {}
    for resource in REQUIRED_RESOURCES:
        scopes = grouped.setdefault(resource.app_id, [])
        if resource.scope not in scopes:
            scopes.append(resource.scope)
    return [
        (app_id, RESOURCE_NAMES.get(app_id, app_id), tuple(scopes))
        for app_id, scopes in grouped.items()
    ]


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _json_or_empty(response: ResponseLike) -> dict[str, Any]:
    try:
        return response.json()
    except ValueError:
        return {}


def _request(
    request: RequestFn,
    method: str,
    url: str,
    token: str,
    *,
    json_body: dict[str, Any] | None = None,
    expected: tuple[int, ...] = (200,),
    action: str,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"headers": _headers(token)}
    if json_body is not None:
        kwargs["json"] = json_body
    response = request(method, url, **kwargs)
    if response.status_code not in expected:
        raise A365PermissionError(
            f"{action} failed ({response.status_code}): {response.text[:400]}"
        )
    return _json_or_empty(response)


def _find_service_principal(
    app_id: str,
    token: str,
    *,
    request: RequestFn,
) -> dict[str, Any] | None:
    data = _request(
        request,
        "GET",
        f"{GRAPH_V1}/servicePrincipals"
        f"?$filter=appId eq '{_odata_escape(app_id)}'"
        "&$select=id,appId,displayName",
        token,
        action=f"query service principal {app_id}",
    )
    values = data.get("value", [])
    if not isinstance(values, list) or not values:
        return None
    first = values[0]
    if not isinstance(first, dict):
        raise A365PermissionError(f"Malformed service principal response for {app_id}")
    return first


def _ensure_resource_service_principal(
    app_id: str,
    display_name: str,
    token: str,
    *,
    request: RequestFn,
    sleep: SleepFn,
) -> str:
    existing = _find_service_principal(app_id, token, request=request)
    if existing:
        object_id = str(existing.get("id") or "")
        if not object_id:
            raise A365PermissionError(f"Service principal for {display_name} has no object id")
        print(f"  [skip] {display_name} SP already exists: {object_id}")
        return object_id

    print(f"  Creating {display_name} service principal...")
    data = _request(
        request,
        "POST",
        f"{GRAPH_V1}/servicePrincipals",
        token,
        json_body={"appId": app_id},
        expected=(200, 201),
        action=f"create {display_name} service principal",
    )
    object_id = str(data.get("id") or "")
    if object_id:
        print(f"  [new] {display_name} SP created: {object_id}")
        return object_id

    for wait in RETRY_WAITS:
        sleep(wait)
        existing = _find_service_principal(app_id, token, request=request)
        if existing and existing.get("id"):
            object_id = str(existing["id"])
            print(f"  [new] {display_name} SP created: {object_id}")
            return object_id

    raise A365PermissionError(
        f"Created {display_name} service principal but could not resolve object id"
    )


def _resolve_blueprint_sp_object_id(
    blueprint_app_id: str,
    token: str,
    *,
    request: RequestFn,
) -> str:
    existing = _find_service_principal(blueprint_app_id, token, request=request)
    if not existing or not existing.get("id"):
        raise A365PermissionError(
            "Blueprint service principal was not found. Run Entrabot provisioning before "
            "configuring A365 Work IQ."
        )
    return str(existing["id"])


def _find_oauth_grant(
    blueprint_sp_object_id: str,
    resource_sp_object_id: str,
    token: str,
    *,
    request: RequestFn,
) -> dict[str, Any] | None:
    data = _request(
        request,
        "GET",
        f"{GRAPH_V1}/oauth2PermissionGrants"
        f"?$filter=clientId eq '{_odata_escape(blueprint_sp_object_id)}'"
        f" and resourceId eq '{_odata_escape(resource_sp_object_id)}'"
        " and consentType eq 'AllPrincipals'",
        token,
        action=f"query OAuth grant for resource {resource_sp_object_id}",
    )
    values = data.get("value", [])
    if not isinstance(values, list) or not values:
        return None
    first = values[0]
    if not isinstance(first, dict):
        raise A365PermissionError(
            f"Malformed OAuth grant response for resource {resource_sp_object_id}"
        )
    return first


def _add_scopes_to_grant(
    display_name: str,
    scopes: tuple[str, ...],
    existing: dict[str, Any],
    token: str,
    *,
    request: RequestFn,
    sleep: SleepFn,
) -> None:
    """Make an existing grant carry ``scopes`` too, keeping whatever it already has."""
    existing_scopes = set(str(existing.get("scope") or "").split())
    missing = [scope for scope in scopes if scope not in existing_scopes]
    if not missing:
        print(f"  [skip] {display_name} grant already has {' '.join(scopes)}")
        return
    grant_id = str(existing.get("id") or "")
    if not grant_id:
        raise A365PermissionError(f"Existing {display_name} grant has no grant id")
    merged = " ".join(sorted(existing_scopes | set(scopes)))
    # A grant that was created moments ago can be listed before it can be updated: Entra
    # answers 404 until it has caught up. Wait and try again rather than give up.
    waits = iter(RETRY_WAITS)
    while True:
        response = request(
            "PATCH",
            f"{GRAPH_V1}/oauth2PermissionGrants/{grant_id}",
            headers=_headers(token),
            json={"scope": merged},
        )
        if response.status_code in (200, 204):
            break
        wait = next(waits, None) if response.status_code == 404 else None
        if wait is None:
            raise A365PermissionError(
                f"patch {display_name} OAuth grant failed "
                f"({response.status_code}): {response.text[:400]}"
            )
        sleep(wait)
    print(f"  [updated] Added {' '.join(missing)} to {display_name} grant")


def _ensure_oauth_grant(
    display_name: str,
    scopes: tuple[str, ...],
    blueprint_sp_object_id: str,
    resource_sp_object_id: str,
    token: str,
    *,
    request: RequestFn,
    sleep: SleepFn = time.sleep,
) -> None:
    """Give the Blueprint ``scopes`` on one resource, in one write."""
    existing = _find_oauth_grant(
        blueprint_sp_object_id,
        resource_sp_object_id,
        token,
        request=request,
    )
    if existing:
        _add_scopes_to_grant(display_name, scopes, existing, token, request=request, sleep=sleep)
        return

    response = request(
        "POST",
        f"{GRAPH_V1}/oauth2PermissionGrants",
        headers=_headers(token),
        json={
            "clientId": blueprint_sp_object_id,
            "consentType": "AllPrincipals",
            "resourceId": resource_sp_object_id,
            "scope": " ".join(sorted(scopes)),
        },
    )
    if response.status_code == 409 and "Permission entry already exists" in response.text:
        for wait in RETRY_WAITS:
            sleep(wait)
            existing = _find_oauth_grant(
                blueprint_sp_object_id,
                resource_sp_object_id,
                token,
                request=request,
            )
            if existing:
                _add_scopes_to_grant(
                    display_name, scopes, existing, token, request=request, sleep=sleep
                )
                return
        raise A365PermissionError(
            f"create {display_name} OAuth grant conflicted, but the "
            "existing grant could not be read back"
        )
    if response.status_code not in (200, 201):
        raise A365PermissionError(
            f"create {display_name} OAuth grant failed "
            f"({response.status_code}): {response.text[:400]}"
        )
    print(f"  [new] {display_name} grant created: {' '.join(sorted(scopes))}")


def ensure_a365_work_iq_permissions(
    *,
    token: str,
    blueprint_app_id: str,
    request: RequestFn = requests.request,
    sleep: SleepFn = time.sleep,
) -> None:
    """Ensure the A365 Work IQ resource SPs and Blueprint grants exist."""
    if not blueprint_app_id.strip():
        raise A365PermissionError("Blueprint App ID is required")

    print("\n--- Ensuring Microsoft Agent 365 Work IQ permissions ---\n")
    blueprint_sp_object_id = _resolve_blueprint_sp_object_id(
        blueprint_app_id.strip(),
        token,
        request=request,
    )
    print(f"  Blueprint SP: {blueprint_sp_object_id}")

    for app_id, display_name, scopes in _scopes_by_resource():
        resource_sp_object_id = _ensure_resource_service_principal(
            app_id,
            display_name,
            token,
            request=request,
            sleep=sleep,
        )
        _ensure_oauth_grant(
            display_name,
            scopes,
            blueprint_sp_object_id,
            resource_sp_object_id,
            token,
            request=request,
            sleep=sleep,
        )

    print("\n  ✅ A365 Work IQ resource service principals and grants are ready")


# ── Inheritable permissions ──────────────────────────────────────────────────────────────────

# Between reading back an entry just written: a read straight after a 201 came back empty on a
# real tenant. About a minute in all, then it is an error rather than a hang.
CONFIRM_WAITS: tuple[float, ...] = (2, 3, 5, 10, 15, 20)
_NO_ROLES = {"@odata.type": "#microsoft.graph.noRoles", "kind": "none"}


def _inheritable_url(blueprint_app_id: str) -> str:
    return (
        f"{GRAPH_V1}/applications/microsoft.graph.agentIdentityBlueprint/"
        f"{quote(blueprint_app_id, safe='')}/inheritablePermissions"
    )


def _enumerated(scopes: list[str]) -> dict[str, Any]:
    return {
        "@odata.type": "#microsoft.graph.enumeratedScopes",
        "kind": "enumerated",
        "scopes": scopes,
    }


def _inheritable_scopes(entry: dict[str, Any] | None) -> tuple[str, list[str]]:
    """``(kind, scopes)`` of an entry's inheritable scopes: ``allAllowed``, ``enumerated``
    with its scopes, or something else (``none``, missing)."""
    if not entry:
        return "", []
    raw = entry.get("inheritableScopes")
    if not isinstance(raw, dict):
        return "", []
    kind = str(raw.get("kind") or "")
    odata_type = str(raw.get("@odata.type") or "").lower()
    if kind.lower() == "allallowed" or odata_type.endswith("allallowedscopes"):
        return "allAllowed", []
    if kind.lower() == "enumerated" or odata_type.endswith("enumeratedscopes"):
        scopes = raw.get("scopes")
        return "enumerated", [str(s) for s in scopes] if isinstance(scopes, list) else []
    return kind or "none", []


def _covers(entry: dict[str, Any] | None, scopes: tuple[str, ...]) -> bool:
    kind, have = _inheritable_scopes(entry)
    return kind == "allAllowed" or (kind == "enumerated" and set(scopes) <= set(have))


def _read_inheritable(
    blueprint_app_id: str, token: str, *, request: RequestFn
) -> dict[str, dict[str, Any]]:
    data = _request(
        request,
        "GET",
        _inheritable_url(blueprint_app_id),
        token,
        action="read the Blueprint's inheritable permissions",
    )
    values = data.get("value", [])
    if not isinstance(values, list):
        raise A365PermissionError("Malformed inheritable permissions response")
    return {
        str(item.get("resourceAppId") or "").lower(): item
        for item in values
        if isinstance(item, dict) and item.get("resourceAppId")
    }


def _wait_for_entry(
    app_id: str,
    scopes: tuple[str, ...],
    blueprint_app_id: str,
    token: str,
    *,
    request: RequestFn,
    sleep: SleepFn,
) -> dict[str, Any] | None:
    """Read the collection back until the entry for *app_id* covers *scopes*."""
    for wait in CONFIRM_WAITS:
        sleep(wait)
        entry = _read_inheritable(blueprint_app_id, token, request=request).get(app_id.lower())
        if _covers(entry, scopes):
            return entry
    return None


def _widen(
    app_id: str,
    scopes: tuple[str, ...],
    entry: dict[str, Any],
    blueprint_app_id: str,
    token: str,
    *,
    request: RequestFn,
) -> None:
    """Add *scopes* to an existing entry, keeping its scopes and leaving its roles alone."""
    _kind, have = _inheritable_scopes(entry)
    merged = [*have, *(scope for scope in scopes if scope not in have)]
    _request(
        request,
        "PATCH",
        f"{_inheritable_url(blueprint_app_id)}/{quote(app_id, safe='')}",
        token,
        json_body={"inheritableScopes": _enumerated(merged)},
        expected=(200, 204),
        action=f"widen the inheritable permissions for {RESOURCE_NAMES.get(app_id, app_id)}",
    )


def _ensure_inheritable(
    app_id: str,
    scopes: tuple[str, ...],
    blueprint_app_id: str,
    token: str,
    *,
    request: RequestFn,
    sleep: SleepFn,
) -> None:
    """One resource's entry: written if missing, widened if narrower, then read back."""
    name = RESOURCE_NAMES.get(app_id, app_id)
    entry = _read_inheritable(blueprint_app_id, token, request=request).get(app_id.lower())
    if _covers(entry, scopes):
        print(f"  [skip] {name}: the Blueprint's agents already inherit {' '.join(scopes)}")
        return
    if entry is None:
        response = request(
            "POST",
            _inheritable_url(blueprint_app_id),
            headers=_headers(token),
            json={
                "resourceAppId": app_id,
                "inheritableScopes": _enumerated(list(scopes)),
                "inheritableRoles": dict(_NO_ROLES),
            },
        )
        if response.status_code == 409:  # it exists, though the read did not show it yet
            for wait in CONFIRM_WAITS:
                sleep(wait)
                entry = _read_inheritable(blueprint_app_id, token, request=request).get(
                    app_id.lower()
                )
                if entry is not None:
                    break
            if entry is None:
                raise A365PermissionError(
                    f"The inheritable permissions for {name} already exist but could not be "
                    "read back"
                )
            if not _covers(entry, scopes):
                _widen(app_id, scopes, entry, blueprint_app_id, token, request=request)
        elif response.status_code not in (200, 201):
            raise A365PermissionError(
                f"write the inheritable permissions for {name} failed "
                f"({response.status_code}): {response.text[:400]}"
            )
    else:
        _widen(app_id, scopes, entry, blueprint_app_id, token, request=request)
    if (
        _wait_for_entry(app_id, scopes, blueprint_app_id, token, request=request, sleep=sleep)
        is None
    ):
        raise A365PermissionError(
            f"The inheritable permissions for {name} were written but could not be read back"
        )
    print(f"  [ok] {name}: the Blueprint's agents inherit {' '.join(scopes)}")


def ensure_inheritable_permissions(
    *,
    token: str,
    blueprint_app_id: str,
    request: RequestFn = requests.request,
    sleep: SleepFn = time.sleep,
    wanted: dict[str, tuple[str, ...]] | None = None,
) -> None:
    """Let the Blueprint's agents inherit the scopes it is granted, least privilege: only
    those scopes, and no app roles. An entry that already covers them is left alone, and
    nothing is ever narrowed. Entries are written one at a time, each read back before the
    next, and all are checked again at the end: a write can be lost when another lands
    moments later, and one that was is written again."""
    if not blueprint_app_id.strip():
        raise A365PermissionError("Blueprint App ID is required")
    blueprint_app_id = blueprint_app_id.strip()
    wanted = wanted if wanted is not None else granted_scopes()
    print("\n--- Letting the Blueprint's agents inherit the Work IQ permissions ---\n")
    for app_id, scopes in wanted.items():
        _ensure_inheritable(app_id, scopes, blueprint_app_id, token, request=request, sleep=sleep)
    entries = _read_inheritable(blueprint_app_id, token, request=request)
    for app_id, scopes in wanted.items():
        if not _covers(entries.get(app_id.lower()), scopes):
            print(f"  [again] {RESOURCE_NAMES.get(app_id, app_id)} was not kept; writing it again")
            _ensure_inheritable(
                app_id, scopes, blueprint_app_id, token, request=request, sleep=sleep
            )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Ensure the Work IQ service principals, OAuth grants and inheritable permissions."
        )
    )
    parser.add_argument(
        "--blueprint-app-id",
        default="",
        help="Agent Identity Blueprint application ID. Defaults to .entrabot-state.json.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    blueprint_app_id = args.blueprint_app_id.strip() or (get_state("BLUEPRINT_APP_ID") or "")
    if not blueprint_app_id:
        print("ERROR: Blueprint App ID not found. Run provisioning first.", file=sys.stderr)
        return 2

    try:
        token = get_existing_graph_token()
        ensure_a365_work_iq_permissions(token=token, blueprint_app_id=blueprint_app_id)
        ensure_inheritable_permissions(token=token, blueprint_app_id=blueprint_app_id)
    except (A365PermissionError, ProvisionerBootstrapError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
