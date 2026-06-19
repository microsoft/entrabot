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
