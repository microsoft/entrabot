"""Which sandbox the server runs in, what is recorded about it, and the MXC request."""

from __future__ import annotations

import base64
import json
import os
import signal
import stat
import sys
from pathlib import Path

import pytest

from entrabot.sandbox import launch, mxc
from entrabot.sandbox.policy import ContainmentPolicy


def _choose(platform: str, env: dict[str, str] | None = None, **kwargs: object) -> launch.Choice:
    defaults: dict[str, object] = {
        "which": lambda name: None,
        "exists": lambda path: False,
        "mxc_executor": lambda: None,
        "bwrap_works": lambda path: True,
        "machine": "x86_64",
    }
    defaults.update(kwargs)
    return launch.choose(platform=platform, env=env or {}, **defaults)  # type: ignore[arg-type]


def _linux_tools(name: str) -> str | None:
    """A Linux system with both programs the bubblewrap sandbox needs."""
    return f"/usr/bin/{name}" if name in ("bwrap", "xdg-dbus-proxy") else None


class TestChoice:
    def test_macos_uses_seatbelt_by_default(self) -> None:
        choice = _choose("darwin", exists=lambda path: path == launch.SANDBOX_EXEC)
        assert choice.backend == "seatbelt"

    def test_linux_uses_bwrap_when_installed(self) -> None:
        choice = _choose("linux", which=_linux_tools)
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

    def test_linux_with_a_bwrap_the_system_refuses_runs_uncontained_and_says_why(self) -> None:
        # Stock Ubuntu 24.04: AppArmor stops unprivileged user namespaces, so an installed
        # bwrap exits 1 ("setting up uid map: Permission denied"). Found in a run for real:
        # the server did not start at all.
        choice = _choose(
            "linux",
            which=_linux_tools,
            bwrap_works=lambda path: False,
        )
        assert choice.backend is None
        assert "installed" in choice.reason and "user namespaces" in choice.reason

    @pytest.mark.parametrize("mode", ["required", "bwrap"])
    def test_a_refused_bwrap_cannot_honour_a_mode_that_needs_it(self, mode: str) -> None:
        with pytest.raises(SystemExit, match="user namespaces"):
            _choose(
                "linux",
                {"ENTRABOT_CONTAINMENT": mode},
                which=_linux_tools,
                bwrap_works=lambda path: False,
            )

    def test_the_bwrap_trial_uses_the_namespaces_the_server_gets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> object:
            seen.append(cmd)
            return type("Done", (), {"returncode": 1})()

        monkeypatch.setattr(launch.subprocess, "run", fake_run)
        assert launch.bwrap_works("/usr/bin/bwrap") is False
        assert seen[0][0] == "/usr/bin/bwrap"
        assert {"--unshare-net", "--unshare-pid", "--proc"} <= set(seen[0])

    def test_a_bwrap_trial_that_cannot_run_is_a_no(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(cmd: list[str], **_kwargs: object) -> object:
            raise OSError("no such file")

        monkeypatch.setattr(launch.subprocess, "run", boom)
        assert launch.bwrap_works("/usr/bin/bwrap") is False

    def test_linux_without_the_dbus_proxy_runs_uncontained_and_says_why(self) -> None:
        # The session bus is never mounted raw (it carries systemd). Without the filtering
        # proxy the server could not read its key, so bubblewrap alone is not a sandbox here.
        choice = _choose("linux", which=lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
        assert choice.backend is None
        assert "xdg-dbus-proxy" in choice.reason

    def test_linux_without_the_dbus_proxy_cannot_honour_required(self) -> None:
        with pytest.raises(SystemExit, match="xdg-dbus-proxy"):
            _choose(
                "linux",
                {"ENTRABOT_CONTAINMENT": "required"},
                which=lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
            )

    def test_linux_on_a_machine_without_a_system_call_filter_is_not_contained(self) -> None:
        choice = _choose("linux", which=_linux_tools, machine="riscv64")
        assert choice.backend is None
        assert "system-call filter" in choice.reason

    def test_the_bwrap_version_is_read_from_what_it_prints(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd: list[str], **_kwargs: object) -> object:
            return type("Done", (), {"returncode": 0, "stdout": "bubblewrap 0.9.0\n"})()

        monkeypatch.setattr(launch.subprocess, "run", fake_run)
        assert launch.bwrap_version("/usr/bin/bwrap") == (0, 9, 0)
        assert launch.BWRAP_DISABLE_USERNS <= (0, 9, 0)

    def test_an_unreadable_bwrap_version_is_the_oldest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd: list[str], **_kwargs: object) -> object:
            return type("Done", (), {"returncode": 0, "stdout": "something else\n"})()

        monkeypatch.setattr(launch.subprocess, "run", fake_run)
        assert launch.bwrap_version("/usr/bin/bwrap") == (0, 0, 0)

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


class TestChildEnvironment:
    """The contained server gets a named part of the launcher's environment, not all of it:
    run for real, a token in the shell that started the server was readable inside."""

    def test_only_named_variables_are_passed_on(self) -> None:
        env = launch.child_env(
            {
                "PATH": "/usr/bin",
                "HOME": "/home/u",
                "LANG": "en_US.UTF-8",
                "LC_ALL": "C",
                "ENTRABOT_TENANT_ID": "t",
                "PERSONA_SATI_MCP_URL": "https://p.example",
                "EFFERENT_COPY_ENABLE": "1",
                "OTEL_SERVICE_NAME": "x",
                "GITHUB_TOKEN": "secret",
                "AWS_SECRET_ACCESS_KEY": "secret",
                "SSH_AUTH_SOCK": "/run/user/1000/ssh",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
                "XDG_RUNTIME_DIR": "/run/user/1000",
                "HTTPS_PROXY": "http://corp:8080",
                "NO_PROXY": "*",
                "PYTHONPATH": "/somewhere",
                "LD_PRELOAD": "/x.so",
            }
        )
        assert env == {
            "PATH": "/usr/bin",
            "HOME": "/home/u",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "C",
            "ENTRABOT_TENANT_ID": "t",
            "PERSONA_SATI_MCP_URL": "https://p.example",
            "EFFERENT_COPY_ENABLE": "1",
            "OTEL_SERVICE_NAME": "x",
        }

    def test_more_names_can_be_passed_on_by_name(self) -> None:
        env = launch.child_env(
            {
                "MY_TOKEN_HELPER_CONFIG": "a",
                "OTHER": "b",
                launch.EXTRA_ENV: " MY_TOKEN_HELPER_CONFIG ,",
            }
        )
        assert env["MY_TOKEN_HELPER_CONFIG"] == "a"
        assert "OTHER" not in env


class TestStatusRecord:
    def test_written_read_and_gone_with_its_process(self, tmp_path: Path) -> None:
        launch.write_status(tmp_path, {"backend": "seatbelt", "pid": os.getpid()})
        assert launch.read_status(tmp_path)["backend"] == "seatbelt"
        launch.write_status(tmp_path, {"backend": "seatbelt", "pid": 2**22 + 12345})
        assert launch.read_status(tmp_path) is None  # no such process: nothing is running

    @pytest.mark.parametrize("pid", [0, -1])
    def test_a_pid_that_names_no_single_process_is_not_running(
        self, tmp_path: Path, pid: int
    ) -> None:
        # os.kill(0 or -1, 0) on POSIX probes a whole process group and succeeds.
        launch.write_status(tmp_path, {"backend": "seatbelt", "pid": pid})
        assert launch.read_status(tmp_path) is None

    def test_unreadable_is_none(self, tmp_path: Path) -> None:
        (tmp_path / "runtime").mkdir()
        (tmp_path / "runtime" / "containment.json").write_text("{not json")
        assert launch.read_status(tmp_path) is None

    def test_the_contained_server_does_not_contain_itself_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(launch.CONTAINED_ENV, "seatbelt")
        assert launch.maybe_contain([]) is None


class TestLivenessOnWindows:
    """On Windows signal 0 is CTRL_C_EVENT: ``os.kill(pid, 0)`` sends Ctrl+C rather than
    probing, so reading the status record must never signal anything there."""

    def test_never_signals_on_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_signals(pid: int, sig: int) -> None:
            raise AssertionError(f"os.kill({pid}, {sig}) on Windows")

        asked: list[int] = []
        monkeypatch.setattr(launch.sys, "platform", "win32")
        monkeypatch.setattr(launch.os, "kill", no_signals)
        monkeypatch.setattr(launch, "_alive_windows", lambda pid: asked.append(pid) or True)
        assert launch._alive(4242) is True
        assert asked == [4242]

    def test_status_and_lock_holder_never_signal_on_windows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_signals(pid: int, sig: int) -> None:
            raise AssertionError(f"os.kill({pid}, {sig}) on Windows")

        monkeypatch.setattr(launch.sys, "platform", "win32")
        monkeypatch.setattr(launch.os, "kill", no_signals)
        monkeypatch.setattr(launch, "_alive_windows", lambda pid: False)
        launch.write_status(tmp_path, {"backend": "none", "pid": 4242, "reason": "Windows"})
        assert launch.read_status(tmp_path) is None
        assert launch.describe_running(tmp_path)[0] in ("skip", "warn")

    @pytest.mark.skipif(sys.platform != "win32", reason="the Windows process query")
    def test_the_windows_query_itself(self) -> None:
        assert launch._alive_windows(os.getpid()) is True
        assert launch._alive_windows(2**22 + 12345) is False


class TestForwardedSignals:
    def test_only_signals_this_system_has(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(signal, "SIGHUP", raising=False)  # as on Windows
        assert launch._forwarded_signals() == (signal.SIGTERM, signal.SIGINT)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX has SIGHUP")
    def test_hangup_is_forwarded_where_it_exists(self) -> None:
        assert signal.SIGHUP in launch._forwarded_signals()


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

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner and mode bits")
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

    def test_no_owner_check_means_not_trusted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Windows has no os.getuid: the trust check must refuse, not raise.
        exe = tmp_path / "lxc-exec"
        exe.write_bytes(b"binary")
        monkeypatch.delattr(mxc.os, "getuid", raising=False)
        assert mxc.find_executor(env={"ENTRABOT_MXC_EXECUTOR": str(exe)}, platform="linux") is None

    def test_none_on_windows(self) -> None:
        assert mxc.find_executor(env={}, platform="win32") is None


class TestReporting:
    def test_contained(self, tmp_path: Path) -> None:
        launch.write_status(
            tmp_path,
            {"backend": "seatbelt", "pid": os.getpid(), "allowed_hosts": ["graph.microsoft.com"]},
        )
        status, detail = launch.describe_running(tmp_path)
        assert status == "pass" and "contained by seatbelt;" in detail
        assert detail.rsplit("it can reach only ", 1)[1].split(", ") == ["graph.microsoft.com"]

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
