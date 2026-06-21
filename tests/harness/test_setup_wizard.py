"""Tests for the `entrabot init` Teams-recipient stage.

The wizard resolves one or more recipient emails into the ENTRABOT_HUMAN_* block, detecting B2B
guests (userType == 'Guest' or a '#EXT#' UPN) and resolving them to their HOME tenant so federated
chat reaches the real identity — never the local guest object id (hard-won learning #28). The
az / OpenID lookups are injected so these tests stay offline.
"""

import pytest

from entrabot.harness import globalcfg
from entrabot.harness import setup_wizard as sw


# ── fake lookups ──────────────────────────────────────────────────────────────
def _fake_az(directory):
    """directory: email -> az-style user dict (or absent → not found)."""

    def show(email):
        return directory.get(email)

    return show


def _fake_tenant(mapping):
    """mapping: home-domain -> tenant GUID."""
    return lambda domain: mapping.get(domain, "")


# ── _az_user_show fallback (B2B guests aren't found by --id <home-email>) ──────
def _az_runner(responses):
    """Build a fake `run` for _az_user_show. responses: list of (predicate, result) — the first
    whose predicate matches the az args wins; default None (miss)."""

    def run(args):
        for pred, result in responses:
            if pred(args):
                return result
        return None

    return run


def test_az_user_show_direct_hit_for_member():
    run = _az_runner([(lambda a: a[2] == "show", {"id": "m1", "userType": "Member",
                                                  "mail": "bob@corp.com", "upn": "bob@corp.com"})])
    assert sw._az_user_show("bob@corp.com", run=run)["id"] == "m1"


def test_az_user_show_falls_back_to_mail_filter_for_guest():
    # direct `--id` misses (guest UPN is mangled); the `mail eq` list filter finds them.
    guest = {"id": "g1", "userType": None, "mail": "jaly@microsoft.com",
             "upn": "jaly_microsoft.com#EXT#@sdnnm.onmicrosoft.com"}
    run = _az_runner([
        (lambda a: a[2] == "show", None),
        (lambda a: a[2] == "list" and "mail eq" in a[4], guest),
    ])
    got = sw._az_user_show("jaly@microsoft.com", run=run)
    assert got["upn"].endswith("#EXT#@sdnnm.onmicrosoft.com")
    assert got["mail"] == "jaly@microsoft.com"


def test_az_user_show_falls_back_to_upn_prefix():
    guest = {"id": "g2", "userType": None, "mail": None,
             "upn": "alice_example.com#EXT#@sdnnm.onmicrosoft.com"}
    run = _az_runner([
        (lambda a: a[2] == "show", None),
        (lambda a: a[2] == "list" and "mail eq" in a[4], None),  # no mail on this guest
        (lambda a: a[2] == "list" and "startsWith" in a[4], guest),
    ])
    assert sw._az_user_show("alice@example.com", run=run)["id"] == "g2"


def test_az_user_show_none_when_all_miss():
    assert sw._az_user_show("ghost@nowhere.com", run=_az_runner([])) is None


def test_az_user_show_guest_resolves_end_to_end():
    """The mail-filter fallback feeds resolve_teams_user, which classifies the null-userType guest
    via its #EXT# UPN and resolves the home tenant — the exact path that was failing live."""
    guest = {"id": "g1", "userType": None, "mail": "jaly@microsoft.com",
             "upn": "jaly_microsoft.com#EXT#@sdnnm.onmicrosoft.com"}

    def az(email):
        return sw._az_user_show(email, run=_az_runner([
            (lambda a: a[2] == "show", None),
            (lambda a: a[2] == "list" and "mail eq" in a[4], guest),
        ]))

    out = sw.resolve_teams_user("jaly@microsoft.com", az_show=az,
                                tenant_lookup=_fake_tenant({"microsoft.com": "72f988bf"}))
    assert out["ENTRABOT_HUMAN_USER_TYPES"] == "Guest"
    assert out["ENTRABOT_HUMAN_USER_TENANT_IDS"] == "72f988bf"


# ── resolve_teams_user ────────────────────────────────────────────────────────
def test_resolve_member_has_no_tenant_id():
    az = _fake_az(
        {"bob@corp.com": {"id": "bob-id", "userType": "Member", "mail": "bob@corp.com",
                          "upn": "bob@corp.com"}}
    )
    out = sw.resolve_teams_user("bob@corp.com", az_show=az, tenant_lookup=_fake_tenant({}))
    assert out["ENTRABOT_HUMAN_USER_IDS"] == "bob-id"
    assert out["ENTRABOT_HUMAN_UPNS"] == "bob@corp.com"
    assert out["ENTRABOT_HUMAN_USER_TYPES"] == "Member"
    assert out["ENTRABOT_HUMAN_USER_TENANT_IDS"] == ""  # in-tenant member → no federation
    # backward-compat singulars track the primary (first) recipient
    assert out["ENTRABOT_HUMAN_USER_ID"] == "bob-id"
    assert out["ENTRABOT_HUMAN_UPN"] == "bob@corp.com"


