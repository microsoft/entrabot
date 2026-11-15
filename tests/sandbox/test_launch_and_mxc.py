"""Which sandbox the server runs in, what is recorded about it, and the MXC request."""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

import pytest

from entrabot.sandbox import launch, mxc
from entrabot.sandbox.policy import ContainmentPolicy


def _choose(platform: str, env: dict[str, str] | None = None, **kwargs: object) -> launch.Choice:
    defaults: dict[str, object] = {
        "which": lambda name: None,
        "exists": lambda path: False,
        "mxc_executor": lambda: None,
    }
    defaults.update(kwargs)
    return launch.choose(platform=platform, env=env or {}, **defaults)  # type: ignore[arg-type]


class TestChoice:
    def test_macos_uses_seatbelt_by_default(self) -> None:
        choice = _choose("darwin", exists=lambda path: path == launch.SANDBOX_EXEC)
        assert choice.backend == "seatbelt"

    def test_linux_uses_bwrap_when_installed(self) -> None:
        choice = _choose("linux", which=lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
        assert choice.backend == "bwrap"

    def test_mxc_wins_when_its_executor_is_there(self) -> None:
        choice = _choose(
            "darwin",
            exists=lambda path: True,
            mxc_executor=lambda: "/opt/mxc/mxc-exec-mac",
        )
        assert choice.backend == "mxc"

    def test_linux_without_bwrap_runs_uncontained_and_says_why(self) -> None:
        choice = _choose("linux")
        assert choice.backend is None and "bwrap" in choice.reason

    def test_windows_is_not_contained_yet(self) -> None:
        choice = _choose("win32", mxc_executor=lambda: "C:/mxc/wxc-exec.exe")
        assert choice.backend is None and "Windows" in choice.reason

    def test_off_is_off(self) -> None:
        choice = _choose("darwin", {"ENTRABOT_CONTAINMENT": "off"}, exists=lambda path: True)
        assert choice.backend is None

    @pytest.mark.parametrize("mode", ["required", "bwrap", "mxc"])
    def test_a_mode_that_cannot_be_honoured_refuses_to_start(self, mode: str) -> None:
        with pytest.raises(SystemExit, match="refusing to start uncontained"):
            _choose("linux", {"ENTRABOT_CONTAINMENT": mode})

    def test_an_unknown_mode_refuses_to_start(self) -> None:
        with pytest.raises(SystemExit):
            _choose("darwin", {"ENTRABOT_CONTAINMENT": "maybe"})


class TestStatusRecord:
    def test_written_read_and_gone_with_its_process(self, tmp_path: Path) -> None:
        launch.write_status(tmp_path, {"backend": "seatbelt", "pid": os.getpid()})
        assert launch.read_status(tmp_path)["backend"] == "seatbelt"
        launch.write_status(tmp_path, {"backend": "seatbelt", "pid": 2**22 + 12345})
        assert launch.read_status(tmp_path) is None  # no such process: nothing is running

    def test_unreadable_is_none(self, tmp_path: Path) -> None:
        (tmp_path / "runtime").mkdir()
        (tmp_path / "runtime" / "containment.json").write_text("{not json")
        assert launch.read_status(tmp_path) is None

    def test_the_contained_server_does_not_contain_itself_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(launch.CONTAINED_ENV, "seatbelt")
        assert launch.maybe_contain([]) is None


def _policy(tmp_path: Path) -> ContainmentPolicy:
    return ContainmentPolicy(
        home=tmp_path / "home",
        read_only=(tmp_path / "repo",),
        read_write=(tmp_path / "home" / ".entrabot",),
        temp_dir=tmp_path / "home" / ".entrabot" / "tmp",
        allowed_hosts=("graph.microsoft.com",),
    )


class TestMxc:
    def test_the_request_denies_egress_and_names_the_proxy(self, tmp_path: Path) -> None:
        body = mxc.request(
            _policy(tmp_path),
            proxy_port=4242,
            inner_argv=["/venv/bin/python", "-m", "entrabot.mcp_server"],
            env={"HTTPS_PROXY": "http://127.0.0.1:4242"},
            platform="darwin",
        )
        assert body["version"] == "1.0.0" and body["containment"] == "process"
        assert body["process"]["commandLine"] == "/venv/bin/python -m entrabot.mcp_server"
        assert body["process"]["timeout"] == 0
        assert body["network"]["egress"] == {"default": "deny"}
        assert body["runtimeConfig"]["networkProxy"] == "http://127.0.0.1:4242"
        assert str(tmp_path / "repo") in body["filesystem"]["readonlyPaths"]
        assert str(tmp_path / "home" / ".entrabot") in body["filesystem"]["readwritePaths"]
        assert body["seatbelt"] == {"keychainAccess": True}

    def test_the_request_goes_in_base64_not_on_stdin(self, tmp_path: Path) -> None:
        argv = mxc.command(
            _policy(tmp_path),
            proxy_port=1,
            inner_argv=["python"],
            env={},
            executor="/opt/mxc/lxc-exec",
        )
        assert argv[:2] == ["/opt/mxc/lxc-exec", "--config-base64"]
        assert json.loads(base64.b64decode(argv[2]))["version"] == "1.0.0"

    def test_an_executor_others_can_write_is_not_used(self, tmp_path: Path) -> None:
        exe = tmp_path / "mxc-exec-mac"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        found = mxc.find_executor(env={"ENTRABOT_MXC_EXECUTOR": str(exe)}, platform="darwin")
        assert found == str(exe)
        exe.chmod(exe.stat().st_mode | stat.S_IWOTH)
        assert mxc.find_executor(env={"ENTRABOT_MXC_EXECUTOR": str(exe)}, platform="darwin") is None

    def test_a_pinned_hash_must_match(self, tmp_path: Path) -> None:
        exe = tmp_path / "lxc-exec"
        exe.write_bytes(b"binary")
        exe.chmod(0o755)
        env = {"ENTRABOT_MXC_EXECUTOR": str(exe), "ENTRABOT_MXC_SHA256": "0" * 64}
        assert mxc.find_executor(env=env, platform="linux") is None

    def test_none_on_windows(self) -> None:
        assert mxc.find_executor(env={}, platform="win32") is None


class TestReporting:
    def test_contained(self, tmp_path: Path) -> None:
        launch.write_status(
            tmp_path,
            {"backend": "seatbelt", "pid": os.getpid(), "allowed_hosts": ["graph.microsoft.com"]},
        )
        status, detail = launch.describe_running(tmp_path)
        assert status == "pass" and "seatbelt" in detail and "graph.microsoft.com" in detail

    def test_uncontained_is_a_warning_with_the_reason(self, tmp_path: Path) -> None:
        launch.write_status(
            tmp_path, {"backend": "none", "pid": os.getpid(), "reason": "bwrap is not installed"}
        )
        assert launch.describe_running(tmp_path) == (
            "warn",
            f"the running server (PID {os.getpid()}) is NOT contained: bwrap is not installed",
        )

    def test_no_server(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(launch, "_lock_holder", lambda: None)
        assert launch.describe_running(tmp_path)[0] == "skip"

    def test_a_server_from_before_containment_is_reported_as_uncontained(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(launch, "_lock_holder", lambda: 4321)
        status, detail = launch.describe_running(tmp_path)
        assert status == "warn" and "4321" in detail and "NOT contained" in detail

    def test_diagnose_reports_it(self, tmp_path: Path) -> None:
        from entrabot.preflight import CONTAINMENT_CHECK_NAME, check_containment

        launch.write_status(tmp_path, {"backend": "seatbelt", "pid": os.getpid()})
        check = check_containment(tmp_path)
        assert check.name == CONTAINMENT_CHECK_NAME and check.status == "pass"
