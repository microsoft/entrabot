"""The interactive session core (port of Session/InteractiveSession.cs).

Owns the Copilot client/session, renders streamed events to the UI, dispatches slash
commands, injects steering (from the Teams bridge + scheduler), and gates tools through
the per-caller permission policy.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, List, Optional

import copilot
from copilot.rpc import CommandsInvokeRequest, CommandsListRequest
from pydantic import BaseModel, Field

from . import banner
from .config import HarnessConfig
from . import config as cfgmod
from . import mcp_loader
from .permissions import PermissionPolicy, build_permission_handler
from .scheduler import SelfScheduler
from .teams_comms import TeamsBridge, TokenProvider, TurnContext
from .teams_tools import build_teams_tools
from .ui import UI, UiStyle

_ET = copilot.SessionEventType


class _SchedArgs(BaseModel):
    prompt: str = Field(description="The prompt to run when the schedule fires.")
    schedule: str = Field(description="e.g. 'every 30m', 'in 90s', 'daily at 09:00', '@hourly', or 5-field cron.")


class _CancelArgs(BaseModel):
    id: str = Field(description="The schedule id to cancel.")


class InteractiveSession:
    def __init__(
        self,
        config: HarnessConfig,
        root: str,
        ui: UI,
        *,
        yolo: bool = False,
        fresh: bool = False,
        autopilot: bool = True,
        token_provider: Optional[TokenProvider] = None,
        self_id: Optional[str] = None,
    ):
        self._config = config
        self._root = root
        self._ui = ui
        self._yolo = yolo
        self._fresh = fresh
        self._mode = "autopilot" if autopilot else "interactive"
        self._token_provider = token_provider
        self._self_id = self_id

        self._client: Optional[copilot.CopilotClient] = None
        self._session: Optional[copilot.CopilotSession] = None
        self._bridge: Optional[TeamsBridge] = None
        self._scheduler: Optional[SelfScheduler] = None
        self._policy = PermissionPolicy.from_config(config.permissions)

        self._idle = asyncio.Event()
        self._idle.set()
        self._inject_lock = asyncio.Lock()
        # framed steering prompt -> (caller_id, chat_id), promoted to the active turn on echo
        self._injected: dict[str, tuple] = {}
        self._ctx = TurnContext()  # caller + chat bound to the currently-running turn
        self._runtime_cmds: list = []  # SlashCommandInfo from the SDK (for /help + forwarding)
        self._streamed = False
        self._current_model = config.model
        self._reasoning = config.reasoning_effort

    # ---- lifecycle -------------------------------------------------------------------
    async def run(self) -> None:
        await self._start()
        try:
            await self._ui.run(self._handle_input, self._interrupt)
        finally:
            await self._dispose()

    async def _interrupt(self) -> None:
        """Abort the running turn (Esc/Ctrl+C)."""
        if self._session and not self._idle.is_set():
            try:
                await self._session.abort()
            except Exception as e:
                self._ui.append_line(f"interrupt failed: {e}", UiStyle.ERROR)
            self._ui.append_line("⏹ interrupted", UiStyle.WARN)

    async def _start(self) -> None:
        self._ui.banner(banner.render())
        self._ui.set_identity(self._config.name)

        self._client = copilot.CopilotClient(working_directory=self._root, log_level="error")
        await self._client.start()

        if self._token_provider:
            self._bridge = TeamsBridge(
                self._token_provider, self._config.watched_chats, self._inject, self_id=self._self_id
            )

        tools: List[Any] = []
        if self._bridge:
            tools += build_teams_tools(self._bridge, self._ctx)
        tools += self._schedule_tools()

        mcp = mcp_loader.load(self._root)

        # caller is bound to the running turn (see _on_event); operator-typed input -> None
        on_perm = build_permission_handler(
            self._policy,
            lambda: self._ctx.caller,
            yolo=self._yolo,
            confirm=self._ui.confirm if not self._yolo else None,
        )

        self._session = await self._establish(tools or None, mcp, on_perm)
        self._session.on(self._on_event)
        await self._load_commands()

        self._scheduler = SelfScheduler(self._root, self._inject)
        self._scheduler.start()
        if self._bridge:
            self._bridge.start()

        self._refresh_status()
        self._ui.append_line(
            f"ready — {self._config.name} ({self._mode}{', yolo' if self._yolo else ''})", UiStyle.SUCCESS
        )

    async def _establish(self, tools, mcp, on_perm) -> copilot.CopilotSession:
        kwargs = dict(
            on_permission_request=on_perm,
            model=self._current_model,
            reasoning_effort=self._reasoning,
            context_tier=self._config.context_tier,
            tools=tools,
            mcp_servers=mcp,
            streaming=True,
            enable_config_discovery=True,
        )
        sid = self._config.agent_id
        if not self._fresh and sid:
            try:
                return await self._client.resume_session(sid, **kwargs)
            except Exception:
                pass
        return await self._client.create_session(session_id=sid, **kwargs)

    async def _dispose(self) -> None:
        if self._bridge:
            await self._bridge.stop()
        if self._scheduler:
            await self._scheduler.stop()
        if self._session:
            try:
                await self._session.disconnect()
            except Exception:
                pass
        if self._client:
            try:
                await self._client.stop()
            except Exception:
                pass

    # ---- input + turns ---------------------------------------------------------------
    async def _handle_input(self, line: str) -> None:
        if line.startswith("/"):
            await self._handle_slash(line)
        else:
            await self._send(line)

    async def _send(self, prompt: str) -> None:
        self._idle.clear()
        self._ui.set_working(True)
        try:
            await self._session.send(prompt, agent_mode=self._mode)
        except Exception as e:
            self._ui.append_line(f"send failed: {e}", UiStyle.ERROR)
            self._idle.set()
            self._ui.set_working(False)
            return
        await self._idle.wait()

    async def _inject(self, prompt: str, caller=None, chat=None) -> None:
        """Inject steering (a Teams message or scheduled prompt) into the session.

        The caller + chat travel with the prompt so the session can bind them to the turn
        that the message kicks off (see USER_MESSAGE handling in _on_event).
        """
        if not self._session:
            return
        async with self._inject_lock:
            self._injected[prompt] = (caller, chat)
            try:
                await self._session.send(prompt, agent_mode=self._mode)
            except Exception as e:
                self._ui.append_line(f"[inject failed] {e}", UiStyle.ERROR)

    # ---- event rendering -------------------------------------------------------------
    def _on_event(self, event: copilot.SessionEvent) -> None:
        t = event.type
        d = event.data
        if t == _ET.ASSISTANT_MESSAGE_START:
            self._streamed = False
            self._ui.begin_assistant()
        elif t == _ET.ASSISTANT_MESSAGE_DELTA:
            self._streamed = True
            self._ui.append_inline(getattr(d, "delta_content", "") or "")
        elif t == _ET.ASSISTANT_MESSAGE:
            if not self._streamed:
                self._ui.append_line(getattr(d, "content", "") or "", UiStyle.ASSISTANT)
            else:
                self._ui.append_line("")  # close the streamed line
        elif t == _ET.ASSISTANT_REASONING:
            text = getattr(d, "content", "") or ""
            if text:
                self._ui.append_line(text, UiStyle.REASONING)
        elif t == _ET.TOOL_EXECUTION_START:
            name = getattr(d, "tool_name", "tool")
            args = getattr(d, "arguments", None)
            self._ui.append_line(f"⚙ {name} {_short(args)}", UiStyle.TOOL)
        elif t == _ET.TOOL_EXECUTION_COMPLETE:
            if getattr(d, "success", True) is False:
                err = getattr(getattr(d, "error", None), "message", "") or "tool failed"
                self._ui.append_line(f"  ✗ {err}", UiStyle.ERROR)
        elif t == _ET.SESSION_ERROR:
            self._ui.append_line(getattr(d, "message", "session error") or "session error", UiStyle.ERROR)
        elif t == _ET.USER_MESSAGE:
            content = getattr(d, "content", "") or ""
            if content in self._injected:
                # our own injected steering echo: bind its caller/chat to this turn + swallow
                caller, chat = self._injected.pop(content)
                self._ctx.caller, self._ctx.chat = caller, chat
        elif t == _ET.SESSION_IDLE:
            self._ui.append_line("")
            self._ctx.caller = self._ctx.chat = None  # turn over; back to operator/no caller
            self._ui.set_working(False)
            self._idle.set()

    # ---- slash commands --------------------------------------------------------------
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
            self._print_mcp()
        elif cmd == "watch":
            self._handle_watch(args)
        elif cmd == "reload":
            await self._handle_reload()
        else:
            await self._forward_command(cmd, args)

    _BUILTINS = ["help", "model", "schedules", "watch", "mcp", "reload", "clear", "exit", "quit"]

    def _print_help(self) -> None:
        for c, desc in [
            ("/help", "show this help"),
            ("/model [name]", "show or switch the model + reasoning effort"),
            ("/schedules", "list scheduled prompts"),
            ("/watch <chat-id>", "add a Teams chat to listen to"),
            ("/mcp", "list configured MCP servers"),
            ("/reload", "re-read .mcp.json and rebuild the session"),
            ("/clear", "clear the screen"),
            ("/exit", "quit"),
        ]:
            self._ui.append_line(f"  {c:<20} {desc}", UiStyle.INFO)
        if self._runtime_cmds:
            self._ui.append_line("runtime commands:", UiStyle.DIM)
            for c in sorted(self._runtime_cmds, key=lambda x: getattr(x, "name", "")):
                name = getattr(c, "name", "")
                desc = getattr(c, "description", "") or ""
                exp = " (experimental)" if getattr(c, "experimental", False) else ""
                self._ui.append_line(f"  /{name:<18} {desc}{exp}", UiStyle.INFO)

    # ---- runtime command registry (forward unknown /cmd to the SDK) -------------------
    async def _load_commands(self) -> None:
        try:
            result = await self._session.rpc.commands.list(
                CommandsListRequest(include_builtins=True, include_client_commands=True, include_skills=True)
            )
            self._runtime_cmds = list(getattr(result, "commands", []) or [])
        except Exception:
            self._runtime_cmds = []
        names = {"/" + b for b in self._BUILTINS}
        for c in self._runtime_cmds:
            names.add("/" + getattr(c, "name", ""))
            for a in getattr(c, "aliases", None) or []:
                names.add("/" + a)
        try:
            self._ui.set_commands(sorted(n for n in names if n != "/"))
        except Exception:
            pass

    async def _forward_command(self, cmd: str, args: List[str]) -> None:
        known = {getattr(c, "name", "").lower() for c in self._runtime_cmds}
        for c in self._runtime_cmds:
            known.update(a.lower() for a in (getattr(c, "aliases", None) or []))
        if cmd not in known:
            self._ui.append_line(f"unknown command: /{cmd}  (try /help)", UiStyle.WARN)
            return
        try:
            result = await self._session.rpc.commands.invoke(
                CommandsInvokeRequest(name=cmd, input=" ".join(args) or None)
            )
        except Exception as e:
            self._ui.append_line(f"/{cmd} failed: {e}", UiStyle.ERROR)
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
            for o in options:
                self._ui.append_line(f"  - {getattr(o, 'name', o)}", UiStyle.NORMAL)

    async def _handle_model(self, args: List[str]) -> None:
        if not args:
            models = await self._client.list_models()
            self._ui.append_line("models:", UiStyle.INFO)
            for m in models:
                mark = "*" if m.id == self._current_model else " "
                self._ui.append_line(f"  {mark} {m.id}  ({m.name})", UiStyle.NORMAL)
            return
        model = args[0]
        effort = args[1] if len(args) > 1 else self._reasoning
        try:
            await self._session.set_model(model, reasoning_effort=effort, context_tier=self._config.context_tier)
        except Exception as e:
            self._ui.append_line(f"could not switch model: {e}", UiStyle.ERROR)
            return
        self._current_model, self._reasoning = model, effort
        self._config.model, self._config.reasoning_effort = model, effort
        cfgmod.save(self._root, self._config)
        self._refresh_status()
        self._ui.append_line(f"model → {model} ({effort or 'default'})", UiStyle.SUCCESS)

    def _print_schedules(self) -> None:
        tasks = self._scheduler.list() if self._scheduler else []
        if not tasks:
            self._ui.append_line("(no schedules)", UiStyle.DIM)
            return
        for t in tasks:
            self._ui.append_line(f"  {t.id}  {t.spec.raw}  → {t.next_due:%Y-%m-%d %H:%M}  {t.prompt[:50]}", UiStyle.INFO)

    def _print_mcp(self) -> None:
        mcp = mcp_loader.load(self._root) or {}
        if not mcp:
            self._ui.append_line("(no MCP servers configured)", UiStyle.DIM)
            return
        for name, conf in mcp.items():
            self._ui.append_line(f"  {name}  ({conf.get('type', '?')})", UiStyle.INFO)

    def _handle_watch(self, args: List[str]) -> None:
        if not args:
            self._ui.append_line("usage: /watch <chat-id>", UiStyle.WARN)
            return
        chat = args[0]
        if self._bridge:
            self._bridge.watch(chat)
        if chat not in self._config.watched_chats:
            self._config.watched_chats.append(chat)
            cfgmod.save(self._root, self._config)
        self._ui.append_line(f"now watching {chat}", UiStyle.SUCCESS)

    async def _handle_reload(self) -> None:
        self._ui.append_line("reloading MCP + session…", UiStyle.DIM)
        confirm = self._ui.confirm if not self._yolo else None
        on_perm = build_permission_handler(
            self._policy, lambda: self._ctx.caller, yolo=self._yolo, confirm=confirm
        )
        tools: List[Any] = []
        if self._bridge:
            tools += build_teams_tools(self._bridge, self._ctx)
        tools += self._schedule_tools()
        mcp = mcp_loader.load(self._root)
        self._fresh = True
        self._session = await self._establish(tools or None, mcp, on_perm)
        self._session.on(self._on_event)
        await self._load_commands()
        self._ui.append_line("reloaded.", UiStyle.SUCCESS)

    # ---- schedule tools --------------------------------------------------------------
    def _schedule_tools(self) -> List[Any]:
        async def _add(_ctx: Any, inv: copilot.ToolInvocation) -> str:
            a = inv.arguments
            prompt = a.get("prompt") if isinstance(a, dict) else getattr(a, "prompt", "")
            schedule = a.get("schedule") if isinstance(a, dict) else getattr(a, "schedule", "")
            try:
                task = self._scheduler.add(prompt, schedule)
            except ValueError as e:
                return f"error: {e}"
            return f"scheduled {task.id}: {schedule}"

        async def _cancel(_ctx: Any, inv: copilot.ToolInvocation) -> str:
            a = inv.arguments
            tid = a.get("id") if isinstance(a, dict) else getattr(a, "id", "")
            return "cancelled" if self._scheduler.cancel(tid) else f"no such schedule {tid}"

        return [
            copilot.define_tool(
                name="schedule_task",
                description="Schedule a prompt to run later (recurring or one-shot).",
                handler=_add,
                params_type=_SchedArgs,
                skip_permission=True,
            ),
            copilot.define_tool(
                name="schedule_cancel",
                description="Cancel a scheduled prompt by id.",
                handler=_cancel,
                params_type=_CancelArgs,
                skip_permission=True,
            ),
        ]

    # ---- misc ------------------------------------------------------------------------
    def _refresh_status(self) -> None:
        self._ui.set_status(self._root, self._current_model or "default model")


def _short(args: Any) -> str:
    s = str(args) if args is not None else ""
    return (s[:60] + "…") if len(s) > 60 else s