def test_resolve_guest_by_usertype_resolves_home_tenant():
    az = _fake_az(
        {"jaly@microsoft.com": {
            "id": "guest-shadow-id", "userType": "Guest", "mail": "jaly@microsoft.com",
            "upn": "jaly_microsoft.com#EXT#@sdnnm.onmicrosoft.com"}}
    )
    out = sw.resolve_teams_user(
        "jaly@microsoft.com", az_show=az,
        tenant_lookup=_fake_tenant({"microsoft.com": "72f988bf-tenant"}),
    )
    assert out["ENTRABOT_HUMAN_USER_TYPES"] == "Guest"
    assert out["ENTRABOT_HUMAN_USER_TENANT_IDS"] == "72f988bf-tenant"
    # federated chat binds on the email, not the local guest object id (learning #28)
    assert out["ENTRABOT_HUMAN_UPNS"] == "jaly@microsoft.com"
    assert out["ENTRABOT_HUMAN_USER_MAILS"] == "jaly@microsoft.com"


def test_resolve_guest_inferred_from_ext_upn_when_usertype_null():
    # az returns userType: null for some guests → infer from the #EXT# UPN pattern.
    az = _fake_az(
        {"alice@example.com": {
            "id": "ax", "userType": None, "mail": "alice@example.com",
            "upn": "alice_example.com#EXT#@sdnnm.onmicrosoft.com"}}
    )
    out = sw.resolve_teams_user(
        "alice@example.com", az_show=az,
        tenant_lookup=_fake_tenant({"example.com": "ex-tenant-guid"}),
    )
    assert out["ENTRABOT_HUMAN_USER_TYPES"] == "Guest"
    assert out["ENTRABOT_HUMAN_USER_TENANT_IDS"] == "ex-tenant-guid"


def test_resolve_group_preserves_positional_alignment():
    az = _fake_az({
        "jaly@microsoft.com": {
            "id": "g1", "userType": "Guest", "mail": "jaly@microsoft.com",
            "upn": "jaly_microsoft.com#EXT#@sdnnm.onmicrosoft.com"},
        "bob@corp.com": {
            "id": "m1", "userType": "Member", "mail": "bob@corp.com", "upn": "bob@corp.com"},
    })
    out = sw.resolve_teams_user(
        "jaly@microsoft.com, bob@corp.com", az_show=az,
        tenant_lookup=_fake_tenant({"microsoft.com": "ms-tid"}),
    )
    # every list has 2 comma-separated positions, aligned by index; the member's tenant slot is
    # empty (preserved, not dropped) so _parse_csv_preserve_empty keeps the lists in sync.
    assert out["ENTRABOT_HUMAN_USER_IDS"] == "g1,m1"
    assert out["ENTRABOT_HUMAN_USER_TENANT_IDS"] == "ms-tid,"
    assert out["ENTRABOT_HUMAN_USER_TYPES"] == "Guest,Member"
    assert out["ENTRABOT_HUMAN_UPNS"] == "jaly@microsoft.com,bob@corp.com"


def test_resolve_blank_returns_empty():
    out = sw.resolve_teams_user("  ", az_show=_fake_az({}), tenant_lookup=_fake_tenant({}))
    assert out == {}


def test_resolve_unknown_user_raises():
    with pytest.raises(sw.TeamsUserNotFound) as ei:
        sw.resolve_teams_user(
            "ghost@nowhere.com", az_show=_fake_az({}), tenant_lookup=_fake_tenant({})
        )
    assert "ghost@nowhere.com" in ei.value.emails


# ── _apply_existing_env (idempotent re-run / resume) ──────────────────────────
def test_apply_existing_env_layers_agent_over_global(tmp_path, monkeypatch):
    """Re-running `init` in a provisioned dir must load global (tenant/blueprint) + this dir's
    agent identity into the process so the connection re-test and recipient edit work."""
    import os

    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path / "home"))
    for k in ("ENTRABOT_TENANT_ID", "ENTRABOT_AGENT_USER_UPN", "ENTRABOT_AGENT_ID"):
        monkeypatch.delenv(k, raising=False)
    globalcfg.write_env(
        globalcfg.global_env_path(),
        {"ENTRABOT_TENANT_ID": "tid", "ENTRABOT_BLUEPRINT_APP_ID": "bp"},
    )
    agentdir = str(tmp_path / "proj")
    globalcfg.write_env(
        globalcfg.agent_env_path(agentdir),
        {"ENTRABOT_AGENT_ID": "ag", "ENTRABOT_AGENT_USER_UPN": "bot@x.onmicrosoft.com"},
    )

    sw._apply_existing_env(agentdir)
    assert os.environ["ENTRABOT_TENANT_ID"] == "tid"  # from global
    assert os.environ["ENTRABOT_AGENT_USER_UPN"] == "bot@x.onmicrosoft.com"  # from per-agent
