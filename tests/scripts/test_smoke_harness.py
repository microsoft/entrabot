from __future__ import annotations

import importlib.util
import py_compile
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def smokeit_module():
    spec = importlib.util.spec_from_file_location("smokeit", REPO_ROOT / "tests/smoke/smokeit.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["smokeit"] = module
    spec.loader.exec_module(module)
    return module


def test_smoke_unix_wrapper_delegates_to_python_runner() -> None:
    script = read("tests/smoke/smokeit.sh")

    assert (REPO_ROOT / "tests" / "smoke").parts[-2:] == ("tests", "smoke")
    assert "smokeit.py" in script
    assert 'exec "$PYTHON" "$SCRIPT_DIR/smokeit.py" "$@"' in script


def test_smoke_windows_wrapper_delegates_to_python_runner() -> None:
    script = read("tests/smoke/smokeit.ps1")

    assert "smokeit.py" in script
    assert "ValueFromRemainingArguments" in script
    assert "exit $LASTEXITCODE" in script


def test_smoke_runner_requires_explicit_destructive_confirmation() -> None:
    script = read("tests/smoke/smokeit.py")

    assert "--confirm-destroy-test-resources" in script
    assert "Refusing to run without --confirm-destroy-test-resources" in script


def test_smoke_runner_supports_chat_id_and_sponsor_upn_paths() -> None:
    script = read("tests/smoke/smokeit.py")

    assert "--chat-id" in script
    assert "--sponsor-upn" in script
    assert "--test-teams" in script
    assert "create_one_on_one_chat" in script
    assert "send," in script
    assert "send_message" not in script


def test_smoke_runner_has_separate_teams_and_a365_gates() -> None:
    script = read("tests/smoke/smokeit.py")

    assert "--test-teams" in script
    assert "--test-a365" in script
    assert "ENTRABOT_ASSIGN_TEAMS_LICENSE" in script
    assert "ENTRABOT_ASSIGN_WORK_IQ_LICENSE" in script
    assert "Pass --test-teams when providing --chat-id or --sponsor-upn" in script


def test_smoke_runner_can_wait_for_teams_reply() -> None:
    script = read("tests/smoke/smokeit.py")

    assert "--wait-for-teams-reply" in script
    assert "--teams-reply-timeout" in script
    assert "--teams-reply-interval" in script
    assert "wait_for_reply" in script
    assert "teams-reply.json" in script
    assert "Pass --test-teams when using --wait-for-teams-reply" in script


def test_smoke_runner_logs_and_restores_local_state() -> None:
    script = read("tests/smoke/smokeit.py")

    assert "smoke-summary.json" in script
    assert "local-state-backup" in script
    assert "state-after-setup.json" in script
    assert "restore_local_state" in script
    assert "Last {TAIL_LINES} log lines" in script


def test_smoke_runner_deletes_and_verifies_storage_account() -> None:
    script = read("tests/smoke/smokeit.py")

    assert "delete-storage-account" in script
    assert "verify-storage-deleted" in script
    assert "az" in script
    assert "storage" in script
    assert "account" in script


def test_smoke_runner_verifies_graph_resources_deleted() -> None:
    script = read("tests/smoke/smokeit.py")

    assert "verify_graph_deleted.py" in script
    assert "graph-deletion-verification.json" in script
    assert "/users/" in script
    assert "/servicePrincipals/" in script
    assert "/applications/" in script
    assert "max_attempts = 12" in script
    assert "Graph resources still visible" in script


def test_generated_smoke_drivers_compile(tmp_path: Path) -> None:
    module = smokeit_module()
    teams_driver = tmp_path / "send_teams_smoke.py"
    verify_driver = tmp_path / "verify_graph_deleted.py"

    module.write_send_teams_driver(teams_driver)
    module.write_verify_deleted_driver(
        verify_driver,
        {
            "AGENT_USER_ID": "user-id",
            "AGENT_OBJECT_ID": "agent-id",
            "BLUEPRINT_OBJECT_ID": "blueprint-id",
        },
        tmp_path / "out.json",
    )

    py_compile.compile(str(teams_driver), doraise=True)
    py_compile.compile(str(verify_driver), doraise=True)


def test_generated_teams_driver_accepts_reply_wait_args(tmp_path: Path) -> None:
    module = smokeit_module()
    teams_driver = tmp_path / "send_teams_smoke.py"

    module.write_send_teams_driver(teams_driver)
    source = teams_driver.read_text(encoding="utf-8")

    assert 'parser.add_argument("--wait-for-reply", action="store_true")' in source
    assert 'parser.add_argument("--reply-timeout", type=int, default=300)' in source
    assert 'parser.add_argument("--reply-interval", type=int, default=5)' in source
    assert "await wait_for_reply(" in source


def test_teardown_has_smoke_preservation_flags() -> None:
    script = read("scripts/teardown.sh")

    assert "--preserve-provisioner" in script
    assert "--preserve-local-state" in script
    assert "Preserving Provisioner app" in script
    assert "Preserving local state and keychain entries" in script


def test_targeted_teardown_does_not_emit_state_delete_warnings() -> None:
    script = read("scripts/teardown.sh")

    assert "TARGETED_DEPROVISION_DONE=false" in script
    assert "Targeted teardown already removed the identity chain" in script
    assert "if [ \"$TARGETED_DEPROVISION_DONE\" = false ]; then" in script


def test_top_level_dry_run_exits_before_provisioner_token_and_deletions() -> None:
    """--dry-run in default (state-based) mode must stop before any token
    acquisition or deletion. Regression test: the top-level dry-run branch
    used to print its message and fall through to the Provisioner token
    fetch and the deletion sections below."""
    script = read("scripts/teardown.sh")

    match = re.search(r'if \[ "\$DRY_RUN" = true \]; then(.*?)elif', script, re.DOTALL)
    assert match, "top-level DRY_RUN branch not found in scripts/teardown.sh"
    dry_run_branch = match.group(1)

    assert "exit 0" in dry_run_branch, (
        "top-level dry-run branch prints its message but never exits, "
        "so control falls through to Provisioner token acquisition and deletions"
    )
