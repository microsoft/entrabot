"""The registry half of Agent 365 integration, as setup runs it: the bootstrap's line about the
setup app's permission, and scripts/register_agent_instance.py in a child, with its result
read from one marked line. Nothing reaches a tenant."""

from __future__ import annotations

import json
from pathlib import Path

from entrabot.winsetup import agentregistry
from entrabot.winsetup.agentregistry import Permission, Registration
from entrabot.winsetup.helpers import RESULT_MARKER, Helpers, Paths
from entrabot.winsetup.words import words_for
from tests.winsetup.fakes import FakeSpawn, Reply, make_console, script

VENV_PYTHON = Path("/venv/bin/python")
HELD = Permission("present")


def _reply(outcome: str, registration_id: str = "", reason: str = "") -> Reply:
    body = {"outcome": outcome, "id": registration_id, "reason": reason}
    code = 0 if outcome in ("created", "updated", "recreated", "deleted", "gone") else 1
    return Reply(returncode=code, stdout="  chatter\n" + RESULT_MARKER + json.dumps(body))


class World:
    def __init__(self, tmp_path: Path, *replies: Reply) -> None:
        self.root = tmp_path
        self.replies = list(replies)
        self.spawn = FakeSpawn()
        self.spawn.on(script("register_agent_instance.py"), self._answer)
        self.console, self.screen = make_console(spawn=self.spawn, platform="darwin")
        self.slept: list[float] = []
        self.state({})

    def _answer(self, _request: object) -> Reply:
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    def state(self, values: dict[str, str]) -> None:
        (self.root / ".entrabot-state.json").write_text(json.dumps(values), encoding="utf-8")

    @property
    def helpers(self) -> Helpers:
        return Helpers(self.console, Paths(self.root, VENV_PYTHON, self.root / "data"), {})

    def register(self, permission: Permission = HELD) -> Registration:
        return agentregistry.register_agent(
            state_file=self.root / ".entrabot-state.json",
            helpers=self.helpers,
            console=self.console,
            owner_ids=["owner-1", "owner-2"],
            permission=permission,
            sleep=self.slept.append,
        )

    def calls(self) -> list[list[str]]:
        return [call.argv for call in self.spawn.calls]


class TestThePermissionLine:
    def test_the_marker_is_the_one_the_bootstrap_prints(self) -> None:
        assert agentregistry.PERMISSION_MARKER == "ENTRABOT_REGISTRY_PERMISSION="
        assert agentregistry.GRANT_ENV == "ENTRABOT_GRANT_AGENT_REGISTRATION"

    def test_it_is_read_from_the_bootstrap_output(self) -> None:
        out = (
            "  Ensuring 26 Graph application permissions...\n"
            'ENTRABOT_REGISTRY_PERMISSION={"permission": "AgentRegistration.ReadWrite.All", '
            '"status": "failed", "reason": "admin consent failed"}\n'
            "Provisioner bootstrap complete\n"
        )
        assert agentregistry.permission_from(out) == Permission("failed", "admin consent failed")
        assert not agentregistry.permission_from(out).held
        assert Permission("granted").held and Permission("present").held

    def test_no_line_or_a_broken_one_is_unknown(self) -> None:
        assert agentregistry.permission_from("nothing here").status == "unknown"
        assert agentregistry.permission_from("ENTRABOT_REGISTRY_PERMISSION={").status == ("unknown")


class TestRegister:
    def test_it_runs_the_script_with_the_owners_and_reads_its_result(self, tmp_path: Path) -> None:
        world = World(tmp_path, _reply("created", "registration-1"))
        assert world.register() == Registration("created", "registration-1")
        ((python, path, *args),) = world.calls()
        assert Path(path).name == "register_agent_instance.py"
        assert args == [
            "--apply",
            "--owner-id",
            "owner-1",
            "--owner-id",
            "owner-2",
            "--print-result",
        ]
        assert world.register().registered

    def test_a_joined_computer_without_a_registration_runs_nothing(self, tmp_path: Path) -> None:
        world = World(tmp_path, _reply("created", "registration-1"))
        world.state({"JOINED_EXISTING_AGENT": "true"})
        assert world.register() == Registration("joined")
        assert world.calls() == []

    def test_a_joined_computer_with_a_registration_updates_it(self, tmp_path: Path) -> None:
        world = World(tmp_path, _reply("updated", "registration-1"))
        world.state({"JOINED_EXISTING_AGENT": "true", "AGENT_REGISTRATION_ID": "registration-1"})
        assert world.register().outcome == "updated"
        assert len(world.calls()) == 1

    def test_a_permission_granted_in_this_run_is_waited_for(self, tmp_path: Path) -> None:
        world = World(
            tmp_path,
            _reply("permission-missing", reason="not yet"),
            _reply("permission-missing", reason="not yet"),
            _reply("created", "registration-1"),
        )
        assert world.register(Permission("granted")).outcome == "created"
        assert world.slept == [15, 30]
        assert len(world.calls()) == 3

    def test_a_permission_that_was_not_granted_is_not_waited_for(self, tmp_path: Path) -> None:
        world = World(tmp_path, _reply("permission-missing", reason="the app lacks it"))
        result = world.register(Permission("failed", "admin consent for it failed"))
        assert result.outcome == "permission-missing"
        assert "admin consent for it failed" in result.reason
        assert world.slept == [] and len(world.calls()) == 1

    def test_waiting_ends_when_the_wait_is_over(self, tmp_path: Path) -> None:
        world = World(tmp_path, _reply("permission-missing", reason="not yet"))
        assert world.register(Permission("granted")).outcome == "permission-missing"
        assert world.slept == list(agentregistry.RETRY_WAITS)

    def test_a_refusal_is_returned_not_raised(self, tmp_path: Path) -> None:
        world = World(tmp_path, _reply("refused", reason="the registration was refused (HTTP 400)"))
        result = world.register()
        assert result.outcome == "refused" and "HTTP 400" in result.reason
        assert not result.registered

    def test_a_script_that_says_nothing_is_a_failure_with_its_last_words(
        self, tmp_path: Path
    ) -> None:
        world = World(tmp_path, Reply(returncode=1, stderr="Traceback ...\nImportError: requests"))
        result = world.register()
        assert result.outcome == "failed" and "ImportError" in result.reason


class TestDelete:
    def test_it_deletes_the_recorded_registration(self, tmp_path: Path) -> None:
        world = World(tmp_path, _reply("deleted", "registration-1"))
        assert agentregistry.delete_registration(world.helpers) == Registration(
            "deleted", "registration-1"
        )
        ((_python, _path, *args),) = world.calls()
        assert args == ["--delete", "--apply", "--print-result"]


def test_recorded_reads_the_state_file(tmp_path: Path) -> None:
    path = tmp_path / ".entrabot-state.json"
    assert agentregistry.recorded(path) == ("", False)
    path.write_text(json.dumps({"AGENT_REGISTRATION_ID": "r1"}), encoding="utf-8")
    assert agentregistry.recorded(path) == ("r1", False)
    path.write_text("{broken", encoding="utf-8")
    assert agentregistry.recorded(path) == ("", False)


def test_the_command_a_person_runs_is_this_systems() -> None:
    assert agentregistry.script_command(words_for("win32"), "--apply") == (
        r".venv\Scripts\python.exe scripts\register_agent_instance.py --apply"
    )
    assert agentregistry.script_command(words_for("linux"), "--apply") == (
        ".venv/bin/python scripts/register_agent_instance.py --apply"
    )
