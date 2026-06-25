"""ConsoleUI permissions matrix now has three columns: cli · sponsor · guest."""

from entrabot.harness.ui.console import ConsoleUI


async def test_console_edit_permissions_three_columns(monkeypatch):
    ui = ConsoleUI()
    sections = [("native", [{"name": "powershell"}, {"name": "view"}])]
    state = {
        "cli_all": True, "sponsor_all": False, "guest_all": False,
        "cli": set(), "sponsor": set(), "guest": set(),
    }
    cmds = iter(["powershell sponsor", "view guest", "view cli", "yolo guest", "done"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(cmds))

    result = await ui.edit_permissions(sections, state)
    assert set(result) == {"cli_all", "sponsor_all", "guest_all", "cli", "sponsor", "guest"}
    assert result["sponsor"] == {"powershell"}
    assert result["guest"] == {"view"}
    assert result["cli"] == {"view"}  # cli is a real, independent column now
    assert result["guest_all"] is True
    assert result["cli_all"] is True
