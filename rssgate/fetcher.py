"""Feed / bare-page fetching with strict change detection (token efficiency first)."""
from __future__ import annotations

import hashlib
import html as htmlmod
import json
import re
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import feedparser
import requests

from .extract import extract_candidate_links

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 rssgate/0.1")

XML_SNIFF = re.compile(r"^\s*(<\?xml|<rss|<feed|<rdf:RDF)", re.I)


def _get(url: str, etag: str | None = None, last_modified: str | None = None) -> dict:
    """Conditional GET. Returns dict(ok, not_modified, status, content, content_type,
    etag, last_modified)."""
    headers = {"user-agent": UA, "accept": "application/rss+xml, application/atom+xml,"
               " application/xml, text/html;q=0.9, */*;q=0.5"}
    if etag:
        headers["if-none-match"] = etag
    if last_modified:
        headers["if-modified-since"] = last_modified
    try:
        resp = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
    except requests.RequestException as exc:
        return {"ok": False, "not_modified": False, "status": 0, "error": str(exc),
                "content": b"", "content_type": "", "etag": None, "last_modified": None}
    if resp.status_code == 304:
        return {"ok": True, "not_modified": True, "status": 304, "content": b"",
                "content_type": "", "etag": None, "last_modified": None}
    return {
        "ok": resp.ok, "not_modified": False, "status": resp.status_code,
        "content": resp.content if resp.ok else b"",
        "content_type": resp.headers.get("content-type", ""),
        "etag": resp.headers.get("etag"),
        "last_modified": resp.headers.get("last-modified"),
    }


def looks_like_feed(content: bytes) -> bool:
    return bool(XML_SNIFF.match(content[:512].decode("utf-8", "ignore")))


def probe(url: str) -> str:
    """Classify a URL as 'feed' or 'page'."""
    res = _get(url)
    return "feed" if res["ok"] and looks_like_feed(res["content"]) else "page"


def _iso(dt_struct) -> str | None:
    if not dt_struct:
        return None
    import datetime
    return datetime.datetime.fromtimestamp(
        __import__("calendar").timegm(dt_struct), datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry_pubdate(entry) -> str | None:
    for key in ("published_parsed", "updated_parsed"):
        if entry.get(key):
            return _iso(entry[key])
    return None


def _collect_categories(parsed) -> list[str]:
    """Union of channel/feed-level categories; falls back to entry-level union.
    Handles <category>, itunes:category, dc:subject, media:keywords."""
    def norm(seq):
        out = []
        for item in seq or []:
            term = item.get("term") if isinstance(item, dict) else str(item)
            term = (term or "").strip()
            term = htmlmod.unescape(term)
            if term and term.lower() not in [o.lower() for o in out]:
                out.append(term)
        return out

    feed_cats = norm(parsed.feed.get("tags")) + norm(parsed.feed.get("categories"))
    kw = parsed.feed.get("media_category") or {}
    feed_cats += norm(kw.get("tags") if isinstance(kw, dict) else [])
    if feed_cats:
        return feed_cats[:8]
    entry_cats: list[str] = []
    for e in parsed.entries[:20]:
        for c in norm(e.get("tags")) + norm(e.get("categories")):
            if c.lower() not in [o.lower() for o in entry_cats]:
                entry_cats.append(c)
    return entry_cats[:8]


def fetch_feed(url: str, etag: str | None = None, last_modified: str | None = None) -> dict:
    """Fetch & parse an RSS/Atom feed.
    Returns {ok, changed, not_modified?, meta:{title,description,categories},
             entries:[{guid,link,title,published_at}], etag, last_modified, error?}"""
    res = _get(url, etag, last_modified)
    if not res["ok"]:
        return {"ok": False, "changed": False, **res}
    if res["not_modified"]:
        return {"ok": True, "changed": False, "not_modified": True,
                "meta": {}, "entries": [], "etag": None, "last_modified": None}
    parsed = feedparser.parse(res["content"])
    if parsed.version == "":
        return {"ok": False, "changed": False, "error": "not a valid feed",
                "meta": {}, "entries": [], "etag": res["etag"], "last_modified": res["last_modified"]}
    entries = []
    for e in parsed.entries:
        link = e.get("link") or e.get("id") or url
        entries.append({
            "guid": e.get("id") or link,
            "link": link,
            "title": (e.get("title") or "").strip() or "(untitled)",
            "published_at": _entry_pubdate(e),
        })
    meta = {
        "title": (parsed.feed.get("title") or "").strip(),
        "description": (parsed.feed.get("description") or "").strip(),
        "categories": _collect_categories(parsed),
    }
    fingerprint = hashlib.sha256(
        "|".join(e["guid"] for e in entries).encode()).hexdigest()
    changed = fingerprint != (etag or "")  # caller may override with content_hash compare
    return {"ok": True, "changed": True, "meta": meta, "entries": entries,
            "etag": res["etag"], "last_modified": res["last_modified"],
            "fingerprint": fingerprint}


def fetch_page(url: str, etag: str | None = None, last_modified: str | None = None) -> dict:
    """Fetch a bare web page. changed=False when conditional GET or content hash
    proves nothing moved -> callers must skip the (expensive) discovery LLM call."""
    res = _get(url, etag, last_modified)
    if not res["ok"]:
        return {"ok": False, "changed": False, **res}
    if res["not_modified"]:
        return {"ok": True, "changed": False, "not_modified": True, "html": "",
                "title": "", "etag": None, "last_modified": None}
    html = res["content"].decode("utf-8", "replace")
    digest = hashlib.sha256(res["content"]).hexdigest()
    return {"ok": True, "changed": True, "html": html, "title": "", "etag": res["etag"],
            "last_modified": res["last_modified"], "fingerprint": digest}


DISCOVER_PROMPT = (
    "You are examining a bare web page that has no RSS feed. From the candidate links "
    "below, select only the ones that are links to actual articles/posts (not menus, "
    "tags, categories, authors, archives, or navigation). Return a JSON array of "
    "objects {\"title\": ..., \"link\": ...} using the exact candidate data, newest/"
    "most prominent first, maximum 15 items. Respond with JSON only, no commentary.\n\n"
    "Candidates:\n{candidates}"
)


def discover_page_articles(html: str, base_url: str, llm) -> list[dict]:
    """For pages without feeds: harvest candidate links, let the LLM pick the real
    articles. Falls back to raw candidates if the LLM call fails."""
    candidates = extract_candidate_links(html, base_url)
    if not candidates:
        return {"items": [], "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
    slim = [{"title": c["title"], "link": c["link"]} for c in candidates]
    try:
        prompt = DISCOVER_PROMPT.replace(
            "{candidates}", json.dumps(slim, ensure_ascii=False)[:16000])
        text, usage = llm.chat([{"role": "user", "content": prompt}], max_tokens=800)
        match = re.search(r"\[.*\]", text, re.S)
        picked = json.loads(match.group(0)) if match else []
        valid = [p for p in picked
                 if isinstance(p, dict) and p.get("link")
                 and urljoin(base_url, p["link"]) in {c["link"] for c in candidates}]
        if valid:
            return {"items": valid[:15], "usage": usage}
    except Exception:
        pass
    return {"items": slim[:15], "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
