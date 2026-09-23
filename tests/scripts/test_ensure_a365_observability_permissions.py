"""Tests for scripts/ensure_a365_observability_permissions.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ensure_a365_observability_permissions.py"

BLUEPRINT_APP_ID = "blueprint-app-id"
BLUEPRINT_OBJECT_ID = "blueprint-object-id"
BLUEPRINT_SP_ID = "blueprint-principal-id"
AGENT_IDENTITY_OBJECT_ID = "agent-identity-object-id"
AGENT_USER_OBJECT_ID = "agent-user-object-id"
RESOURCE_SP_ID = "observability-resource-sp-id"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "ensure_a365_observability_permissions",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_observability_uses_shared_graph_filter_escaping():
    from entrabot.graph_helpers import odata_escape

    module = load_module()
    assert module._odata_escape is odata_escape
    assert module._odata_escape("name'with'quotes") == "name''with''quotes"


class FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.text = str(self._body)

    def json(self) -> dict[str, Any]:
        return self._body


def _scope(module, *, enabled: bool = True, value: str | None = None) -> dict[str, Any]:
    return {
        "id": "observability-scope-id",
        "value": value or module.A365_OBSERVABILITY_SCOPE,
        "isEnabled": enabled,
    }


def _correct_inheritance(module) -> dict[str, Any]:
    return {
        "resourceAppId": module.A365_OBSERVABILITY_APP_ID,
        "inheritableScopes": {
            "@odata.type": "#microsoft.graph.allAllowedScopes",
            "kind": "allAllowed",
        },
        "inheritableRoles": {
            "@odata.type": "#microsoft.graph.noRoles",
            "kind": "none",
        },
    }


class GraphState:
    def __init__(self, module) -> None:
        self.module = module
        self.agent_parent = BLUEPRINT_APP_ID
        self.user_parent = AGENT_IDENTITY_OBJECT_ID
        self.blueprint: dict[str, Any] | None = {
            "id": BLUEPRINT_OBJECT_ID,
            "appId": BLUEPRINT_APP_ID,
        }
        self.blueprint_principal: dict[str, Any] | None = {
            "id": BLUEPRINT_SP_ID,
            "appId": BLUEPRINT_APP_ID,
        }
        self.resource: dict[str, Any] | None = {
            "id": RESOURCE_SP_ID,
            "appId": module.A365_OBSERVABILITY_APP_ID,
            "oauth2PermissionScopes": [_scope(module)],
        }
        self.blueprint_grant: dict[str, Any] | None = {
            "id": "blueprint-grant-id",
            "clientId": BLUEPRINT_SP_ID,
            "consentType": "AllPrincipals",
            "resourceId": RESOURCE_SP_ID,
            "scope": module.A365_OBSERVABILITY_SCOPE,
        }
        self.principal_grant: dict[str, Any] | None = {
            "id": "principal-grant-id",
            "clientId": AGENT_IDENTITY_OBJECT_ID,
            "consentType": "Principal",
            "principalId": AGENT_USER_OBJECT_ID,
            "resourceId": RESOURCE_SP_ID,
            "scope": module.A365_OBSERVABILITY_SCOPE,
        }
        self.inheritance: dict[str, Any] | None = _correct_inheritance(module)
        self.inheritance_status = 200
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        assert kwargs["timeout"] == self.module.REQUEST_TIMEOUT_SECONDS
        assert kwargs["headers"]["Authorization"] == "Bearer token"
        body = kwargs.get("json")
        self.calls.append((method, url, body))

        if method == "GET" and f"/beta/servicePrincipals/{AGENT_IDENTITY_OBJECT_ID}?" in url:
            return FakeResponse(200, {
                "id": AGENT_IDENTITY_OBJECT_ID,
                "agentIdentityBlueprintId": self.agent_parent,
            })
        if method == "GET" and f"/beta/users/{AGENT_USER_OBJECT_ID}?" in url:
            return FakeResponse(200, {
                "id": AGENT_USER_OBJECT_ID,
                "identityParentId": self.user_parent,
            })

        if (
            method == "GET"
            and (
                f"/applications/microsoft.graph.agentIdentityBlueprint/{BLUEPRINT_OBJECT_ID}" in url
            )
            and "/inheritablePermissions/" not in url
        ):
            if self.blueprint is None:
                return FakeResponse(404, {"error": {"message": "not found"}})
            return FakeResponse(200, self.blueprint)

        if method == "GET" and (
            "/servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal?" in url
        ):
            values = [] if self.blueprint_principal is None else [self.blueprint_principal]
            return FakeResponse(200, {"value": values})

        if method == "GET" and "/servicePrincipals?" in url:
            values = [] if self.resource is None else [self.resource]
            return FakeResponse(200, {"value": values})

        if method == "GET" and "/oauth2PermissionGrants?" in url:
            is_principal = (
                f"clientId eq '{AGENT_IDENTITY_OBJECT_ID}'" in url
            )
            grant = self.principal_grant if is_principal else self.blueprint_grant
            return FakeResponse(200, {"value": [] if grant is None else [grant]})

        if method == "GET" and "/inheritablePermissions/" in url:
            return FakeResponse(self.inheritance_status, self.inheritance)

        raise AssertionError(f"unexpected request: {method} {url} {body}")

    @property
    def mutations(self) -> list[tuple[str, str, dict[str, Any] | None]]:
        return [call for call in self.calls if call[0] in {"POST", "PATCH"}]


def _ensure(module, request, *, sleep=lambda _seconds: None) -> None:
    module.ensure_a365_observability_permissions(
        token="token",
        blueprint_app_id=BLUEPRINT_APP_ID,
        blueprint_object_id=BLUEPRINT_OBJECT_ID,
        agent_identity_object_id=AGENT_IDENTITY_OBJECT_ID,
        agent_user_object_id=AGENT_USER_OBJECT_ID,
        request=request,
        sleep=sleep,
    )


def test_find_blueprint_grant_filters_only_by_client_and_matches_exactly() -> None:
    module = load_module()
    client_id = "blueprint'principal-id"
    spec = module.OAuthGrantSpec(
        label="Blueprint-principal",
        client_id=client_id,
        consent_type="AllPrincipals",
        resource_id=RESOURCE_SP_ID,
        scope=module.A365_OBSERVABILITY_SCOPE,
    )
    expected_url = (
        f"{module.GRAPH_V1_BASE}/oauth2PermissionGrants"
        "?$filter=clientId eq 'blueprint''principal-id'"
        "&$select=id,clientId,consentType,principalId,resourceId,scope"
    )
    correct = {
        "id": "correct-blueprint-grant",
        "clientId": client_id,
        "consentType": "AllPrincipals",
        "resourceId": RESOURCE_SP_ID,
        "scope": module.A365_OBSERVABILITY_SCOPE,
    }

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        assert method == "GET"
        assert url == expected_url
        assert kwargs["timeout"] == module.REQUEST_TIMEOUT_SECONDS
        return FakeResponse(
            200,
            {
                "value": [
                    {**correct, "id": "wrong-client", "clientId": "other-client"},
                    {**correct, "id": "wrong-resource", "resourceId": "other-resource"},
                    {**correct, "id": "wrong-consent", "consentType": "Principal"},
                    {**correct, "id": "wrong-principal", "principalId": "some-user"},
                    correct,
                ]
            },
        )

    assert module._find_oauth_grant(spec, "token", request=request) == correct


def test_find_principal_grant_filters_only_by_client_and_matches_exactly() -> None:
    module = load_module()
    client_id = "agent'identity-id"
    spec = module.OAuthGrantSpec(
        label="Agent-Identity-to-Agent-User principal",
        client_id=client_id,
        consent_type="Principal",
        principal_id=AGENT_USER_OBJECT_ID,
        resource_id=RESOURCE_SP_ID,
        scope=module.A365_OBSERVABILITY_SCOPE,
    )
    expected_url = (
        f"{module.GRAPH_V1_BASE}/oauth2PermissionGrants"
        "?$filter=clientId eq 'agent''identity-id'"
        "&$select=id,clientId,consentType,principalId,resourceId,scope"
    )
    correct = {
        "id": "correct-principal-grant",
        "clientId": client_id,
        "consentType": "Principal",
        "principalId": AGENT_USER_OBJECT_ID,
        "resourceId": RESOURCE_SP_ID,
        "scope": module.A365_OBSERVABILITY_SCOPE,
    }

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        assert method == "GET"
        assert url == expected_url
        assert kwargs["timeout"] == module.REQUEST_TIMEOUT_SECONDS
        return FakeResponse(
            200,
            {
                "value": [
                    {**correct, "id": "wrong-client", "clientId": "other-client"},
                    {**correct, "id": "wrong-resource", "resourceId": "other-resource"},
                    {**correct, "id": "wrong-consent", "consentType": "AllPrincipals"},
                    {**correct, "id": "wrong-principal", "principalId": "other-user"},
                    correct,
                ]
            },
        )

    assert module._find_oauth_grant(spec, "token", request=request) == correct


def test_all_permissions_already_correct_make_no_mutations() -> None:
    module = load_module()
    graph = GraphState(module)

    _ensure(module, graph.request)

    assert graph.mutations == []


@pytest.mark.parametrize("relationship", ["agent_parent", "user_parent"])
def test_wrong_identity_chain_is_rejected_before_grants(relationship: str) -> None:
    module = load_module()
    graph = GraphState(module)
    setattr(graph, relationship, "different-parent")

    with pytest.raises(module.A365ObservabilityPermissionError, match="does not belong"):
        _ensure(module, graph.request)

    assert graph.mutations == []


def test_creates_missing_resource_sp_and_retries_until_propagated() -> None:
    module = load_module()
    graph = GraphState(module)
    graph.resource = None
    resource_reads = 0
    sleeps: list[float] = []

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        nonlocal resource_reads
        if method == "GET" and "/servicePrincipals?" in url:
            assert kwargs["timeout"] == module.REQUEST_TIMEOUT_SECONDS
            resource_reads += 1
            if resource_reads < 3:
                return FakeResponse(200, {"value": []})
            graph.resource = {
                "id": RESOURCE_SP_ID,
                "appId": module.A365_OBSERVABILITY_APP_ID,
                "oauth2PermissionScopes": [_scope(module)],
            }
        if method == "POST" and url.endswith("/servicePrincipals"):
            assert kwargs["json"] == {"appId": module.A365_OBSERVABILITY_APP_ID}
            assert kwargs["timeout"] == module.REQUEST_TIMEOUT_SECONDS
            graph.calls.append((method, url, kwargs["json"]))
            return FakeResponse(201, {"id": RESOURCE_SP_ID})
        return graph.request(method, url, **kwargs)

    _ensure(module, request, sleep=sleeps.append)

    assert resource_reads == 3
    assert sleeps == [5, 10]


@pytest.mark.parametrize(
    ("enabled", "value"),
    [
        (False, "Agent365.Observability.OtelWrite"),
        (True, "Some.Other.Scope"),
    ],
)
def test_missing_or_disabled_scope_advertisement_fails_explicitly(
    enabled: bool,
    value: str,
) -> None:
    module = load_module()
    graph = GraphState(module)
    assert graph.resource is not None
    graph.resource["oauth2PermissionScopes"] = [_scope(module, enabled=enabled, value=value)]

    with pytest.raises(
        module.A365ObservabilityPermissionError,
        match="enabled.*Agent365\\.Observability\\.OtelWrite",
    ):
        _ensure(module, graph.request)

    assert graph.mutations == []


def test_creates_exact_blueprint_all_principals_grant() -> None:
    module = load_module()
    graph = GraphState(module)
    graph.blueprint_grant = None

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        if method == "POST" and url.endswith("/oauth2PermissionGrants"):
            body = kwargs["json"]
            graph.calls.append((method, url, body))
            assert body == {
                "clientId": BLUEPRINT_SP_ID,
                "consentType": "AllPrincipals",
                "resourceId": RESOURCE_SP_ID,
                "scope": module.A365_OBSERVABILITY_SCOPE,
                "startTime": body["startTime"],
            }
            assert body["startTime"].endswith("Z")
            graph.blueprint_grant = {"id": "new-blueprint-grant", **body}
            return FakeResponse(201, graph.blueprint_grant)
        return graph.request(method, url, **kwargs)

    _ensure(module, request)


def test_blueprint_grant_retries_resource_id_propagation_failure() -> None:
    module = load_module()
    graph = GraphState(module)
    graph.blueprint_grant = None
    blueprint_posts = 0
    sleeps: list[float] = []

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        nonlocal blueprint_posts
        if method == "POST" and url.endswith("/oauth2PermissionGrants"):
            blueprint_posts += 1
            if blueprint_posts == 1:
                return FakeResponse(
                    400,
                    {
                        "error": {
                            "code": "Request_BadRequest",
                            "message": "Specified resourceId was not found.",
                        }
                    },
                )
            return FakeResponse(201, {"id": "new-blueprint-grant"})
        return graph.request(method, url, **kwargs)

    _ensure(module, request, sleep=sleeps.append)

    assert blueprint_posts == 2
    assert sleeps[0] == 15


def test_creates_exact_delegated_only_inheritable_permissions() -> None:
    module = load_module()
    graph = GraphState(module)
    graph.inheritance = None
    graph.inheritance_status = 404

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        if method == "POST" and url.endswith("/inheritablePermissions"):
            assert url == (
                f"{module.GRAPH_V1_BASE}/applications/"
                "microsoft.graph.agentIdentityBlueprint/"
                f"{BLUEPRINT_OBJECT_ID}/inheritablePermissions"
            )
            graph.calls.append((method, url, kwargs["json"]))
            assert kwargs["json"] == _correct_inheritance(module)
            return FakeResponse(201, kwargs["json"])
        return graph.request(method, url, **kwargs)

    _ensure(module, request)


def test_patches_incorrect_inheritance_to_scopes_only() -> None:
    module = load_module()
    graph = GraphState(module)
    graph.inheritance = {
        "resourceAppId": module.A365_OBSERVABILITY_APP_ID,
        "inheritableScopes": {
            "@odata.type": "#microsoft.graph.noScopes",
            "kind": "none",
        },
        "inheritableRoles": {
            "@odata.type": "#microsoft.graph.allAllowedRoles",
            "kind": "allAllowed",
        },
    }

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        if method == "PATCH" and "/inheritablePermissions/" in url:
            assert url == (
                f"{module.GRAPH_V1_BASE}/applications/"
                "microsoft.graph.agentIdentityBlueprint/"
                f"{BLUEPRINT_OBJECT_ID}/inheritablePermissions/"
                f"{module.A365_OBSERVABILITY_APP_ID}"
            )
            graph.calls.append((method, url, kwargs["json"]))
            assert kwargs["json"] == _correct_inheritance(module)
            return FakeResponse(204)
        return graph.request(method, url, **kwargs)

    _ensure(module, request)


def test_creates_exact_agent_user_principal_grant() -> None:
    module = load_module()
    graph = GraphState(module)
    graph.principal_grant = None

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        if method == "POST" and url.endswith("/oauth2PermissionGrants"):
            body = kwargs["json"]
            graph.calls.append((method, url, body))
            assert body == {
                "clientId": AGENT_IDENTITY_OBJECT_ID,
                "consentType": "Principal",
                "principalId": AGENT_USER_OBJECT_ID,
                "resourceId": RESOURCE_SP_ID,
                "scope": module.A365_OBSERVABILITY_SCOPE,
                "startTime": body["startTime"],
            }
            assert body["startTime"].endswith("Z")
            graph.principal_grant = {"id": "new-principal-grant", **body}
            return FakeResponse(201, graph.principal_grant)
        return graph.request(method, url, **kwargs)

    _ensure(module, request)


def test_merges_existing_blueprint_and_principal_grant_scopes() -> None:
    module = load_module()
    graph = GraphState(module)
    assert graph.blueprint_grant is not None
    assert graph.principal_grant is not None
    graph.blueprint_grant["scope"] = "Existing.Blueprint.Scope"
    graph.principal_grant["scope"] = "Existing.Principal.Scope"
    patches: dict[str, dict[str, Any]] = {}

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        if method == "PATCH" and "/oauth2PermissionGrants/" in url:
            grant_id = url.rsplit("/", 1)[-1]
            patches[grant_id] = kwargs["json"]
            return FakeResponse(204)
        return graph.request(method, url, **kwargs)

    _ensure(module, request)

    assert patches == {
        "blueprint-grant-id": {
            "scope": "Agent365.Observability.OtelWrite Existing.Blueprint.Scope"
        },
        "principal-grant-id": {
            "scope": "Agent365.Observability.OtelWrite Existing.Principal.Scope"
        },
    }


@pytest.mark.parametrize(
    ("blueprint", "match"),
    [
        (None, "Blueprint application.*not found"),
        (
            {"id": BLUEPRINT_OBJECT_ID, "appId": "different-app-id"},
            "Blueprint application ID mismatch",
        ),
    ],
)
def test_missing_or_mismatched_blueprint_identity_fails(
    blueprint: dict[str, Any] | None,
    match: str,
) -> None:
    module = load_module()
    graph = GraphState(module)
    graph.blueprint = blueprint

    with pytest.raises(module.A365ObservabilityPermissionError, match=match):
        _ensure(module, graph.request)

    assert graph.mutations == []


def test_missing_blueprint_principal_fails() -> None:
    module = load_module()
    graph = GraphState(module)
    graph.blueprint_principal = None

    with pytest.raises(
        module.A365ObservabilityPermissionError,
        match="Blueprint principal.*not found",
    ):
        _ensure(module, graph.request)


@pytest.mark.parametrize(
    "field",
    [
        "token",
        "blueprint_app_id",
        "blueprint_object_id",
        "agent_identity_object_id",
        "agent_user_object_id",
    ],
)
def test_missing_required_inputs_fail_before_requests(field: str) -> None:
    module = load_module()
    values = {
        "token": "token",
        "blueprint_app_id": BLUEPRINT_APP_ID,
        "blueprint_object_id": BLUEPRINT_OBJECT_ID,
        "agent_identity_object_id": AGENT_IDENTITY_OBJECT_ID,
        "agent_user_object_id": AGENT_USER_OBJECT_ID,
    }
    values[field] = "  "

    def unexpected_request(*_args: Any, **_kwargs: Any) -> FakeResponse:
        raise AssertionError("request must not be called")

    with pytest.raises(module.A365ObservabilityPermissionError, match=field):
        module.ensure_a365_observability_permissions(
            **values,
            request=unexpected_request,
            sleep=lambda _seconds: None,
        )


def test_grant_create_conflict_requeries_and_merges() -> None:
    module = load_module()
    graph = GraphState(module)
    graph.principal_grant = None
    principal_queries = 0
    sleeps: list[float] = []
    patches: list[dict[str, Any]] = []

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        nonlocal principal_queries
        if (
            method == "GET"
            and "/oauth2PermissionGrants?" in url
            and f"clientId eq '{AGENT_IDENTITY_OBJECT_ID}'" in url
        ):
            principal_queries += 1
            if principal_queries == 1:
                return FakeResponse(200, {"value": []})
            return FakeResponse(
                200,
                {
                    "value": [
                        {
                            "id": "conflicting-grant-id",
                            "clientId": AGENT_IDENTITY_OBJECT_ID,
                            "consentType": "Principal",
                            "principalId": AGENT_USER_OBJECT_ID,
                            "resourceId": RESOURCE_SP_ID,
                            "scope": "Existing.Scope",
                        }
                    ]
                },
            )
        if method == "POST" and url.endswith("/oauth2PermissionGrants"):
            return FakeResponse(
                409,
                {"error": {"message": "Permission entry already exists"}},
            )
        if method == "PATCH" and url.endswith("/conflicting-grant-id"):
            patches.append(kwargs["json"])
            return FakeResponse(204)
        return graph.request(method, url, **kwargs)

    _ensure(module, request, sleep=sleeps.append)

    assert principal_queries == 2
    assert sleeps == [5]
    assert patches == [{"scope": "Agent365.Observability.OtelWrite Existing.Scope"}]


def test_cli_defaults_ids_from_state(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    module = load_module()
    state = {
        "BLUEPRINT_APP_ID": BLUEPRINT_APP_ID,
        "BLUEPRINT_OBJECT_ID": BLUEPRINT_OBJECT_ID,
        "AGENT_OBJECT_ID": AGENT_IDENTITY_OBJECT_ID,
        "AGENT_USER_ID": AGENT_USER_OBJECT_ID,
    }
    captured: dict[str, Any] = {}

    monkeypatch.setattr(module, "get_state", state.get)
    monkeypatch.setattr(module, "get_existing_graph_token", lambda: "token")

    def ensure(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(module, "ensure_a365_observability_permissions", ensure)

    assert module.main([]) == 0
    assert captured["token"] == "token"
    assert captured["blueprint_app_id"] == BLUEPRINT_APP_ID
    assert captured["blueprint_object_id"] == BLUEPRINT_OBJECT_ID
    assert captured["agent_identity_object_id"] == AGENT_IDENTITY_OBJECT_ID
    assert captured["agent_user_object_id"] == AGENT_USER_OBJECT_ID
    assert "ready" in capsys.readouterr().out.lower()


def test_cli_returns_nonzero_without_success_output_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    module = load_module()
    state = {
        "BLUEPRINT_APP_ID": BLUEPRINT_APP_ID,
        "BLUEPRINT_OBJECT_ID": BLUEPRINT_OBJECT_ID,
        "AGENT_OBJECT_ID": AGENT_IDENTITY_OBJECT_ID,
        "AGENT_USER_ID": AGENT_USER_OBJECT_ID,
    }

    monkeypatch.setattr(module, "get_state", state.get)
    monkeypatch.setattr(module, "get_existing_graph_token", lambda: "token")

    def fail(**_kwargs: Any) -> None:
        raise module.A365ObservabilityPermissionError("Graph rejected the grant")

    monkeypatch.setattr(module, "ensure_a365_observability_permissions", fail)

    assert module.main([]) == 1
    output = capsys.readouterr()
    assert "ready" not in output.out.lower()
    assert "Graph rejected the grant" in output.err


def test_cli_missing_id_returns_usage_error_before_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    monkeypatch.setattr(module, "get_state", lambda _key: None)

    def unexpected_token() -> str:
        raise AssertionError("token must not be acquired")

    monkeypatch.setattr(module, "get_existing_graph_token", unexpected_token)

    assert module.main([]) == 2


def test_principal_grant_retries_propagation_failure() -> None:
    module = load_module()
    graph = GraphState(module)
    graph.principal_grant = None
    principal_posts = 0
    sleeps: list[float] = []

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        nonlocal principal_posts
        if method == "POST" and url.endswith("/oauth2PermissionGrants"):
            principal_posts += 1
            if principal_posts == 1:
                return FakeResponse(404, {"error": {"message": "Principal was not found"}})
            return FakeResponse(201, {"id": "new-principal-grant"})
        return graph.request(method, url, **kwargs)

    _ensure(module, request, sleep=sleeps.append)

    assert principal_posts == 2
    assert sleeps == [15]
