"""Adaptive Card templates for EntraBot Teams messages.

Provides card builders for real-time tool activity, task status,
and build results. Cards follow the Adaptive Card 1.4 schema
and are compatible with Microsoft Graph API chat message attachments.

Usage:
    from entrabot.tools.cards import tool_activity_card, card_attachment

    card = tool_activity_card(tool_name="read_file", status="complete", detail="42 lines")
    attachment = card_attachment(card)
    # Pass attachment to send() via the attachments parameter
"""

from __future__ import annotations

_MAX_DETAIL_LENGTH = 200

_STATUS_EMOJI = {
    "running": "\u23f3",  # hourglass
    "complete": "\u2705",  # green check
    "error": "\u274c",  # red X
}

_STATUS_COLOR = {
    "running": "accent",
    "complete": "good",
    "error": "attention",
}


def card_attachment(card: dict) -> dict:
    """Wrap an Adaptive Card dict into Graph API attachment format.

    Returns a dict suitable for inclusion in the ``attachments`` list
    of a Graph ``POST /chats/{id}/messages`` payload.
    """
    import json as _json

    return {
        "id": "card1",
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": _json.dumps(card),
    }


def tool_activity_card(
    *,
    tool_name: str,
    status: str,
    detail: str,
) -> dict:
    """Card showing Claude Code tool activity in real-time.

    Args:
        tool_name: Name of the tool (e.g., "read_file", "git_log").
        status: One of "running", "complete", "error".
        detail: Short description of what the tool is doing/did.
    """
    emoji = _STATUS_EMOJI.get(status, "\u2753")  # question mark fallback
    color = _STATUS_COLOR.get(status, "default")
    truncated = detail[:_MAX_DETAIL_LENGTH] + ("..." if len(detail) > _MAX_DETAIL_LENGTH else "")

    return {
        "type": "AdaptiveCard",
        "version": "1.4",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": [
            {
                "type": "ColumnSet",
                "columns": [
                    {
                        "type": "Column",
                        "width": "auto",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": emoji,
                                "size": "Large",
                            }
                        ],
                    },
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": f"**{tool_name}**",
                                "wrap": True,
                            },
                            {
                                "type": "TextBlock",
                                "text": truncated,
                                "wrap": True,
                                "isSubtle": True,
                                "size": "Small",
                            },
                        ],
                    },
                    {
                        "type": "Column",
                        "width": "auto",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": status.capitalize(),
                                "color": color,
                                "weight": "Bolder",
                                "size": "Small",
                            }
                        ],
                    },
                ],
            },
        ],
    }


def task_status_card(
    *,
    task: str,
    status: str,
    duration: str,
    details: dict[str, str] | None = None,
) -> dict:
    """Card showing structured task progress.

    Args:
        task: Task name (e.g., "Security review", "Build PR #42").
        status: One of "in_progress", "complete", "error".
        duration: Human-readable duration (e.g., "2m 34s").
        details: Optional key-value pairs for extra info.
    """
    mapped_status = "running" if status == "in_progress" else status
    emoji = _STATUS_EMOJI.get(mapped_status, "\u2753")

    facts = [
        {"title": "Task", "value": task},
        {"title": "Status", "value": f"{emoji} {status.replace('_', ' ').capitalize()}"},
        {"title": "Duration", "value": duration},
    ]
    if details:
        for key, value in details.items():
            facts.append({"title": key, "value": value})

    return {
        "type": "AdaptiveCard",
        "version": "1.4",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": [
            {
                "type": "TextBlock",
                "text": f"{emoji} **Task Update**",
                "size": "Medium",
                "weight": "Bolder",
                "wrap": True,
            },
            {
                "type": "FactSet",
                "facts": facts,
            },
        ],
    }


def build_result_card(
    *,
    passed: bool,
    summary: str,
    details: str | None = None,
) -> dict:
    """Card showing build/test pass or fail with expandable details.

    Args:
        passed: True for green, False for red.
        summary: One-line summary (e.g., "225 tests passed").
        details: Optional multi-line details (e.g., failure output).
    """
    emoji = "\u2705" if passed else "\u274c"
    color = "good" if passed else "attention"

    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": f"{emoji} **{summary}**",
            "size": "Medium",
            "weight": "Bolder",
            "color": color,
            "wrap": True,
        },
    ]

    if details:
        body.append(
            {
                "type": "TextBlock",
                "text": details,
                "wrap": True,
                "fontType": "Monospace",
                "size": "Small",
                "isSubtle": True,
            }
        )

    return {
        "type": "AdaptiveCard",
        "version": "1.4",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": body,
    }
