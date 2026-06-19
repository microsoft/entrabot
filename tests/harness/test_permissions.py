from types import SimpleNamespace

from entrabot.harness import permissions


def ns(**kw):
    return SimpleNamespace(**kw)


def test_describe_kinds():
    assert permissions.describe(ns(kind="shell", full_command_text="rm -rf /"))[0] == "shell"
    assert permissions.describe(ns(kind="write", file_name="/x"))[0] == "write"
    assert permissions.describe(ns(kind="mcp", server_name="s", tool_name="t"))[0] == "mcp"


def test_policy_kind_and_glob():
    p = permissions.PermissionPolicy.from_config({"default": {"mode": "allow", "deny": ["shell:rm*", "write"]}})
    pol = p.for_caller(None)
    assert pol.decide("shell", "rm file") == "deny"
    assert pol.decide("shell", "ls") == "allow"
    assert pol.decide("write", "/etc/x") == "deny"


def test_per_caller_override_case_insensitive():
    p = permissions.PermissionPolicy.from_config(
        {"default": {"mode": "deny"}, "callers": {"Boss@Contoso.com": {"mode": "allow"}}}
    )
    assert p.for_caller("boss@contoso.com").decide("shell", "anything") == "allow"
    assert p.for_caller("guest@x.com").decide("read", "/x") == "deny"


async def test_handler_accepts_sdk_two_arg_call():
    req = ns(kind="shell", full_command_text="ls")
    pol = permissions.PermissionPolicy.from_config({"default": {"mode": "deny"}})
    h = permissions.build_permission_handler(pol, lambda: None, yolo=False)
    # the SDK calls handler(request, {"session_id": ...})
    dec = await h(req, {"session_id": "s1"})
    assert type(dec).__name__ == "PermissionDecisionReject"
    # and the one-arg form still works
    assert type(await h(req)).__name__ == "PermissionDecisionReject"


async def test_yolo_does_not_override_explicit_deny():
    req = ns(kind="shell", full_command_text="ls")
    pol = permissions.PermissionPolicy.from_config({"default": {"mode": "deny"}})
    h = permissions.build_permission_handler(pol, lambda: None, yolo=True)
    assert type(await h(req)).__name__ == "PermissionDecisionReject"


async def test_yolo_approves_ask():
    req = ns(kind="shell", full_command_text="ls")
    pol = permissions.PermissionPolicy.from_config({"default": {"mode": "ask"}})
    h = permissions.build_permission_handler(pol, lambda: None, yolo=True)
    assert type(await h(req)).__name__ == "PermissionDecisionApproveOnce"


async def test_ask_fail_closed_without_ui():
    req = ns(kind="shell", full_command_text="ls")
    pol = permissions.PermissionPolicy.from_config({"default": {"mode": "ask"}})
    h = permissions.build_permission_handler(pol, lambda: None, yolo=False)  # no confirm
    assert type(await h(req)).__name__ == "PermissionDecisionReject"
