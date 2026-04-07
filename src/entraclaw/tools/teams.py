"""Teams Graph API integration — 1:1 chat creation and messaging.

All HTTP calls use ``httpx.AsyncClient`` with proper auth headers.
Errors are mapped to the typed hierarchy in ``entraclaw.errors``.

The agent token is acquired via the three-hop Agent User flow:
  1. Blueprint authenticates with client_credentials → Blueprint token
  2. Agent Identity authenticates with Blueprint token (FIC) → Agent Identity token
  3. Agent User token via user_fic grant → delegated user token (idtyp=user)

No human in the loop.  No device-code flow.  No OBO.
The Agent User has its own Teams identity and license.
"""

from __future__ import annotations

import logging

import httpx

from entraclaw.auth.certificate import build_client_assertion
from entraclaw.config import EntraClawConfig
from entraclaw.errors import (
    AgentIDNotAvailable,
    ChatNotFound,
    MessageTooLong,
    RateLimitError,
    TeamsNotLicensed,
    TokenExchangeError,
    TokenExpiredError,
)
from entraclaw.platform import get_credential_store
from entraclaw.tools.rate_limit import RetryOn429Transport

logger = logging.getLogger("entraclaw.tools.teams")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_ENDPOINT = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

MAX_MESSAGE_LENGTH = 28_000


def _token_url(tenant_id: str) -> str:
    return TOKEN_ENDPOINT.format(tenant=tenant_id)


def _check_token_response(hop: str, data: dict) -> str:
    """Extract access_token from a token response, raising on error."""
    if "error" in data:
        raise TokenExchangeError(
            hop=hop,
            error=data["error"],
            description=data.get("error_description", "unknown"),
        )
    token = data.get("access_token")
    if not token:
        raise TokenExchangeError(
            hop=hop,
            error="missing_token",
            description="Response did not contain access_token",
        )
    return token


def acquire_agent_user_token(config: EntraClawConfig) -> str:
    """Acquire a delegated token for the Agent User via the three-hop flow.

    Hop 1: Blueprint → client_credentials → Blueprint token
    Hop 2: Agent Identity → FIC exchange (Blueprint token as assertion) → Agent Identity token
    Hop 3: Agent User → user_fic grant → delegated user token (idtyp=user)

    The resulting token can call any Graph API requiring user context
    (Teams, Exchange, OneDrive, etc.) as the Agent User identity.

    Raises ``AgentIDNotAvailable`` if config is incomplete,
    or ``TokenExchangeError`` if any hop fails.
    """
    if not all(
        [
            config.blueprint_app_id,
            config.blueprint_cert_thumbprint,
            config.tenant_id,
            config.agent_id,
            config.agent_user_id,
        ]
    ):
        raise AgentIDNotAvailable(
            "Agent User credentials not configured. Run ./scripts/setup.sh first."
        )

    url = _token_url(config.tenant_id)  # type: ignore[arg-type]

    timeout = httpx.Timeout(15.0)

    # Retrieve private key from OS credential store (Keychain/TPM/Keyring)
    store = get_credential_store()
    private_key_pem = store.retrieve("entraclaw", "blueprint-private-key")
    if not private_key_pem:
        raise AgentIDNotAvailable(
            "Blueprint private key not found in credential store. "
            "Run ./scripts/setup.sh to generate and store the certificate."
        )

    # Build JWT assertion (replaces client_secret per ADR-003)
    jwt_assertion = build_client_assertion(
        private_key_pem=private_key_pem,
        cert_thumbprint=config.blueprint_cert_thumbprint,
        client_id=config.blueprint_app_id,
        token_endpoint=url,
    )

    # Hop 1: Blueprint exchange token (T1) via client_credentials
    # The Blueprint authenticates with a certificate assertion and requests a token
    # scoped for Agent Identity impersonation (fmi_path=AgentIdentity).
    with httpx.Client(timeout=timeout) as client:
        hop1_resp = client.post(
            url,
            data={
                "client_id": config.blueprint_app_id,
                "scope": "api://AzureADTokenExchange/.default",
                "fmi_path": config.agent_id,
                "grant_type": "client_credentials",
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": jwt_assertion,
            },
        )
    t1_token = _check_token_response("hop1:blueprint", hop1_resp.json())

    # Hop 2: Agent Identity exchange token (T2)
    # The Agent Identity presents T1 as its client assertion.
    # Entra validates T1.aud == Agent Identity's parent (Blueprint).
    with httpx.Client(timeout=timeout) as client:
        hop2_resp = client.post(
            url,
            data={
                "client_id": config.agent_id,
                "scope": "api://AzureADTokenExchange/.default",
                "grant_type": "client_credentials",
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": t1_token,
            },
        )
    t2_token = _check_token_response("hop2:agent_identity", hop2_resp.json())

    # Hop 3: Agent User resource token via user_fic grant
    # Presents both T1 (client_assertion) and T2 (user_federated_identity_credential).
    # Entra validates T2.aud == Agent Identity, then issues a delegated token
    # with idtyp=user for the Agent User.
    with httpx.Client(timeout=timeout) as client:
        hop3_resp = client.post(
            url,
            data={
                "client_id": config.agent_id,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "user_fic",
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": t1_token,
                "user_id": config.agent_user_id,
                "user_federated_identity_credential": t2_token,
                "requested_token_use": "on_behalf_of",
            },
        )
    resource_token = _check_token_response("hop3:agent_user", hop3_resp.json())

    return resource_token


