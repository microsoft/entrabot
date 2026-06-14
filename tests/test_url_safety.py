"""URL safety helpers."""

from __future__ import annotations

import pytest

from entrabot.url_safety import _is_graph_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://graph.microsoft.com/v1.0/me", True),
        ("https://attacker.com/?x=graph.microsoft.com", False),
        ("https://graph.microsoft.com.attacker.com/", False),
        ("https://attacker.com/graph.microsoft.com/", False),
        ("https://user:pwd@graph.microsoft.com/", False),
        ("http://graph.microsoft.com/", False),
        ("https://GRAPH.MICROSOFT.COM/", True),
    ],
)
def test_is_graph_url_requires_exact_https_graph_host_without_userinfo(
    url: str, expected: bool
) -> None:
    assert _is_graph_url(url) is expected
