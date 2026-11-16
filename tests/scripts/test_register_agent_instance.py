"""Registering the agent in the Agent 365 registry (Agent Registry API, Graph beta).

Microsoft Learn: "Creating a Microsoft Entra identity alone doesn't register the instance."
An agent whose Blueprint was made through the Graph API registers with
``POST /beta/copilot/agentRegistrations`` (``AgentRegistration.ReadWrite.All``). The API has
no list call, so the registration's ID is kept in the state file and is how a second run
finds it. A dry run is the default; nothing is written without ``--apply``.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "register_agent_instance.py"
URL = "https://graph.microsoft.com/beta/copilot/agentRegistrations"


def _jwt(claims: dict[str, Any]) -> str:
    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


TOKEN = _jwt({"roles": ["AgentRegistration.ReadWrite.All", "Application.Read.All"]})
STATE = {
    "TENANT_ID": "tenant",
    "PROVISIONER_CLIENT_ID": "provisioner-app",
    "BLUEPRINT_APP_ID": "blueprint-app",
    "AGENT_ID": "agent-app",
    "AGENT_USER_UPN": "entrabot-agent-sati@contoso.test",
}


class Response:
    def __init__(self, status: int, body: dict[str, Any] | None = None) -> None:
        self.status_code = status
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self) -> dict[str, Any]:
        return self._body


@pytest.fixture
def module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    spec = importlib.util.spec_from_file_location("register_agent_instance", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    state = dict(STATE)
    monkeypatch.setattr(mod, "get_state", state.get)
    monkeypatch.setattr(mod, "set_state", state.__setitem__)
    monkeypatch.setattr(mod, "clear_state", lambda key: state.pop(key, None))
    monkeypatch.setattr(mod, "get_existing_graph_token", lambda: TOKEN)
    # Never this checkout's own .env: a scratch one, empty unless a test writes it.
    monkeypatch.setattr(mod, "ENV_FILE", tmp_path / ".env")
    for key in (
        "ENTRABOT_HUMAN_USER_ID",
        "ENTRABOT_HUMAN_USER_IDS",
        "ENTRABOT_HUMAN_USER_TYPES",
        "ENTRABOT_AGENT_DISPLAY_NAME",
    ):
        monkeypatch.delenv(key, raising=False)
    mod._test_state = state
    return mod


def _result(out: str) -> dict[str, Any]:
    lines = [line for line in out.splitlines() if line.startswith("ENTRABOT_RESULT=")]
    assert lines, out
    return json.loads(lines[-1].removeprefix("ENTRABOT_RESULT="))


class Graph:
    def __init__(self, *replies: Response) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(self, method: str, url: str, **kwargs: Any) -> Response:
        self.calls.append((method, url, kwargs.get("json")))
        return self.replies.pop(0)


def test_a_dry_run_writes_nothing_and_shows_the_payload(module, monkeypatch, capsys) -> None:
    graph = Graph()
    monkeypatch.setattr(module, "_request", graph)
    assert module.main(["--owner-id", "owner-1"]) == 0
    assert graph.calls == []
    out = capsys.readouterr().out
    assert "dry run" in out.lower() and '"agentIdentityId": "agent-app"' in out
    assert "AGENT_REGISTRATION_ID" not in module._test_state


def test_apply_creates_a_registration_and_records_its_id(module, monkeypatch) -> None:
    graph = Graph(Response(201, {"id": "registration-1"}))
    monkeypatch.setattr(module, "_request", graph)
    assert module.main(["--apply", "--owner-id", "owner-1"]) == 0
    ((method, url, body),) = graph.calls
    assert (method, url) == ("POST", URL)
    assert body["agentIdentityId"] == "agent-app"
    assert body["agentIdentityBlueprintId"] == "blueprint-app"
    assert body["ownerIds"] == ["owner-1"]
    assert body["createdBy"] == "provisioner-app"
    for required in ("displayName", "sourceCreatedDateTime", "sourceLastModifiedDateTime"):
        assert body[required]
    assert module._test_state["AGENT_REGISTRATION_ID"] == "registration-1"


def test_a_recorded_registration_is_updated_not_duplicated(module, monkeypatch) -> None:
    module._test_state["AGENT_REGISTRATION_ID"] = "registration-1"
    graph = Graph(Response(200, {"id": "registration-1"}), Response(200))
    monkeypatch.setattr(module, "_request", graph)
    assert module.main(["--apply", "--owner-id", "owner-1"]) == 0
    assert [call[0] for call in graph.calls] == ["GET", "PATCH"]
    assert graph.calls[1][1] == f"{URL}/registration-1"
    assert "sourceCreatedDateTime" not in graph.calls[1][2]  # not updatable


def test_a_recorded_registration_that_is_gone_is_created_again(module, monkeypatch) -> None:
    module._test_state["AGENT_REGISTRATION_ID"] = "registration-1"
    graph = Graph(Response(404), Response(201, {"id": "registration-2"}))
    monkeypatch.setattr(module, "_request", graph)
    assert module.main(["--apply", "--owner-id", "owner-1"]) == 0
    assert [call[0] for call in graph.calls] == ["GET", "POST"]
    assert module._test_state["AGENT_REGISTRATION_ID"] == "registration-2"


def test_a_lookup_that_fails_creates_nothing(module, monkeypatch) -> None:
    # "Could not check" is not "there is none": a second registration is not created on it.
    module._test_state["AGENT_REGISTRATION_ID"] = "registration-1"
    graph = Graph(Response(503))
    monkeypatch.setattr(module, "_request", graph)
    assert module.main(["--apply", "--owner-id", "owner-1"]) == 1
    assert [call[0] for call in graph.calls] == ["GET"]


def test_without_the_permission_it_stops_before_any_call(module, monkeypatch, capsys) -> None:
    monkeypatch.setattr(module, "get_existing_graph_token", lambda: _jwt({"roles": []}))
    graph = Graph()
    monkeypatch.setattr(module, "_request", graph)
    assert module.main(["--apply", "--owner-id", "owner-1"]) == 1
    assert graph.calls == []
    assert "AgentRegistration.ReadWrite.All" in capsys.readouterr().err


def test_an_owner_is_required(module, monkeypatch, capsys) -> None:
    # App-only registration needs owners, and they must be users (Learn, bulk onboarding).
    monkeypatch.setattr(module, "_request", Graph())
    assert module.main(["--apply"]) == 2
    assert "owner" in capsys.readouterr().err.lower()


def test_a_missing_agent_id_stops(module, monkeypatch) -> None:
    del module._test_state["AGENT_ID"]
    monkeypatch.setattr(module, "_request", Graph())
    assert module.main(["--apply", "--owner-id", "owner-1"]) == 2


def test_no_token_is_printed(module, monkeypatch, capsys) -> None:
    monkeypatch.setattr(module, "_request", Graph(Response(201, {"id": "r"})))
    module.main(["--apply", "--owner-id", "owner-1"])
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


def test_owners_and_the_display_name_come_from_dotenv(module, monkeypatch) -> None:
    module.ENV_FILE.write_text(
        "ENTRABOT_HUMAN_USER_IDS=member-1,guest-1\n"
        "ENTRABOT_HUMAN_USER_TYPES=Member,Guest\n"
        "ENTRABOT_AGENT_DISPLAY_NAME=Sati\n",
        encoding="utf-8",
    )
    graph = Graph(Response(201, {"id": "registration-1"}))
    monkeypatch.setattr(module, "_request", graph)
    assert module.main(["--apply"]) == 0
    body = graph.calls[0][2]
    assert body["ownerIds"] == ["member-1"]
    assert body["displayName"] == "Sati"


def test_without_a_display_name_the_upn_prefix_is_used(module, monkeypatch, capsys) -> None:
    monkeypatch.setattr(module, "_request", Graph())
    assert module.main(["--owner-id", "owner-1"]) == 0
    assert '"displayName": "entrabot-agent-sati"' in capsys.readouterr().out


def test_print_result_ends_with_one_marked_line(module, monkeypatch, capsys) -> None:
    monkeypatch.setattr(module, "_request", Graph(Response(201, {"id": "registration-1"})))
    assert module.main(["--apply", "--owner-id", "owner-1", "--print-result"]) == 0
    assert _result(capsys.readouterr().out) == {
        "outcome": "created",
        "id": "registration-1",
        "reason": "",
    }


def test_a_missing_permission_is_its_own_outcome(module, monkeypatch, capsys) -> None:
    monkeypatch.setattr(module, "get_existing_graph_token", lambda: _jwt({"roles": []}))
    monkeypatch.setattr(module, "_request", Graph())
    assert module.main(["--apply", "--owner-id", "o", "--print-result"]) == 1
    result = _result(capsys.readouterr().out)
    assert result["outcome"] == "permission-missing"
    assert "AgentRegistration.ReadWrite.All" in result["reason"]


def test_graph_refusing_is_its_own_outcome_and_records_nothing(module, monkeypatch, capsys) -> None:
    monkeypatch.setattr(module, "_request", Graph(Response(403, {"error": "denied"})))
    assert module.main(["--apply", "--owner-id", "o", "--print-result"]) == 1
    result = _result(capsys.readouterr().out)
    assert result["outcome"] == "refused" and "403" in result["reason"]
    assert "AGENT_REGISTRATION_ID" not in module._test_state


def test_no_setup_app_token_is_its_own_outcome(module, monkeypatch, capsys) -> None:
    def refuse() -> str:
        raise module.ProvisionerBootstrapError("not signed in")

    monkeypatch.setattr(module, "get_existing_graph_token", refuse)
    monkeypatch.setattr(module, "_request", Graph())
    assert module.main(["--apply", "--owner-id", "o", "--print-result"]) == 1
    assert _result(capsys.readouterr().out)["outcome"] == "no-access"


def test_a_registration_made_elsewhere_is_adopted_not_duplicated(module, monkeypatch) -> None:
    graph = Graph(Response(200, {"id": "elsewhere-1"}), Response(204))
    monkeypatch.setattr(module, "_request", graph)
    argv = ["--apply", "--owner-id", "o", "--registration-id", "elsewhere-1"]
    assert module.main(argv) == 0
    assert [(call[0], call[1]) for call in graph.calls] == [
        ("GET", f"{URL}/elsewhere-1"),
        ("PATCH", f"{URL}/elsewhere-1"),
    ]
    assert module._test_state["AGENT_REGISTRATION_ID"] == "elsewhere-1"


def test_a_recorded_registration_for_another_agent_is_left_alone(
    module, monkeypatch, capsys
) -> None:
    module._test_state["AGENT_REGISTRATION_ID"] = "registration-1"
    graph = Graph(Response(200, {"id": "registration-1", "agentIdentityId": "other-agent"}))
    monkeypatch.setattr(module, "_request", graph)
    assert module.main(["--apply", "--owner-id", "o", "--print-result"]) == 1
    assert [call[0] for call in graph.calls] == ["GET"]
    assert _result(capsys.readouterr().out)["outcome"] == "not-ours"


class TestCheck:
    def test_nothing_recorded_is_an_answer_without_a_call(self, module, monkeypatch, capsys):
        graph = Graph()
        monkeypatch.setattr(module, "_request", graph)
        assert module.main(["--check", "--print-result"]) == 0
        assert _result(capsys.readouterr().out)["outcome"] == "not-recorded"
        assert graph.calls == []

    @pytest.mark.parametrize(("status", "outcome"), [(200, "present"), (404, "gone")])
    def test_a_recorded_one_is_read(self, module, monkeypatch, capsys, status, outcome):
        module._test_state["AGENT_REGISTRATION_ID"] = "registration-1"
        graph = Graph(Response(status, {"id": "registration-1"}))
        monkeypatch.setattr(module, "_request", graph)
        assert module.main(["--check", "--print-result"]) == 0
        assert graph.calls == [("GET", f"{URL}/registration-1", None)]
        assert _result(capsys.readouterr().out) == {
            "outcome": outcome,
            "id": "registration-1",
            "reason": "",
        }

    def test_one_that_cannot_be_read_says_why(self, module, monkeypatch, capsys):
        module._test_state["AGENT_REGISTRATION_ID"] = "registration-1"
        monkeypatch.setattr(module, "_request", Graph(Response(500, {"error": "x"})))
        assert module.main(["--check", "--print-result"]) == 1
        result = _result(capsys.readouterr().out)
        assert result["outcome"] == "could-not-check" and "500" in result["reason"]

    def test_without_the_permission_nothing_is_read(self, module, monkeypatch, capsys):
        module._test_state["AGENT_REGISTRATION_ID"] = "registration-1"
        monkeypatch.setattr(module, "get_existing_graph_token", lambda: _jwt({"roles": []}))
        graph = Graph()
        monkeypatch.setattr(module, "_request", graph)
        assert module.main(["--check", "--print-result"]) == 1
        assert _result(capsys.readouterr().out)["outcome"] == "permission-missing"
        assert graph.calls == []


class TestDelete:
    def test_a_dry_run_deletes_nothing(self, module, monkeypatch, capsys):
        module._test_state["AGENT_REGISTRATION_ID"] = "registration-1"
        graph = Graph()
        monkeypatch.setattr(module, "_request", graph)
        assert module.main(["--delete"]) == 0
        assert graph.calls == []
        assert module._test_state["AGENT_REGISTRATION_ID"] == "registration-1"

    @pytest.mark.parametrize(("status", "outcome"), [(204, "deleted"), (404, "gone")])
    def test_apply_deletes_and_forgets_it(self, module, monkeypatch, capsys, status, outcome):
        module._test_state["AGENT_REGISTRATION_ID"] = "registration-1"
        graph = Graph(Response(status))
        monkeypatch.setattr(module, "_request", graph)
        assert module.main(["--delete", "--apply", "--print-result"]) == 0
        assert graph.calls == [("DELETE", f"{URL}/registration-1", None)]
        assert _result(capsys.readouterr().out)["outcome"] == outcome
        assert "AGENT_REGISTRATION_ID" not in module._test_state

    def test_a_refused_delete_keeps_the_record(self, module, monkeypatch, capsys):
        module._test_state["AGENT_REGISTRATION_ID"] = "registration-1"
        monkeypatch.setattr(module, "_request", Graph(Response(403, {"error": "denied"})))
        assert module.main(["--delete", "--apply", "--print-result"]) == 1
        assert _result(capsys.readouterr().out)["outcome"] == "refused"
        assert module._test_state["AGENT_REGISTRATION_ID"] == "registration-1"

    def test_nothing_recorded_is_nothing_to_delete(self, module, monkeypatch, capsys):
        monkeypatch.setattr(module, "_request", Graph())
        assert module.main(["--delete", "--apply", "--print-result"]) == 0
        assert _result(capsys.readouterr().out)["outcome"] == "not-recorded"


class TestWithoutAzOnPath:
    """Found live on Ubuntu 24.04 with v0.3.0: the only Azure CLI was setup's private copy
    (not on PATH), and ``--check`` stopped with ``FileNotFoundError: 'az'``, so the registry
    line of ``--diagnose`` showed a traceback. The setup app's token comes from its
    certificate and needs no Azure CLI. Here that token path is the real one: only the
    keystore, the token endpoint and Graph are stood in for, and no child process is."""

    PEM = (
        "-----BEGIN CERTIFICATE-----\ncert\n-----END CERTIFICATE-----\n"
        "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n"
    )

    @pytest.fixture
    def real_token(self, module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request):
        import types

        from tests.private_azure_cli import computer_without_az_on_path, install_private_copy

        data_root = computer_without_az_on_path(monkeypatch, tmp_path)
        if request.param:
            install_private_copy(data_root)
        provisioning = sys.modules["entra_provisioning"]
        monkeypatch.delenv("ENTRABOT_TENANT_ID", raising=False)
        monkeypatch.setattr(provisioning, "get_state", module._test_state.get)
        monkeypatch.setattr(provisioning, "_uses_cng", lambda: False)
        monkeypatch.setattr(provisioning, "_keychain_get_cert", lambda tenant: self.PEM)

        class _Credential:
            def __init__(self, **_kwargs: object) -> None:
                pass

            def get_token(self, _scope: str) -> object:
                return types.SimpleNamespace(token=TOKEN)

        fake = types.ModuleType("azure.identity")
        fake.CertificateCredential = _Credential
        monkeypatch.setitem(sys.modules, "azure.identity", fake)
        monkeypatch.setattr(
            module, "get_existing_graph_token", provisioning.get_existing_graph_token
        )
        return module

    @pytest.mark.parametrize(
        "real_token", [False, True], ids=["no-azure-cli", "private-copy"], indirect=True
    )
    def test_check_reads_the_registration(self, real_token, monkeypatch, capsys) -> None:
        real_token._test_state["AGENT_REGISTRATION_ID"] = "registration-1"
        graph = Graph(Response(200, {"id": "registration-1"}))
        monkeypatch.setattr(real_token, "_request", graph)

        assert real_token.main(["--check", "--print-result"]) == 0

        assert _result(capsys.readouterr().out) == {
            "outcome": "present",
            "id": "registration-1",
            "reason": "",
        }

    @pytest.mark.parametrize(
        "real_token", [False, True], ids=["no-azure-cli", "private-copy"], indirect=True
    )
    def test_teardown_deletes_the_registration(self, real_token, monkeypatch, capsys) -> None:
        """Teardown deletes the registration with ``--delete --apply``, the same token."""
        real_token._test_state["AGENT_REGISTRATION_ID"] = "registration-1"
        graph = Graph(Response(204))
        monkeypatch.setattr(real_token, "_request", graph)

        assert real_token.main(["--delete", "--apply", "--print-result"]) == 0

        assert _result(capsys.readouterr().out)["outcome"] == "deleted"
        assert "AGENT_REGISTRATION_ID" not in real_token._test_state
