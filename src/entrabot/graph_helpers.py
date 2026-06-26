"""Shared Microsoft Graph API utilities.

Centralises the ``graph_request``, ``odata_escape``, ``graph_collection_values``,
and ``resolve_user_by_email`` helpers that were previously copy-pasted across
five or more provisioning scripts.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import requests

from entrabot.tools.rate_limit import parse_retry_after
from entrabot.url_safety import _is_graph_url

GRAPH_BETA = "https://graph.microsoft.com/beta"
GRAPH_V1 = "https://graph.microsoft.com/v1.0"

_RETRYABLE = frozenset({429, 500, 502, 503, 504})
logger = logging.getLogger(__name__)


class GraphPaginationError(RuntimeError):
    """Graph collection pagination failed."""


class UnsafeGraphNextLinkError(GraphPaginationError):
    """Graph returned an unsafe ``@odata.nextLink`` URL."""


def _next_link_origin(next_link: str) -> str:
    try:
        parsed = urlparse(next_link)
    except Exception:
        return "<unparseable>"
    scheme = parsed.scheme or "<missing>"
    host = parsed.hostname or "<missing>"
    return f"{scheme}://{host}"


def odata_escape(value: str) -> str:
    """Escape single quotes for OData ``$filter`` strings."""
    return value.replace("'", "''")


def graph_request(
    method: str,
    path: str,
    token: str,
    json_body: dict | None = None,
    *,
    retry: bool = True,
    base_url: str = GRAPH_BETA,
    timeout: int = 30,
) -> requests.Response:
    """Make a request to the Microsoft Graph API with optional retry.

    Parameters
    ----------
    method:
        HTTP method (GET, POST, PATCH, DELETE, …).
    path:
        Graph path *after* the base URL (e.g. ``/users``).
    token:
        Bearer token.
    json_body:
        Optional JSON payload for POST/PATCH.
    retry:
        If *True* (default), retry once on 429 or 5xx using the
        ``Retry-After`` header (defaults to 10 s).
    base_url:
        API base; defaults to the beta endpoint.  Pass
        :data:`GRAPH_V1` for endpoints that require v1.0.
    timeout:
        Request timeout in seconds (default 30).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    url = f"{base_url}{path}"
    resp = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)

    if retry and resp.status_code in _RETRYABLE:
        wait = parse_retry_after(resp.headers.get("Retry-After"), default=10)
        print(f"  Graph API returned {resp.status_code}; retrying in {wait}s…")
        time.sleep(wait)
        resp = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)

    return resp


def require_ok(resp: requests.Response, action: str) -> None:
    """Raise :class:`RuntimeError` unless *resp* indicates success (2xx)."""
    if resp.status_code in (200, 201, 204):
        return
    raise RuntimeError(f"{action} failed ({resp.status_code}): {resp.text[:500]}")


def graph_collection_values(
    path: str,
    token: str,
    action: str = "Graph collection request",
    *,
    base_url: str = GRAPH_BETA,
) -> list[dict]:
    """Fetch all pages of a Graph collection and return the merged ``value`` list."""
    values: list[dict] = []
    resp = graph_request("GET", path, token, base_url=base_url, timeout=30)
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"{action} failed ({resp.status_code}): {resp.text[:500]}")
    data = resp.json()
    values.extend(data.get("value", []))
    next_link = data.get("@odata.nextLink")
    while isinstance(next_link, str):
        if not _is_graph_url(next_link):
            logger.warning(
                "Graph returned unsafe @odata.nextLink origin: %s",
                _next_link_origin(next_link),
            )
            raise UnsafeGraphNextLinkError(f"{action} failed: unsafe @odata.nextLink")
        resp = requests.request(
            "GET",
            next_link,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"{action} failed ({resp.status_code}): {resp.text[:500]}")
        data = resp.json()
        values.extend(data.get("value", []))
        next_link = data.get("@odata.nextLink")
    return values


def resolve_user_by_email(token: str, email: str) -> tuple[str, str]:
    """Resolve an email address to ``(object_id, display_name)`` in the tenant.

    Tries, in order: ``userPrincipalName eq``, ``mail eq``,
    ``otherMails/any``, ``proxyAddresses/any (smtp:)``,
    ``proxyAddresses/any (SMTP:)``.

    Raises :class:`LookupError` if the user cannot be found.
    """
    headers = {"Authorization": f"Bearer {token}"}
    select = "id,displayName,userPrincipalName,mail,proxyAddresses"
    quoted = odata_escape(email)

    filters = (
        f"userPrincipalName eq '{quoted}'",
        f"mail eq '{quoted}'",
        f"otherMails/any(m: m eq '{quoted}')",
        f"proxyAddresses/any(p: p eq 'smtp:{quoted}')",
        f"proxyAddresses/any(p: p eq 'SMTP:{quoted}')",
    )

    for filt in filters:
        url = f"{GRAPH_BETA}/users?$filter={filt}&$select={select}"
        resp = requests.get(url, headers=headers, params={"$count": "true"}, timeout=15)
        # Some advanced queries require ConsistencyLevel: eventual
        if resp.status_code == 400:
            resp = requests.get(
                url,
                headers={**headers, "ConsistencyLevel": "eventual"},
                timeout=15,
            )
        if resp.status_code != 200:
            continue
        results = resp.json().get("value", [])
        if results:
            user = results[0]
            return user["id"], user.get("displayName") or email

    raise LookupError(
        f"Could not resolve {email!r} to a user object in the tenant. "
        "Is this person a guest? Try inviting them via 'az ad user invite' first."
    )
