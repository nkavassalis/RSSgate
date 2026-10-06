from rssgate.config import load_config, save_config, masked_config, _merge, expand_env


def test_defaults_when_no_file(tmp_path):
    cfg = load_config(str(tmp_path / "missing.yaml"))
    assert cfg["server"]["port"] == 8088
    assert cfg["server"]["host"] == "127.0.0.1"   # loopback by default: no auth, so no wildcard bind
    assert cfg["llm"]["base_url"] == "http://10.1.13.99:8000/v1"
    assert cfg["polling"]["feed_interval_minutes"] == 30
    assert cfg["polling"]["page_interval_minutes"] == 180
    assert "{length}" in cfg["summarizer"]["system_prompt"]


def test_roundtrip_and_partial_override(tmp_path):
    path = str(tmp_path / "c.yaml")
    cfg = load_config(path)
    cfg["llm"]["model"] = "llama-3.1-8b"
    cfg["polling"]["feed_interval_minutes"] = 15
    save_config(cfg, path)
    again = load_config(path)
    assert again["llm"]["model"] == "llama-3.1-8b"
    assert again["polling"]["feed_interval_minutes"] == 15
    assert again["llm"]["provider"] == "local"  # untouched default survives


def test_merge_is_deep():
    merged = _merge({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}})
    assert merged == {"a": {"b": 1, "c": 3}}


def test_masked_config_hides_key():
    cfg = {"llm": {"api_key": "sk-secret", "provider": "openai"}, "server": {}}
    m = masked_config(cfg)
    assert m["llm"]["api_key"] == "***"
    assert m["llm"]["api_key_set"] is True


def test_env_expansion(monkeypatch):
    monkeypatch.setenv("MY_KEY", "abc123")
    assert expand_env("env:MY_KEY") == "abc123"
    assert expand_env("plain") == "plain"
    assert expand_env("env:NOT_SET_X") == ""


def test_snapshot_width_roundtrip_and_clamp(client):
    r = client.get("/api/config").get_json()
    assert r["ui"]["snapshot_width"] == 800                 # default
    body = client.get("/api/config").get_json()
    body["ui"]["snapshot_width"] = 960
    client.put("/api/config", json=body)
    assert client.get("/api/config").get_json()["ui"]["snapshot_width"] == 960
    body["ui"]["snapshot_width"] = 99999                    # out of range: ignored
    client.put("/api/config", json=body)
    assert client.get("/api/config").get_json()["ui"]["snapshot_width"] == 960
    assert client.get("/api/resume").get_json()["snapshot_width"] == 960
    body["ui"]["stream_width"] = 1200
    client.put("/api/config", json=body)
    r = client.get("/api/config").get_json()
    assert r["ui"]["stream_width"] == 1200 and r["ui"]["snapshot_width"] == 960
    assert client.get("/api/resume").get_json()["stream_width"] == 1200
    body["ui"]["stream_width"] = 100                       # below range: ignored
    client.put("/api/config", json=body)
    assert client.get("/api/config").get_json()["ui"]["stream_width"] == 1200
    body["ui"]["read_delay"] = 0                        # instant is legal
    client.put("/api/config", json=body)
    assert client.get("/api/config").get_json()["ui"]["read_delay"] == 0
    assert client.get("/api/resume").get_json()["read_delay"] == 0
    body["ui"]["read_delay"] = 61                       # out of range: ignored
    client.put("/api/config", json=body)
    assert client.get("/api/config").get_json()["ui"]["read_delay"] == 0
