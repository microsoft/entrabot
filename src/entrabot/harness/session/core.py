"""The interactive session core (port of Session/InteractiveSession.cs).

Owns the Copilot client/session and the session lifecycle (connect → establish → dispose).
The per-concern behavior (event rendering, slash commands, the /model · /mcp · /permissions
panels, sponsors, scheduling) lives in the mixins this class is composed from.
"""

from __future__ import annotations

import asyncio
from typing import Any

import copilot

from ..config import HarnessConfig
from ..scheduler import SelfScheduler
from ..teams import TeamsBridge, TokenProvider, TurnContext, build_teams_tools
from ..ui import UI, UiStyle, banner
from . import mcp_loader, toolcatalog
from .common import LOCKED_TOOLS
from .events import _EventsMixin
from .mcp_panel import _McpPanelMixin
from .model_config import _ModelConfigMixin
from .permissions import ToolPolicy, build_tool_gate
from .scheduling import _SchedulingMixin
from .slash_commands import _SlashCommandsMixin
from .sponsors import _SponsorsMixin
from .status import _StatusMixin


class InteractiveSession(
    _EventsMixin,
    _SlashCommandsMixin,
    _ModelConfigMixin,
    _McpPanelMixin,
    _SponsorsMixin,
    _SchedulingMixin,
    _StatusMixin,
):
    def __init__(
        self,
        config: HarnessConfig,
        root: str,
        ui: UI,
        *,
        yolo: bool = False,
        fresh: bool = False,
        autopilot: bool = True,
        token_provider: TokenProvider | None = None,
        self_id: str | None = None,
    ):
        self._config = config
        self._root = root
        self._ui = ui
        self._yolo = yolo
        self._fresh = fresh
        self._mode = "autopilot" if autopilot else "interactive"
        self._token_provider = token_provider
        self._self_id = self_id

        self._client: copilot.CopilotClient | None = None
        self._session: copilot.CopilotSession | None = None
        self._bridge: TeamsBridge | None = None
        self._scheduler: SelfScheduler | None = None
        self._policy = ToolPolicy.from_config(config.permissions)
        self._sponsors: set = set()  # Agent-ID sponsor user ids; loaded async in _start()
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
            except Exception as error:
                self._ui.append_line(f"interrupt failed: {error}", UiStyle.ERROR)
            self._ui.append_line("⏹ interrupted", UiStyle.WARN)

    async def _start(self) -> None:
        self._ui.banner(banner.render())
        self._ui.set_identity(self._config.name)

        self._ui.start_spinner("connecting to Copilot…")
        self._client = copilot.CopilotClient(working_directory=self._root, log_level="error")
        await self._client.start()

        if self._token_provider:
            await self._init_teams_bridge()

        tools = self._build_tools()
        mcp = mcp_loader.load(
            self._root,
            on_skip=lambda name: self._ui.append_line(
                f"● skipped MCP '{name}' — the harness already provides Teams tools + polling",
                UiStyle.INFO),
        )
        gate = self._build_gate()

        self._ui.update_spinner("starting session…")
        self._session = await self._establish(tools or None, mcp, gate)
        self._session.on(self._on_event)
        self._ui.update_spinner("discovering commands…")
        await self._discover_slash_commands()
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
        self._announce_startup()

    async def _init_teams_bridge(self) -> None:
        # Load the Agent-ID sponsor set before the Teams bridge starts pushing turns, so the
        # caller-class gate is correct from the first inbound message.
        self._ui.update_spinner("loading sponsors…")
        await self._refresh_sponsors()
        self._bridge = TeamsBridge(
            self._token_provider,
            self._config.watched_chats,
            self._inject,
            self_id=self._self_id,
            on_note=lambda m: self._ui.append_line("● " + m, UiStyle.INFO),
        )

    def _build_tools(self) -> list[Any]:
        """The harness-provided tools for a session: the Teams reply path + the schedule tools."""
        tools: list[Any] = []
        if self._bridge:
            tools += build_teams_tools(self._bridge, self._ctx)
        tools += self._schedule_tools()
        return tools

    def _build_gate(self):
        """The per-tool permission hook bound to the current caller class (cli/sponsor/guest)."""
        return build_tool_gate(
            self._policy, self._caller_class, force_yolo=self._yolo, always_allow=LOCKED_TOOLS
        )

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
            # Keep the SDK's MCP/skill auto-discovery on (it finds the user's github MCP, skills,
            # etc.), but block the entrabot MCP *body* — the SDK discovers it from the user's
            # ~/.copilot/mcp-config.json, which our mcp_servers filter can't reach, and it would
            # duplicate the harness's own Teams reply path. Excluded by tool name (the SDK has no
            # per-server disable); source-derived so it can't drift.
            enable_config_discovery=True,
            excluded_tools=self._body_excluded_tools(),
        )
        session_id = self._config.agent_id
        # Only attempt resume if a prior session actually exists, so a fresh start doesn't
        # log a noisy "Session not found" from the SDK before falling back to create.
        if not self._fresh and session_id:
            try:
                metadata = await self._client.get_session_metadata(session_id)
            except Exception:
                metadata = None
            if metadata is not None:
                try:
                    return await self._client.resume_session(session_id, **kwargs)
                except Exception:
                    pass
        return await self._client.create_session(session_id=session_id, **kwargs)

    def _system_message(self) -> dict:
        teams = (
            "New Microsoft Teams messages are delivered to you as steering updates prefixed with "
            "'[teams]'. Reply to people using the entrabot_send tool — the active chat is the one the "
            "current message came from. "
            if self._bridge
            else "Teams is not configured this run, so you are talking to your operator locally. "
        )
        cli = (
            "Local terminal input from your operator is prefixed with '[cli]'. Answer it in the "
            "terminal as a normal reply — do NOT call entrabot_send or post to Teams for a '[cli]' "
            "turn. "
        )
        return {
            "mode": "append",
            "content": (
                f"You are {self._config.name}, an agent running in the ENTRABOT harness. You are NOT "
                f"operating as an MCP server and do not need one connected. {teams}{cli}"
                "Be concise and helpful."
            ),
        }

    def _announce_startup(self) -> None:
        self._ui.append_line(
            f"● {self._config.name} — {self._mode}{', yolo' if self._yolo else ''}", UiStyle.INFO
        )
        if self._bridge:
            self._ui.append_line(
                "● Teams: enabled — auto-discovering the chats you're in…", UiStyle.INFO)
        else:
            self._ui.append_line(
                "● Teams: not configured — running console-only. Set ENTRABOT_GRAPH_TOKEN, or "
                "complete entrabot auth (client_id / agent IDs are currently empty), to enable.",
                UiStyle.WARN,
            )
        self._refresh_status()
        self._ui.append_line("● ready", UiStyle.SUCCESS)

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
