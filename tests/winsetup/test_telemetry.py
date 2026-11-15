"""Agent 365 telemetry, switched on by setup itself, the same way as Work IQ.

Setup gives the agent's Blueprint and Agent User the delegated Agent365.Observability.OtelWrite
permission (scripts/ensure_a365_observability_permissions.py, with the Provisioner
credential), checks as the agent that its token carries the scope, and only then records the
runtime switches in ``.env``. A failure never stops setup.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from entrabot.winsetup.envfile import EnvGuard, read_env
from entrabot.winsetup.envstep import Identity
from entrabot.winsetup.helpers import RESULT_MARKER, Helpers, Paths
from entrabot.winsetup.telemetry import (
    CHECK_WAITS,
    ENV_SWITCHES,
    PERMISSIONS_REASON,
    TelemetryError,
    configure_telemetry,
    telemetry_configured,
)
from tests.winsetup.fakes import FakeSpawn, Reply, make_console, script, snippet

VENV_PYTHON = Path("/venv/bin/python")
IDENTITY = Identity(
    tenant_id="tenant",
    blueprint_app_id="bp-app",
    blueprint_object_id="bp-object",
    agent_id="agent-app",
    agent_object_id="agent-object",
    agent_user_id="agent-user",
    agent_user_upn="agent@example.test",
    provisioner_app_id="provisioner",
)


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.spawn = FakeSpawn()
        self.checks = 0
        self.honoured_after = 1  # 0: never within the wait
        self.slept: list[float] = []
        self.spawn.on(script("ensure_a365_observability_permissions.py"), Reply())
        self.spawn.on(snippet("telemetry_token_scope"), self._check)
        self.console, self.screen = make_console(
            spawn=self.spawn, log_file=tmp_path / "log.txt", platform="darwin"
        )
        self.env_file.write_text("ENTRABOT_TENANT_ID=tenant\n", encoding="utf-8")

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    def _check(self, _request: object) -> Reply:
        self.checks += 1
        ok = 0 < self.honoured_after <= self.checks
        body = {"ok": ok, "error": "" if ok else "invalid_grant: AADSTS65001: no consent"}
        return Reply(stdout=RESULT_MARKER + json.dumps(body))

    def configure(self, identity: Identity = IDENTITY) -> str:
        paths = Paths(self.root, VENV_PYTHON, self.root / "data")
        return configure_telemetry(
            paths=paths,
            helpers=Helpers(self.console, paths, {}),
            console=self.console,
            identity=identity,
            guard=EnvGuard(tolerate_failure=True),
            sleep=self.slept.append,
        )

    def calls(self) -> list[list[str]]:
        return [call.argv for call in self.spawn.calls]

    @property
    def out(self) -> str:
        return self.screen.getvalue()


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


class TestSwitchingItOn:
    def test_permissions_then_the_check_then_the_switches(self, world: World) -> None:
        assert world.configure() == "on"
        calls = world.calls()
        (grant,) = [i for i, argv in enumerate(calls) if argv[1].endswith(".py")]
        assert calls[grant][2:] == [
            "--blueprint-app-id", "bp-app",
            "--blueprint-object-id", "bp-object",
            "--agent-object-id", "agent-object",
            "--agent-user-id", "agent-user",
        ]
        check = next(i for i, argv in enumerate(calls) if argv[1] == "-c")
        assert grant < check
        env = read_env(world.env_file)
        assert {key: env[key] for key in ENV_SWITCHES} == dict.fromkeys(ENV_SWITCHES, "true")
        assert env["ENTRABOT_TENANT_ID"] == "tenant"  # every other line kept
        assert telemetry_configured(world.env_file)
        assert "Agent 365 telemetry is switched on" in world.out

    def test_it_never_uses_microsofts_agent_365_cli(self, world: World) -> None:
        world.configure()
        assert not any(Path(argv[0]).name.lower() == "a365" for argv in world.calls())

    def test_honoured_after_a_wait_is_on(self, world: World) -> None:
        world.honoured_after = 3
        assert world.configure() == "on"
        assert world.checks == 3 and len(world.slept) == 2

    def test_not_honoured_within_the_wait_is_pending_and_still_recorded(
        self, world: World
    ) -> None:
        # The grants are in place; the runtime retries its token, so telemetry starts once
        # Microsoft honours them. Setup says so instead of calling it done.
        world.honoured_after = 0
        assert world.configure() == "pending"
        assert world.checks == len(CHECK_WAITS) + 1
        assert telemetry_configured(world.env_file)

    def test_a_check_that_crashes_is_a_not_yet(self, world: World) -> None:
        world.spawn.on(snippet("telemetry_token_scope"), Reply(returncode=1, stderr="Traceback"))
        assert world.configure() == "pending"

    def test_no_token_reaches_the_screen(self, world: World) -> None:
        world.configure()
        assert "eyJ" not in world.out


class TestAFailure:
    def test_a_failed_permission_step_changes_nothing_in_env(self, world: World) -> None:
        world.spawn.on(
            script("ensure_a365_observability_permissions.py"),
            Reply(returncode=1, stderr="ERROR: create OAuth grant failed (403)"),
        )
        before = world.env_file.read_text("utf-8")
        with pytest.raises(TelemetryError) as raised:
            world.configure()
        assert raised.value.reason == PERMISSIONS_REASON
        assert "403" not in raised.value.reason
        assert world.checks == 0
        assert world.env_file.read_text("utf-8") == before
        assert not telemetry_configured(world.env_file)

    @pytest.mark.parametrize(
        "missing", ["blueprint_app_id", "blueprint_object_id", "agent_object_id", "agent_user_id"]
    )
    def test_without_the_agents_ids_nothing_is_done(self, world: World, missing: str) -> None:
        from dataclasses import replace

        with pytest.raises(TelemetryError):
            world.configure(replace(IDENTITY, **{missing: ""}))
        assert world.calls() == []


class TestTelemetryConfigured:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("", False),
            ("ENTRABOT_A365_OBSERVABILITY_ENABLED=true\n", False),
            (
                "ENTRABOT_A365_OBSERVABILITY_ENABLED=true\nENTRABOT_A365_EXPORT_ENABLED=TRUE\n",
                True,
            ),
            (
                "ENTRABOT_A365_OBSERVABILITY_ENABLED=false\nENTRABOT_A365_EXPORT_ENABLED=true\n",
                False,
            ),
        ],
    )
    def test_reads_the_switches_from_env(self, tmp_path: Path, text: str, expected: bool) -> None:
        path = tmp_path / ".env"
        path.write_text(text, encoding="utf-8")
        assert telemetry_configured(path) is expected

    def test_a_missing_file_is_off(self, tmp_path: Path) -> None:
        assert telemetry_configured(tmp_path / "absent.env") is False
