"""ENTRABOT harness — a single-agent Copilot harness that routes Microsoft Teams
traffic through a Copilot session and gates tool/CLI permissions per caller.

Ported from the .NET ``teammate`` harness (copilot-team). The multi-agent MQTT
"workspace fabric" is intentionally dropped; the channel/steering transport is the
Teams layer that already lives in :mod:`entrabot.tools.teams` + :mod:`entrabot.identity`.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
