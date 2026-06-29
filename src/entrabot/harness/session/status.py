"""Status bar refresh, the ``/permissions`` editor, and ``/reload`` (rebuild the session)."""

from __future__ import annotations

from .. import config as cfgmod
from ..ui import UiStyle
from . import mcp_loader, toolcatalog
from .common import LOCKED_TOOLS


class _StatusMixin:
    def _refresh_status(self) -> None:
        if self._current_model:
            model = (
                f"{self._current_model} · {self._reasoning}"
                if self._reasoning
                else self._current_model
            )
        else:
            model = "default model"
        self._ui.set_status(self._root, model)

    async def _handle_permissions(self) -> None:
        if not self._catalog:  # enumerate on demand if startup couldn't
            self._ui.start_spinner("enumerating tools…")
            try:
                self._catalog = await toolcatalog.enumerate_tools(self._session)
            except Exception:
                pass
            self._ui.stop_spinner()
        for item in self._catalog:  # mark the harness reply-path tools as locked ON
            item["locked"] = item["name"] in LOCKED_TOOLS
        sections = toolcatalog.group_sections(self._catalog)
        state = {
            "cli_all": self._policy.cli_all,
            "sponsor_all": self._policy.sponsor_all,
            "guest_all": self._policy.guest_all,
            "cli": set(self._policy.cli),
            "sponsor": set(self._policy.sponsor),
            "guest": set(self._policy.guest),
        }
        result = await self._ui.edit_permissions(sections, state)
        if result is None:
            return
        # mutate the SAME policy object so the live gate hook picks up the change immediately
        self._policy.cli_all = bool(result["cli_all"])
        self._policy.sponsor_all = bool(result["sponsor_all"])
        self._policy.guest_all = bool(result["guest_all"])
        self._policy.cli = set(result["cli"])
        self._policy.sponsor = set(result["sponsor"])
        self._policy.guest = set(result["guest"])
        self._config.permissions = self._policy.to_config()
        cfgmod.save(self._root, self._config)

        def _summary(all_on: bool, tools: set) -> str:
            return "all" if all_on else f"{len(tools)} tool(s)"

        self._ui.append_line(
            f"permissions updated → cli: {_summary(self._policy.cli_all, self._policy.cli)}, "
            f"sponsor: {_summary(self._policy.sponsor_all, self._policy.sponsor)}, "
            f"guest: {_summary(self._policy.guest_all, self._policy.guest)}",
            UiStyle.SUCCESS,
        )

    async def _handle_reload(self) -> None:
        self._ui.append_line("reloading MCP + session…", UiStyle.DIM)
        gate = self._build_gate()
        tools = self._build_tools()
        mcp = mcp_loader.load(self._root)
        self._fresh = True
        self._session = await self._establish(tools or None, mcp, gate)
        self._session.on(self._on_event)
        await self._discover_slash_commands()
        try:
            self._catalog = await toolcatalog.enumerate_tools(self._session)
        except Exception:
            pass
        self._ui.append_line("reloaded.", UiStyle.SUCCESS)
