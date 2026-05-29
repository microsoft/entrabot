"""Teams Graph API integration — 1:1 chat creation and messaging.

All HTTP calls use ``httpx.AsyncClient`` with proper auth headers.
Errors are mapped to the typed hierarchy in ``entrabot.errors``.

Supports two identity modes:
  - **Agent User** (three-hop flow): messages sent as 'EntraBot Agent'
  - **Delegated** (MSAL): messages sent as the human, prefixed [EntraBot]
"""

from __future__ import annotations

import logging
import re
import sys

import httpx

from entrabot.auth.certificate import build_client_assertion
from entrabot.config import EntraBotConfig
from entrabot.errors import (
    AgentIDNotAvailable,
    ChatNotFound,
    MessageTooLong,
    RateLimitError,
    RequesterNotInChatError,
    RequesterNotSponsorError,
    TeamsNotLicensed,
    TokenExchangeError,
    TokenExpiredError,
)
from entrabot.graph_helpers import odata_escape
from entrabot.platform import get_credential_store
from entrabot.tools.audit import log_event
from entrabot.tools.rate_limit import RetryOn429Transport

logger = logging.getLogger("entrabot.tools.teams")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_ENDPOINT = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

GRAPH_RESOURCE_SCOPE = "https://graph.microsoft.com/.default"
STORAGE_RESOURCE_SCOPE = "https://storage.azure.com/.default"

MAX_MESSAGE_LENGTH = 28_000


def _token_url(tenant_id: str) -> str:
    return TOKEN_ENDPOINT.format(tenant=tenant_id)


def _build_blueprint_assertion(config: EntraBotConfig, token_endpoint: str) -> str:
    """Build the Hop-1 client assertion.

    Try the keystore PEM first (Mac/Linux runtime, and any platform whose
    setup wrote a PEM into keyring — including most test fixtures). If
    no PEM is in the keystore, fall back to the Windows CNG path keyed
    by ``blueprint_cert_sha1``. A Windows-only setup writes the SHA-1
    but never a PEM, so real Windows deployments take the CNG branch.
    """
    store = get_credential_store()
    private_key_pem = store.retrieve("entrabot", "blueprint-private-key")
    if private_key_pem:
        return build_client_assertion(
            private_key_pem=private_key_pem,
            cert_thumbprint=config.blueprint_cert_thumbprint,
            client_id=config.blueprint_app_id,
            token_endpoint=token_endpoint,
        )

    if sys.platform == "win32":
        if not config.blueprint_cert_sha1:
            raise AgentIDNotAvailable(
                "ENTRABOT_BLUEPRINT_CERT_SHA1 missing. "
                "Run setup-windows.cmd to generate the Blueprint cert."
            )
        return build_client_assertion(
            cert_thumbprint=config.blueprint_cert_thumbprint,
            cert_sha1=config.blueprint_cert_sha1,
            client_id=config.blueprint_app_id,
            token_endpoint=token_endpoint,
        )

    raise AgentIDNotAvailable(
        "Blueprint private key not found in credential store. "
        "Run ./scripts/setup.sh to generate and store the certificate."
    )


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


