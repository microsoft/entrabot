from types import SimpleNamespace

from entrabot.harness import permissions


def ns(**kw):
    return SimpleNamespace(**kw)


def test_describe_kinds():
    assert permissions.describe(ns(kind="shell", full_command_text="rm -rf /"))[0] == "shell"
    assert permissions.describe(ns(kind="write", file_name="/x"))[0] == "write"
    assert permissions.describe(ns(kind="mcp", server_name="s", tool_name="t"))[0] == "mcp"
    assert permissions.describe(ns(kind="read", path="/x"))[0] == "read"


def test_toolpolicy_defaults():
    p = permissions.ToolPolicy()
    assert p.sponsor == {k for k, _ in permissions.TOOL_CATEGORIES}  # sponsors: all on
    assert p.guest == set()  # guests: nothing
    assert p.yolo is False


def test_toolpolicy_config_roundtrip():
    p = permissions.ToolPolicy(yolo=False, sponsor={"shell", "read"}, guest={"read"})
    cfg = p.to_config()
    assert cfg == {"yolo": False, "sponsor": ["read", "shell"], "guest": ["read"]}
    p2 = permissions.ToolPolicy.from_config(cfg)
    assert p2.sponsor == {"shell", "read"} and p2.guest == {"read"} and p2.yolo is False


def test_allowed_per_class():
    p = permissions.ToolPolicy(sponsor={"shell", "read"}, guest={"read"})
    assert p.allowed("sponsor", "shell") is True
    assert p.allowed("sponsor", "write") is False
    assert p.allowed("guest", "read") is True
    assert p.allowed("guest", "shell") is False


def test_allowed_yolo_overrides():
    p = permissions.ToolPolicy(yolo=True, sponsor=set(), guest=set())
    assert p.allowed("guest", "shell") is True
    assert p.allowed("sponsor", "write") is True


async def test_handler_two_arg_and_per_class():
    req = ns(kind="shell", full_command_text="ls")
    p = permissions.ToolPolicy(sponsor={"shell"}, guest=set())
    hs = permissions.build_permission_handler(p, lambda: "sponsor")
    # SDK calls handler(request, context); one-arg form also works
    assert type(await hs(req, {"session_id": "s"})).__name__ == "PermissionDecisionApproveOnce"
    assert type(await hs(req)).__name__ == "PermissionDecisionApproveOnce"
    hg = permissions.build_permission_handler(p, lambda: "guest")
    assert type(await hg(req)).__name__ == "PermissionDecisionReject"


async def test_handler_force_yolo():
    req = ns(kind="shell", full_command_text="ls")
    p = permissions.ToolPolicy(sponsor=set(), guest=set())
    h = permissions.build_permission_handler(p, lambda: "guest", force_yolo=True)
    assert type(await h(req)).__name__ == "PermissionDecisionApproveOnce"


async def test_handler_local_operator_is_sponsor():
    req = ns(kind="read", path="/x")
    p = permissions.ToolPolicy(sponsor={"read"}, guest=set())
    h = permissions.build_permission_handler(p, lambda: None)  # local operator -> sponsor
    assert type(await h(req)).__name__ == "PermissionDecisionApproveOnce"
