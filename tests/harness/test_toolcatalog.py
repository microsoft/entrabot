from entrabot.harness.session.toolcatalog import group_sections


def test_group_sections_orders_native_mcp_skills():
    catalog = [
        {"name": "view", "section": "Native", "kind": "tool"},
        {"name": "edit", "section": "Native", "kind": "tool"},
        {"name": "entrabot-send_teams_message", "section": "MCP · entrabot", "kind": "tool"},
        {"name": "github-mcp-server-search_code", "section": "MCP · github-mcp-server", "kind": "tool"},
        {"name": "docx", "section": "Skills", "kind": "skill"},
    ]
    sections = group_sections(catalog)
    names = [s for s, _ in sections]

    assert names[0] == "Native"  # native first
    assert names[-1] == "Skills"  # skills last
    # MCP servers sit between, alphabetical
    assert names.index("MCP · entrabot") < names.index("MCP · github-mcp-server")
    # items grouped under their section
    assert {i["name"] for i in dict(sections)["Native"]} == {"view", "edit"}
    assert len(dict(sections)["MCP · github-mcp-server"]) == 1


def test_group_sections_empty():
    assert group_sections([]) == []
