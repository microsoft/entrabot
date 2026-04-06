"""Teams Graph API integration — 1:1 chat creation and messaging.

All HTTP calls use ``httpx.AsyncClient`` with proper auth headers.
Errors are mapped to the typed hierarchy in ``openclaw.errors``.

The agent token is acquired via ROPC (Resource Owner Password Credentials)
for the dedicated agent user created by ``scripts/setup.sh``.  Messages
are sent FROM the agent user, not the human.
"""

from __future__ import annotations

import logging

import httpx
from msal import PublicClientApplication

from openclaw.config import OpenclawConfig
from openclaw.errors import (
    AgentIDNotAvailable,
    ChatNotFound,
    MessageTooLong,
    MSALError,
    RateLimitError,
    TeamsNotLicensed,
    TokenExpiredError,
)

logger = logging.getLogger("openclaw.tools.teams")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

MAX_MESSAGE_LENGTH = 28_000

# Delegated scopes requested via ROPC for the agent user
AGENT_SCOPES = [
    "https://graph.microsoft.com/Chat.Create",
    "https://graph.microsoft.com/ChatMessage.Send",
    "https://graph.microsoft.com/Chat.ReadWrite",
    "https://graph.microsoft.com/User.Read",
]


def acquire_agent_token(config: OpenclawConfig) -> str:
    """Acquire a delegated token for the agent user via ROPC.

    Raises ``AgentIDNotAvailable`` if required config fields are missing,
    or ``MSALError`` if the ROPC flow fails.
    """
    if not all([config.client_id, config.tenant_id, config.agent_upn, config.agent_password]):
        raise AgentIDNotAvailable("Agent credentials not configured. Run ./scripts/setup.sh first.")

    app = PublicClientApplication(
        client_id=config.client_id,
        authority=f"https://login.microsoftonline.com/{config.tenant_id}",
    )

    result = app.acquire_token_by_username_password(
        username=config.agent_upn,
        password=config.agent_password,
        scopes=AGENT_SCOPES,
    )

    if "error" in result:
        raise MSALError(
            result["error"],
            result.get("error_description", "ROPC token acquisition failed"),
        )

    return result["access_token"]


async def create_or_find_chat(
    *,
    token: str,
    agent_user_id: str,
    human_user_id: str,
) -> dict:
    """Create or resume a 1:1 Teams chat between the agent and human.

    The Graph ``POST /chats`` call is idempotent for ``oneOnOne`` chats —
    if a chat already exists between the two members it is returned unchanged.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    chat_payload = {
        "chatType": "oneOnOne",
        "members": [
            {
                "@odata.type": "#microsoft.graph.aadUserConversationMember",
                "roles": ["owner"],
                "user@odata.bind": (f"https://graph.microsoft.com/v1.0/users('{agent_user_id}')"),
            },
            {
                "@odata.type": "#microsoft.graph.aadUserConversationMember",
                "roles": ["owner"],
                "user@odata.bind": (f"https://graph.microsoft.com/v1.0/users('{human_user_id}')"),
            },
        ],
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{GRAPH_BASE}/chats",
            json=chat_payload,
            headers=headers,
        )
        if resp.status_code == 403:
            raise TeamsNotLicensed("Agent or human user does not have a Teams license")
        if resp.status_code == 401:
            raise TokenExpiredError("Agent token expired — re-run ./scripts/setup.sh")
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            raise RateLimitError(retry_after)
        resp.raise_for_status()

        chat = resp.json()
        logger.info("Teams chat established: %s", chat["id"])
        return {
            "chat_id": chat["id"],
            "created_at": chat.get("createdDateTime"),
        }


async def send(
    *,
    chat_id: str,
    message: str,
    token: str,
    content_type: str = "text",
) -> dict:
    """Send *message* to the Teams chat identified by *chat_id*.

    ``content_type`` must be ``"text"`` or ``"html"``.
    The message is sent FROM the agent user (via the agent's delegated token).
    """
    if len(message) > MAX_MESSAGE_LENGTH:
        raise MessageTooLong(f"Message is {len(message)} chars, max is {MAX_MESSAGE_LENGTH}")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{GRAPH_BASE}/chats/{chat_id}/messages",
            json={"body": {"contentType": content_type, "content": message}},
            headers=headers,
        )
        if resp.status_code == 401:
            raise TokenExpiredError("Agent token expired — re-run ./scripts/setup.sh")
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            raise RateLimitError(retry_after)
        if resp.status_code == 404:
            raise ChatNotFound(f"Chat {chat_id} not found")
        resp.raise_for_status()

        msg = resp.json()
        logger.info("Message sent to chat %s: %s", chat_id, msg["id"])
        return {
            "message_id": msg["id"],
            "sent_at": msg.get("createdDateTime"),
        }


async def read(
    *,
    chat_id: str,
    token: str,
    count: int = 5,
) -> list[dict]:
    """Read recent messages from the human in the Teams chat.

    Returns up to *count* most recent messages, newest first.
    """
    headers = {
        "Authorization": f"Bearer {token}",
    }

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GRAPH_BASE}/chats/{chat_id}/messages",
            params={"$top": str(count), "$orderby": "createdDateTime desc"},
            headers=headers,
        )
        if resp.status_code == 401:
            raise TokenExpiredError("Agent token expired — re-run ./scripts/setup.sh")
        if resp.status_code == 404:
            raise ChatNotFound(f"Chat {chat_id} not found")
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            raise RateLimitError(retry_after)
        resp.raise_for_status()

        messages = resp.json().get("value", [])
        return [
            {
                "message_id": m["id"],
                "from": m.get("from", {}).get("user", {}).get("displayName", "unknown"),
                "content": m.get("body", {}).get("content", ""),
                "sent_at": m.get("createdDateTime"),
            }
            for m in messages
        ]
