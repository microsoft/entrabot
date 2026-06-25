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


def test_skips_entrabot_body_mcp_by_name(tmp_path):
    # The harness IS entrabot — loading the entrabot MCP body would double the Teams tools and
    # spawn a second background poller. Drop it; keep the others.
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "entrabot": {"command": "/x/.venv/bin/entrabot-mcp"},
        "ado": {"command": "agency", "args": ["mcp", "ado"]},
    }}))
    skipped = []
    res = mcp_loader.load(str(tmp_path), on_skip=skipped.append)
    assert res is not None and "entrabot" not in res and "ado" in res
    assert skipped == ["entrabot"]


def test_skips_entrabot_body_mcp_by_command_even_if_renamed(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "myagent": {"command": "C:\\x\\entrabot-mcp.exe"},
        "foo": {"command": "node"},
    }}))
    res = mcp_loader.load(str(tmp_path))
    assert res is not None and "myagent" not in res and "foo" in res


def test_only_entrabot_body_returns_none(tmp_path):
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"entrabot": {"command": "entrabot-mcp"}}}))
    assert mcp_loader.load(str(tmp_path)) is None  # nothing left to load
