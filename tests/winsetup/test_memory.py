"""Where the agent's memory lives: on this computer, or in Azure Blob Storage.

These rules come from the tests of the PowerShell memory step this replaced (they ran only
where PowerShell 7 was installed; these run everywhere).

The rule that matters most: re-running setup is how a machine gets repaired, so a machine on
cloud memory must stay there unless the person says otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from entrabot.winsetup.envfile import read_env
from entrabot.winsetup.helpers import RESULT_MARKER, Helpers, Paths
from entrabot.winsetup.memory import (
    MemoryRequest,
    MemoryResult,
    MemoryStopped,
    apply_memory,
    current_memory_mode,
    offer_memory_upload,
)
from tests.winsetup.fakes import FakeSpawn, Reply, make_console, script, snippet

CLOUD_LINES = [
    "ENTRABOT_BLOB_ENDPOINT=https://old.example.invalid",
    "ENTRABOT_BLOB_CONTAINER=old-container",
]
LOCAL_LINE = "ENTRABOT_KEEP_MEMORY_LOCAL=true"
GOOD_OUTPUT = "BLOB_ENDPOINT=https://new.example.invalid\nBLOB_CONTAINER=new\n"


class Run:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.path = tmp_path / ".env"
        self.spawn = FakeSpawn()
        self.out = ""

    def __call__(
        self,
        env: list[str] | None = None,
        *,
        choice: str = "keep",
        same_agent: bool = True,
        agent_user: str = "agent-user",
        provision: int = 0,
        provision_output: str = GOOD_OUTPUT,
        answers: dict[str, str] | None = None,
        **request: str,
    ) -> MemoryResult:
        if env is not None:
            self.path.write_text("\n".join(env) + "\n", encoding="utf-8")
        elif not self.path.exists():
            self.path.write_text("", encoding="utf-8")
        self.spawn.on(
            script("provision_blob_storage.py"),
            Reply(returncode=provision, stdout=provision_output, stderr="  • working...\n"),
        )
        console, out = make_console(spawn=self.spawn, answers=answers, assume_yes=True)
        paths = Paths(self.tmp_path, Path("/venv/python"), self.tmp_path / "data")
        try:
            return apply_memory(
                self.path,
                MemoryRequest(
                    choice=choice,  # type: ignore[arg-type]
                    tenant_id="tenant",
                    agent_user_id=agent_user,
                    **request,
                ),
                same_agent=same_agent,
                helpers=Helpers(console, paths, {}),
                console=console,
            )
        finally:
            self.out = out.getvalue()

    @property
    def lines(self) -> list[str]:
        return self.path.read_text(encoding="utf-8").splitlines()

    def values(self, key: str) -> list[str]:
        return [line.split("=", 1)[1] for line in self.lines if line.startswith(f"{key}=")]

    def effective(self, key: str) -> str | None:
        return read_env(self.path).get(key)

    @property
    def provisioned(self) -> bool:
        return any(script("provision_blob_storage.py")(call) for call in self.spawn.calls)


@pytest.fixture
def run(tmp_path: Path) -> Run:
    return Run(tmp_path)


class TestKeepWhatIsThere:
    def test_a_new_machine_defaults_to_local_memory(self, run: Run) -> None:
        result = run()
        assert run.values("ENTRABOT_KEEP_MEMORY_LOCAL") == ["true"]
        assert not run.provisioned
        assert result.mode == "local"

    def test_re_running_keeps_a_machine_on_cloud_memory(self, run: Run) -> None:
        # ENTRABOT_KEEP_MEMORY_LOCAL=true outranks the blob settings at runtime, so writing it
        # would silently move the agent to an empty local store.
        result = run(CLOUD_LINES)
        assert run.values("ENTRABOT_KEEP_MEMORY_LOCAL") == []
        assert run.values("ENTRABOT_BLOB_ENDPOINT") == ["https://old.example.invalid"]
        assert run.values("ENTRABOT_BLOB_CONTAINER") == ["old-container"]
        assert result.mode == "cloud" and result.endpoint == "https://old.example.invalid"

    def test_re_running_keeps_a_machine_on_local_memory_without_duplicating_lines(
        self, run: Run
    ) -> None:
        run()
        run()
        assert run.values("ENTRABOT_KEEP_MEMORY_LOCAL") == ["true"]

    def test_local_stays_local_when_env_has_both_settings(self, run: Run) -> None:
        # The local flag outranks the blob settings at runtime, so this machine is local today.
        result = run([*CLOUD_LINES, LOCAL_LINE])
        assert run.values("ENTRABOT_KEEP_MEMORY_LOCAL") == ["true"]
        assert run.values("ENTRABOT_BLOB_ENDPOINT") == ["https://old.example.invalid"]
        assert result.mode == "local"

    def test_padded_cloud_settings_count_as_cloud(self, run: Run) -> None:
        run(
            [
                "  ENTRABOT_BLOB_ENDPOINT = https://old.example.invalid",
                "ENTRABOT_BLOB_CONTAINER = c",
            ]
        )
        assert run.effective("ENTRABOT_KEEP_MEMORY_LOCAL") is None
        assert run.effective("ENTRABOT_BLOB_CONTAINER") == "c"

    @pytest.mark.parametrize(
        "line",
        [
            "ENTRABOT_KEEP_MEMORY_LOCAL = true",
            "ENTRABOT_KEEP_MEMORY_LOCAL=YES",
            "ENTRABOT_KEEP_MEMORY_LOCAL=1",
        ],
    )
    def test_a_local_flag_counts_however_it_is_written(self, run: Run, line: str) -> None:
        assert run([*CLOUD_LINES, line]).mode == "local"

    def test_the_first_value_of_a_repeated_flag_decides(self, run: Run) -> None:
        result = run(
            [*CLOUD_LINES, "ENTRABOT_KEEP_MEMORY_LOCAL=false", "ENTRABOT_KEEP_MEMORY_LOCAL=true"]
        )
        assert result.mode == "cloud"
        assert run.effective("ENTRABOT_BLOB_ENDPOINT") == "https://old.example.invalid"

    def test_a_different_agent_does_not_keep_the_old_blob_store(self, run: Run) -> None:
        # The container is scoped to one Agent User, so the new one couldn't reach it.
        result = run(CLOUD_LINES, same_agent=False)
        assert result.mode == "local"
        assert run.effective("ENTRABOT_KEEP_MEMORY_LOCAL") == "true"

    def test_half_a_cloud_configuration_is_not_treated_as_cloud(self, run: Run) -> None:
        assert run(CLOUD_LINES[:1]).mode == "local"
        assert run.values("ENTRABOT_KEEP_MEMORY_LOCAL") == ["true"]

    def test_legacy_marker_comments_do_not_pile_up(self, run: Run) -> None:
        run(["# ADR-005: keep agent memory local (skip cloud sync)", LOCAL_LINE, "# mine"])
        assert run.lines == ["# mine", "ENTRABOT_KEEP_MEMORY_LOCAL=true"]


class TestCurrentMode:
    def test_modes(self) -> None:
        cloud = {"ENTRABOT_BLOB_ENDPOINT": "e", "ENTRABOT_BLOB_CONTAINER": "c"}
        assert current_memory_mode(cloud) == "cloud"
        assert current_memory_mode({**cloud, "ENTRABOT_KEEP_MEMORY_LOCAL": "Yes"}) == "local"
        assert current_memory_mode({"ENTRABOT_KEEP_MEMORY_LOCAL": "true"}) == "local"
        assert current_memory_mode({}) == "unset"


class TestSwitchToLocal:
    def test_asking_for_local_is_the_way_back_from_cloud(self, run: Run) -> None:
        result = run(CLOUD_LINES, choice="local")
        assert result.mode == "local"
        assert run.effective("ENTRABOT_KEEP_MEMORY_LOCAL") == "true"
        assert not run.provisioned
        # The blob settings stay, so switching back later needs no new storage.
        assert run.effective("ENTRABOT_BLOB_ENDPOINT") == "https://old.example.invalid"


class TestCloud:
    def test_opting_in_on_a_new_machine_writes_the_blob_settings(self, run: Run) -> None:
        result = run(choice="cloud")
        assert run.values("ENTRABOT_BLOB_ENDPOINT") == ["https://new.example.invalid"]
        assert run.values("ENTRABOT_BLOB_CONTAINER") == ["new"]
        assert run.values("ENTRABOT_KEEP_MEMORY_LOCAL") == []
        assert result.mode == "cloud" and result.provisioned

    def test_opting_in_replaces_earlier_memory_lines(self, run: Run) -> None:
        # The local flag outranks the blob settings, so an appended line would leave the new
        # store unused.
        run([*CLOUD_LINES, LOCAL_LINE], choice="cloud")
        assert run.values("ENTRABOT_KEEP_MEMORY_LOCAL") == []
        assert run.values("ENTRABOT_BLOB_ENDPOINT") == ["https://new.example.invalid"]
        assert run.values("ENTRABOT_BLOB_CONTAINER") == ["new"]

    def test_opting_in_replaces_a_padded_local_flag(self, run: Run) -> None:
        run(["ENTRABOT_KEEP_MEMORY_LOCAL = true"], choice="cloud")
        assert run.effective("ENTRABOT_KEEP_MEMORY_LOCAL") is None
        assert run.effective("ENTRABOT_BLOB_ENDPOINT") == "https://new.example.invalid"

    def test_the_provisioner_gets_the_agent_and_every_storage_choice(self, run: Run) -> None:
        run(
            choice="cloud",
            storage_account="mycorpstg",
            container="winagent-mem",
            location="westus2",
            subscription_id="sub-1",
        )
        argv = next(c.argv for c in run.spawn.calls if script("provision_blob_storage.py")(c))
        options = dict(zip(argv[2::2], argv[3::2], strict=False))
        assert options == {
            "--tenant-id": "tenant",
            "--agent-user-object-id": "agent-user",
            "--location": "westus2",
            "--subscription": "sub-1",
            "--with-storage-account": "mycorpstg",
            "--with-container": "winagent-mem",
        }

    def test_create_new_storage_is_passed_as_a_flag(self, run: Run) -> None:
        run(choice="cloud", create_new_storage=True)  # type: ignore[arg-type]
        argv = next(c.argv for c in run.spawn.calls if script("provision_blob_storage.py")(c))
        assert argv[-1] == "--create-new-storage"

    def test_the_provisioners_progress_is_shown_and_its_stdout_is_not(self, run: Run) -> None:
        run(choice="cloud")
        assert "working..." in run.out and "BLOB_ENDPOINT=" not in run.out

    FAILURES = [
        {"provision": 1, "provision_output": ""},
        {"provision": 0, "provision_output": ""},
        {"provision": 0, "provision_output": "BLOB_ENDPOINT=https://new.example.invalid\n"},
        {"agent_user": ""},
    ]
    IDS = ["provisioning-fails", "no-output", "half-output", "no-agent-user"]

    @pytest.mark.parametrize("failure", FAILURES, ids=IDS)
    def test_a_machine_on_cloud_memory_stays_there_when_provisioning_fails(
        self, run: Run, failure: dict
    ) -> None:
        result = run(CLOUD_LINES, choice="cloud", **failure)
        assert run.values("ENTRABOT_KEEP_MEMORY_LOCAL") == []
        assert run.values("ENTRABOT_BLOB_ENDPOINT") == ["https://old.example.invalid"]
        assert run.values("ENTRABOT_BLOB_CONTAINER") == ["old-container"]
        assert result.mode == "cloud" and result.problem

    @pytest.mark.parametrize("failure", FAILURES, ids=IDS)
    def test_a_new_machine_falls_back_to_local_and_says_so(self, run: Run, failure: dict) -> None:
        result = run(choice="cloud", **failure)
        assert run.values("ENTRABOT_KEEP_MEMORY_LOCAL") == ["true"]
        assert run.values("ENTRABOT_BLOB_ENDPOINT") == []
        assert result.mode == "local" and result.problem  # never silently
        assert "[!!]" in run.out

    def test_the_person_can_refuse_the_fallback_and_stop(self, run: Run) -> None:
        with pytest.raises(MemoryStopped):
            run(choice="cloud", provision=1, provision_output="", answers={"memory_fallback": "no"})
        assert run.values("ENTRABOT_KEEP_MEMORY_LOCAL") == []


class TestUploadOffer:
    def _helpers(self, tmp_path: Path, spawn: FakeSpawn, answers: dict[str, str] | None = None):
        console, out = make_console(spawn=spawn, answers=answers, assume_yes=True)
        paths = Paths(tmp_path, Path("/venv/python"), tmp_path / "data")
        return Helpers(console, paths, {}), console, out

    def _probe(self, spawn: FakeSpawn, data_bytes: int, persona_bytes: int) -> None:
        payload = {
            "data_dir": "/d",
            "data_bytes": data_bytes,
            "persona_dir": "/p",
            "persona_bytes": persona_bytes,
        }
        spawn.on(
            snippet("claude_code_memory_dir"), Reply(stdout=RESULT_MARKER + json.dumps(payload))
        )

    def test_nothing_local_means_nothing_is_asked(self, tmp_path: Path) -> None:
        spawn = FakeSpawn()
        self._probe(spawn, 0, 0)
        helpers, console, _ = self._helpers(tmp_path, spawn)
        assert offer_memory_upload(helpers, console) is None
        assert not any(snippet("migrate_local_to_backend")(call) for call in spawn.calls)

    def test_the_default_is_not_to_upload(self, tmp_path: Path) -> None:
        spawn = FakeSpawn()
        self._probe(spawn, 2048, 0)
        helpers, console, _ = self._helpers(tmp_path, spawn)
        assert offer_memory_upload(helpers, console) is None
        assert not any(snippet("migrate_local_to_backend")(call) for call in spawn.calls)

    def test_saying_yes_uploads_both_trees_and_reports(self, tmp_path: Path) -> None:
        spawn = FakeSpawn()
        self._probe(spawn, 2048, 1024)
        report = {"copied": 3, "skipped": 1, "bytes": 3072, "errors": [], "error_count": 0}
        spawn.on(
            snippet("migrate_local_to_backend"), Reply(stdout=RESULT_MARKER + json.dumps(report))
        )
        helpers, console, out = self._helpers(tmp_path, spawn, {"memory_upload": "yes"})
        assert offer_memory_upload(helpers, console) is True
        upload = next(c for c in spawn.calls if snippet("migrate_local_to_backend")(c))
        assert upload.argv[3:] == ["/d", "/p"]  # paths travel as arguments, never in the code
        assert "3 file" in out.getvalue()

    def test_a_failed_upload_is_reported_as_failed(self, tmp_path: Path) -> None:
        spawn = FakeSpawn()
        self._probe(spawn, 2048, 0)
        report = {
            "copied": 0,
            "skipped": 0,
            "bytes": 0,
            "errors": [["interactions.jsonl", "403 AuthorizationFailure"]],
            "error_count": 1,
        }
        spawn.on(
            snippet("migrate_local_to_backend"),
            Reply(returncode=2, stdout=RESULT_MARKER + json.dumps(report)),
        )
        helpers, console, out = self._helpers(tmp_path, spawn, {"memory_upload": "yes"})
        assert offer_memory_upload(helpers, console) is False
        assert "AuthorizationFailure" in out.getvalue()
