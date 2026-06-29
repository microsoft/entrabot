"""Teams integration for the harness: the polling ingress/egress bridge, the agent-facing
reply tools, and the token provider. Public surface re-exported so callers import from
``entrabot.harness.teams``."""

from .auth import make_token_provider
from .bridge import InjectFn, TeamsBridge, TokenProvider, TurnContext
from .tools import TEAMS_TOOL_NAMES, build_teams_tools

__all__ = [
    "TEAMS_TOOL_NAMES",
    "InjectFn",
    "TeamsBridge",
    "TokenProvider",
    "TurnContext",
    "build_teams_tools",
    "make_token_provider",
]
