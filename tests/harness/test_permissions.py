from entrabot.harness import permissions


def test_toolpolicy_defaults():
    p = permissions.ToolPolicy()
    assert p.sponsor_all is True  # sponsors get everything by default
    assert p.guest_all is False
    assert p.guest == set()  # guests get nothing


def test_toolpolicy_config_roundtrip():
    p = permissions.ToolPolicy(sponsor_all=False, guest_all=False, sponsor={"edit", "view"}, guest={"view"})
    cfg = p.to_config()
    assert cfg == {"sponsor_all": False, "guest_all": False, "sponsor": ["edit", "view"], "guest": ["view"]}
    p2 = permissions.ToolPolicy.from_config(cfg)
    assert p2.sponsor == {"edit", "view"} and p2.guest == {"view"} and p2.sponsor_all is False


def test_allowed_per_tool_and_class():
    p = permissions.ToolPolicy(sponsor_all=False, guest_all=False, sponsor={"powershell", "view"}, guest={"view"})
    assert p.allowed("sponsor", "powershell") is True
    assert p.allowed("sponsor", "edit") is False
    assert p.allowed("guest", "view") is True
    assert p.allowed("guest", "powershell") is False


def test_allowed_all_overrides():
    p = permissions.ToolPolicy(sponsor_all=True, guest_all=False, sponsor=set(), guest=set())
    assert p.allowed("sponsor", "anything") is True  # sponsor_all
    assert p.allowed("guest", "anything") is False
    p2 = permissions.ToolPolicy(sponsor_all=False, guest_all=True, sponsor=set(), guest=set())
    assert p2.allowed("guest", "anything") is True  # guest_all


def _shell_input(tool):
    return {"toolName": tool, "toolArgs": {}, "sessionId": "s", "workingDirectory": "."}


async def test_gate_allows_and_denies_per_class():
    p = permissions.ToolPolicy(sponsor_all=False, guest_all=False, sponsor={"edit"}, guest=set())
    gate_s = permissions.build_tool_gate(p, lambda: "sponsor")
    gate_g = permissions.build_tool_gate(p, lambda: "guest")
    # sponsor: edit allowed, view denied
    assert (await gate_s(_shell_input("edit")))["permissionDecision"] == "allow"
    assert (await gate_s(_shell_input("view")))["permissionDecision"] == "deny"
    # guest: nothing allowed
    assert (await gate_g(_shell_input("edit")))["permissionDecision"] == "deny"
    # two-arg call (SDK passes context) + local operator (None -> sponsor)
    gate_local = permissions.build_tool_gate(p, lambda: None)
    assert (await gate_local(_shell_input("edit"), {"session_id": "s"}))["permissionDecision"] == "allow"


async def test_gate_force_yolo_allows_everything():
    p = permissions.ToolPolicy(sponsor_all=False, guest_all=False, sponsor=set(), guest=set())
    gate = permissions.build_tool_gate(p, lambda: "guest", force_yolo=True)
    assert (await gate(_shell_input("powershell")))["permissionDecision"] == "allow"


async def test_gate_passthrough_when_no_tool_name():
    p = permissions.ToolPolicy()
    gate = permissions.build_tool_gate(p, lambda: "guest")
    assert await gate({"toolArgs": {}}) is None  # no toolName -> don't intervene
