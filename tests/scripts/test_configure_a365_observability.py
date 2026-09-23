"""Explicitly authorized A365 observability onboarding."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from entrabot.harness.config import HarnessConfig, globalcfg, save

REPO_ROOT = Path(__file__).resolve().parents[2]
IDENTITY = {
    "ENTRABOT_TENANT_ID": "test-tenant",
    "ENTRABOT_BLUEPRINT_APP_ID": "test-blueprint-app",
    "ENTRABOT_BLUEPRINT_OBJECT_ID": "test-blueprint-object",
    "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT": "test-thumbprint",
    "ENTRABOT_AGENT_ID": "test-agent-app",
    "ENTRABOT_AGENT_OBJECT_ID": "test-agent-object",
    "ENTRABOT_AGENT_USER_ID": "test-agent-user",
    "ENTRABOT_AGENT_USER_UPN": "agent@example.test",
}


@pytest.fixture
def onboarding(monkeypatch, tmp_path):
    path = REPO_ROOT / "scripts" / "configure_a365_observability.py"
    spec = importlib.util.spec_from_file_location("configure_a365_observability", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(globalcfg, "global_env_path", lambda: str(tmp_path / "global.env"))
    monkeypatch.setattr(module, "SETUP_ROOT", tmp_path / "empty-setup-clone")
    monkeypatch.setattr(module, "version", lambda name: "test-sdk-version")
    monkeypatch.setattr(
        module.permissions, "get_existing_graph_token", Mock(side_effect=AssertionError("network"))
    )
    return module


@pytest.fixture
def agent_root(tmp_path):
    root = tmp_path / "agent"
    save(str(root), HarnessConfig(name="fixture-agent-name", description="Offline fixture"))
    globalcfg.write_env(str(root / ".env"), {**IDENTITY, "UNRELATED_SETTING": "preserve-me"})
    return root


def test_missing_config_fails_without_provisioning(onboarding, tmp_path, capsys):
    assert onboarding.main(["--authorize", "--agent-root", str(tmp_path)]) != 0
    error = capsys.readouterr().err
    assert "existing agent directory" in error
    assert ".entrabot" in error and "harness.json" in error
    assert "init" not in error.lower()
    onboarding.permissions.get_existing_graph_token.assert_not_called()


def test_primary_setup_clone_does_not_require_harness(onboarding, tmp_path, monkeypatch):
    clone = tmp_path / "entrabot"
    clone.mkdir()
    monkeypatch.setattr(onboarding, "SETUP_ROOT", clone)
    globalcfg.write_env(str(clone / ".env"), IDENTITY)

    target = onboarding.load_target(clone)

    assert target.root == clone
    assert target.agent_name == "agent"
    assert target.agent_id == IDENTITY["ENTRABOT_AGENT_ID"]


def test_onboarding_rejects_partial_selected_identity_instead_of_using_primary(
    onboarding, agent_root,
):
    globalcfg.write_env(
        globalcfg.agent_env_path(str(agent_root)), {"ENTRABOT_AGENT_ID": "different-agent"},
    )
    with pytest.raises(ValueError, match="ENTRABOT_AGENT_USER_ID"):
        onboarding.load_target(agent_root)


def test_onboarding_does_not_use_agent_ids_from_legacy_global_file(
    onboarding, agent_root,
):
    globalcfg.write_env(globalcfg.global_env_path(), IDENTITY)
    (agent_root / ".env").unlink()
    with pytest.raises(ValueError, match="ENTRABOT_AGENT_ID"):
        onboarding.load_target(agent_root)


def test_authorize_uses_selected_identity_and_enables_only_after_success(
    onboarding, agent_root, monkeypatch
):
    calls = []
    leaf = agent_root / ".entrabot" / ".env"
    globalcfg.write_env(
        str(leaf),
        {
            "ENTRABOT_AGENT_ID": "second-agent-app",
            "ENTRABOT_AGENT_OBJECT_ID": "second-agent-object",
            "ENTRABOT_AGENT_USER_ID": "second-agent-user",
            "ENTRABOT_AGENT_USER_UPN": "second@example.test",
            "ENTRABOT_A365_EXPORT_ENABLED": "false",
            "CUSTOM_AGENT_SETTING": "keep",
        },
    )
    before = (agent_root / ".env").read_bytes()
    monkeypatch.setattr(onboarding.permissions, "get_existing_graph_token", lambda: "test-token")

    def grant(**kwargs):
        assert globalcfg.read_env(str(leaf))["ENTRABOT_A365_EXPORT_ENABLED"] == "false"
        calls.append(kwargs)

    async def refresh(config, *, force=False):
        assert config.agent_id == "second-agent-app"
        assert config.agent_user_id == "second-agent-user"
        assert config.tenant_id == "test-tenant"
        assert config.a365_observability_enabled and config.a365_export_enabled
        assert force
        assert globalcfg.read_env(str(leaf))["ENTRABOT_A365_EXPORT_ENABLED"] == "false"

    monkeypatch.setattr(onboarding.permissions, "ensure_a365_observability_permissions", grant)
    monkeypatch.setattr(onboarding, "refresh_observability_token", refresh)

    assert onboarding.main(["--authorize", "--agent-root", str(agent_root)]) == 0

    assert calls == [
        {
            "token": "test-token",
            "blueprint_app_id": "test-blueprint-app",
            "blueprint_object_id": "test-blueprint-object",
            "agent_identity_object_id": "second-agent-object",
            "agent_user_object_id": "second-agent-user",
        }
    ]
    saved = globalcfg.read_env(str(leaf))
    assert saved["ENTRABOT_A365_OBSERVABILITY_ENABLED"] == "true"
    assert saved["ENTRABOT_A365_EXPORT_ENABLED"] == "true"
    assert saved["CUSTOM_AGENT_SETTING"] == "keep"
    assert (agent_root / ".env").read_bytes() == before


@pytest.mark.parametrize("failure", ["permissions", "token"])
def test_failed_authorization_does_not_enable_export(onboarding, agent_root, monkeypatch, failure):
    monkeypatch.setattr(onboarding.permissions, "get_existing_graph_token", lambda: "test-token")
    grant = Mock()
    refresh = AsyncMock()
    if failure == "permissions":
        grant.side_effect = onboarding.permissions.A365ObservabilityPermissionError("denied")
    else:
        refresh.side_effect = ValueError("malformed token")
    monkeypatch.setattr(onboarding.permissions, "ensure_a365_observability_permissions", grant)
    monkeypatch.setattr(onboarding, "refresh_observability_token", refresh)

    assert onboarding.main(["--authorize", "--agent-root", str(agent_root)]) != 0
    assert not (agent_root / ".entrabot" / ".env").exists()
    if failure == "permissions":
        refresh.assert_not_called()


def test_rejects_conflicting_shared_blueprint_before_authorization(onboarding, agent_root):
    globalcfg.write_env(
        globalcfg.global_env_path(),
        {
            "ENTRABOT_TENANT_ID": "another-tenant",
            "ENTRABOT_BLUEPRINT_APP_ID": "another-blueprint",
        },
    )

    assert onboarding.main(["--authorize", "--agent-root", str(agent_root)]) != 0
    onboarding.permissions.get_existing_graph_token.assert_not_called()


def test_setup_mode_exits_before_identity_provisioning():
    unix = (REPO_ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")
    windows = (REPO_ROOT / "scripts" / "setup-windows.ps1").read_text(encoding="utf-8-sig")
    assert "--prepare-alm" not in unix
    assert "--configure-a365-observability" in unix
    assert 'exec "$OBSERVABILITY_PYTHON"' in unix
    assert unix.index('exec "$OBSERVABILITY_PYTHON"') < unix.index(
        'step 5 "Creating Entra Agent Identities'
    )
    assert "[switch]$PrepareAlm" not in windows
    assert "[switch]$ConfigureA365Observability" in windows
    assert windows.index("'configure_a365_observability.py'") < windows.index(
        'Step 5 "Provisioning Entra Agent Identity"'
    )
    assert "--authorize" in unix and "'--authorize'" in windows


@pytest.mark.parametrize("existing_state", [True, False])
def test_additional_agent_stays_pending_and_primary_state_is_preserved(
    monkeypatch, tmp_path, existing_state
):
    spec = importlib.util.spec_from_file_location(
        "add_agent", REPO_ROOT / "scripts" / "add_agent.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "__file__", str(tmp_path / "scripts" / "add_agent.py"))
    state = tmp_path / ".entrabot-state.json"
    if existing_state:
        state.write_text('{"primary": "unchanged"}')
    monkeypatch.setenv("_ENTRABOT_UPN_SUFFIX", "second-fixture")
    monkeypatch.delenv("ENTRABOT_NEW_CHAIN", raising=False)
    monkeypatch.setenv("ENTRABOT_A365_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("ENTRABOT_A365_EXPORT_ENABLED", "true")
    monkeypatch.setattr(module.C, "get_existing_graph_token", lambda: "test-token")
    monkeypatch.setattr(module.C, "create_blueprint", lambda token: ("blueprint", "blueprint-obj"))
    monkeypatch.setattr(module.C, "create_agent_identity", lambda *args: ("agent", "agent-obj"))

    def create_user(*args, **kwargs):
        state.write_text('{"primary": "overwritten-by-helper"}')
        return "user", "second@example.test"

    monkeypatch.setattr(module.C, "create_agent_user", create_user)
    for name in (
        "grant_agent_identity_app_permissions",
        "grant_agent_user_consent",
        "grant_agent_user_storage_consent",
        "assign_license_to_agent_user",
    ):
        monkeypatch.setattr(module.C, name, lambda *args: None)
    printed = []
    monkeypatch.setattr(
        "builtins.print", lambda *args, **kwargs: printed.append(" ".join(map(str, args)))
    )

    assert module.main() == 0
    result = json.loads(
        next(line.removeprefix("AGENT_JSON=") for line in printed if line.startswith("AGENT_JSON="))
    )
    assert result["ENTRABOT_A365_OBSERVABILITY_ENABLED"] == "false"
    assert result["ENTRABOT_A365_EXPORT_ENABLED"] == "false"
    if existing_state:
        assert json.loads(state.read_text()) == {"primary": "unchanged"}
    else:
        assert not state.exists()


def test_observability_enablement_is_per_agent_not_shared():
    shared, agent = globalcfg.split(
        {
            **IDENTITY,
            "ENTRABOT_A365_OBSERVABILITY_ENABLED": "true",
            "ENTRABOT_A365_EXPORT_ENABLED": "true",
            "ENTRABOT_A365_SERVICE_NAMESPACE": "test.namespace",
        }
    )

    assert "ENTRABOT_A365_EXPORT_ENABLED" not in shared
    assert agent["ENTRABOT_A365_EXPORT_ENABLED"] == "true"
    assert shared["ENTRABOT_A365_SERVICE_NAMESPACE"] == "test.namespace"


def test_authorization_rerun_preserves_leaf_and_shared_settings(
    onboarding, agent_root, monkeypatch
):
    leaf = agent_root / ".entrabot" / ".env"
    globalcfg.write_env(
        str(leaf),
        {
            **IDENTITY,
            "ENTRABOT_A365_SERVICE_NAMESPACE": "fixture.namespace",
            "CUSTOM_AGENT_SETTING": "keep",
        },
    )
    monkeypatch.setattr(onboarding.permissions, "get_existing_graph_token", lambda: "test-token")
    monkeypatch.setattr(onboarding.permissions, "ensure_a365_observability_permissions", Mock())
    monkeypatch.setattr(onboarding, "refresh_observability_token", AsyncMock())

    assert onboarding.main(["--authorize", "--agent-root", str(agent_root)]) == 0
    first = leaf.read_bytes()
    assert onboarding.main(["--authorize", "--agent-root", str(agent_root)]) == 0

    assert leaf.read_bytes() == first
    assert globalcfg.read_env(str(leaf))["CUSTOM_AGENT_SETTING"] == "keep"
    assert not Path(globalcfg.global_env_path()).exists()


def test_split_config_and_home_root_are_supported(onboarding, agent_root):
    (agent_root / ".env").unlink()
    shared, agent = globalcfg.split(IDENTITY)
    globalcfg.write_env(globalcfg.global_env_path(), shared)
    globalcfg.write_env(str(agent_root / ".env"), agent)

    target = onboarding.load_target(agent_root)

    assert target.tenant_id == IDENTITY["ENTRABOT_TENANT_ID"]
    assert target.agent_id == IDENTITY["ENTRABOT_AGENT_ID"]


def test_legacy_setup_clone_supplies_only_shared_config_for_selected_agent(
    onboarding, tmp_path, monkeypatch
):
    clone = tmp_path / "setup-clone"
    clone.mkdir()
    monkeypatch.setattr(onboarding, "SETUP_ROOT", clone)
    globalcfg.write_env(
        str(clone / ".env"),
        {
            **IDENTITY,
            "ENTRABOT_AGENT_ID": "primary-agent-app",
            "ENTRABOT_AGENT_OBJECT_ID": "primary-agent-object",
            "ENTRABOT_AGENT_USER_ID": "primary-agent-user",
            "ENTRABOT_AGENT_USER_UPN": "primary@example.test",
            "ENTRABOT_A365_OBSERVABILITY_ENABLED": "true",
            "ENTRABOT_A365_EXPORT_ENABLED": "true",
        },
    )
    selected = tmp_path / "selected-agent"
    save(str(selected), HarnessConfig(name="selected-agent", description="Existing agent"))
    globalcfg.write_env(
        globalcfg.agent_env_path(str(selected)),
        {
            "ENTRABOT_AGENT_ID": "selected-agent-app",
            "ENTRABOT_AGENT_OBJECT_ID": "selected-agent-object",
            "ENTRABOT_AGENT_USER_ID": "selected-agent-user",
            "ENTRABOT_AGENT_USER_UPN": "selected@example.test",
        },
    )

    target = onboarding.load_target(selected)

    assert target.agent_id == "selected-agent-app"
    assert target.ids.agent_identity_object_id == "selected-agent-object"
    assert target.ids.agent_user_object_id == "selected-agent-user"
    assert target.config.blueprint_app_id == IDENTITY["ENTRABOT_BLUEPRINT_APP_ID"]
    assert target.config.a365_observability_enabled is False
    assert target.config.a365_export_enabled is False


def test_legacy_setup_clone_conflict_with_selected_agent_is_rejected(
    onboarding, tmp_path, monkeypatch
):
    clone = tmp_path / "setup-clone"
    clone.mkdir()
    monkeypatch.setattr(onboarding, "SETUP_ROOT", clone)
    globalcfg.write_env(str(clone / ".env"), IDENTITY)
    selected = tmp_path / "selected-agent"
    save(str(selected), HarnessConfig(name="selected-agent", description="Existing agent"))
    globalcfg.write_env(
        globalcfg.agent_env_path(str(selected)),
        {
            **IDENTITY,
            "ENTRABOT_BLUEPRINT_APP_ID": "conflicting-blueprint",
        },
    )

    with pytest.raises(ValueError, match="ENTRABOT_BLUEPRINT_APP_ID"):
        onboarding.load_target(selected)


def test_strict_config_read_surfaces_io_errors(monkeypatch):
    monkeypatch.setattr("builtins.open", Mock(side_effect=PermissionError("unreadable")))

    with pytest.raises(PermissionError):
        globalcfg.read_env("unreadable.env", strict=True)


def test_missing_a365_core_blocks_authorization(onboarding, agent_root, monkeypatch):
    monkeypatch.setattr(onboarding, "find_spec", lambda name: None)

    assert onboarding.main(["--authorize", "--agent-root", str(agent_root)]) != 0
    onboarding.permissions.get_existing_graph_token.assert_not_called()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows setup wrapper")
def test_windows_observability_rejects_provisioning_combination_before_any_setup(tmp_path):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is not installed")
    result = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(REPO_ROOT / "scripts" / "setup-windows.ps1"),
            "-ConfigureA365Observability",
            "-NewChain",
            "-AgentRoot",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )

    assert result.returncode != 0
    assert "cannot be combined" in result.stderr
    assert not (tmp_path / ".entrabot").exists()


def test_normal_setup_wrappers_persist_shared_config_after_writing_env():
    unix = (REPO_ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")
    windows = (REPO_ROOT / "scripts" / "setup-windows.ps1").read_text(encoding="utf-8-sig")

    assert "persist_global_from_env" in unix
    assert unix.index("persist_global_from_env") > unix.index("cat > .env")
    assert "persist_global_from_env" in windows
    assert windows.index("persist_global_from_env") > windows.index("Update-EnvFile $envPath")


def test_unix_wrapper_routes_only_to_onboarding_and_propagates_failure(
    tmp_path,
):
    bash = shutil.which("bash")
    if sys.platform == "win32":
        git_bash = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Git/bin/bash.exe"
        bash = str(git_bash) if git_bash.exists() else None
    if not bash:
        pytest.skip("Bash is not installed")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    setup = scripts / "setup.sh"
    shutil.copyfile(REPO_ROOT / "scripts" / "setup.sh", setup)
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\nexit 7\n', encoding="utf-8")
    python.chmod(0o700)

    result = subprocess.run(
        [
            bash,
            str(setup),
            "--configure-a365-observability",
            "--agent-root=fixture-agent-directory",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )

    assert result.returncode == 7, result.stdout + result.stderr
    assert "configure_a365_observability.py" in result.stdout
    assert "--authorize" in result.stdout
    assert "fixture-agent-directory" in result.stdout
    assert not (tmp_path / ".env").exists()


def test_unix_rejects_status_combined_with_observability(tmp_path):
    bash = shutil.which("bash")
    if sys.platform == "win32":
        git_bash = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Git/bin/bash.exe"
        bash = str(git_bash) if git_bash.exists() else None
    if not bash:
        pytest.skip("Bash is not installed")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    setup = scripts / "setup.sh"
    shutil.copyfile(REPO_ROOT / "scripts" / "setup.sh", setup)
    status = tmp_path / "status.sh"
    status.write_text("#!/usr/bin/env bash\nexit 77\n", encoding="utf-8")
    status.chmod(0o700)

    result = subprocess.run(
        [bash, str(setup), "--status", "--configure-a365-observability"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "cannot be combined" in result.stderr
