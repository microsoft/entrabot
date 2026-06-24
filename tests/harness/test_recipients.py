"""Tests for federated-recipient (ENTRABOT_HUMAN_*) list management.

Recipients are stored as parallel, positionally-aligned CSVs in the shared global config. This
module parses that into records, edits them, and serializes back — the shared representation
behind both `entrabot init` and `entrabot users`.
"""

from entrabot.harness import globalcfg
from entrabot.harness import recipients as rc


# ── Role (sponsor flag) ───────────────────────────────────────────────────────
def test_role_defaults_to_guest_when_flags_absent():
    # legacy block with no SPONSOR_FLAGS column → everyone defaults Guest (decided behavior)
    [r] = rc.parse({"ENTRABOT_HUMAN_UPNS": "jaly@microsoft.com"})
    assert r.sponsor is False


def test_role_parses_and_round_trips_flags():
    env = {
        "ENTRABOT_HUMAN_USER_IDS": "g1,m1",
        "ENTRABOT_HUMAN_UPNS": "jaly@microsoft.com,bob@corp.com",
        "ENTRABOT_HUMAN_USER_TENANT_IDS": "ms-tid,",
        "ENTRABOT_HUMAN_USER_MAILS": "jaly@microsoft.com,bob@corp.com",
        "ENTRABOT_HUMAN_USER_TYPES": "Guest,Member",
        "ENTRABOT_HUMAN_SPONSOR_FLAGS": "1,0",  # jaly elevated, bob not
        "ENTRABOT_HUMAN_USER_ID": "g1",
        "ENTRABOT_HUMAN_UPN": "jaly@microsoft.com",
    }
    recs = rc.parse(env)
    assert [r.sponsor for r in recs] == [True, False]
    assert rc.to_env(recs) == env  # round-trips, flags column included


def test_set_role_elevates_and_demotes():
    recs = [rc.Recipient(upn="jaly@microsoft.com", mail="jaly@microsoft.com"),
            rc.Recipient(upn="bob@corp.com")]
    recs, changed = rc.set_role(recs, "jaly@microsoft.com", sponsor=True)
    assert changed is True
    assert next(r for r in recs if r.upn == "jaly@microsoft.com").sponsor is True
    # idempotent: setting the same role reports no change
    recs, changed = rc.set_role(recs, "jaly@microsoft.com", sponsor=True)
    assert changed is False
    # unknown user
    _, changed = rc.set_role(recs, "ghost@x.com", sponsor=True)
    assert changed is False


def test_upsert_preserves_existing_role():
    existing = [rc.Recipient(upn="jaly@microsoft.com", user_type="Guest", sponsor=True)]
    # re-resolving jaly (e.g. `users add` again) yields a fresh record with sponsor=False default
    fresh = [rc.Recipient(upn="JALY@microsoft.com", user_type="Guest", tenant_id="ms")]
    merged = rc.upsert(existing, fresh)
    [r] = merged
    assert r.sponsor is True  # role preserved, not silently demoted
    assert r.tenant_id == "ms"  # but the freshly-resolved data is taken


# ── parse / to_env ────────────────────────────────────────────────────────────
def test_parse_empty_is_empty_list():
    assert rc.parse({}) == []
    assert rc.parse({"ENTRABOT_HUMAN_UPNS": ""}) == []


def test_parse_single_guest():
    [r] = rc.parse({
        "ENTRABOT_HUMAN_USER_IDS": "g1",
        "ENTRABOT_HUMAN_UPNS": "jaly@microsoft.com",
        "ENTRABOT_HUMAN_USER_TENANT_IDS": "ms-tid",
        "ENTRABOT_HUMAN_USER_MAILS": "jaly@microsoft.com",
        "ENTRABOT_HUMAN_USER_TYPES": "Guest",
    })
    assert r.upn == "jaly@microsoft.com"
    assert r.tenant_id == "ms-tid"
    assert r.user_type == "Guest"
    assert r.is_guest is True


def test_parse_preserves_positional_alignment_with_empty_slots():
    recs = rc.parse({
        "ENTRABOT_HUMAN_USER_IDS": "g1,m1",
        "ENTRABOT_HUMAN_UPNS": "jaly@microsoft.com,bob@corp.com",
        "ENTRABOT_HUMAN_USER_TENANT_IDS": "ms-tid,",  # member slot empty, must not desync
        "ENTRABOT_HUMAN_USER_TYPES": "Guest,Member",
    })
    assert [r.upn for r in recs] == ["jaly@microsoft.com", "bob@corp.com"]
    assert recs[0].tenant_id == "ms-tid"
    assert recs[1].tenant_id == ""  # member, no federation
    assert recs[1].is_guest is False