def acquire_agent_user_token(
    config: EntraBotConfig,
    *,
    resource_scope: str = GRAPH_RESOURCE_SCOPE,
) -> str:
    """Acquire a delegated token for the Agent User via the three-hop flow.

    Hop 1: Blueprint → client_credentials → Blueprint token
    Hop 2: Agent Identity → FIC exchange (Blueprint token as assertion) → Agent Identity token
    Hop 3: Agent User → user_fic grant → delegated user token (idtyp=user)

    The resulting token can call any resource the Agent User has been
    consented for. *resource_scope* selects the resource at Hop 3 only —
    Hops 1+2 always exchange against ``api://AzureADTokenExchange/.default``
    (the FIC exchange scope). Defaults to Microsoft Graph; pass
    :data:`STORAGE_RESOURCE_SCOPE` for Azure Blob Storage (ADR-005).

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

    jwt_assertion = _build_blueprint_assertion(config, url)

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
    # with idtyp=user for the Agent User scoped for *resource_scope*.
    with httpx.Client(timeout=timeout) as client:
        hop3_resp = client.post(
            url,
            data={
                "client_id": config.agent_id,
                "scope": resource_scope,
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


def acquire_agent_user_storage_token(config: EntraBotConfig) -> str:
    """Three-hop variant for Azure Blob Storage (ADR-005).

    Same first two hops as :func:`acquire_agent_user_token`; Hop 3 swaps
    the resource scope from Graph to ``https://storage.azure.com/.default``.
    Requires the Agent Identity to have been consented for Storage during
    ``setup.sh``.
    """
    return acquire_agent_user_token(config, resource_scope=STORAGE_RESOURCE_SCOPE)


def acquire_agent_identity_token(
    config: EntraBotConfig,
    *,
    resource_scope: str = GRAPH_RESOURCE_SCOPE,
) -> str:
    """Acquire an app-only token as the Agent Identity via the Blueprint FIC.

    Two hops only — stops at the Agent Identity (no user_fic grant). Used
    by :mod:`entrabot.identity.sponsors` to read the Agent Identity's
    Graph sponsors relationship, which requires app-only auth.
    """
    if not all(
        [
            config.blueprint_app_id,
            config.blueprint_cert_thumbprint,
            config.tenant_id,
            config.agent_id,
        ]
    ):
        raise AgentIDNotAvailable(
            "Agent Identity credentials not configured. Run ./scripts/setup.sh first."
        )

    url = _token_url(config.tenant_id)  # type: ignore[arg-type]
    timeout = httpx.Timeout(15.0)

    jwt_assertion = _build_blueprint_assertion(config, url)

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

    with httpx.Client(timeout=timeout) as client:
        hop2_resp = client.post(
            url,
            data={
                "client_id": config.agent_id,
                "scope": resource_scope,
                "grant_type": "client_credentials",
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": t1_token,
            },
        )
    return _check_token_response("hop2:agent_identity", hop2_resp.json())


async def create_one_on_one_chat(
    *,
    token: str,
    target_email: str,
    target_tenant_id: str | None = None,
    agent_user_id: str | None = None,
) -> dict:
    """Create a 1:1 chat between the Agent User and a target user by email.

    Graph ``POST /chats`` with ``chatType: "oneOnOne"`` is idempotent —
    calling it twice with the same pair returns the existing chat.

    For cross-tenant users, provide ``target_tenant_id`` (their home tenant
    GUID) so Graph resolves the identity correctly.

    Returns dict with ``chat_id`` and ``created_at``.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    target_member: dict = {
        "@odata.type": "#microsoft.graph.aadUserConversationMember",
        "roles": ["owner"],
        "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{odata_escape(target_email)}')",
    }
    if target_tenant_id:
        target_member["tenantId"] = target_tenant_id

    # Agent User must be listed with their object ID — /me is not valid
    # in user@odata.bind. If not provided, resolve via /me endpoint.
    if not agent_user_id:
        transport_me = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())
        async with httpx.AsyncClient(transport=transport_me) as me_client:
            me_resp = await me_client.get(
                f"{GRAPH_BASE}/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            if me_resp.status_code == 200:
                agent_user_id = me_resp.json().get("id", "")

    _agent_bind_value = odata_escape(agent_user_id or "")
    agent_member: dict = {
        "@odata.type": "#microsoft.graph.aadUserConversationMember",
        "roles": ["owner"],
        "user@odata.bind": (
            f"https://graph.microsoft.com/v1.0/users('{_agent_bind_value}')"
        ),
    }

    payload = {
        "chatType": "oneOnOne",
        "members": [agent_member, target_member],
    }

    logger.info("Creating 1:1 chat with %s", target_email)

    transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/chats",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 403:
            raise TeamsNotLicensed(
                "Agent User does not have a Teams license. "
                "Assign E3/E5/Teams Enterprise to the Agent User."
            )
        if resp.status_code == 401:
            raise TokenExpiredError("Agent User token expired — re-acquire via three-hop flow")
        if resp.status_code == 400:
            try:
                error_body = resp.json()
                error_msg = error_body.get("error", {}).get("message", resp.text)
            except Exception:
                error_msg = resp.text or "Bad Request"
            logger.error("400 creating 1:1 chat: %s", error_msg)
            raise ValueError(f"Graph API rejected chat creation: {error_msg}")
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            raise RateLimitError(retry_after)
        resp.raise_for_status()

        chat = resp.json()
        chat_id = chat["id"]
        logger.info("1:1 chat established: %s with %s", chat_id, target_email)
        return {
            "chat_id": chat_id,
            "created_at": chat.get("createdDateTime"),
        }


async def create_or_find_chat(
    *,
    token: str,
    human_user_ids: list[str],
    agent_user_id: str | None = None,
    human_user_tenant_ids: list[str] | None = None,
    human_user_mails: list[str] | None = None,
    human_user_types: list[str] | None = None,
) -> dict:
    """Create or resume a Teams chat between the Agent User and human(s).

    If one human user is provided, creates a ``oneOnOne`` chat (idempotent).
    If multiple humans are provided, creates a ``group`` chat with a topic.

    For B2B guest users (``human_user_types`` contains ``"Guest"``), the chat
    is always created as ``group`` with ``role: "guest"`` for the guest member,
    matching Graph API Create Chat Example 6.

    For external/federated users, ``human_user_tenant_ids`` provides the home
    tenant GUID (parallel to ``human_user_ids``).  When a tenant ID is
    present the member payload includes ``tenantId`` and the
    ``user@odata.bind`` references the user's email (from
    ``human_user_mails``) so Graph can route the chat cross-tenant
    (see Graph API Create chat — Example 7).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    tenant_ids = human_user_tenant_ids or [""] * len(human_user_ids)
    mails = human_user_mails or [""] * len(human_user_ids)
    user_types = human_user_types or [""] * len(human_user_ids)

    members: list[dict] = []
    for i, uid in enumerate(human_user_ids):
        tid = tenant_ids[i] if i < len(tenant_ids) else ""
        mail = mails[i] if i < len(mails) else ""
        utype = user_types[i] if i < len(user_types) else ""

        is_guest = utype.lower() == "guest" if utype and utype.lower() != "none" else False

        if is_guest and tid and mail:
            # Example 7: B2B guest → federated via home tenant.
            # Uses the user's email + home tenantId so Graph resolves
            # their real identity (not the guest object, which is invisible
            # to Teams).  Role must be "owner" per the docs.
            member: dict = {
                "@odata.type": "#microsoft.graph.aadUserConversationMember",
                "roles": ["owner"],
                "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{odata_escape(mail)}')",
                "tenantId": tid,
            }
        elif tid:
            # Example 7: Non-guest federated user — email + tenantId
            user_ref = mail if mail else uid
            member = {
                "@odata.type": "#microsoft.graph.aadUserConversationMember",
                "roles": ["owner"],
                "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{odata_escape(user_ref)}')",
                "tenantId": tid,
            }
        else:
            # In-tenant member — use object ID, role="owner"
            member = {
                "@odata.type": "#microsoft.graph.aadUserConversationMember",
                "roles": ["owner"],
                "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{odata_escape(uid)}')",
            }
        members.append(member)

    # Add Agent User as explicit member if ID is provided
    if agent_user_id:
        members.insert(
            0,
            {
                "@odata.type": "#microsoft.graph.aadUserConversationMember",
                "roles": ["owner"],
                "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{odata_escape(agent_user_id)}')",
            },
        )

    is_group = len(human_user_ids) > 1
    chat_payload: dict = {
        "chatType": "group" if is_group else "oneOnOne",
        "members": members,
    }
    if is_group:
        chat_payload["topic"] = "EntraBot Agent Chat"

    logger.info("Creating chat — payload: %s", chat_payload)

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
        if resp.status_code == 404:
            error_body = resp.json().get("error", {})
            error_msg = error_body.get("message", resp.text)
            raise ChatNotFound(
                f"Chat creation failed (404): {error_msg}. "
                "This usually means a federated user's email doesn't match "
                "their actual UPN. Check ENTRABOT_HUMAN_USER_MAILS in .env."
            )
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            raise RateLimitError(retry_after)
        resp.raise_for_status()

        chat = resp.json()
        chat_id = chat["id"]
        logger.info("Teams chat established: %s (type=%s)", chat_id, chat.get("chatType"))

        # Verify chat members were added correctly
        try:
            verify_resp = await client.get(
                f"{GRAPH_BASE}/chats/{chat_id}/members",
                headers={"Authorization": f"Bearer {token}"},
            )
            if verify_resp.status_code == 200:
                actual_members = verify_resp.json().get("value", [])
                for m in actual_members:
                    display = m.get("displayName", "?")
                    roles = m.get("roles", [])
                    tid = m.get("tenantId", "")
                    logger.info(
                        "  Chat member: %s (roles=%s, tenantId=%s)",
                        display,
                        roles,
                        tid,
                    )
            else:
                logger.warning("Could not verify chat members: HTTP %d", verify_resp.status_code)
        except Exception:
            logger.warning("Chat member verification failed", exc_info=True)

        return {
            "chat_id": chat_id,
            "created_at": chat.get("createdDateTime"),
        }


async def _get_sponsor_records() -> list:
    """Fetch Agent Identity sponsor records (defers to the files-tool helper).

    Centralized in ``tools.files`` so the same enrichment + chat-members
    email-fallback logic is shared between the file-share gate and the
    Teams add-member gate. Re-exported here so tests can patch the
    symbol at ``entrabot.tools.teams._get_sponsor_records``.
    """
    from entrabot.tools.files import _get_sponsor_records as _files_get_sponsors

    return await _files_get_sponsors()


async def _fetch_chat_members_for_gate(chat_id: str) -> list[dict]:
    """Fetch chat members for the sponsor-membership gate (sync helper, thread-offloaded).

    Wraps ``identity.sponsors.fetch_chat_members`` so tests can patch
    this single attribute on ``entrabot.tools.teams`` and so the live
    code path doesn't hit Graph from inside ``add_member`` synchronously.
    """
    import asyncio

    from entrabot.config import get_config
    from entrabot.identity.sponsors import fetch_chat_members

    return await asyncio.to_thread(fetch_chat_members, get_config(), chat_id)


async def add_member(
    *,
    chat_id: str,
    token: str,
    email: str,
    requester_email: str,
    tenant_id: str | None = None,
) -> dict:
    """Add a user to an existing Teams chat. Sponsor-gated.

    Authorization model (mirrors ``share_file`` 2026-04-30 inverted
    gate): only Agent Identity sponsors are authorized to direct the
    agent to invite anyone into a Teams chat. A sponsor may invite
    anyone they choose — the invitee (``email``) is not checked against
    the allowlist. Two gates run before the Graph mutation:

    1. **Requester is a sponsor.** ``requester_email`` must match an
       Agent Identity sponsor record (any email identifier form).
    2. **Requester is a member of ``chat_id``.** Their ``user_id`` must
       appear in the chat's member list — defends against an LLM
       fabricating a sponsor email that doesn't match the actual
       conversation context.

    ``audit_log`` (via ``log_event``) fires BEFORE the Graph call so a
    blocked add still produces a record.

    For external/federated invitees, provide ``tenant_id`` (their home
    tenant GUID); Graph resolves the email cross-tenant via Example 7.
    For in-tenant invitees, omit ``tenant_id``.

    Args:
        chat_id: Teams chat ID to invite into. Required.
        token: Agent User access token for Graph.
        email: Invitee email address (sponsors may invite anyone).
        requester_email: Email of the human (sponsor) who asked the
            agent to invite. Required. Derived from the active
            conversation context — never the agent's own address.
        tenant_id: Optional home tenant GUID for federated invitees.

    Raises:
        ValueError: ``requester_email`` or ``chat_id`` missing.
        RequesterNotSponsorError: ``requester_email`` not in sponsor allowlist.
        RequesterNotInChatError: requester is a sponsor but not a member of ``chat_id``.
    """
    if not requester_email:
        raise ValueError("requester_email is required")
    if not chat_id:
        raise ValueError("chat_id is required")

    audit_resource = f"chats/{chat_id}/members"
    audit_metadata = {
        "requester_email": requester_email,
        "chat_id": chat_id,
        "invitee_email": email,
        "tenant_id": tenant_id,
    }
    log_event(
        action="teams.add_member",
        resource=audit_resource,
        outcome="pending",
        metadata=audit_metadata,
    )

    try:
        # Gate 1: requester must be in the static sponsor allowlist.
        sponsors = await _get_sponsor_records()
        requester_lower = requester_email.strip().lower()
        matched_sponsor = next(
            (
                s
                for s in sponsors
                if any(
                    e.lower() == requester_lower for e in s.email_identifiers()
                )
            ),
            None,
        )
        if matched_sponsor is None or not matched_sponsor.user_id:
            err = RequesterNotSponsorError(requester=requester_email)
            log_event(
                action="teams.add_member",
                resource=audit_resource,
                outcome="failure",
                metadata={
                    **audit_metadata,
                    "error": type(err).__name__,
                    "message": str(err),
                },
            )
            raise err

        # Gate 2: matched sponsor must be a member of the cited chat.
        members = await _fetch_chat_members_for_gate(chat_id)
        matched_user_id = matched_sponsor.user_id.lower()
        if not any(
            (m.get("user_id") or "").strip().lower() == matched_user_id
            for m in members
        ):
            err = RequesterNotInChatError(
                requester=requester_email, chat_id=chat_id
            )
            log_event(
                action="teams.add_member",
                resource=audit_resource,
                outcome="failure",
                metadata={
                    **audit_metadata,
                    "error": type(err).__name__,
                    "message": str(err),
                },
            )
            raise err
    except (RequesterNotSponsorError, RequesterNotInChatError):
        raise

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    member_payload: dict = {
        "@odata.type": "#microsoft.graph.aadUserConversationMember",
        "roles": ["owner"],
        "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{odata_escape(email)}')",
    }
    if tenant_id:
        member_payload["tenantId"] = tenant_id

    logger.info("Adding member to chat %s: %s (tenant=%s)", chat_id, email, tenant_id)

    transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())
    try:
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.post(
                f"{GRAPH_BASE}/chats/{chat_id}/members",
                json=member_payload,
                headers=headers,
            )
            if resp.status_code == 404:
                try:
                    error_body = resp.json().get("error", {})
                    error_msg = error_body.get("message", resp.text)
                except Exception:
                    error_msg = resp.text or "Not found"
                raise ChatNotFound(f"Could not add member: {error_msg}")
            if resp.status_code == 401:
                raise TokenExpiredError(
                    "Agent User token expired — re-acquire via three-hop flow"
                )
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "60"))
                raise RateLimitError(retry_after)
            resp.raise_for_status()

            # POST /members may return 201 with empty body
            if resp.text.strip():
                result = resp.json()
                display_name = result.get("displayName", email)
            else:
                result = {}
                display_name = email
            logger.info("Member added: %s", display_name)
    except Exception as exc:
        log_event(
            action="teams.add_member",
            resource=audit_resource,
            outcome="failure",
            metadata={
                **audit_metadata,
                "error": type(exc).__name__,
                "message": str(exc),
            },
        )
        raise

    log_event(
        action="teams.add_member",
        resource=audit_resource,
        outcome="success",
        metadata={
            **audit_metadata,
            "member_id": result.get("id", ""),
            "display_name": display_name,
        },
    )
    return {
        "member_id": result.get("id", ""),
        "display_name": display_name,
        "roles": result.get("roles", ["owner"]),
    }


async def list_members(
    *,
    chat_id: str,
    token: str,
) -> list[dict]:
    """List members of a Teams chat with their user IDs.

    Returns a list of dicts with user_id, name, and email — useful for
    resolving @mention targets.
    """
    headers = {"Authorization": f"Bearer {token}"}

    transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/chats/{chat_id}/members",
            headers=headers,
        )
        if resp.status_code == 401:
            raise TokenExpiredError("Agent User token expired — re-acquire via three-hop flow")
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            raise RateLimitError(retry_after)
        resp.raise_for_status()

        members = resp.json().get("value", [])
        return [
            {
                "user_id": m.get("userId", ""),
                "name": m.get("displayName", ""),
                "email": m.get("email", ""),
                "roles": m.get("roles", []),
            }
            for m in members
        ]


async def send(
    *,
    chat_id: str,
    message: str,
    token: str,
    content_type: str = "html",
    mentions: list[dict] | None = None,
    prefix: str | None = None,
    attachments: list[dict] | None = None,
) -> dict:
    """Send *message* to the Teams chat identified by *chat_id*.

    ``content_type`` defaults to ``"html"`` per
    ``prompts/anatomy/channel-discipline.md`` ("Always HTML in
    Teams — no exceptions"). Pass ``content_type="text"`` explicitly
    only when plain text is genuinely required.

    When *prefix* is set (e.g. ``"[EntraBot]"``), the message is prefixed
    to indicate it was sent by the agent using delegated credentials.
    This is used in delegated mode where the message appears to come from
    the human's identity.

    ``mentions`` is an optional list of dicts with keys:
      - ``id``: int — matches the ``<at id="N">`` in the HTML body
      - ``name``: str — display name shown in the mention
      - ``user_id``: str — Entra user GUID of the mentioned user
    """
    if (not message or not message.strip()) and not attachments:
        raise ValueError("Message cannot be empty")

    # Apply prefix for delegated mode attribution
    if prefix:
        message = f"{prefix} {message}"

    if len(message) > MAX_MESSAGE_LENGTH:
        raise MessageTooLong(f"Message is {len(message)} chars, max is {MAX_MESSAGE_LENGTH}")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    payload: dict = {"body": {"contentType": content_type, "content": message}}
    if attachments:
        payload["attachments"] = attachments
    if mentions:
        payload["mentions"] = [
            {
                "id": int(m["id"]),
                "mentionText": m["name"],
                "mentioned": {
                    "user": {
                        "displayName": m["name"],
                        "id": m["user_id"],
                        "userIdentityType": "aadUser",
                    }
                },
            }
            for m in mentions
        ]

    transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/chats/{chat_id}/messages",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 401:
            raise TokenExpiredError("Token expired — re-acquire")
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            raise RateLimitError(retry_after)
        if resp.status_code == 404:
            raise ChatNotFound(f"Chat {chat_id} not found")
        if resp.status_code == 400:
            try:
                error_body = resp.json()
                error_msg = error_body.get("error", {}).get("message", resp.text)
            except Exception:
                error_msg = resp.text or "Bad Request"
            logger.error("400 sending message: %s", error_msg)
            raise ValueError(f"Graph API rejected message: {error_msg}")
        resp.raise_for_status()

        msg = resp.json()
        logger.info("Message sent to chat %s: %s", chat_id, msg["id"])
        return {
            "message_id": msg["id"],
            "sent_at": msg.get("createdDateTime"),
        }


async def post_thinking_placeholder(
    chat_id: str,
    token: str,
    text: str = "thinking…",
) -> str:
    """Post a low-key HTML placeholder and return its message_id.

    Sent as italicized HTML so it reads as a working-indicator, not a
    substantive reply. Resolve via :func:`resolve_placeholder` when the
    real answer is ready.
    """
    payload = {
        "body": {
            "contentType": "html",
            "content": f"<i>{text}</i>",
        },
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/chats/{chat_id}/messages",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 401:
            raise TokenExpiredError("Token expired — re-acquire")
        if resp.status_code == 404:
            raise ChatNotFound(f"Chat {chat_id} not found")
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            raise RateLimitError(retry_after)
        resp.raise_for_status()

        msg_id: str = resp.json()["id"]
        logger.info("Posted thinking placeholder %s to chat %s", msg_id, chat_id)
        return msg_id


async def update_placeholder(
    chat_id: str,
    placeholder_id: str,
    progress_text: str,
    token: str,
) -> dict:
    """PATCH an existing placeholder with a short italic progress note.

    Pairs with :func:`post_thinking_placeholder` and
    :func:`resolve_placeholder` to implement the three-stage Teams
    pattern:

      1. ``post_thinking_placeholder`` — ack the human immediately.
      2. ``update_placeholder`` (zero or more) — surface progress so
         the human sees the agent working, not frozen.
      3. ``resolve_placeholder`` — commit the final answer (audit-logged).

    Unlike ``resolve_placeholder``, this is NOT a final commitment: no
    audit event, no fallback-to-new-message on Graph failure. A failed
    PATCH is logged and returned as ``mode="edit_failed"`` so the caller
    can see the degradation; the eventual ``resolve_placeholder`` call
    handles the real fallback. A spurious progress message appearing as
    a fresh chat ping would be worse UX than a stale placeholder.
    """
    payload = {
        "body": {
            "contentType": "html",
            "content": f"<i>{progress_text}</i>",
        },
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.patch(
            f"{GRAPH_BASE}/chats/{chat_id}/messages/{placeholder_id}",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 401:
            raise TokenExpiredError("Token expired — re-acquire")
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            raise RateLimitError(retry_after)
        if 200 <= resp.status_code < 300:
            return {"message_id": placeholder_id, "mode": "edit"}

    logger.warning(
        "PATCH placeholder %s (progress update) failed (%d) — "
        "skipping, resolve_placeholder will retry",
        placeholder_id,
        resp.status_code,
    )
    return {"message_id": placeholder_id, "mode": "edit_failed"}


async def resolve_placeholder(
    chat_id: str,
    placeholder_id: str,
    final_message: str,
    token: str,
    *,
    content_type: str = "html",
    mentions: list[dict] | None = None,
    mode: str = "edit",
) -> dict:
    """Replace the placeholder with the final message.

    ``mode="edit"`` PATCHes the existing placeholder in place.
    ``mode="delete_repost"`` soft-deletes the placeholder and posts a
    fresh message so the chat pings again.

    If the underlying Graph call fails, falls back to posting the final
    message as a NEW message and returns ``mode="fallback_new"`` so the
    caller sees the degradation. Never leaves a stale placeholder with
    no final reply.
    """
    if mode not in ("edit", "delete_repost"):
        raise ValueError(f"invalid mode: {mode!r} (expected 'edit' or 'delete_repost')")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())

    if mode == "edit":
        payload: dict = {
            "body": {"contentType": content_type, "content": final_message},
        }
        if mentions:
            payload["mentions"] = mentions
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.patch(
                f"{GRAPH_BASE}/chats/{chat_id}/messages/{placeholder_id}",
                json=payload,
                headers=headers,
            )
            if resp.status_code == 401:
                raise TokenExpiredError("Token expired — re-acquire")
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "60"))
                raise RateLimitError(retry_after)
            if 200 <= resp.status_code < 300:
                return {"message_id": placeholder_id, "mode": "edit"}
            logger.warning(
                "PATCH placeholder %s failed (%d) — falling back to new message",
                placeholder_id,
                resp.status_code,
            )
        return await _post_fallback(chat_id, final_message, token, content_type, mentions)

    # delete_repost
    # Graph returns 405 on ``/chats/{id}/messages/{id}/softDelete`` — the
    # correct route for a delegated user token is the ``/me/`` alias, per
    # https://learn.microsoft.com/graph/api/chatmessage-softdelete.
    async with httpx.AsyncClient(transport=transport) as client:
        sd_resp = await client.post(
            f"{GRAPH_BASE}/me/chats/{chat_id}/messages/{placeholder_id}/softDelete",
            headers=headers,
        )
        if sd_resp.status_code == 401:
            raise TokenExpiredError("Token expired — re-acquire")
        if sd_resp.status_code == 429:
            retry_after = int(sd_resp.headers.get("Retry-After", "60"))
            raise RateLimitError(retry_after)
        delete_ok = 200 <= sd_resp.status_code < 300
    if not delete_ok:
        logger.warning(
            "softDelete placeholder %s failed (%d) — posting final as new message",
            placeholder_id,
            sd_resp.status_code,
        )
        return await _post_fallback(chat_id, final_message, token, content_type, mentions)

    sent = await send(
        chat_id=chat_id,
        message=final_message,
        token=token,
        content_type=content_type,
        mentions=mentions,
    )
    return {"message_id": sent["message_id"], "mode": "delete_repost"}


async def _post_fallback(
    chat_id: str,
    final_message: str,
    token: str,
    content_type: str,
    mentions: list[dict] | None,
) -> dict:
    sent = await send(
        chat_id=chat_id,
        message=final_message,
        token=token,
        content_type=content_type,
        mentions=mentions,
    )
    return {"message_id": sent["message_id"], "mode": "fallback_new"}


async def delete_chat_message(
    chat_id: str,
    message_id: str,
    *,
    token: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """Soft-delete a chat message the Agent User sent.

    Graph ``chatMessage: softDelete`` replaces the message body with a
    tombstone visible to chat participants; the message id is retained
    but the content is gone. Only the sender of the message can delete
    it via the delegated ``Chat.ReadWrite`` path (application permissions
    are NOT supported).

    URL shape: ``POST {GRAPH_BASE}/me/chats/{chat_id}/messages/{message_id}/softDelete``.
    The ``/me/`` alias is required — the ``/chats/{id}/...`` form returns
    405 Method Not Allowed.

    Returns:
        True on 2xx from Graph; False on other 4xx/5xx (e.g. 403 when
        trying to delete someone else's message, 404 for a missing
        message). The False path is logged for observability.

    Raises:
        TokenExpiredError on 401 so the caller can re-acquire.
        RateLimitError on 429 so the caller can honour Retry-After.
    """
    headers = {"Authorization": f"Bearer {token}"}

    owned_transport = transport or RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())
    async with httpx.AsyncClient(transport=owned_transport) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/me/chats/{chat_id}/messages/{message_id}/softDelete",
            headers=headers,
        )
        if resp.status_code == 401:
            raise TokenExpiredError("Token expired — re-acquire")
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            raise RateLimitError(retry_after)
        if 200 <= resp.status_code < 300:
            return True
        logger.warning(
            "softDelete chat message %s in chat %s failed: %d %s",
            message_id,
            chat_id,
            resp.status_code,
            resp.text[:200],
        )
        return False


async def fetch_hosted_image(*, token: str, url: str) -> bytes | None:
    """Fetch an image from a Graph API hosted content URL.

    Only accepts URLs under ``graph.microsoft.com`` to prevent
    leaking the Bearer token to arbitrary hosts.

    Returns the raw image bytes on success, None on 404,
    raises TokenExpiredError on 401.
    """
    if "graph.microsoft.com" not in url:
        raise ValueError(f"URL is not a Graph API URL: {url}")

    headers = {"Authorization": f"Bearer {token}"}

    transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 401:
            raise TokenExpiredError("Token expired — re-acquire")
        if resp.status_code == 404:
            return None
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            raise RateLimitError(retry_after)
        resp.raise_for_status()
        return resp.content


# Teams chats (unlike channels) don't populate `replyToId` at the message
# level. When a user hits the "Reply" UI in a chat, Graph encodes the quote
# as an ``<attachment id="SOURCE_MESSAGE_ID"></attachment>`` tag embedded at
# the start of the body HTML. We parse those out into ``reply_to_ids`` so
# the agent can detect "is this a reply to one of my messages?" without
# additional Graph calls.
_REPLY_ATTACHMENT_RE = re.compile(
    r'<attachment\s+id=(?P<quote>["\'])(?P<id>[^"\']+)(?P=quote)[^>]*>',
    re.IGNORECASE,
)


def extract_reply_to_ids(body_content: str) -> list[str]:
    """Return message IDs referenced as Teams chat 'reply' quote-attachments.

    Returns an empty list if *body_content* has no ``<attachment id="...">``
    tags or is falsy. Deduplicates while preserving order.
    """
    if not body_content:
        return []
    seen: set[str] = set()
    ids: list[str] = []
    for m in _REPLY_ATTACHMENT_RE.finditer(body_content):
        msg_id = m.group("id")
        if msg_id and msg_id not in seen:
            seen.add(msg_id)
            ids.append(msg_id)
    return ids


async def fetch_chat_type(*, chat_id: str, token: str) -> str:
    """Look up a chat's ``chatType`` via Graph. Returns one of
    ``"oneOnOne"``, ``"group"``, ``"meeting"``, or ``""`` on any
    failure. Fail-open: never raises. Caller is responsible for
    caching — Graph rate limits apply.
    """
    if not chat_id or not token:
        return ""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{GRAPH_BASE}/chats/{chat_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={"$select": "id,chatType"},
            )
            if resp.status_code != 200:
                return ""
            return str(resp.json().get("chatType") or "")
    except Exception:  # noqa: BLE001 - fail-open
        return ""


async def fetch_message(
    *,
    chat_id: str,
    message_id: str,
    token: str,
) -> dict | None:
    """Fetch a single chat message's body by ID. Fail-open.

    Used by the channel-push path to inline quoted context when a user
    hits the Teams "Reply" UI: :func:`extract_reply_to_ids` yields the
    source IDs, and we fetch each one to give the agent the original
    message without a tool round-trip.

    Returns the same shape as :func:`read` entries minus ``reply_to_ids``.
    Returns ``None`` on any 4xx/5xx — this is observability, not
    security; losing a quote fetch must not block the primary push.
    Caller should log a warning and proceed.

    Raises :class:`TokenExpiredError` on 401 so the standard refresh
    wrapper (``_with_token_retry``) can re-drive the call.
    """
    headers = {"Authorization": f"Bearer {token}"}

    transport = RetryOn429Transport(wrapped=httpx.AsyncHTTPTransport())
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/chats/{chat_id}/messages/{message_id}",
            headers=headers,
        )
        if resp.status_code == 401:
            raise TokenExpiredError("Token expired — re-acquire")
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            raise RateLimitError(retry_after)
        if not (200 <= resp.status_code < 300):
            return None

        m = resp.json()
        return {
            "message_id": m.get("id", message_id),
            "from": (m.get("from") or {}).get("user", {}).get("displayName", "unknown"),
            "content": (m.get("body") or {}).get("content", "") or "",
            "sent_at": m.get("createdDateTime"),
        }


async def read(
    *,
    chat_id: str,
    token: str,
    count: int = 5,
) -> list[dict]:
    """Read recent messages from the human in the Teams chat.

    Returns up to *count* most recent messages, newest first. Each entry
    includes a ``reply_to_ids`` list — message IDs this message is an
    explicit quote-reply to (empty for regular chat messages).
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
        out: list[dict] = []
        for m in messages:
            body_content = (m.get("body") or {}).get("content", "") or ""
            # Surface Graph's ``attachments`` array so downstream tools
            # (e.g. view_image) can resolve the hostedContents URL for
            # UUID-tagged inline images. The body HTML only contains
            # ``<attachment id="UUID">`` — the contentUrl lives here.
            raw_attachments = m.get("attachments") or []
            attachments = [
                {
                    "id": a.get("id"),
                    "content_type": a.get("contentType"),
                    "content_url": a.get("contentUrl"),
                    "name": a.get("name"),
                    "thumbnail_url": a.get("thumbnailUrl"),
                }
                for a in raw_attachments
            ]
            user_obj = (m.get("from") or {}).get("user", {}) or {}
            sender_id = str(user_obj.get("id") or "")
            sender_email = user_obj.get("userPrincipalName") or user_obj.get("mail") or ""
            out.append(
                {
                    "message_id": m["id"],
                    "from": user_obj.get("displayName", "unknown"),
                    "sender": str(sender_email or ""),
                    "sender_id": sender_id,
                    "content": body_content,
                    "content_text": body_content,
                    "sent_at": m.get("createdDateTime"),
                    "reply_to_ids": extract_reply_to_ids(body_content),
                    "attachments": attachments,
                }
            )
        return out


def filter_human_messages(
    messages: list[dict],
    agent_user_display_name: str,
    *,
    sent_message_ids: set[str] | None = None,
) -> list[dict]:
    """Return only messages from the human (not the agent, not system messages).

    Filters out:
    - Messages where ``from`` starts with the agent's display name. Graph
      can append a persona suffix (e.g. "EntraBot Agent (sati-agent)"),
      so prefix-match instead of exact-match to catch agent echoes.
    - Messages where ``from`` is ``"unknown"`` (system messages with null from field)
    - Messages whose ``message_id`` is in *sent_message_ids* (echo prevention for delegated mode)
    - Messages whose content starts with ``[EntraBot]`` (restart-safe dedup filter)

    All filtering is client-side — Graph API ``$filter`` is unreliable for chat messages.
    """
    exclude_ids = sent_message_ids or set()

    def _is_agent(sender: str) -> bool:
        if not agent_user_display_name:
            return False
        return sender == agent_user_display_name or sender.startswith(agent_user_display_name + " ")

    return [
        m
        for m in messages
        if not _is_agent(m.get("from", ""))
        and m.get("from") != "unknown"
        and m.get("message_id") not in exclude_ids
        and not (m.get("content", "").strip().startswith("[EntraBot]"))
    ]
