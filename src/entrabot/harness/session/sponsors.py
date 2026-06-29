"""The agent's sponsors + caller classification.

Sponsorship comes from the Entra Agent-Identity sponsor relationship (``identity.sponsors``)
— the same source the entrabot body gates on. ``/users`` is a read-only view of it; the
caller class (cli/sponsor/guest) the permission gate uses is derived from the same set.
"""

from __future__ import annotations

import asyncio

from ..ui import UiStyle


class _SponsorsMixin:
    async def _handle_users(self, args: list[str]) -> None:
        """List the agent's sponsors — the Entra Agent-Identity sponsor relationship
        (identity.sponsors), the same source the entrabot body gates on. Read-only: add/remove
        sponsors directly in Entra (or via scripts/add_agent_sponsor.py)."""
        records = await asyncio.to_thread(self._sponsor_records)
        if not records:
            self._ui.append_line(
                "no sponsors (manage in Entra → the agent's sponsor relationship)", UiStyle.INFO)
            return
        self._ui.append_line(f"agent sponsors ({len(records)}):", UiStyle.INFO)
        for record in records:
            label = record.mail or record.user_principal_name or record.user_id
            self._ui.append_line(f"  • {label}", UiStyle.INFO)

    def _sponsor_records(self):
        """The Agent Identity's current sponsors (enriched with emails for display) via the core
        read helper. Blocking Graph call — invoke off-thread. [] on any failure (incl. none)."""
        try:
            from entrabot.config import get_config
            from entrabot.identity.sponsors import fetch_agent_identity_sponsors
            from entrabot.tools.teams import acquire_agent_user_token

            return fetch_agent_identity_sponsors(
                get_config(), user_token_provider=acquire_agent_user_token)
        except Exception:
            return []

    def _load_sponsors(self) -> set:
        """The Agent Identity's sponsors — the SAME Entra relationship the entrabot body gates on
        (identity.sponsors.load_agent_identity_sponsor_gate), matched by user_id for the caller
        class. Blocking Graph call (run off-thread via _refresh_sponsors). Fail-safe: empty set
        (every Teams caller is a guest) so a Graph/token failure can't silently over-grant."""
        try:
            from entrabot.config import get_config
            from entrabot.identity.sponsors import load_agent_identity_sponsor_gate

            return set(load_agent_identity_sponsor_gate(get_config()).user_ids)
        except Exception:
            return set()

    async def _refresh_sponsors(self) -> None:
        """(Re)load the Agent-ID sponsor set off the event loop. The gate reads self._sponsors per
        tool call, so this applies live once it returns."""
        self._sponsors = await asyncio.to_thread(self._load_sponsors)

    def _caller_class(self) -> str | None:
        caller = self._ctx.caller
        if caller is None:
            return "cli"  # local operator typing in the terminal — its own caller class
        return "sponsor" if caller in self._sponsors else "guest"
