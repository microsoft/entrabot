"""The agent's sponsors in the directory: who it actually takes instructions from in Teams.

At runtime the agent obeys the sponsors on its Agent Identity in Microsoft Entra, not the
sponsor lines in ``.env``. Setup therefore has to put the chosen sponsor THERE, and say
truthfully who is there afterwards.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from entrabot.winsetup.agentsponsors import (
    DirectorySponsor,
    add_command,
    remove_command,
    sync_sponsors,
)
from entrabot.winsetup.console import RunRequest
from entrabot.winsetup.helpers import RESULT_MARKER, Helpers, Paths
from tests.winsetup.fakes import FakeSpawn, Reply, make_console, snippet

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT = "agent-object"
ADMIN = {
    "id": "admin-id",
    "displayName": "Admin",
    "userPrincipalName": "admin@contoso.com",
    "mail": "admin@contoso.com",
}


def _helpers(tmp_path: Path, spawn: FakeSpawn) -> Helpers:
    console, _ = make_console(spawn=spawn)
    paths = Paths(tmp_path, tmp_path / ".venv" / "Scripts" / "python.exe", tmp_path / "data")
    return Helpers(console, paths, {"PATH": "x"})


def _reply(**result: object) -> Reply:
    base = {"before": [ADMIN], "added": [], "failed": {}, "after": [ADMIN], "error": ""}
    return Reply(stdout=RESULT_MARKER + json.dumps({**base, **result}))


class TestSync:
    def test_the_request_names_the_agent_and_the_object_ids_to_add(self, tmp_path: Path) -> None:
        spawn = FakeSpawn()
        spawn.on(snippet("_add_sponsor"), _reply())
        helpers = _helpers(tmp_path, spawn)
        sync_sponsors(helpers, AGENT, ["pat-id", "sam-id"], env={"ENTRABOT_TENANT_ID": "t"})
        request = spawn.calls[0]
        # Values travel as arguments, never inside the program text.
        assert request.argv[3:] == [str(helpers.paths.scripts), AGENT, "pat-id", "sam-id"]
        assert "pat-id" not in request.argv[2]
        assert request.env is not None and request.env["ENTRABOT_TENANT_ID"] == "t"

    def test_agent_identity_calls_use_the_provisioner_token_never_the_azure_cli(
        self, tmp_path: Path
    ) -> None:
        # Azure CLI tokens are refused by the Agent Identity APIs.
        spawn = FakeSpawn()
        spawn.on(snippet("_add_sponsor"), _reply())
        sync_sponsors(_helpers(tmp_path, spawn), AGENT, [], env={})
        code = spawn.calls[0].argv[2]
        assert "get_existing_graph_token" in code
        assert "get-access-token" not in code and "az " not in code
        assert len(spawn.calls) == 1

    def test_nothing_to_add_is_a_read(self, tmp_path: Path) -> None:
        spawn = FakeSpawn()
        spawn.on(snippet("_add_sponsor"), _reply())
        result = sync_sponsors(_helpers(tmp_path, spawn), AGENT, [], env={})
        assert result.read and result.added == () and not result.problems
        assert [item.object_id for item in result.after] == ["admin-id"]
        assert result.after[0].label == "admin@contoso.com"

    def test_an_added_sponsor_is_confirmed_by_the_read_back(self, tmp_path: Path) -> None:
        spawn = FakeSpawn()
        spawn.on(snippet("_add_sponsor"), _reply(added=["pat-id"], after=[ADMIN, {"id": "pat-id"}]))
        result = sync_sponsors(_helpers(tmp_path, spawn), AGENT, ["pat-id"], env={})
        assert result.added == ("pat-id",) and result.unconfirmed == () and not result.problems
        assert result.is_sponsor("PAT-ID")

    def test_an_add_the_read_back_does_not_show_is_unconfirmed(self, tmp_path: Path) -> None:
        spawn = FakeSpawn()
        spawn.on(snippet("_add_sponsor"), _reply(added=["pat-id"], after=[ADMIN]))
        result = sync_sponsors(_helpers(tmp_path, spawn), AGENT, ["pat-id"], env={})
        assert result.added == () and result.unconfirmed == ("pat-id",)
        assert result.problems

    def test_a_refused_add_carries_the_reason(self, tmp_path: Path) -> None:
        spawn = FakeSpawn()
        spawn.on(
            snippet("_add_sponsor"),
            _reply(failed={"pat-id": "Failed to add sponsor: 403 Authorization_RequestDenied"}),
        )
        result = sync_sponsors(_helpers(tmp_path, spawn), AGENT, ["pat-id"], env={})
        assert "Authorization_RequestDenied" in result.failed["pat-id"]
        assert result.problems and not result.is_sponsor("pat-id")

    def test_someone_already_there_is_neither_added_nor_a_problem(self, tmp_path: Path) -> None:
        spawn = FakeSpawn()
        spawn.on(snippet("_add_sponsor"), _reply())
        result = sync_sponsors(_helpers(tmp_path, spawn), AGENT, ["admin-id"], env={})
        assert result.added == () and not result.problems and result.was_sponsor("admin-id")

    def test_a_directory_that_cannot_be_read_is_not_an_empty_list(self, tmp_path: Path) -> None:
        spawn = FakeSpawn()
        spawn.on(
            snippet("_add_sponsor"),
            _reply(before=None, after=None, error="Provisioner app is not bootstrapped."),
        )
        result = sync_sponsors(_helpers(tmp_path, spawn), AGENT, ["pat-id"], env={})
        assert not result.read and "not bootstrapped" in result.error
        assert result.after == () and result.problems

    def test_a_helper_that_crashes_is_reported_with_its_last_words(self, tmp_path: Path) -> None:
        spawn = FakeSpawn()
        spawn.on(
            snippet("_add_sponsor"),
            Reply(returncode=1, stderr="Traceback...\nModuleNotFoundError: No module named 'x'"),
        )
        result = sync_sponsors(_helpers(tmp_path, spawn), AGENT, [], env={})
        assert not result.read and "ModuleNotFoundError" in result.error

    def test_no_agent_object_id_is_refused_without_running_anything(self, tmp_path: Path) -> None:
        spawn = FakeSpawn()
        result = sync_sponsors(_helpers(tmp_path, spawn), "", ["pat-id"], env={})
        assert not result.read and spawn.calls == []

    def test_the_helper_is_quiet_on_screen(self, tmp_path: Path) -> None:
        spawn = FakeSpawn()
        spawn.on(snippet("_add_sponsor"), _reply())
        console, out = make_console(spawn=spawn)
        paths = Paths(tmp_path, tmp_path / "python", tmp_path / "data")
        sync_sponsors(Helpers(console, paths, {}), AGENT, [], env={})
        assert RESULT_MARKER not in out.getvalue()


class TestLabels:
    def test_an_address_is_preferred_over_a_name(self) -> None:
        sponsor = DirectorySponsor("id", "Pat", "pat@contoso.com", "pat@contoso.com")
        assert sponsor.label == "pat@contoso.com"

    def test_an_external_sign_in_name_is_shown_as_the_address_it_was_made_from(self) -> None:
        sponsor = DirectorySponsor("id", "B", "alex_example.com#EXT#@t.onmicrosoft.com", "")
        assert sponsor.label == "alex@example.com"

    def test_an_entry_with_only_an_id_falls_back_to_the_id(self) -> None:
        # Graph often returns nothing but the id for a sponsor.
        assert DirectorySponsor("only-id", "", "", "").label == "only-id"


class TestManualCommands:
    def test_the_commands_are_the_repos_own_scripts_run_with_the_projects_python(
        self, tmp_path: Path
    ) -> None:
        paths = Paths(tmp_path, tmp_path / ".venv" / "Scripts" / "python.exe", tmp_path / "d")
        assert add_command(paths, "pat@contoso.com") == (
            ".venv\\Scripts\\python.exe scripts\\add_agent_sponsor.py pat@contoso.com"
        )
        assert remove_command(paths, "admin@contoso.com") == (
            ".venv\\Scripts\\python.exe scripts\\remove_agent_sponsor.py admin@contoso.com"
        )

    def test_an_address_a_shell_would_split_is_quoted(self, tmp_path: Path) -> None:
        # An apostrophe starts a string in PowerShell; "&" separates commands in cmd.
        paths = Paths(tmp_path, tmp_path / ".venv" / "Scripts" / "python.exe", tmp_path / "d")
        assert add_command(paths, "o'brien@contoso.com").endswith(' "o\'brien@contoso.com"')
        assert add_command(paths, "r&d@contoso.com").endswith(' "r&d@contoso.com"')
        assert add_command(paths, "pat@contoso.com").endswith(" pat@contoso.com")

    @pytest.mark.parametrize("address", ["pat$x@contoso.com", "a`b@contoso.com", "50%off@x.com"])
    def test_an_address_a_shell_would_expand_even_in_quotes_is_given_as_its_object_id(
        self, tmp_path: Path, address: str
    ) -> None:
        # Inside double quotes PowerShell still expands $name and `, and cmd still expands
        # %name%. Copied and run, "pat$x@contoso.com" would add pat@contoso.com: somebody
        # else. The person's object ID means the same person in every shell.
        paths = Paths(tmp_path, tmp_path / ".venv" / "Scripts" / "python.exe", tmp_path / "d")
        object_id = "0f3c1c7e-1111-2222-3333-444455556666"
        for command in (
            add_command(paths, address, object_id=object_id),
            remove_command(paths, address, object_id=object_id),
        ):
            assert command.endswith(f" {object_id}") and address not in command

    def test_the_object_id_is_only_used_when_the_address_cannot_be_written_safely(
        self, tmp_path: Path
    ) -> None:
        paths = Paths(tmp_path, tmp_path / ".venv" / "Scripts" / "python.exe", tmp_path / "d")
        object_id = "0f3c1c7e-1111-2222-3333-444455556666"
        assert add_command(paths, "o'brien@contoso.com", object_id=object_id).endswith(
            ' "o\'brien@contoso.com"'
        )


class TestTheScriptsThisRelyOn:
    """The add and the read are add_agent_sponsor.py's own functions, so there is one
    implementation of the Graph calls. This fails if that script stops providing them."""

    @pytest.mark.parametrize(
        ("name", "arguments"),
        [
            ("_add_sponsor", ["token", "agent_object_id", "user_id"]),
            ("_list_sponsors", ["token", "agent_object_id"]),
        ],
    )
    def test_add_agent_sponsor_still_provides(self, name: str, arguments: list[str]) -> None:
        tree = ast.parse((REPO_ROOT / "scripts" / "add_agent_sponsor.py").read_text("utf-8"))
        found = {
            node.name: [arg.arg for arg in node.args.args]
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
        }
        assert found.get(name) == arguments

    def test_the_manual_scripts_exist(self) -> None:
        for name in ("add_agent_sponsor.py", "remove_agent_sponsor.py", "list_sponsors.py"):
            assert (REPO_ROOT / "scripts" / name).is_file()

    def test_the_snippet_is_valid_python(self) -> None:
        from entrabot.winsetup.helpers import SPONSOR_SNIPPET

        compile(SPONSOR_SNIPPET, "<sponsor snippet>", "exec")


def test_a_request_is_a_single_child_process(tmp_path: Path) -> None:
    spawn = FakeSpawn()
    seen: list[RunRequest] = []

    def answer(request: RunRequest) -> Reply:
        seen.append(request)
        return _reply()

    spawn.on(snippet("_add_sponsor"), answer)
    sync_sponsors(_helpers(tmp_path, spawn), AGENT, ["a", "b", "c"], env={})
    assert len(seen) == 1
