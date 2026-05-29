"""Adaptive Card templates for EntraBot bot messages.

Builders produce dicts ready for Bot Framework's Attachment system.
Each builder returns a dict with ``contentType`` and ``content`` keys
that can be passed directly to the outbound JSONL ``attachments`` field.

Card schema: https://adaptivecards.io/schemas/adaptive-card.json
"""

from __future__ import annotations

CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"


def _card_envelope(body: list, actions: list | None = None) -> dict:
    """Wrap body elements in an Adaptive Card envelope."""
    card: dict = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "body": body,
    }
    if actions:
        card["actions"] = actions
    return card


def _attachment(card: dict) -> dict:
    """Wrap a card dict as a Bot Framework attachment."""
    return {"contentType": CONTENT_TYPE, "content": card}


# ── Public card builders ───────────────────────────────────────────


def status_card(
    *,
    title: str = "Agent Status",
    status: str = "idle",
    message: str = "",
    details: dict | None = None,
) -> dict:
    """Build a status update card.

    Args:
        title: Card header text.
        status: Short status label (e.g. "building", "idle", "error").
        message: Human-readable status description.
        details: Optional key-value pairs shown in a fact set.
    """
    status_colors = {
        "idle": "default",
        "building": "accent",
        "running": "accent",
        "success": "good",
        "done": "good",
        "error": "attention",
        "failed": "attention",
    }
    color = status_colors.get(status.lower(), "default")

    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": title,
            "weight": "bolder",
            "size": "medium",
        },
        {
            "type": "TextBlock",
            "text": status.upper(),
            "color": color,
            "weight": "bolder",
            "spacing": "small",
        },
    ]

    if message:
        body.append({"type": "TextBlock", "text": message, "wrap": True, "spacing": "small"})

    if details:
        body.append(
            {
                "type": "FactSet",
                "facts": [{"title": k, "value": str(v)} for k, v in details.items()],
            }
        )

    return _attachment(_card_envelope(body))


def pr_card(
    *,
    title: str,
    url: str,
    state: str = "open",
    author: str = "",
    description: str = "",
    files_changed: int = 0,
) -> dict:
    """Build a pull request card.

    Args:
        title: PR title.
        url: Link to the PR.
        state: PR state (open, merged, closed).
        author: PR author display name.
        description: Short description or body excerpt.
        files_changed: Number of files changed.
    """
    state_colors = {"open": "accent", "merged": "good", "closed": "attention"}

    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": f"Pull Request: {title}",
            "weight": "bolder",
            "size": "medium",
            "wrap": True,
        },
        {
            "type": "ColumnSet",
            "columns": [
                {
                    "type": "Column",
                    "width": "auto",
                    "items": [
                        {
                            "type": "TextBlock",
                            "text": state.upper(),
                            "color": state_colors.get(state.lower(), "default"),
                            "weight": "bolder",
                        }
                    ],
                },
                {
                    "type": "Column",
                    "width": "stretch",
                    "items": [
                        {
                            "type": "TextBlock",
                            "text": f"by {author}" if author else "",
                            "isSubtle": True,
                        }
                    ],
                },
            ],
        },
    ]

    if description:
        body.append({"type": "TextBlock", "text": description, "wrap": True, "spacing": "small"})

    if files_changed:
        body.append(
            {
                "type": "FactSet",
                "facts": [{"title": "Files changed", "value": str(files_changed)}],
            }
        )

    actions = [{"type": "Action.OpenUrl", "title": "Open PR", "url": url}]

    return _attachment(_card_envelope(body, actions))


def build_card(
    *,
    pipeline: str = "",
    status: str = "succeeded",
    duration: str = "",
    url: str = "",
    commit: str = "",
    errors: list[str] | None = None,
) -> dict:
    """Build a CI/CD build result card.

    Args:
        pipeline: Pipeline or workflow name.
        status: Build status (succeeded, failed, cancelled).
        duration: Human-readable duration (e.g. "3m 42s").
        url: Link to the build run.
        commit: Short commit SHA or message.
        errors: List of error messages (for failed builds).
    """
    status_colors = {
        "succeeded": "good",
        "failed": "attention",
        "cancelled": "warning",
    }

    emoji = {"succeeded": "✅", "failed": "❌", "cancelled": "⚠️"}.get(status.lower(), "🔵")

    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": f"{emoji} Build: {pipeline}" if pipeline else f"{emoji} Build",
            "weight": "bolder",
            "size": "medium",
        },
        {
            "type": "TextBlock",
            "text": status.upper(),
            "color": status_colors.get(status.lower(), "default"),
            "weight": "bolder",
            "spacing": "small",
        },
    ]

    facts = []
    if duration:
        facts.append({"title": "Duration", "value": duration})
    if commit:
        facts.append({"title": "Commit", "value": commit})
    if facts:
        body.append({"type": "FactSet", "facts": facts})

    if errors:
        body.append(
            {
                "type": "TextBlock",
                "text": "Errors:",
                "weight": "bolder",
                "color": "attention",
                "spacing": "medium",
            }
        )
        for err in errors[:5]:
            body.append(
                {
                    "type": "TextBlock",
                    "text": f"• {err}",
                    "wrap": True,
                    "spacing": "none",
                    "isSubtle": True,
                }
            )

    actions = []
    if url:
        actions.append({"type": "Action.OpenUrl", "title": "View Build", "url": url})

    return _attachment(_card_envelope(body, actions))


def task_complete_card(
    *,
    task: str,
    summary: str = "",
    next_steps: list[str] | None = None,
) -> dict:
    """Build a task completion card.

    Args:
        task: What was completed.
        summary: Brief summary of what was done.
        next_steps: Optional list of suggested follow-up actions.
    """
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": "✅ Task Complete",
            "weight": "bolder",
            "size": "medium",
            "color": "good",
        },
        {"type": "TextBlock", "text": task, "wrap": True, "weight": "bolder"},
    ]

    if summary:
        body.append({"type": "TextBlock", "text": summary, "wrap": True})

    if next_steps:
        body.append(
            {
                "type": "TextBlock",
                "text": "Next steps:",
                "weight": "bolder",
                "spacing": "medium",
            }
        )
        for step in next_steps:
            body.append({"type": "TextBlock", "text": f"→ {step}", "wrap": True, "spacing": "none"})

    return _attachment(_card_envelope(body))
