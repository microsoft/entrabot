from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ensure_a365_work_iq_permissions.py"


def load_module():
    spec = importlib.util.spec_from_file_location("ensure_a365_work_iq_permissions", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.text = str(self._body)

    def json(self) -> dict[str, Any]:
        return self._body


def test_creates_missing_resource_sps_and_blueprint_grants() -> None:
    module = load_module()
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    service_principal_gets: dict[str, int] = {}

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        body = kwargs.get("json")
        calls.append((method, url, body))

        if method == "GET" and "servicePrincipals?$filter=appId eq 'blueprint-app'" in url:
            return FakeResponse(200, {"value": [{"id": "blueprint-sp"}]})
        if method == "GET" and "servicePrincipals?$filter=appId eq 'ea9ffc3e" in url:
            service_principal_gets["agent-tools"] = (
                service_principal_gets.get("agent-tools", 0) + 1
            )
            if service_principal_gets["agent-tools"] == 1:
                return FakeResponse(200, {"value": []})
            return FakeResponse(
                200,
                {"value": [{"id": f"sp-{module.A365_AGENT_TOOLS_APP_ID[:4]}"}]},
            )
        if method == "GET" and "servicePrincipals?$filter=appId eq 'c2d0c2b6" in url:
            return FakeResponse(200, {"value": []})
        if method == "POST" and url.endswith("/servicePrincipals"):
            assert body is not None
            return FakeResponse(201, {"id": f"sp-{body['appId'][:4]}"})
        if method == "GET" and "oauth2PermissionGrants" in url:
            return FakeResponse(200, {"value": []})
        if method == "POST" and url.endswith("/oauth2PermissionGrants"):
            return FakeResponse(201, {"id": "grant-id"})
        raise AssertionError(f"unexpected request: {method} {url} {body}")

    module.ensure_a365_work_iq_permissions(
        token="token",
        blueprint_app_id="blueprint-app",
        request=request,
        sleep=lambda _seconds: None,
    )

    sp_posts = [
        body
        for method, url, body in calls
        if method == "POST" and url.endswith("/servicePrincipals")
    ]
    assert sp_posts == [
        {"appId": module.A365_AGENT_TOOLS_APP_ID},
        {"appId": module.A365_WORD_MCP_APP_ID},
    ]

    grant_posts = [
        body
        for method, url, body in calls
        if method == "POST" and url.endswith("/oauth2PermissionGrants")
    ]
    assert grant_posts == [
        {
            "clientId": "blueprint-sp",
            "consentType": "AllPrincipals",
            "resourceId": f"sp-{module.A365_AGENT_TOOLS_APP_ID[:4]}",
            "scope": (
                "McpServers.Excel.All McpServers.OneDriveSharepoint.All "
                "McpServersMetadata.Read.All"
            ),
        },
        {
            "clientId": "blueprint-sp",
            "consentType": "AllPrincipals",
            "resourceId": f"sp-{module.A365_WORD_MCP_APP_ID[:4]}",
            "scope": "Tools.ListInvoke.All",
        },
    ]


def test_patches_existing_grant_when_scope_is_missing() -> None:
    module = load_module()
    patch_bodies: list[dict[str, str]] = []

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        body = kwargs.get("json")
        if method == "GET" and "servicePrincipals?$filter=appId eq 'blueprint-app'" in url:
            return FakeResponse(200, {"value": [{"id": "blueprint-sp"}]})
        if method == "GET" and "servicePrincipals?$filter=appId eq 'ea9ffc3e" in url:
            return FakeResponse(200, {"value": [{"id": "agent-tools-sp"}]})
        if method == "GET" and "servicePrincipals?$filter=appId eq 'c2d0c2b6" in url:
            return FakeResponse(200, {"value": [{"id": "word-sp"}]})
        if method == "GET" and "resourceId eq 'agent-tools-sp'" in url:
            return FakeResponse(200, {"value": [{"id": "metadata-grant", "scope": "Old.Scope"}]})
        if method == "GET" and "resourceId eq 'word-sp'" in url:
            return FakeResponse(
                200,
                {"value": [{"id": "word-grant", "scope": "Tools.ListInvoke.All"}]},
            )
        if method == "PATCH" and url.endswith("/oauth2PermissionGrants/metadata-grant"):
            assert body is not None
            patch_bodies.append(body)
            return FakeResponse(204)
        raise AssertionError(f"unexpected request: {method} {url} {body}")

    module.ensure_a365_work_iq_permissions(
        token="token",
        blueprint_app_id="blueprint-app",
        request=request,
        sleep=lambda _seconds: None,
    )

    assert patch_bodies == [
        {
            "scope": (
                "McpServers.Excel.All McpServers.OneDriveSharepoint.All "
                "McpServersMetadata.Read.All Old.Scope"
            )
        },
    ]


AGENT_TOOLS_SCOPES = {
    "McpServers.OneDriveSharepoint.All",
    "McpServers.Excel.All",
    "McpServersMetadata.Read.All",
}


def _stateful_grant_request(grants: dict[str, dict[str, str]]):
    """Fake Graph that stores one grant per resource, as Entra does."""

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        body = kwargs.get("json")
        if method == "GET" and "servicePrincipals?$filter=appId eq 'blueprint-app'" in url:
            return FakeResponse(200, {"value": [{"id": "blueprint-sp"}]})
        if method == "GET" and "servicePrincipals?$filter=appId eq 'ea9ffc3e" in url:
            return FakeResponse(200, {"value": [{"id": "agent-tools-sp"}]})
        if method == "GET" and "servicePrincipals?$filter=appId eq 'c2d0c2b6" in url:
            return FakeResponse(200, {"value": [{"id": "word-sp"}]})
        if method == "GET" and "oauth2PermissionGrants" in url:
            resource_id = url.split("resourceId eq '")[1].split("'")[0]
            grant = grants.get(resource_id)
            return FakeResponse(200, {"value": [grant] if grant else []})
        if method == "POST" and url.endswith("/oauth2PermissionGrants"):
            assert body is not None
            resource_id = body["resourceId"]
            assert resource_id not in grants, "one grant per client/resource pair"
            grants[resource_id] = {"id": f"grant-{resource_id}", "scope": body["scope"]}
            return FakeResponse(201, grants[resource_id])
        if method == "PATCH" and "/oauth2PermissionGrants/" in url:
            assert body is not None
            grant_id = url.rsplit("/", 1)[1]
            grant = next(g for g in grants.values() if g["id"] == grant_id)
            grant["scope"] = body["scope"]
            return FakeResponse(204)
        raise AssertionError(f"unexpected request: {method} {url} {body}")

    return request


def test_excel_scope_is_required_on_agent_tools_resource() -> None:
    module = load_module()

    assert module.A365_AGENT_TOOLS_APP_ID == "ea9ffc3e-8a23-4a7d-836d-234d7c7565c1"
    scopes_by_app: dict[str, set[str]] = {}
    for resource in module.REQUIRED_RESOURCES:
        scopes_by_app.setdefault(resource.app_id, set()).add(resource.scope)
    assert scopes_by_app[module.A365_AGENT_TOOLS_APP_ID] == AGENT_TOOLS_SCOPES


def test_fresh_tenant_merges_all_scopes_onto_one_agent_tools_grant() -> None:
    module = load_module()
    grants: dict[str, dict[str, str]] = {}

    module.ensure_a365_work_iq_permissions(
        token="token",
        blueprint_app_id="blueprint-app",
        request=_stateful_grant_request(grants),
        sleep=lambda _seconds: None,
    )

    assert set(grants) == {"agent-tools-sp", "word-sp"}
    assert set(grants["agent-tools-sp"]["scope"].split()) == AGENT_TOOLS_SCOPES
    assert grants["word-sp"]["scope"] == "Tools.ListInvoke.All"


def test_existing_tenant_gains_excel_scope_without_losing_others() -> None:
    module = load_module()
    grants = {
        "agent-tools-sp": {
            "id": "grant-agent-tools",
            "scope": "McpServers.OneDriveSharepoint.All McpServersMetadata.Read.All Other.Scope",
        },
        "word-sp": {"id": "grant-word", "scope": "Tools.ListInvoke.All"},
    }

    module.ensure_a365_work_iq_permissions(
        token="token",
        blueprint_app_id="blueprint-app",
        request=_stateful_grant_request(grants),
        sleep=lambda _seconds: None,
    )

    assert set(grants["agent-tools-sp"]["scope"].split()) == AGENT_TOOLS_SCOPES | {"Other.Scope"}
    assert grants["word-sp"]["scope"] == "Tools.ListInvoke.All"


def _not_found() -> FakeResponse:
    return FakeResponse(404, {"error": {"code": "Request_ResourceNotFound"}})


def test_a_new_agent_gets_each_resources_scopes_in_one_write() -> None:
    # From a real run on a new agent: the grant for the first scope was created, and the PATCH
    # that added the next scope to it moments later got 404 (Entra could not read the new
    # grant yet), so Word, Excel and OneDrive files were never switched on. Entra keeps one
    # grant per client and resource, so all of a resource's scopes now go in one write.
    module = load_module()
    grants: dict[str, dict[str, str]] = {}
    writes: list[str] = []
    stored = _stateful_grant_request(grants)

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        if method in ("POST", "PATCH") and "oauth2PermissionGrants" in url:
            writes.append(method)
        if method == "PATCH":
            return _not_found()  # a grant created moments ago
        return stored(method, url, **kwargs)

    module.ensure_a365_work_iq_permissions(
        token="token",
        blueprint_app_id="blueprint-app",
        request=request,
        sleep=lambda _seconds: None,
    )

    assert writes == ["POST", "POST"]
    assert set(grants["agent-tools-sp"]["scope"].split()) == AGENT_TOOLS_SCOPES
    assert grants["word-sp"]["scope"] == "Tools.ListInvoke.All"


def test_a_grant_that_cannot_be_updated_yet_is_tried_again() -> None:
    module = load_module()
    grants = {
        "agent-tools-sp": {"id": "grant-agent-tools", "scope": "McpServers.OneDriveSharepoint.All"},
        "word-sp": {"id": "grant-word", "scope": "Tools.ListInvoke.All"},
    }
    stored = _stateful_grant_request(grants)
    refused = 0
    sleeps: list[float] = []

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        nonlocal refused
        if method == "PATCH" and refused < 2:
            refused += 1
            return _not_found()
        return stored(method, url, **kwargs)

    module.ensure_a365_work_iq_permissions(
        token="token",
        blueprint_app_id="blueprint-app",
        request=request,
        sleep=sleeps.append,
    )

    assert sleeps == [5, 10]
    assert set(grants["agent-tools-sp"]["scope"].split()) == AGENT_TOOLS_SCOPES


def test_a_grant_that_never_becomes_updatable_is_an_error_not_a_hang() -> None:
    module = load_module()
    grants = {
        "agent-tools-sp": {"id": "grant-agent-tools", "scope": "McpServers.OneDriveSharepoint.All"},
    }
    stored = _stateful_grant_request(grants)
    sleeps: list[float] = []

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        if method == "PATCH":
            return _not_found()
        return stored(method, url, **kwargs)

    with pytest.raises(module.A365PermissionError, match=r"patch Agent 365 tools OAuth grant"):
        module.ensure_a365_work_iq_permissions(
            token="token",
            blueprint_app_id="blueprint-app",
            request=request,
            sleep=sleeps.append,
        )
    assert sleeps == [5, 10, 15, 20]


def test_post_conflict_requeries_and_patches_existing_grant() -> None:
    module = load_module()
    grant_queries = 0
    patch_bodies: list[dict[str, str]] = []
    sleeps: list[float] = []

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        nonlocal grant_queries
        body = kwargs.get("json")
        if method == "GET" and "oauth2PermissionGrants" in url:
            grant_queries += 1
            if grant_queries == 1:
                return FakeResponse(200, {"value": []})
            return FakeResponse(
                200,
                {
                    "value": [
                        {
                            "id": "agent-tools-grant",
                            "scope": "McpServers.OneDriveSharepoint.All",
                        }
                    ]
                },
            )
        if method == "POST" and url.endswith("/oauth2PermissionGrants"):
            return FakeResponse(
                409,
                {
                    "error": {
                        "code": "Request_MultipleObjectsWithSameKeyValue",
                        "message": "Permission entry already exists.",
                    }
                },
            )
        if method == "PATCH" and url.endswith("/oauth2PermissionGrants/agent-tools-grant"):
            assert body is not None
            patch_bodies.append(body)
            return FakeResponse(204)
        raise AssertionError(f"unexpected request: {method} {url} {body}")

    module._ensure_oauth_grant(
        "Agent 365 tools",
        ("McpServersMetadata.Read.All",),
        blueprint_sp_object_id="blueprint-sp",
        resource_sp_object_id="agent-tools-sp",
        token="token",
        request=request,
        sleep=sleeps.append,
    )

    assert sleeps == [5]
    assert patch_bodies == [
        {"scope": "McpServers.OneDriveSharepoint.All McpServersMetadata.Read.All"}
    ]


def test_raises_when_blueprint_service_principal_is_missing() -> None:
    module = load_module()

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        if method == "GET" and "servicePrincipals?$filter=appId eq 'missing-blueprint'" in url:
            return FakeResponse(200, {"value": []})
        raise AssertionError(f"unexpected request: {method} {url}")

    with pytest.raises(module.A365PermissionError, match="Blueprint service principal"):
        module.ensure_a365_work_iq_permissions(
            token="token",
            blueprint_app_id="missing-blueprint",
            request=request,
            sleep=lambda _seconds: None,
        )


# ── Inheritable permissions on the Blueprint ─────────────────────────────────────────────────
#
# What lets the agent get a token for Word, Excel and OneDrive files: with the grants alone, the
# agent's token request failed with AADSTS65001 on a real tenant. Microsoft's tool sets these as
# part of `a365 setup permissions mcp`; the script now writes them itself.

INHERITABLE = (
    "https://graph.microsoft.com/v1.0/applications/microsoft.graph.agentIdentityBlueprint/"
    "blueprint-app/inheritablePermissions"
)
TOOLS = "ea9ffc3e-8a23-4a7d-836d-234d7c7565c1"
WORD = "c2d0c2b6-8013-4346-9f8b-b81d3b754a29"


class FakeInheritable:
    """The Blueprint's inheritablePermissions collection, the way Graph answered on a real
    tenant: a read straight after a write can come back empty, and a write can be lost when
    another lands moments later."""

    def __init__(
        self,
        entries: list[dict[str, Any]] | None = None,
        *,
        empty_reads_after_write: int = 1,
        lose_the_first_when_the_second_lands: bool = False,
        never_readable: bool = False,
    ) -> None:
        self.entries = {entry["resourceAppId"]: entry for entry in entries or []}
        self.empty_reads_after_write = empty_reads_after_write
        self.lose_first = lose_the_first_when_the_second_lands
        self.never_readable = never_readable
        self.empty_reads = 0
        self.posted: list[str] = []
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        body = kwargs.get("json")
        self.calls.append((method, url, body))
        assert kwargs["headers"]["Authorization"] == "Bearer token"
        if method == "GET" and url == INHERITABLE:
            if self.never_readable or self.empty_reads:
                self.empty_reads = max(0, self.empty_reads - 1)
                return FakeResponse(200, {"value": []})
            return FakeResponse(200, {"value": list(self.entries.values())})
        if method == "POST" and url == INHERITABLE:
            assert body is not None
            app = body["resourceAppId"]
            if app in self.entries:
                return FakeResponse(409, {"error": {"message": "already exists"}})
            if self.lose_first and len(self.posted) == 1:
                self.entries.pop(self.posted[0], None)  # seen for real: both answered 201
                self.lose_first = False
            self.posted.append(app)
            self.entries[app] = dict(body)
            self.empty_reads = self.empty_reads_after_write
            return FakeResponse(201, body)
        if method == "PATCH" and url.startswith(INHERITABLE + "/"):
            assert body is not None
            app = url.rsplit("/", 1)[1]
            self.entries[app] = {**self.entries[app], **body}
            self.empty_reads = self.empty_reads_after_write
            return FakeResponse(204)
        raise AssertionError(f"unexpected request: {method} {url} {body}")

    def writes(self) -> list[tuple[str, str, dict[str, Any] | None]]:
        return [call for call in self.calls if call[0] != "GET"]


def _enumerated(*scopes: str) -> dict[str, Any]:
    return {
        "@odata.type": "#microsoft.graph.enumeratedScopes",
        "kind": "enumerated",
        "scopes": list(scopes),
    }


def _inherit(graph: FakeInheritable, sleeps: list[float] | None = None) -> None:
    module = load_module()
    module.ensure_inheritable_permissions(
        token="token",
        blueprint_app_id="blueprint-app",
        request=graph,
        sleep=(sleeps.append if sleeps is not None else lambda _seconds: None),
    )


def test_each_resource_gets_a_least_privilege_entry() -> None:
    graph = FakeInheritable()
    _inherit(graph)
    assert [body for _method, _url, body in graph.writes()] == [
        {
            "resourceAppId": TOOLS,
            "inheritableScopes": _enumerated(
                "McpServers.OneDriveSharepoint.All",
                "McpServers.Excel.All",
                "McpServersMetadata.Read.All",
            ),
            "inheritableRoles": {"@odata.type": "#microsoft.graph.noRoles", "kind": "none"},
        },
        {
            "resourceAppId": WORD,
            "inheritableScopes": _enumerated("Tools.ListInvoke.All"),
            "inheritableRoles": {"@odata.type": "#microsoft.graph.noRoles", "kind": "none"},
        },
    ]


def test_one_entry_is_confirmed_before_the_next_is_written() -> None:
    # Two writes sent back to back both answered 201 on a real tenant, and only one was kept.
    graph = FakeInheritable(empty_reads_after_write=2)
    sleeps: list[float] = []
    _inherit(graph, sleeps)
    methods = [method for method, _url, _body in graph.calls]
    first, second = (i for i, method in enumerate(methods) if method == "POST")
    reads_between = methods[first + 1 : second]
    assert reads_between.count("GET") >= 3  # two empty answers, then the entry
    assert sleeps  # it waited between reads rather than hammering


def test_an_entry_lost_after_it_was_confirmed_is_written_again() -> None:
    graph = FakeInheritable(lose_the_first_when_the_second_lands=True)
    _inherit(graph)
    assert graph.posted == [TOOLS, WORD, TOOLS]
    assert set(graph.entries) == {TOOLS, WORD}


def test_broader_entries_from_microsofts_tool_are_left_alone() -> None:
    # On a working Mac agent, Microsoft's tool set allAllowed scopes and roles: a no-op here.
    all_allowed = {
        "inheritableScopes": {
            "@odata.type": "#microsoft.graph.allAllowedScopes",
            "kind": "allAllowed",
        },
        "inheritableRoles": {
            "@odata.type": "#microsoft.graph.allAllowedRoles",
            "kind": "allAllowed",
        },
    }
    graph = FakeInheritable(
        [{"resourceAppId": TOOLS, **all_allowed}, {"resourceAppId": WORD, **all_allowed}]
    )
    _inherit(graph)
    assert graph.writes() == []


def test_an_enumerated_entry_is_widened_never_narrowed() -> None:
    graph = FakeInheritable(
        [
            {
                "resourceAppId": TOOLS,
                "inheritableScopes": _enumerated("Someone.Else.Needs", "McpServers.Excel.All"),
                "inheritableRoles": {"@odata.type": "#microsoft.graph.allAllowedRoles"},
            },
            {"resourceAppId": WORD, "inheritableScopes": _enumerated("Tools.ListInvoke.All")},
        ]
    )
    _inherit(graph)
    ((method, url, body),) = graph.writes()
    assert method == "PATCH" and url == f"{INHERITABLE}/{TOOLS}"
    assert body == {
        "inheritableScopes": _enumerated(
            "Someone.Else.Needs",
            "McpServers.Excel.All",
            "McpServers.OneDriveSharepoint.All",
            "McpServersMetadata.Read.All",
        )
    }  # and its roles are not touched
    assert graph.entries[TOOLS]["inheritableRoles"] == {
        "@odata.type": "#microsoft.graph.allAllowedRoles"
    }


def test_a_conflicting_create_reads_the_entry_and_widens_it() -> None:
    graph = FakeInheritable()

    def first_read_misses(method: str, url: str, **kwargs: Any) -> FakeResponse:
        # The entry exists, but the first read does not show it (Graph had not caught up).
        if method == "GET" and not graph.calls:
            graph.calls.append((method, url, None))
            graph.entries[TOOLS] = {
                "resourceAppId": TOOLS,
                "inheritableScopes": _enumerated("McpServers.Excel.All"),
            }
            return FakeResponse(200, {"value": []})
        return graph(method, url, **kwargs)

    module = load_module()
    module.ensure_inheritable_permissions(
        token="token",
        blueprint_app_id="blueprint-app",
        request=first_read_misses,
        sleep=lambda _seconds: None,
    )
    writes = graph.writes()
    assert writes[0][0] == "POST" and writes[1][0] == "PATCH"  # 409, then a merge
    scopes = graph.entries[TOOLS]["inheritableScopes"]["scopes"]
    assert set(scopes) >= {"McpServers.Excel.All", "McpServers.OneDriveSharepoint.All"}


def test_an_entry_that_never_shows_up_is_an_error_not_a_hang() -> None:
    graph = FakeInheritable(never_readable=True)
    sleeps: list[float] = []
    module = load_module()
    with pytest.raises(module.A365PermissionError, match="could not be read back"):
        module.ensure_inheritable_permissions(
            token="token", blueprint_app_id="blueprint-app", request=graph, sleep=sleeps.append
        )
    assert sum(sleeps) < 120


def test_a_refused_write_is_an_error_with_graphs_answer() -> None:
    def refused(method: str, url: str, **kwargs: Any) -> FakeResponse:
        if method == "GET":
            return FakeResponse(200, {"value": []})
        return FakeResponse(403, {"error": {"code": "Authorization_RequestDenied"}})

    module = load_module()
    with pytest.raises(module.A365PermissionError, match="403"):
        module.ensure_inheritable_permissions(
            token="token",
            blueprint_app_id="blueprint-app",
            request=refused,
            sleep=lambda _seconds: None,
        )


def test_main_grants_then_lets_agents_inherit(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    done: list[str] = []
    monkeypatch.setattr(module, "get_existing_graph_token", lambda: "token")
    monkeypatch.setattr(
        module, "ensure_a365_work_iq_permissions", lambda **_kwargs: done.append("grants")
    )
    monkeypatch.setattr(
        module, "ensure_inheritable_permissions", lambda **_kwargs: done.append("inherit")
    )
    assert module.main(["--blueprint-app-id", "blueprint-app"]) == 0
    assert done == ["grants", "inherit"]


def test_the_scopes_come_from_the_package_catalog() -> None:
    # One source of truth: the manifest setup writes and the permissions it grants.
    from entrabot.a365.catalog import granted_scopes

    module = load_module()
    scopes: dict[str, list[str]] = {}
    for resource in module.REQUIRED_RESOURCES:
        scopes.setdefault(resource.app_id, []).append(resource.scope)
    assert {app: tuple(found) for app, found in scopes.items()} == granted_scopes()
