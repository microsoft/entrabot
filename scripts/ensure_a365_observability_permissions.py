#!/usr/bin/env python3
"""Idempotently provision Microsoft Agent 365 Observability permissions.

This is a one-time Entra provisioning command. It grants the Blueprint and its
Agent User path the delegated Observability write scope; it does not run during
harness startup and it never calls the Agent 365 service itself.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import requests

from entrabot.graph_helpers import odata_escape as _odata_escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
from entra_provisioning import (  # noqa: E402
    ProvisionerBootstrapError,
    get_existing_graph_token,
    get_state,
)

GRAPH_V1_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_BETA_BASE = "https://graph.microsoft.com/beta"
A365_OBSERVABILITY_APP_ID = "9b975845-388f-4429-889e-eab1ef63949c"
A365_OBSERVABILITY_SCOPE = "Agent365.Observability.OtelWrite"
REQUEST_TIMEOUT_SECONDS = 15
_READ_RETRY_DELAYS = (5, 10, 15, 20)
_GRANT_RETRY_DELAYS = (15, 30, 45)


class ResponseLike(Protocol):
    status_code: int
    text: str

    def json(self) -> dict[str, Any]: ...


RequestFn = Callable[..., ResponseLike]
SleepFn = Callable[[float], None]


class A365ObservabilityPermissionError(RuntimeError):
    """Raised when A365 Observability permissions cannot be made ready."""


@dataclass(frozen=True)
class ProvisioningIds:
    blueprint_app_id: str
    blueprint_object_id: str
    agent_identity_object_id: str
    agent_user_object_id: str


@dataclass(frozen=True)
class OAuthGrantSpec:
    label: str
    client_id: str
    consent_type: str
    resource_id: str
    scope: str
    principal_id: str | None = None

    def create_payload(self) -> dict[str, str]:
        payload = {
            "clientId": self.client_id,
            "consentType": self.consent_type,
            "resourceId": self.resource_id,
            "scope": self.scope,
            "startTime": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if self.principal_id is not None:
            payload["principalId"] = self.principal_id
        return payload


@dataclass(frozen=True)
class ResourceServicePrincipal:
    object_id: str
    app_id: str


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _response_json(response: ResponseLike, *, action: str) -> dict[str, Any]:
    try:
        data = response.json()
    except (TypeError, ValueError) as exc:
        raise A365ObservabilityPermissionError(f"{action} returned a non-JSON response") from exc
    if not isinstance(data, dict):
        raise A365ObservabilityPermissionError(f"{action} returned a malformed JSON response")
    return data


def _send(
    request: RequestFn,
    method: str,
    url: str,
    token: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> ResponseLike:
    kwargs: dict[str, Any] = {
        "headers": _headers(token),
        "timeout": REQUEST_TIMEOUT_SECONDS,
    }
    if json_body is not None:
        kwargs["json"] = json_body
    try:
        return request(method, url, **kwargs)
    except requests.RequestException as exc:
        raise A365ObservabilityPermissionError(
            f"Graph request failed while calling {method} {url}: {exc}"
        ) from exc


def _require_status(
    response: ResponseLike,
    *,
    expected: tuple[int, ...],
    action: str,
) -> None:
    if response.status_code not in expected:
        raise A365ObservabilityPermissionError(
            f"{action} failed ({response.status_code}): {response.text[:400]}"
        )


def _get_json(
    request: RequestFn,
    url: str,
    token: str,
    *,
    action: str,
) -> dict[str, Any]:
    response = _send(request, "GET", url, token)
    _require_status(response, expected=(200,), action=action)
    return _response_json(response, action=action)


def _validate_inputs(
    *,
    token: str,
    blueprint_app_id: str,
    blueprint_object_id: str,
    agent_identity_object_id: str,
    agent_user_object_id: str,
) -> tuple[str, ProvisioningIds]:
    values = {
        "token": token,
        "blueprint_app_id": blueprint_app_id,
        "blueprint_object_id": blueprint_object_id,
        "agent_identity_object_id": agent_identity_object_id,
        "agent_user_object_id": agent_user_object_id,
    }
    normalized: dict[str, str] = {}
    for field, value in values.items():
        normalized[field] = value.strip()
        if not normalized[field]:
            raise A365ObservabilityPermissionError(f"{field} is required")
    return normalized["token"], ProvisioningIds(
        blueprint_app_id=normalized["blueprint_app_id"],
        blueprint_object_id=normalized["blueprint_object_id"],
        agent_identity_object_id=normalized["agent_identity_object_id"],
        agent_user_object_id=normalized["agent_user_object_id"],
    )


def _validate_blueprint_application(
    ids: ProvisioningIds,
    token: str,
    *,
    request: RequestFn,
) -> None:
    url = (
        f"{GRAPH_V1_BASE}/applications/microsoft.graph.agentIdentityBlueprint/"
        f"{ids.blueprint_object_id}?$select=id,appId,displayName"
    )
    response = _send(request, "GET", url, token)
    if response.status_code == 404:
        raise A365ObservabilityPermissionError(
            f"Blueprint application {ids.blueprint_object_id} was not found"
        )
    _require_status(response, expected=(200,), action="read Blueprint application")
    data = _response_json(response, action="read Blueprint application")
    returned_object_id = str(data.get("id") or "")
    returned_app_id = str(data.get("appId") or "")
    if returned_object_id != ids.blueprint_object_id:
        raise A365ObservabilityPermissionError(
            "Blueprint application object ID mismatch: "
            f"expected {ids.blueprint_object_id}, got {returned_object_id or '<missing>'}"
        )
    if returned_app_id != ids.blueprint_app_id:
        raise A365ObservabilityPermissionError(
            "Blueprint application ID mismatch: "
            f"expected {ids.blueprint_app_id}, got {returned_app_id or '<missing>'}"
        )


def _validate_agent_chain(
    ids: ProvisioningIds,
    token: str,
    *,
    request: RequestFn,
) -> None:
    agent = _get_json(
        request,
        f"{GRAPH_BETA_BASE}/servicePrincipals/{ids.agent_identity_object_id}"
        "?$select=id,agentIdentityBlueprintId",
        token,
        action="read Agent Identity parent",
    )
    if (
        agent.get("id") != ids.agent_identity_object_id
        or agent.get("agentIdentityBlueprintId") != ids.blueprint_app_id
    ):
        raise A365ObservabilityPermissionError(
            "Agent Identity does not belong to the selected Blueprint"
        )
    user = _get_json(
        request,
        f"{GRAPH_BETA_BASE}/users/{ids.agent_user_object_id}?$select=id,identityParentId",
        token,
        action="read Agent User parent",
    )
    if (
        user.get("id") != ids.agent_user_object_id
        or user.get("identityParentId") != ids.agent_identity_object_id
    ):
        raise A365ObservabilityPermissionError(
            "Agent User does not belong to the selected Agent Identity"
        )


def _resolve_blueprint_principal(
    blueprint_app_id: str,
    token: str,
    *,
    request: RequestFn,
) -> str:
    url = (
        f"{GRAPH_V1_BASE}/servicePrincipals/"
        "microsoft.graph.agentIdentityBlueprintPrincipal"
        f"?$filter=appId eq '{_odata_escape(blueprint_app_id)}'"
        "&$select=id,appId"
    )
    data = _get_json(request, url, token, action="query Blueprint principal")
    values = data.get("value")
    if not isinstance(values, list):
        raise A365ObservabilityPermissionError(
            "Query Blueprint principal returned malformed results"
        )
    matches = [
        value
        for value in values
        if isinstance(value, dict) and value.get("appId") == blueprint_app_id
    ]
    if not matches:
        raise A365ObservabilityPermissionError(
            f"Blueprint principal for application {blueprint_app_id} was not found"
        )
    if len(matches) > 1:
        raise A365ObservabilityPermissionError(
            f"Multiple Blueprint principals found for application {blueprint_app_id}"
        )
    object_id = str(matches[0].get("id") or "")
    if not object_id:
        raise A365ObservabilityPermissionError("Blueprint principal has no object ID")
    return object_id


def _find_resource_service_principal(
    token: str,
    *,
    request: RequestFn,
) -> dict[str, Any] | None:
    url = (
        f"{GRAPH_V1_BASE}/servicePrincipals"
        f"?$filter=appId eq '{A365_OBSERVABILITY_APP_ID}'"
        "&$select=id,appId,displayName,oauth2PermissionScopes"
    )
    data = _get_json(
        request,
        url,
        token,
        action="query A365 Observability service principal",
    )
    values = data.get("value")
    if not isinstance(values, list):
        raise A365ObservabilityPermissionError(
            "A365 Observability service principal query returned malformed results"
        )
    matches = [
        value
        for value in values
        if isinstance(value, dict) and value.get("appId") == A365_OBSERVABILITY_APP_ID
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise A365ObservabilityPermissionError(
            "Multiple A365 Observability service principals were returned"
        )
    return matches[0]


def _validate_observability_resource(data: dict[str, Any]) -> ResourceServicePrincipal:
    object_id = str(data.get("id") or "")
    app_id = str(data.get("appId") or "")
    if not object_id:
        raise A365ObservabilityPermissionError(
            "A365 Observability service principal has no object ID"
        )
    if app_id != A365_OBSERVABILITY_APP_ID:
        raise A365ObservabilityPermissionError(
            "A365 Observability service principal app ID mismatch"
        )
    scopes = data.get("oauth2PermissionScopes")
    if not isinstance(scopes, list):
        raise A365ObservabilityPermissionError(
            "A365 Observability service principal did not advertise OAuth scopes"
        )
    advertised = any(
        isinstance(scope, dict)
        and scope.get("value") == A365_OBSERVABILITY_SCOPE
        and scope.get("isEnabled") is True
        for scope in scopes
    )
    if not advertised:
        raise A365ObservabilityPermissionError(
            "A365 Observability service principal does not advertise the enabled "
            f"{A365_OBSERVABILITY_SCOPE} delegated scope"
        )
    return ResourceServicePrincipal(object_id=object_id, app_id=app_id)


def _ensure_resource_service_principal(
    token: str,
    *,
    request: RequestFn,
    sleep: SleepFn,
) -> ResourceServicePrincipal:
    existing = _find_resource_service_principal(token, request=request)
    if existing is not None:
        return _validate_observability_resource(existing)

    response = _send(
        request,
        "POST",
        f"{GRAPH_V1_BASE}/servicePrincipals",
        token,
        json_body={"appId": A365_OBSERVABILITY_APP_ID},
    )
    _require_status(
        response,
        expected=(200, 201, 409),
        action="create A365 Observability service principal",
    )

    for delay in _READ_RETRY_DELAYS:
        sleep(delay)
        existing = _find_resource_service_principal(token, request=request)
        if existing is not None:
            return _validate_observability_resource(existing)
    raise A365ObservabilityPermissionError(
        "A365 Observability service principal creation did not propagate"
    )


def _find_oauth_grant(
    spec: OAuthGrantSpec,
    token: str,
    *,
    request: RequestFn,
) -> dict[str, Any] | None:
    url = (
        f"{GRAPH_V1_BASE}/oauth2PermissionGrants"
        f"?$filter=clientId eq '{_odata_escape(spec.client_id)}'"
        "&$select=id,clientId,consentType,principalId,resourceId,scope"
    )
    data = _get_json(request, url, token, action=f"query {spec.label} OAuth grant")
    values = data.get("value")
    if not isinstance(values, list):
        raise A365ObservabilityPermissionError(
            f"Query {spec.label} OAuth grant returned malformed results"
        )
    matches = []
    for value in values:
        if not isinstance(value, dict):
            continue
        if value.get("clientId") != spec.client_id:
            continue
        if value.get("resourceId") != spec.resource_id:
            continue
        if value.get("consentType") != spec.consent_type:
            continue
        if spec.principal_id is not None:
            if value.get("principalId") != spec.principal_id:
                continue
        elif value.get("principalId") not in (None, ""):
            continue
        matches.append(value)
    if not matches:
        return None
    if len(matches) > 1:
        raise A365ObservabilityPermissionError(f"Multiple {spec.label} OAuth grants were returned")
    return matches[0]


def _patch_grant_if_needed(
    grant: dict[str, Any],
    spec: OAuthGrantSpec,
    token: str,
    *,
    request: RequestFn,
) -> None:
    existing_scopes = set(str(grant.get("scope") or "").split())
    if spec.scope in existing_scopes:
        return
    grant_id = str(grant.get("id") or "")
    if not grant_id:
        raise A365ObservabilityPermissionError(f"Existing {spec.label} OAuth grant has no grant ID")
    merged_scope = " ".join(sorted(existing_scopes | {spec.scope}))
    response = _send(
        request,
        "PATCH",
        f"{GRAPH_V1_BASE}/oauth2PermissionGrants/{grant_id}",
        token,
        json_body={"scope": merged_scope},
    )
    _require_status(
        response,
        expected=(200, 204),
        action=f"patch {spec.label} OAuth grant",
    )


def _resolve_grant_conflict(
    spec: OAuthGrantSpec,
    token: str,
    *,
    request: RequestFn,
    sleep: SleepFn,
) -> None:
    for delay in _READ_RETRY_DELAYS:
        sleep(delay)
        existing = _find_oauth_grant(spec, token, request=request)
        if existing is not None:
            _patch_grant_if_needed(existing, spec, token, request=request)
            return
    raise A365ObservabilityPermissionError(
        f"Create {spec.label} OAuth grant conflicted, but no grant could be read back"
    )


def _is_propagation_failure(response: ResponseLike) -> bool:
    text = response.text.lower()
    resource_not_propagated = (
        response.status_code == 400 and "specified resourceid was not found." in text
    )
    return (
        response.status_code == 404
        or resource_not_propagated
        or any(
            marker in text
            for marker in (
                "principal was not found",
                "principal does not exist",
                "does not exist in the directory",
            )
        )
    )


def _ensure_oauth_grant(
    spec: OAuthGrantSpec,
    token: str,
    *,
    request: RequestFn,
    sleep: SleepFn,
) -> None:
    existing = _find_oauth_grant(spec, token, request=request)
    if existing is not None:
        _patch_grant_if_needed(existing, spec, token, request=request)
        return

    payload = spec.create_payload()
    attempts = len(_GRANT_RETRY_DELAYS) + 1
    for attempt in range(attempts):
        response = _send(
            request,
            "POST",
            f"{GRAPH_V1_BASE}/oauth2PermissionGrants",
            token,
            json_body=payload,
        )
        if response.status_code in (200, 201):
            return
        if response.status_code == 409:
            _resolve_grant_conflict(
                spec,
                token,
                request=request,
                sleep=sleep,
            )
            return
        if _is_propagation_failure(response) and attempt < len(_GRANT_RETRY_DELAYS):
            sleep(_GRANT_RETRY_DELAYS[attempt])
            continue
        _require_status(
            response,
            expected=(200, 201),
            action=f"create {spec.label} OAuth grant",
        )
    raise A365ObservabilityPermissionError(
        f"Create {spec.label} OAuth grant exhausted propagation retries"
    )


def _inheritance_payload() -> dict[str, Any]:
    return {
        "resourceAppId": A365_OBSERVABILITY_APP_ID,
        "inheritableScopes": {
            "@odata.type": "#microsoft.graph.allAllowedScopes",
            "kind": "allAllowed",
        },
        "inheritableRoles": {
            "@odata.type": "#microsoft.graph.noRoles",
            "kind": "none",
        },
    }


def _inheritance_is_correct(data: dict[str, Any]) -> bool:
    expected = _inheritance_payload()
    return all(data.get(key) == value for key, value in expected.items())


def _read_inheritance(
    keyed_url: str,
    token: str,
    *,
    request: RequestFn,
) -> tuple[int, dict[str, Any]]:
    response = _send(request, "GET", keyed_url, token)
    if response.status_code == 404:
        return 404, {}
    _require_status(
        response,
        expected=(200,),
        action="read A365 Observability inheritable permissions",
    )
    return 200, _response_json(
        response,
        action="read A365 Observability inheritable permissions",
    )


def _ensure_inheritable_permissions(
    blueprint_object_id: str,
    token: str,
    *,
    request: RequestFn,
    sleep: SleepFn,
) -> None:
    collection_url = (
        f"{GRAPH_V1_BASE}/applications/microsoft.graph.agentIdentityBlueprint/"
        f"{blueprint_object_id}/inheritablePermissions"
    )
    keyed_url = f"{collection_url}/{A365_OBSERVABILITY_APP_ID}"
    status, current = _read_inheritance(keyed_url, token, request=request)
    payload = _inheritance_payload()

    if status == 404:
        response = _send(
            request,
            "POST",
            collection_url,
            token,
            json_body=payload,
        )
        if response.status_code in (200, 201, 204):
            return
        if response.status_code != 409:
            _require_status(
                response,
                expected=(200, 201, 204),
                action="create A365 Observability inheritable permissions",
            )
        for delay in _READ_RETRY_DELAYS:
            sleep(delay)
            status, current = _read_inheritance(keyed_url, token, request=request)
            if status == 200:
                break
        else:
            raise A365ObservabilityPermissionError(
                "A365 Observability inheritable permissions creation conflicted, "
                "but the entry could not be read back"
            )

    if _inheritance_is_correct(current):
        return
    response = _send(
        request,
        "PATCH",
        keyed_url,
        token,
        json_body=payload,
    )
    _require_status(
        response,
        expected=(200, 204),
        action="patch A365 Observability inheritable permissions",
    )


def ensure_a365_observability_permissions(
    *,
    token: str,
    blueprint_app_id: str,
    blueprint_object_id: str,
    agent_identity_object_id: str,
    agent_user_object_id: str,
    request: RequestFn = requests.request,
    sleep: SleepFn = time.sleep,
) -> None:
    """Ensure all delegated A365 Observability permission paths are present."""
    token, ids = _validate_inputs(
        token=token,
        blueprint_app_id=blueprint_app_id,
        blueprint_object_id=blueprint_object_id,
        agent_identity_object_id=agent_identity_object_id,
        agent_user_object_id=agent_user_object_id,
    )

    _validate_blueprint_application(ids, token, request=request)
    _validate_agent_chain(ids, token, request=request)
    blueprint_principal_id = _resolve_blueprint_principal(
        ids.blueprint_app_id,
        token,
        request=request,
    )
    resource = _ensure_resource_service_principal(
        token,
        request=request,
        sleep=sleep,
    )

    _ensure_oauth_grant(
        OAuthGrantSpec(
            label="Blueprint-principal",
            client_id=blueprint_principal_id,
            consent_type="AllPrincipals",
            resource_id=resource.object_id,
            scope=A365_OBSERVABILITY_SCOPE,
        ),
        token,
        request=request,
        sleep=sleep,
    )
    _ensure_inheritable_permissions(
        ids.blueprint_object_id,
        token,
        request=request,
        sleep=sleep,
    )
    _ensure_oauth_grant(
        OAuthGrantSpec(
            label="Agent-Identity-to-Agent-User principal",
            client_id=ids.agent_identity_object_id,
            consent_type="Principal",
            principal_id=ids.agent_user_object_id,
            resource_id=resource.object_id,
            scope=A365_OBSERVABILITY_SCOPE,
        ),
        token,
        request=request,
        sleep=sleep,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ensure delegated Microsoft Agent 365 Observability permissions."
    )
    parser.add_argument("--blueprint-app-id", default="")
    parser.add_argument("--blueprint-object-id", default="")
    parser.add_argument("--agent-object-id", default="")
    parser.add_argument("--agent-user-id", default="")
    return parser.parse_args(argv)


def _argument_or_state(argument: str, state_key: str) -> str:
    return argument.strip() or (get_state(state_key) or "").strip()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    ids = ProvisioningIds(
        blueprint_app_id=_argument_or_state(args.blueprint_app_id, "BLUEPRINT_APP_ID"),
        blueprint_object_id=_argument_or_state(
            args.blueprint_object_id,
            "BLUEPRINT_OBJECT_ID",
        ),
        agent_identity_object_id=_argument_or_state(
            args.agent_object_id,
            "AGENT_OBJECT_ID",
        ),
        agent_user_object_id=_argument_or_state(args.agent_user_id, "AGENT_USER_ID"),
    )
    missing = [
        field
        for field, value in (
            ("BLUEPRINT_APP_ID", ids.blueprint_app_id),
            ("BLUEPRINT_OBJECT_ID", ids.blueprint_object_id),
            ("AGENT_OBJECT_ID", ids.agent_identity_object_id),
            ("AGENT_USER_ID", ids.agent_user_object_id),
        )
        if not value
    ]
    if missing:
        print(
            "ERROR: Missing required ID(s): " + ", ".join(missing),
            file=sys.stderr,
        )
        return 2

    print("Ensuring Microsoft Agent 365 Observability delegated permissions...")
    print(f"  Resource app: {A365_OBSERVABILITY_APP_ID}")
    print(f"  Scope:        {A365_OBSERVABILITY_SCOPE}")
    try:
        token = get_existing_graph_token()
        ensure_a365_observability_permissions(
            token=token,
            blueprint_app_id=ids.blueprint_app_id,
            blueprint_object_id=ids.blueprint_object_id,
            agent_identity_object_id=ids.agent_identity_object_id,
            agent_user_object_id=ids.agent_user_object_id,
        )
    except (A365ObservabilityPermissionError, ProvisionerBootstrapError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("A365 Observability delegated permissions are ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
