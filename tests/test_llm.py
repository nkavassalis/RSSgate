import json
import pytest
import rssgate.llm as llm_mod
from rssgate.llm import LLMClient, LLMError


class FakeResp:
    def __init__(self, payload, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text or json.dumps(payload)

    def json(self):
        return self._payload


def test_openai_compat_chat_and_usage(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, body=json, headers=headers)
        return FakeResp({"choices": [{"message": {"content": "Hello!"}}],
                         "usage": {"prompt_tokens": 11, "completion_tokens": 3}})
    monkeypatch.setattr(llm_mod.requests, "post", fake_post)
    c = LLMClient({"llm": {"provider": "local", "base_url": "http://h:8000/v1",
                           "api_key": "", "model": "one-model"}})
    text, usage = c.chat([{"role": "user", "content": "hi"}])
    assert text == "Hello!"
    assert usage == {"prompt_tokens": 11, "completion_tokens": 3}
    assert captured["url"] == "http://h:8000/v1/chat/completions"
    assert captured["body"]["model"] == "one-model"
    assert "authorization" not in captured["headers"]  # no key => no header


def test_single_model_auto_selection(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        return FakeResp({"data": [{"id": "solo-model"}]})
    monkeypatch.setattr(llm_mod.requests, "get", fake_get)
    c = LLMClient({"llm": {"provider": "local", "base_url": "http://h/v1",
                           "api_key": "", "model": ""}})
    assert c.resolve_model() == "solo-model"


def test_ambiguous_models_require_config(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        return FakeResp({"data": [{"id": "a"}, {"id": "b"}]})
    monkeypatch.setattr(llm_mod.requests, "get", fake_get)
    c = LLMClient({"llm": {"provider": "local", "base_url": "http://h/v1", "model": ""}})
    assert c.resolve_model() == ""
    with pytest.raises(LLMError):
        c.chat([{"role": "user", "content": "x"}])


def test_anthropic_format(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, body=json, headers=headers)
        return FakeResp({"content": [{"type": "text", "text": "Yo"}],
                         "usage": {"input_tokens": 7, "output_tokens": 2}})
    monkeypatch.setattr(llm_mod.requests, "post", fake_post)
    c = LLMClient({"llm": {"provider": "anthropic", "api_key": "sk-ant", "model": "m"}})
    text, usage = c.chat([{"role": "system", "content": "be nice"},
                          {"role": "user", "content": "hi"}])
    assert text == "Yo" and usage["prompt_tokens"] == 7
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "sk-ant"
    assert captured["body"]["system"] == "be nice"
    assert all(m["role"] != "system" for m in captured["body"]["messages"])


def test_error_raises_llmerror(monkeypatch):
    monkeypatch.setattr(llm_mod.requests, "post",
                        lambda *a, **k: FakeResp({}, status=500, text="boom"))
    c = LLMClient({"llm": {"provider": "openai", "base_url": "https://api.openai.com/v1",
                           "api_key": "k", "model": "gpt"}})
    with pytest.raises(LLMError, match="boom"):
        c.chat([{"role": "user", "content": "x"}])
