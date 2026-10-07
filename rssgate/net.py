"""Outbound HTTP for everything that talks to feed/article/image hosts.

One place for: the User-Agent (config fetch.user_agent, default below) and
per-host politeness pacing (fetch.per_host_interval seconds between requests
to the same host, shared by every thread). Sites ban bursts: adding a feed
used to fetch ~100 article pages back-to-back and got an IP blocked.

Calls requests.get at call time, so tests that monkeypatch requests.get or
fetcher._get keep working. Pacing defaults to 0 until configure() runs (the
app entry point does it), so the hermetic suite never sleeps.
"""
from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

import requests

DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 rssgate")

BLOCK_STATUSES = (403, 429)      # site says "go away": back the feed off

_lock = threading.Lock()
_next_ok: dict[str, float] = {}  # host -> monotonic time of next allowed hit
_cfg = {"ua": "", "interval": 0.0}


def is_challenge(resp) -> bool:
    """A JavaScript bot check (Cloudflare "Just a moment..."): only a real
    browser passes, waiting does not help - so it is NOT a block to back off
    from. Detected by Cloudflare's explicit header or the interstitial page."""
    if getattr(resp, "status_code", 200) not in (403, 503):
        return False
    h = getattr(resp, "headers", {}) or {}
    if str(h.get("cf-mitigated", "")).lower() == "challenge":
        return True
    try:
        head = (resp.text or "")[:4000].lower()
    except Exception:  # noqa: BLE001
        return False
    return ("just a moment" in head and "cloudflare" in head) \
        or "challenge-platform" in head


def configure(cfg: dict) -> None:
    f = (cfg or {}).get("fetch", {}) or {}
    _cfg["ua"] = str(f.get("user_agent") or "").strip()
    try:
        _cfg["interval"] = max(0.0, float(f.get("per_host_interval", 3)))
    except (TypeError, ValueError):
        _cfg["interval"] = 3.0


def user_agent() -> str:
    return _cfg["ua"] or DEFAULT_UA


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def pace(url: str) -> float:
    """Block until this host may be hit again; returns seconds waited."""
    gap = _cfg["interval"]
    host = host_of(url)
    if gap <= 0 or not host:
        return 0.0
    with _lock:
        now = time.monotonic()
        slot = max(now, _next_ok.get(host, 0.0))
        _next_ok[host] = slot + gap
    wait = slot - now
    if wait > 0:
        time.sleep(wait)
    return wait


def get(url: str, headers: dict | None = None, **kw) -> requests.Response:
    pace(url)
    h = {"user-agent": user_agent()}
    h.update(headers or {})
    return requests.get(url, headers=h, **kw)
