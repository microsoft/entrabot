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

from . import agency
from . import banner
from . import toolcatalog
from .config import HarnessConfig
from . import config as cfgmod
from . import mcp_loader
from .permissions import ToolPolicy, build_tool_gate
from .scheduler import SelfScheduler
from .teams_comms import TeamsBridge, TokenProvider, TurnContext
from .teams_tools import TEAMS_TOOL_NAMES, build_teams_tools
from .ui import UI, UiStyle

# Harness reply-path tools — locked ON for every caller (the agent's own voice; never gated).
LOCKED_TOOLS = set(TEAMS_TOOL_NAMES)

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
        self._policy = ToolPolicy.from_config(config.permissions)
        self._sponsors = self._load_sponsors()  # Entra user ids treated as sponsors
        self._catalog: list = []  # every tool/skill the session exposes (for /permissions)

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
        # Start the UI first, then run _start() inside the mounted UI (via on_start) so the
        # banner/status/streaming output actually lands instead of writing to a not-yet-built app.
        try:
            await self._ui.run(self._handle_input, self._interrupt, self._start)
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

        self._ui.start_spinner("connecting to Copilot…")
        self._client = copilot.CopilotClient(working_directory=self._root, log_level="error")
        await self._client.start()

        if self._token_provider:
            self._bridge = TeamsBridge(
                self._token_provider,
                self._config.watched_chats,
                self._inject,
                self_id=self._self_id,
                on_note=lambda m: self._ui.append_line("● " + m, UiStyle.INFO),
            )

        tools: List[Any] = []
        if self._bridge:
            tools += build_teams_tools(self._bridge, self._ctx)
        tools += self._schedule_tools()

        mcp = mcp_loader.load(self._root)

        # the running turn's caller class (sponsor/guest) gates every tool; local input -> sponsor
        gate = build_tool_gate(self._policy, self._caller_class, force_yolo=self._yolo, always_allow=LOCKED_TOOLS)

        self._ui.update_spinner("starting session…")
        self._session = await self._establish(tools or None, mcp, gate)
        self._session.on(self._on_event)
        self._ui.update_spinner("loading commands…")
        await self._load_commands()
        self._ui.update_spinner("enumerating tools…")
        try:
            self._catalog = await toolcatalog.enumerate_tools(self._session)
        except Exception:
            self._catalog = []

        self._scheduler = SelfScheduler(self._root, self._inject)
        self._scheduler.start()
        if self._bridge:
            self._bridge.start()

        self._ui.stop_spinner()
        self._ui.append_line(
            f"● {self._config.name} — {self._mode}{', yolo' if self._yolo else ''}", UiStyle.INFO
        )
        if self._bridge:
            self._ui.append_line("● Teams: enabled — auto-discovering the chats you're in…", UiStyle.INFO)
        else:
            self._ui.append_line(
                "● Teams: not configured — running console-only. Set ENTRABOT_GRAPH_TOKEN, or "
                "complete entrabot auth (client_id / agent IDs are currently empty), to enable.",
                UiStyle.WARN,
            )
        self._refresh_status()
        self._ui.append_line("● ready", UiStyle.SUCCESS)

    async def _establish(self, tools, mcp, gate) -> copilot.CopilotSession:
        kwargs = dict(
            hooks=copilot.SessionHooks(on_pre_tool_use=gate),
            model=self._current_model,
            reasoning_effort=self._reasoning,
            context_tier=self._config.context_tier,
            tools=tools,
            mcp_servers=mcp,
            system_message=self._system_message(),
            streaming=True,
            enable_config_discovery=True,
        )
        sid = self._config.agent_id
        # Only attempt resume if a prior session actually exists, so a fresh start doesn't
        # log a noisy "Session not found" from the SDK before falling back to create.
        if not self._fresh and sid:
            try:
                meta = await self._client.get_session_metadata(sid)
            except Exception:
                meta = None
            if meta is not None:
                try:
                    return await self._client.resume_session(sid, **kwargs)
                except Exception:
                    pass
        return await self._client.create_session(session_id=sid, **kwargs)

    def _system_message(self) -> dict:
        teams = (
            "New Microsoft Teams messages are delivered to you as steering updates prefixed with "
            "'[teams]'. Reply to people using the entrabot_send tool — the active chat is the one the "
            "current message came from. "
            if self._bridge
            else "Teams is not configured this run, so you are talking to your operator locally. "
        )
        return {
            "mode": "append",
            "content": (
                f"You are {self._config.name}, an agent running in the ENTRABOT harness. You are NOT "
                f"operating as an MCP server and do not need one connected. {teams}"
                "Be concise and helpful."
            ),
        }

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
        elif cmd in ("mcp", "agency-mcp", "agency"):
            await self._handle_mcp()
        elif cmd in ("permissions", "perms"):
            await self._handle_permissions()
        elif cmd == "watch":
            self._handle_watch(args)
        elif cmd == "reload":
            await self._handle_reload()
        else:
            await self._forward_command(cmd, args)

    _BUILTINS = ["help", "model", "permissions", "schedules", "watch", "mcp", "reload", "clear", "exit", "quit"]

    def _print_help(self) -> None:
        for c, desc in [
            ("/help", "show this help"),
            ("/model [name]", "show or switch the model + reasoning effort"),
            ("/permissions", "edit per-caller-class tool permissions (sponsor vs guest)"),
            ("/schedules", "list scheduled prompts"),
            ("/watch <chat-id>", "add a Teams chat to listen to"),
            ("/mcp", "manage MCP servers + browse/install agency MCPs"),
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
        try:
            models = await self._client.list_models()
        except Exception as e:
            self._ui.append_line(f"could not list models: {e}", UiStyle.ERROR)
            return

        tier = self._config.context_tier
        if args:  # /model <name> [effort] — direct switch, no picker
            model = args[0]
            effort = args[1] if len(args) > 1 else self._reasoning
        else:  # arrow-key picker (model → reasoning effort → context window)
            labels = [
                f"{m.id}{'  ✓' if m.id == self._current_model else ''}   —   {m.name}" for m in models
            ]
            idx = await self._ui.select("Select a model", labels)
            if idx is None:
                return
            chosen = models[idx]
            model = chosen.id

            efforts = list(getattr(chosen, "supported_reasoning_efforts", None) or [])
            default_effort = getattr(chosen, "default_reasoning_effort", None) or self._reasoning
            if efforts:
                ei = await self._ui.select(f"Reasoning effort for {model}", efforts)
                effort = efforts[ei] if ei is not None else default_effort
            else:
                effort = default_effort

            # Context window — only when the model exposes a larger long-context tier.
            tiers = _context_tiers(chosen)
            if tiers:
                ti = await self._ui.select(f"Context window for {model}", [lbl for _, lbl in tiers])
                if ti is not None:
                    tier = tiers[ti][0]

        try:
            await self._session.set_model(model, reasoning_effort=effort, context_tier=tier)
        except Exception as e:
            self._ui.append_line(f"could not switch model: {e}", UiStyle.ERROR)
            return
        self._current_model, self._reasoning = model, effort
        self._config.model, self._config.reasoning_effort, self._config.context_tier = model, effort, tier
        cfgmod.save(self._root, self._config)
        self._refresh_status()
        tier_note = f", {tier}" if tier and tier != "default" else ""
        self._ui.append_line(f"model → {model} ({effort or 'default'}{tier_note})", UiStyle.SUCCESS)

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
            "sponsor_all": self._policy.sponsor_all,
            "guest_all": self._policy.guest_all,
            "sponsor": set(self._policy.sponsor),
            "guest": set(self._policy.guest),
        }
        result = await self._ui.edit_permissions(sections, state)
        if result is None:
            return
        # mutate the SAME policy object so the live gate hook picks up the change immediately
        self._policy.sponsor_all = bool(result["sponsor_all"])
        self._policy.guest_all = bool(result["guest_all"])
        self._policy.sponsor = set(result["sponsor"])
        self._policy.guest = set(result["guest"])
        self._config.permissions = self._policy.to_config()
        cfgmod.save(self._root, self._config)
        s = "all" if self._policy.sponsor_all else f"{len(self._policy.sponsor)} tool(s)"
        g = "all" if self._policy.guest_all else f"{len(self._policy.guest)} tool(s)"
        self._ui.append_line(f"permissions updated → sponsor: {s}, guest: {g}", UiStyle.SUCCESS)

    def _print_schedules(self) -> None:
        tasks = self._scheduler.list() if self._scheduler else []
        if not tasks:
            self._ui.append_line("(no schedules)", UiStyle.DIM)
            return
        for t in tasks:
            self._ui.append_line(f"  {t.id}  {t.spec.raw}  → {t.next_due:%Y-%m-%d %H:%M}  {t.prompt[:50]}", UiStyle.INFO)

    async def _handle_mcp(self) -> None:
        """Unified MCP UX: installed servers + an 'Agency MCPs available' section. Selecting an
        agency MCP installs it (with a params form) or uninstalls it."""
        while True:
            mcp = mcp_loader.load(self._root) or {}
            ag = agency.available()
            catalog = agency.catalog(self._root) if ag else []

            rows: List[tuple] = [("header", None, "── Installed MCP servers ──")]
            if mcp:
                for name, conf in mcp.items():
                    is_ag = isinstance(conf, dict) and conf.get("command") == "agency"
                    t = "agency" if is_ag else (conf.get("type", "stdio") if isinstance(conf, dict) else "?")
                    rows.append(("installed", name, f"   {name}   [{t}]"))
            else:
                rows.append(("header", None, "   (none)"))
            if ag:
                rows.append(("header", None, "── Agency MCPs available ──"))
                for s in catalog:
                    mark = "  ✓ installed" if s["installed"] else ""
                    rows.append(("agency", s["name"], f"   {s['name']}{mark}   {s['description'][:46]}"))

            idx = await self._ui.select(
                "MCP servers   (enter an agency MCP to install/remove · esc when done)",
                [r[2] for r in rows],
            )
            if idx is None:
                return
            kind, name, _ = rows[idx]
            if kind != "agency":
                continue
            if name in agency.installed(self._root):
                if await self._ui.confirm("Uninstall", f"Remove agency MCP '{name}'?"):
                    agency.uninstall(self._root, name)
                    self._ui.append_line(f"removed agency MCP '{name}' — /reload to apply", UiStyle.SUCCESS)
            else:
                fields = agency.discover_params(name)
                vals = await self._ui.form(f"Install agency MCP: {name}", fields) if fields else {}
                if vals is None:
                    continue
                agency.install(self._root, name, agency.build_args(fields, vals))
                self._ui.append_line(f"installed agency MCP '{name}' — /reload to apply", UiStyle.SUCCESS)

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
        gate = build_tool_gate(self._policy, self._caller_class, force_yolo=self._yolo, always_allow=LOCKED_TOOLS)
        tools: List[Any] = []
        if self._bridge:
            tools += build_teams_tools(self._bridge, self._ctx)
        tools += self._schedule_tools()
        mcp = mcp_loader.load(self._root)
        self._fresh = True
        self._session = await self._establish(tools or None, mcp, gate)
        self._session.on(self._on_event)
        await self._load_commands()
        try:
            self._catalog = await toolcatalog.enumerate_tools(self._session)
        except Exception:
            pass
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

    # ---- caller class (sponsor vs guest) ---------------------------------------------
    def _load_sponsors(self) -> set:
        """Entra user ids that count as sponsors (from entrabot's HUMAN_USER_* config)."""
        try:
            from entrabot.config import get_config

            c = get_config()
            ids = set(getattr(c, "human_user_ids", None) or [])
            single = getattr(c, "human_user_id", None)
            if single:
                ids.add(single)
            return ids
        except Exception:
            return set()

    def _caller_class(self) -> Optional[str]:
        caller = self._ctx.caller
        if caller is None:
            return "sponsor"  # local operator typing in the terminal is trusted
        return "sponsor" if caller in self._sponsors else "guest"

    # ---- misc ------------------------------------------------------------------------
    def _refresh_status(self) -> None:
        if self._current_model:
            model = f"{self._current_model} · {self._reasoning}" if self._reasoning else self._current_model
        else:
            model = "default model"
        self._ui.set_status(self._root, model)


def _short(args: Any) -> str:
    s = str(args) if args is not None else ""
    return (s[:60] + "…") if len(s) > 60 else s


def _fmt_tokens(n: int) -> str:
    return f"{n / 1_000_000:g}M" if n >= 1_000_000 else f"{n // 1000}K"


def _context_tiers(model: Any):
    """Return [(tier, label), …] when the model exposes a larger long-context window, else [].

    A model has a context-window choice when its billing carries a ``context_max`` (the default
    tier's cap) below the model's ``max_context_window_tokens`` (the long-context cap).
    """
    billing = getattr(model, "billing", None)
    tp = getattr(billing, "token_prices", None) if billing else None
    ctx_max = getattr(tp, "context_max", None) if tp else None
    limits = getattr(getattr(model, "capabilities", None), "limits", None)
    max_window = getattr(limits, "max_context_window_tokens", None) if limits else None
    if not ctx_max or not max_window or max_window <= ctx_max:
        return []
    return [
        ("default", f"default   (up to {_fmt_tokens(ctx_max)} tokens)"),
        ("long_context", f"long_context   (up to {_fmt_tokens(max_window)} tokens)"),
    ]
