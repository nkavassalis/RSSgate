import json
import rssgate.llm as llm_mod
from rssgate.llm import LLMClient
from rssgate.config import load_config, save_config, NO_THINK_EXTRA_BODY


class FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _capture_post(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(json)
        return FakeResp({"choices": [{"message": {"content": "x"}}], "usage": {}})
    monkeypatch.setattr(llm_mod.requests, "post", fake_post)
    return captured


def test_extra_body_is_sent(monkeypatch):
    cap = _capture_post(monkeypatch)
    c = LLMClient({"llm": {"provider": "local", "base_url": "http://h/v1",
                           "model": "m",
                           "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}})
    c.chat([{"role": "user", "content": "hi"}])
    assert cap["chat_template_kwargs"] == {"enable_thinking": False}


def test_extra_body_omitted_when_empty(monkeypatch):
    cap = _capture_post(monkeypatch)
    c = LLMClient({"llm": {"provider": "local", "base_url": "http://h/v1", "model": "m"}})
    c.chat([{"role": "user", "content": "hi"}])
    assert "chat_template_kwargs" not in cap


def test_model_override_per_call(monkeypatch):
    cap = _capture_post(monkeypatch)
    c = LLMClient({"llm": {"provider": "local", "base_url": "http://h/v1", "model": "default-m"}})
    c.chat([{"role": "user", "content": "hi"}], model="reasoning-m")
    assert cap["model"] == "reasoning-m"


def test_config_defaults_no_think_for_local_only(tmp_path):
    cfg = load_config(str(tmp_path / "none.yaml"))  # provider default: local
    assert cfg["llm"]["extra_body"] == NO_THINK_EXTRA_BODY

    path = str(tmp_path / "c.yaml")
    save_config({"llm": {"provider": "openai"}}, path)
    assert load_config(path)["llm"]["extra_body"] == {}

    path2 = str(tmp_path / "d.yaml")  # explicit opt-out on local is respected
    save_config({"llm": {"provider": "local", "extra_body": {}}}, path2)
    assert load_config(path2)["llm"]["extra_body"] == {}


def test_refresh_uses_purpose_models(conn, cfg, monkeypatch):
    import rssgate.refresh as refresh
    import requests
    from rssgate.config import load_config
    from rssgate import db

    html = "<html><body><article>" + "".join(
        f"<p>Distinct paragraph {i} carrying real article substance for testing.</p>"
        for i in range(15)) + "</article></body></html>"

    class R: ok = True; status_code = 200; text = html
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    seen = {}

    class SpyLLM:
        provider = "local"; model = "default"
        def chat(self, messages, max_tokens=1200, model=""):
            seen["model"] = model
            return "d", {"prompt_tokens": 1, "completion_tokens": 1}

    fid = db.add_feed(conn, "https://ex/feed", type_="feed")
    db.upsert_article(conn, fid, "g", "https://ex.com/p", "t", None)
    full = load_config(cfg)
    full["llm"]["model_summarize"] = "cheap-fast-m"
    refresh.summarize_pending(conn, full, SpyLLM())
    assert seen["model"] == "cheap-fast-m"
