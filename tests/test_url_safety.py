"""URL safety helpers."""

from __future__ import annotations

import pytest

from entrabot.url_safety import _is_graph_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Commercial cloud (worldwide)
        ("https://graph.microsoft.com/v1.0/me", True),
        ("https://GRAPH.MICROSOFT.COM/", True),
        # US Government L4 (GCC High)
        ("https://graph.microsoft.us/v1.0/me", True),
        ("https://GRAPH.MICROSOFT.US/", True),
        # US Government L5 (DoD)
        ("https://dod-graph.microsoft.us/v1.0/me", True),
        # China (operated by 21Vianet)
        ("https://microsoftgraph.chinacloudapi.cn/v1.0/me", True),
        # Bypass attempts that look like a sovereign host
        ("https://attacker.com/?x=graph.microsoft.com", False),
        ("https://graph.microsoft.com.attacker.com/", False),
        ("https://attacker.com/graph.microsoft.com/", False),
        ("https://graph.microsoft.us.attacker.com/", False),
        ("https://dod-graph.microsoft.us.attacker.com/", False),
        ("https://microsoftgraph.chinacloudapi.cn.attacker.com/", False),
        ("https://attacker.com/microsoftgraph.chinacloudapi.cn/", False),
        # Userinfo rejection
        ("https://user:pwd@graph.microsoft.com/", False),
        ("https://user:pwd@graph.microsoft.us/", False),
        # Non-https rejection
        ("http://graph.microsoft.com/", False),
        ("http://graph.microsoft.us/", False),
        # Retired Germany cloud — explicitly not on the allowlist
        ("https://graph.microsoft.de/v1.0/me", False),
        # Other Microsoft domains that are NOT the Graph endpoint
        ("https://login.microsoftonline.com/", False),
        ("https://management.azure.com/", False),
    ],
)
def test_is_graph_url_requires_exact_https_graph_host_without_userinfo(
    url: str, expected: bool
) -> None:
    assert _is_graph_url(url) is expected
