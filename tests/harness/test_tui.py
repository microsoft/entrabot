import asyncio

import pytest

pytest.importorskip("textual")  # the TUI is the default UI but tests skip if textual is absent

from entrabot.harness import banner  # noqa: E402
from entrabot.harness.ui import UiStyle  # noqa: E402
from entrabot.harness.ui.tui import TextualUI  # noqa: E402


async def test_tui_mounts_renders_autocompletes_and_submits():
    ui = TextualUI()
    app = ui._App()
    ui.app = app
    submitted = []

    async def on_sub(text):
        submitted.append(text)

    ui._on_submit = on_sub

    async with app.run_test() as pilot:
        # the surface the session drives
        ui.banner(banner.render())
        ui.set_identity("testbot")
        ui.set_commands(["/help", "/model", "/exit"])
        ui.set_status("~/repo", "claude-opus-4.8 · high")
        ui.begin_assistant()
        ui.append_inline("hello ")
        ui.append_inline("world")
        ui.append_line("")
        ui.append_line("a tool line", UiStyle.TOOL)
        ui.set_working(True)
        ui.set_working(False)

        for sel in ("#log", "#live", "#suggest", "#cwd", "#ident", "#prompt", "#hint", "#model"):
            assert app.query_one(sel) is not None

        # slash autocomplete populates on "/"
        app.query_one("#prompt").value = "/m"
        await pilot.pause()
        assert ui._suggestions == ["/model"]

        # submitting routes to on_submit
        app.query_one("#prompt").value = "hello there"
        await pilot.press("enter")
        await pilot.pause()

    assert submitted == ["hello there"]


async def test_tui_select_picker_returns_index():
    ui = TextualUI()
    app = ui._App()
    ui.app = app
    async with app.run_test() as pilot:
        task = asyncio.create_task(ui.select("Pick a model", ["alpha", "beta", "gamma"]))
        await pilot.pause()
        await pilot.pause()
        await pilot.press("down")  # highlight index 1
        await pilot.press("enter")  # select it
        await pilot.pause()
        result = await asyncio.wait_for(task, timeout=5)
    assert result == 1


async def test_model_picker_via_submit_is_navigable_and_escapable():
    """Regression: submitting a command runs in a worker, so a modal it opens (the /model
    picker) stays responsive. Awaiting on_submit inline blocked Textual's message pump and
    trapped the terminal (arrows/esc dead). This drives the real submit -> modal path."""
    ui = TextualUI()
    app = ui._App()
    ui.app = app
    picked = []

    async def on_submit(text):
        picked.append(await ui.select("Pick a model", ["alpha", "beta", "gamma"]))

    ui._on_submit = on_submit
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/model"
        await pilot.press("enter")  # submit -> worker -> push modal
        for _ in range(6):
            await pilot.pause()
        await pilot.press("down")  # arrow nav must work while the worker awaits
        await pilot.press("enter")  # select
        for _ in range(6):
            await pilot.pause()
    assert picked == [1]


async def test_permissions_matrix_per_tool_toggles_and_saves():
    ui = TextualUI()
    app = ui._App()
    ui.app = app
    # rows: 0=YOLO, 1=header(Native), 2=view, 3=edit
    sections = [("Native", [{"name": "view", "kind": "tool"}, {"name": "edit", "kind": "tool"}])]
    async with app.run_test() as pilot:
        state = {"sponsor_all": True, "guest_all": False, "sponsor": set(), "guest": set()}
        task = asyncio.create_task(ui.edit_permissions(sections, state))
        for _ in range(5):
            await pilot.pause()
        await pilot.press("s")  # YOLO row: sponsor_all -> False
        await pilot.press("down")  # -> header
        await pilot.press("down")  # -> view
        await pilot.press("g")  # view guest on
        await pilot.press("escape")  # save
        for _ in range(4):
            await pilot.pause()
        r = await asyncio.wait_for(task, timeout=5)
    assert r["sponsor_all"] is False
    assert r["guest_all"] is False
    assert "view" in r["guest"]


async def test_permissions_matrix_locked_tool_is_not_toggleable():
    ui = TextualUI()
    app = ui._App()
    ui.app = app
    # rows: 0=YOLO, 1=header, 2=entrabot_send (locked), 3=view
    sections = [("Native", [
        {"name": "entrabot_send", "kind": "tool", "locked": True},
        {"name": "view", "kind": "tool"},
    ])]
    async with app.run_test() as pilot:
        state = {"sponsor_all": False, "guest_all": False, "sponsor": set(), "guest": set()}
        task = asyncio.create_task(ui.edit_permissions(sections, state))
        for _ in range(5):
            await pilot.pause()
        await pilot.press("down")  # -> header
        await pilot.press("down")  # -> entrabot_send (locked)
        await pilot.press("s")  # try to toggle sponsor — should be ignored
        await pilot.press("g")  # try to toggle guest — should be ignored
        await pilot.press("escape")
        for _ in range(4):
            await pilot.pause()
        r = await asyncio.wait_for(task, timeout=5)
    # locked tool never enters the policy sets (it's always-allowed by the gate, not the policy)
    assert "entrabot_send" not in r["sponsor"]
    assert "entrabot_send" not in r["guest"]


async def test_form_edits_all_fields_and_submits():
    ui = TextualUI()
    app = ui._App()
    ui.app = app
    fields = [
        {"key": "--org", "label": "--org", "type": "text", "default": "", "placeholder": "ORG"},
        {"key": "--legacy", "label": "--legacy", "type": "bool", "default": False},
    ]
    async with app.run_test() as pilot:
        task = asyncio.create_task(ui.form("Install", fields))
        for _ in range(6):
            await pilot.pause()
        await pilot.press("t", "e", "s", "t")  # first input
        await pilot.press("tab")  # -> checkbox
        await pilot.press("space")  # toggle on
        await pilot.press("tab")  # -> Submit
        await pilot.press("enter")
        for _ in range(4):
            await pilot.pause()
        r = await asyncio.wait_for(task, timeout=5)
    assert r == {"--org": "test", "--legacy": True}