async def create_or_find_chat(
    *,
    token: str,
    human_user_ids: list[str],
    agent_user_id: str | None = None,
) -> dict:
    """Create or resume a Teams chat between the Agent User and human(s).

    If one human user is provided, creates a ``oneOnOne`` chat (idempotent).
    If multiple humans are provided, creates a ``group`` chat with a topic.

    Uses explicit user IDs for all members (not ``/me``) because the
    ``user@odata.bind`` field doesn't reliably resolve ``/me`` for
    Agent User tokens in the chat creation context.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    members = [
        {
            "@odata.type": "#microsoft.graph.aadUserConversationMember",
            "roles": ["owner"],
            "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{uid}')",
        }
        for uid in human_user_ids
    ]
    # Add Agent User as explicit member if ID is provided
    if agent_user_id:
        members.insert(
            0,
            {
                "@odata.type": "#microsoft.graph.aadUserConversationMember",
                "roles": ["owner"],
                "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{agent_user_id}')",
            },
        )

    is_group = len(human_user_ids) > 1
    chat_payload: dict = {
        "chatType": "group" if is_group else "oneOnOne",
        "members": members,
    }
    if is_group:
        chat_payload["topic"] = "EntraClaw Agent Chat"

    transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/chats",
            json=chat_payload,
            headers=headers,
        )
        if resp.status_code == 403:
            raise TeamsNotLicensed(
                "Agent User does not have a Teams license. "
                "Assign E3/E5/Teams Enterprise to the Agent User."
            )
        if resp.status_code == 401:
            raise TokenExpiredError("Agent User token expired — re-acquire via three-hop flow")
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
    The message is sent FROM the Agent User's own Teams identity.
    """
    if len(message) > MAX_MESSAGE_LENGTH:
        raise MessageTooLong(f"Message is {len(message)} chars, max is {MAX_MESSAGE_LENGTH}")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/chats/{chat_id}/messages",
            json={"body": {"contentType": content_type, "content": message}},
            headers=headers,
        )
        if resp.status_code == 401:
            raise TokenExpiredError("Agent User token expired — re-acquire via three-hop flow")
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

    transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/chats/{chat_id}/messages",
            params={"$top": str(count), "$orderby": "createdDateTime desc"},
            headers=headers,
        )
        if resp.status_code == 401:
            raise TokenExpiredError("Agent User token expired — re-acquire via three-hop flow")
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
                "from": (m.get("from") or {}).get("user", {}).get("displayName", "unknown"),
                "content": (m.get("body") or {}).get("content", ""),
                "sent_at": m.get("createdDateTime"),
            }
            for m in messages
        ]


def filter_human_messages(
    messages: list[dict],
    agent_user_display_name: str,
) -> list[dict]:
    """Return only messages from the human (not the agent, not system messages).

    Filters out:
    - Messages where ``from`` matches the agent's display name
    - Messages where ``from`` is ``"unknown"`` (system messages with null from field)

    All filtering is client-side — Graph API ``$filter`` is unreliable for chat messages.
    """
    return [m for m in messages if m.get("from") not in (agent_user_display_name, "unknown")]
