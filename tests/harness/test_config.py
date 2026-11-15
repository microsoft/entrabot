from entrabot.harness.config import HarnessConfig, exists, save, try_load


def test_roundtrip_camelcase_and_null_omit():
    c = HarnessConfig(name="bot", description="d", model="gpt-5", watched_chats=["c1"])
    c.ensure_identity()
    d = c.to_json_dict()
    assert d["name"] == "bot"
    assert d["model"] == "gpt-5"
    assert d["watchedChats"] == ["c1"]
    assert "agentId" in d and "createdUtc" in d
    # None fields are omitted
    assert "reasoningEffort" not in d
    assert "contextTier" not in d


def test_save_and_load(tmp_path):
    root = str(tmp_path)
    c = HarnessConfig(name="bot", description="d")
    c.ensure_identity()
    save(root, c)
    assert exists(root)
    loaded = try_load(root)
    assert loaded is not None
    assert loaded.name == "bot" and loaded.agent_id == c.agent_id


def test_try_load_missing(tmp_path):
    assert try_load(str(tmp_path)) is None


def test_ensure_identity_idempotent():
    c = HarnessConfig(name="b", description="d")
    assert c.ensure_identity() is True
    aid = c.agent_id
    assert c.ensure_identity() is False
    assert c.agent_id == aid
