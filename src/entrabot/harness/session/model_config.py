"""The ``/model`` command: list models and switch model + reasoning effort + context tier."""

from __future__ import annotations

from typing import Any

from .. import config as cfgmod
from ..ui import UiStyle


def _fmt_tokens(tokens: int) -> str:
    return f"{tokens / 1_000_000:g}M" if tokens >= 1_000_000 else f"{tokens // 1000}K"


def _context_tiers(model: Any):
    """Return [(tier, label), …] when the model exposes a larger long-context window, else [].

    A model has a context-window choice when its billing carries a ``context_max`` (the default
    tier's cap) below the model's ``max_context_window_tokens`` (the long-context cap).
    """
    billing = getattr(model, "billing", None)
    token_prices = getattr(billing, "token_prices", None) if billing else None
    ctx_max = getattr(token_prices, "context_max", None) if token_prices else None
    limits = getattr(getattr(model, "capabilities", None), "limits", None)
    max_window = getattr(limits, "max_context_window_tokens", None) if limits else None
    if not ctx_max or not max_window or max_window <= ctx_max:
        return []
    return [
        ("default", f"default   (up to {_fmt_tokens(ctx_max)} tokens)"),
        ("long_context", f"long_context   (up to {_fmt_tokens(max_window)} tokens)"),
    ]


class _ModelConfigMixin:
    async def _handle_model(self, args: list[str]) -> None:
        try:
            models = await self._client.list_models()
        except Exception as error:
            self._ui.append_line(f"could not list models: {error}", UiStyle.ERROR)
            return

        tier = self._config.context_tier
        if args:  # /model <name> [effort] — direct switch, no picker
            model = args[0]
            effort = args[1] if len(args) > 1 else self._reasoning
        else:  # arrow-key picker (model → reasoning effort → context window)
            labels = [
                f"{m.id}{'  ✓' if m.id == self._current_model else ''}   —   {m.name}"
                for m in models
            ]
            model_index = await self._ui.select("Select a model", labels)
            if model_index is None:
                return
            chosen = models[model_index]
            model = chosen.id
            effort = await self._select_reasoning_effort(model, chosen)
            tier = await self._select_context_tier(model, chosen, tier)

        try:
            await self._session.set_model(model, reasoning_effort=effort, context_tier=tier)
        except Exception as error:
            self._ui.append_line(f"could not switch model: {error}", UiStyle.ERROR)
            return
        self._current_model, self._reasoning = model, effort
        self._config.model = model
        self._config.reasoning_effort = effort
        self._config.context_tier = tier
        cfgmod.save(self._root, self._config)
        self._refresh_status()
        tier_note = f", {tier}" if tier and tier != "default" else ""
        self._ui.append_line(f"model → {model} ({effort or 'default'}{tier_note})", UiStyle.SUCCESS)

    async def _select_reasoning_effort(self, model_id: str, chosen: Any):
        """Prompt for the reasoning effort, falling back to the model's default if skipped."""
        efforts = list(getattr(chosen, "supported_reasoning_efforts", None) or [])
        default_effort = getattr(chosen, "default_reasoning_effort", None) or self._reasoning
        if not efforts:
            return default_effort
        effort_index = await self._ui.select(f"Reasoning effort for {model_id}", efforts)
        return efforts[effort_index] if effort_index is not None else default_effort

    async def _select_context_tier(self, model_id: str, chosen: Any, current_tier):
        """Prompt for a context window only when the model offers a larger long-context tier."""
        tiers = _context_tiers(chosen)
        if not tiers:
            return current_tier
        tier_index = await self._ui.select(
            f"Context window for {model_id}", [label for _, label in tiers]
        )
        if tier_index is None:
            return current_tier
        return tiers[tier_index][0]
