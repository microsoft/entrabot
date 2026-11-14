"""The per-system vocabulary: commands and flags as each launcher takes them."""

from __future__ import annotations

import pytest

from entrabot.winsetup.console import Console
from entrabot.winsetup.words import words_for


def test_windows_commands_and_flags() -> None:
    words = words_for("win32")
    assert words.setup == r"scripts\setup-windows.cmd"
    assert words.diagnose == r"scripts\setup-windows.cmd -Diagnose"
    assert words.flag("use-blueprint", "APP_ID") == "-UseBlueprint APP_ID"
    assert words.flag("yes") == "-Yes"
    assert words.start_script == "start-entrabot.cmd"
    assert words.computer == "PC"
    assert words.set_variable("NAME", "value") == [
        '  cmd.exe:     set "NAME=value"',
        "  PowerShell:  $env:NAME = 'value'",
    ]


@pytest.mark.parametrize("platform", ["darwin", "linux", "freebsd14"])
def test_macos_and_linux_commands_and_flags(platform: str) -> None:
    words = words_for(platform)
    assert words.setup == "./scripts/setup.sh"
    assert words.diagnose == "./scripts/setup.sh --diagnose"
    assert words.rotate == "./scripts/setup.sh --rotate-cert"
    assert words.teardown == "./scripts/teardown.sh"
    assert words.flag("use-blueprint", "APP_ID") == "--use-blueprint=APP_ID"
    assert words.flag("upn-suffix", "NAME") == "--with-upn-suffix=NAME"
    assert words.flag("new") == "--new"
    assert words.start_script == "start-entrabot.sh"
    assert words.set_variable("NAME", "value") == ["  export NAME='value'"]
    assert words.clear_variable("NAME") == ["  unset NAME"]
    assert not words.windows


def test_the_nouns_differ_between_macos_and_linux() -> None:
    assert words_for("darwin").computer == "Mac"
    assert words_for("darwin").key_store == "macOS Keychain"
    assert words_for("linux").computer == "computer"
    assert words_for("linux").key_store == "Linux keyring"


def test_no_posix_word_names_a_windows_command() -> None:
    words = words_for("darwin")
    text = " ".join([words.setup, words.diagnose, words.rotate, words.teardown, words.start_script])
    assert "windows" not in text.lower() and "\\" not in text and ".cmd" not in text


def test_the_console_carries_the_words_for_its_platform() -> None:
    assert Console(platform="win32").words is words_for("win32")
    assert Console(platform="linux").words is words_for("linux")
    assert Console().words is words_for()


def test_the_note_about_an_agent_user_that_is_not_carried_names_this_systems_flags() -> None:
    from entrabot.winsetup.state import IdentityChoice, not_reusing_env_agent_user

    old_bp = "99999999-2222-3333-4444-555555555555"
    new_bp = "11111111-2222-3333-4444-555555555555"
    env = {"ENTRABOT_AGENT_USER_UPN": "bot@contoso.com", "ENTRABOT_BLUEPRINT_APP_ID": old_bp}
    choice = IdentityChoice(mode="join", blueprint_app_id=new_bp)

    windows = not_reusing_env_agent_user(choice, blueprint_app_id=new_bp, env_values=env)
    posix = not_reusing_env_agent_user(
        choice, blueprint_app_id=new_bp, env_values=env, words=words_for("linux")
    )

    assert f"-UseBlueprint {new_bp} and -AgentUserUpn UPN or -UpnSuffix NAME." in windows[-1]
    assert (
        f"--use-blueprint={new_bp} and --agent-user-upn=UPN or --with-upn-suffix=NAME." in posix[-1]
    )
    assert windows[:2] == posix[:2]
