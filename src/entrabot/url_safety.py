"""URL validation helpers for token-bearing requests."""

from __future__ import annotations

from urllib.parse import urlparse


def _is_graph_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower() == "graph.microsoft.com"
        and not parsed.username
        and not parsed.password
    )
