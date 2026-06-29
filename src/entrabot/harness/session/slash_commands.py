"""Slash-command dispatch: built-in ``/commands``, ``/help``, and forwarding unknown
commands to the Copilot SDK's runtime command registry (skills + client commands)."""

from __future__ import annotations

from typing import Any

from copilot.rpc import CommandsInvokeRequest, CommandsListRequest

from ..ui import UiStyle


class _SlashCommandsMixin:
    """In-session ``/command`` handling: the built-in slash commands, ``/help``, discovery of
    the Copilot SDK session's own commands (skills + client commands), and forwarding."""

    _BUILTIN_SLASH_COMMANDS = ["help", "model", "permissions", "schedules", "watch", "users",
                               "mcp", "reload", "clear", "exit", "quit"]

    async def _handle_slash(self, line: str) -> None:
        parts = line[1:].split()
        cmd = parts[0].lower() if parts else ""
        args = parts[1:]
        if cmd in ("exit", "quit"):
            self._ui.request_stop()
        elif cmd == "clear":
            self._ui.clear()
        elif cmd in ("help", "?"):
            self._print_help()
        elif cmd == "model":
            await self._handle_model(args)
        elif cmd in ("schedules", "schedule"):
            self._print_schedules()
        elif cmd == "mcp":
            await self._handle_mcp()
        elif cmd in ("permissions", "perms"):
            await self._handle_permissions()
        elif cmd == "watch":
            self._handle_watch(args)
        elif cmd == "users":
            await self._handle_users(args)
        elif cmd == "reload":
            await self._handle_reload()
        else:
            await self._forward_command(cmd, args)

    def _print_help(self) -> None:
        for label, desc in [
            ("/help", "show this help"),
            ("/model [name]", "show or switch the model + reasoning effort"),
            ("/permissions", "edit per-caller-class tool permissions (sponsor vs guest)"),
            ("/schedules", "list scheduled prompts"),
            ("/watch <chat-id>", "add a Teams chat to listen to"),
            ("/users", "list the agent's sponsors (read-only; manage in Entra)"),
            ("/mcp", "list configured + discovered MCP servers"),
            ("/reload", "re-read .mcp.json and rebuild the session"),
            ("/clear", "clear the screen"),
            ("/exit", "quit"),
        ]:
            self._ui.append_line(f"  {label:<20} {desc}", UiStyle.INFO)
        if self._runtime_cmds:
            self._ui.append_line("runtime commands:", UiStyle.DIM)
            for command in sorted(self._runtime_cmds, key=lambda c: getattr(c, "name", "")):
                name = getattr(command, "name", "")
                desc = getattr(command, "description", "") or ""
                experimental = " (experimental)" if getattr(command, "experimental", False) else ""
                self._ui.append_line(f"  /{name:<18} {desc}{experimental}", UiStyle.INFO)

    # ---- discover the Copilot SDK session's slash commands (skills + client commands) -------
    async def _discover_slash_commands(self) -> None:
        """Ask the session's command registry for its commands (builtins + client commands +
        skills) and surface them: kept in ``self._runtime_cmds`` for ``/help`` + forwarding, and
        pushed to the UI for tab-autocomplete. Called at startup and after ``/reload``."""
        try:
            result = await self._session.rpc.commands.list(
                CommandsListRequest(
                    include_builtins=True, include_client_commands=True, include_skills=True
                )
            )
            self._runtime_cmds = list(getattr(result, "commands", []) or [])
        except Exception:
            self._runtime_cmds = []
        names = {"/" + builtin for builtin in self._BUILTIN_SLASH_COMMANDS}
        for command in self._runtime_cmds:
            names.add("/" + getattr(command, "name", ""))
            for alias in getattr(command, "aliases", None) or []:
                names.add("/" + alias)
        try:
            self._ui.set_commands(sorted(name for name in names if name != "/"))
        except Exception:
            pass

    async def _forward_command(self, cmd: str, args: list[str]) -> None:
        known = {getattr(c, "name", "").lower() for c in self._runtime_cmds}
        for command in self._runtime_cmds:
            known.update(alias.lower() for alias in (getattr(command, "aliases", None) or []))
        if cmd not in known:
            self._ui.append_line(f"unknown command: /{cmd}  (try /help)", UiStyle.WARN)
            return
        try:
            result = await self._session.rpc.commands.invoke(
                CommandsInvokeRequest(name=cmd, input=" ".join(args) or None)
            )
        except Exception as error:
            self._ui.append_line(f"/{cmd} failed: {error}", UiStyle.ERROR)
            return
        await self._render_command_result(result)

    async def _render_command_result(self, result: Any) -> None:
        # SlashCommandTextResult
        text = getattr(result, "text", None)
        if text is not None:
            self._ui.append_line(text, UiStyle.INFO)
            return
        # SlashCommandAgentPromptResult -> run it as a turn
        prompt = getattr(result, "prompt", None)
        if prompt:
            await self._send(prompt)
            return
        # SlashCommandCompletedResult
        msg = getattr(result, "message", None)
        if msg:
            self._ui.append_line(msg, UiStyle.INFO)
            return
        # SlashCommandSelectSubcommandResult
        options = getattr(result, "options", None)
        if options:
            self._ui.append_line(getattr(result, "title", "choose a subcommand:"), UiStyle.INFO)
            for option in options:
                self._ui.append_line(f"  - {getattr(option, 'name', option)}", UiStyle.NORMAL)
