"""URL validation helpers for token-bearing requests."""

from __future__ import annotations

from urllib.parse import urlparse

# Microsoft Graph endpoints across all national clouds.
# - graph.microsoft.com               — Worldwide (commercial)
# - graph.microsoft.us                — US Government L4 (GCC High)
# - dod-graph.microsoft.us            — US Government L5 (DoD)
# - microsoftgraph.chinacloudapi.cn   — China (operated by 21Vianet)
#
# The Germany cloud (graph.microsoft.de) was decommissioned 2021-10-29
# and is intentionally NOT on this allowlist.
GRAPH_HOSTS: frozenset[str] = frozenset(
    {
        "graph.microsoft.com",
        "graph.microsoft.us",
        "dod-graph.microsoft.us",
        "microsoftgraph.chinacloudapi.cn",
    }
)


def _is_graph_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower() in GRAPH_HOSTS
        and not parsed.username
        and not parsed.password
    )
