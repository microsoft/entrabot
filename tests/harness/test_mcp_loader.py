import json

from entrabot.harness import mcp_loader


def test_stdio_and_http(tmp_path):
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "foo": {"command": "node", "args": ["x.js"], "env": {"K": "V"}},
                    "bar": {"url": "https://h/mcp", "headers": {"A": "B"}},
                }
            }
        )
    )
    res = mcp_loader.load(str(tmp_path))
    assert res is not None
    assert res["foo"]["command"] == "node" and res["foo"]["args"] == ["x.js"]
    assert res["foo"]["env"] == {"K": "V"} and res["foo"]["type"] == "stdio"
    assert res["bar"]["url"].endswith("/mcp") and res["bar"]["type"] == "http"
    assert res["bar"]["headers"] == {"A": "B"}


def test_servers_key_alias(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"servers": {"foo": {"command": "x"}}}))
    res = mcp_loader.load(str(tmp_path))
    assert res is not None and "foo" in res


def test_none_when_absent(tmp_path):
    assert mcp_loader.load(str(tmp_path)) is None


def test_malformed_returns_none(tmp_path):
    (tmp_path / ".mcp.json").write_text("{ not json")
    assert mcp_loader.load(str(tmp_path)) is None