def test_parse_falls_back_to_singular_keys():
    [r] = rc.parse({"ENTRABOT_HUMAN_UPN": "solo@corp.com", "ENTRABOT_HUMAN_USER_ID": "s1"})
    assert r.upn == "solo@corp.com"
    assert r.user_id == "s1"
    assert r.user_type == "Member"  # default when types absent


def test_to_env_round_trips_through_parse():
    env = {
        "ENTRABOT_HUMAN_USER_IDS": "g1,m1",
        "ENTRABOT_HUMAN_UPNS": "jaly@microsoft.com,bob@corp.com",
        "ENTRABOT_HUMAN_USER_TENANT_IDS": "ms-tid,",
        "ENTRABOT_HUMAN_USER_MAILS": "jaly@microsoft.com,bob@corp.com",
        "ENTRABOT_HUMAN_USER_TYPES": "Guest,Member",
        "ENTRABOT_HUMAN_SPONSOR_FLAGS": "1,0",
        "ENTRABOT_HUMAN_USER_ID": "g1",
        "ENTRABOT_HUMAN_UPN": "jaly@microsoft.com",
    }
    assert rc.to_env(rc.parse(env)) == env


def test_to_env_empty_is_empty_dict():
    assert rc.to_env([]) == {}


def test_to_env_singulars_track_primary():
    env = rc.to_env([rc.Recipient(upn="a@x.com", user_id="a"),
                     rc.Recipient(upn="b@x.com", user_id="b")])
    assert env["ENTRABOT_HUMAN_UPN"] == "a@x.com"
    assert env["ENTRABOT_HUMAN_USER_ID"] == "a"


# ── upsert / remove ───────────────────────────────────────────────────────────
def test_upsert_adds_new_and_replaces_by_key_case_insensitive():
    existing = [rc.Recipient(upn="bob@corp.com", user_type="Member")]
    merged = rc.upsert(existing, [
        rc.Recipient(upn="JALY@microsoft.com", user_type="Guest", tenant_id="ms"),
        rc.Recipient(upn="bob@corp.com", user_id="bob-updated", user_type="Member"),
    ])
    by_upn = {r.upn.lower(): r for r in merged}
    assert len(merged) == 2
    assert by_upn["jaly@microsoft.com"].is_guest
    assert by_upn["bob@corp.com"].user_id == "bob-updated"  # replaced, not duplicated


def test_remove_by_upn_reports_change():
    recs = [rc.Recipient(upn="a@x.com"), rc.Recipient(upn="b@x.com")]
    kept, changed = rc.remove(recs, "A@X.com")
    assert changed is True
    assert [r.upn for r in kept] == ["b@x.com"]


def test_remove_missing_reports_no_change():
    recs = [rc.Recipient(upn="a@x.com")]
    kept, changed = rc.remove(recs, "ghost@x.com")
    assert changed is False
    assert len(kept) == 1


def test_remove_matches_mail_alias():
    recs = [rc.Recipient(upn="jaly_microsoft.com#EXT#@sdnnm.onmicrosoft.com",
                         mail="jaly@microsoft.com", user_type="Guest")]
    kept, changed = rc.remove(recs, "jaly@microsoft.com")  # match on home SMTP alias
    assert changed is True
    assert kept == []


# ── load_global / save_global ─────────────────────────────────────────────────
def test_save_and_load_global_preserves_tenant(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path))
    for k in list(__import__("os").environ):
        if k.startswith(globalcfg.HUMAN_PREFIX):
            monkeypatch.delenv(k, raising=False)
    globalcfg.write_env(globalcfg.global_env_path(),
                        {"ENTRABOT_TENANT_ID": "tid", "ENTRABOT_BLUEPRINT_APP_ID": "bp"})

    rc.save_global([rc.Recipient(upn="jaly@microsoft.com", user_id="g1",
                                 tenant_id="ms-tid", user_type="Guest")])

    saved = globalcfg.read_env(globalcfg.global_env_path())
    assert saved["ENTRABOT_TENANT_ID"] == "tid"  # untouched
    assert saved["ENTRABOT_HUMAN_USER_TENANT_IDS"] == "ms-tid"
    loaded = rc.load_global()
    assert [r.upn for r in loaded] == ["jaly@microsoft.com"]
    import os
    assert os.environ["ENTRABOT_HUMAN_UPNS"] == "jaly@microsoft.com"  # live process updated


def test_save_global_empty_clears_block(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path))
    globalcfg.write_env(globalcfg.global_env_path(), {
        "ENTRABOT_TENANT_ID": "tid",
        "ENTRABOT_HUMAN_UPNS": "old@corp.com",
        "ENTRABOT_HUMAN_USER_TYPES": "Member",
    })
    rc.save_global([])
    saved = globalcfg.read_env(globalcfg.global_env_path())
    assert saved["ENTRABOT_TENANT_ID"] == "tid"
    assert not any(k.startswith(globalcfg.HUMAN_PREFIX) for k in saved)
