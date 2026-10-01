"""Configuration loading/saving for RSSgate.

Everything lives in a single YAML file (config.yaml by default). Defaults are
defined here; the file only needs to carry overrides, but the admin panel
writes the full merged config back.
"""
from __future__ import annotations

import copy
import os

import yaml

DEFAULTS: dict = {
    "server": {
        "host": "0.0.0.0",
        "port": 8088,
        "data_dir": "./data",
    },
    "llm": {
        "provider": "local",          # local | openai | openrouter | anthropic
        "base_url": "http://10.1.13.99:8000/v1",
        "api_key": "",
        "model": "",                  # empty => auto-select (single model endpoint)
    },
    "polling": {
        "feed_interval_minutes": 30,
        "page_interval_minutes": 180,
        "fetch_on_start": True,
    },
    "summarizer": {
        "length": "medium",           # short | medium | long
        "max_input_chars": 24000,
        "system_prompt": (
            "You are a careful news reader. You will receive the raw text scraped from a "
            "web page. Produce a faithful digest of the article: keep every important "
            "fact, name, number, date and key quotation; remove advertising, sponsored "
            "content, cookie banners, navigation, comment sections, share prompts, "
            "related-article blocks and any other boilerplate. Never invent or add "
            "information that is not present. Write plain flowing prose with no "
            "markdown headings. Length target: {length}. Respond with the digest text "
            "only."
        ),
    },
    "ui": {
        "theme": "auto",              # auto | light | dark
        "items_per_page": 20,
    },
}

LENGTH_TARGETS = {
    "short": "about 100 words",
    "medium": "200-300 words",
    "long": "400-600 words",
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def expand_env(value: str) -> str:
    """Allow 'env:VARNAME' to pull secrets from the environment."""
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value[4:], "")
    return value or ""


def load_config(path: str = "config.yaml") -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            file_cfg = yaml.safe_load(fh) or {}
        if not isinstance(file_cfg, dict):
            raise ValueError(f"config file {path} must contain a YAML mapping")
        cfg = _merge(cfg, file_cfg)
    return cfg


def save_config(cfg: dict, path: str = "config.yaml") -> None:
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False, allow_unicode=True)


def masked_config(cfg: dict) -> dict:
    """Copy of config safe to send to the browser (api key hidden)."""
    out = copy.deepcopy(cfg)
    key = out.get("llm", {}).get("api_key", "")
    out.setdefault("llm", {})["api_key"] = "***" if key else ""
    out["llm"]["api_key_set"] = bool(key)
    return out
