"""Provider-agnostic LLM client (OpenAI-compatible, OpenAI, OpenRouter, Anthropic)."""
from __future__ import annotations

import requests

from .config import expand_env

TIMEOUT = 180


class LLMError(Exception):
    pass


class LLMClient:
    """Thin chat-completion client. Returns (text, usage) and never touches the DB;
    callers log usage via db.log_usage."""

    def __init__(self, cfg: dict):
        llm = cfg.get("llm", {})
        self.provider = llm.get("provider", "local")
        self.base_url = (llm.get("base_url") or "").rstrip("/")
        self.api_key = expand_env(llm.get("api_key", ""))
        self.model = llm.get("model") or ""

    # ----------------------------------------------------------- models

    def list_models(self) -> list[str]:
        """List models. Works for OpenAI-compatible endpoints (local/openai/openrouter)."""
        if self.provider == "anthropic":
            return []
        if not self.base_url:
            return []
        headers = self._headers()
        resp = requests.get(f"{self.base_url}/models", headers=headers, timeout=20)
        if resp.status_code != 200:
            raise LLMError(f"models request failed ({resp.status_code}): {resp.text[:200]}")
        data = resp.json().get("data", [])
        return sorted({m.get("id") for m in data if m.get("id")})

    def resolve_model(self) -> str:
        """Configured model, or auto-select when the endpoint serves exactly one."""
        if self.model:
            return self.model
        models = self.list_models()
        if len(models) == 1:
            return models[0]
        return ""

    # ----------------------------------------------------------- chat

    def _headers(self) -> dict:
        if self.provider == "anthropic":
            return {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    def chat(self, messages: list[dict], max_tokens: int = 1200) -> tuple[str, dict]:
        """messages: [{'role','content'}]. Returns (text, {prompt_tokens, completion_tokens})."""
        if self.provider == "anthropic":
            return self._chat_anthropic(messages, max_tokens)
        return self._chat_openai(messages, max_tokens)

    def _chat_openai(self, messages, max_tokens) -> tuple[str, dict]:
        model = self.resolve_model()
        if not model:
            raise LLMError("no model configured and auto-selection failed")
        body = {"model": model, "messages": messages, "max_tokens": max_tokens}
        resp = requests.post(f"{self.base_url}/chat/completions", json=body,
                             headers=self._headers(), timeout=TIMEOUT)
        if resp.status_code != 200:
            raise LLMError(f"chat failed ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        text = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage") or {}
        return text, {"prompt_tokens": int(usage.get("prompt_tokens", 0)),
                      "completion_tokens": int(usage.get("completion_tokens", 0))}

    def _chat_anthropic(self, messages, max_tokens) -> tuple[str, dict]:
        model = self.model or "claude-sonnet-4-5"
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = [m for m in messages if m["role"] != "system"]
        body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": turns}
        resp = requests.post("https://api.anthropic.com/v1/messages", json=body,
                             headers=self._headers(), timeout=TIMEOUT)
        if resp.status_code != 200:
            raise LLMError(f"chat failed ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", []))
        usage = data.get("usage") or {}
        return text, {"prompt_tokens": int(usage.get("input_tokens", 0)),
                      "completion_tokens": int(usage.get("output_tokens", 0))}


TEST_PROMPT = [{"role": "user", "content": "Reply with exactly: OK"}]
