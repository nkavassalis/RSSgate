"""Feed / bare-page fetching with strict change detection (token efficiency first)."""
from __future__ import annotations

import hashlib
import html as htmlmod
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

import feedparser
import requests

from .extract import extract_candidate_links, page_title

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


def _norm_cats(seq) -> list[str]:
    out: list[str] = []
    for item in seq or []:
        term = item.get("term") if isinstance(item, dict) else str(item)
        term = htmlmod.unescape((term or "").strip())
        if term and term.lower() not in [o.lower() for o in out]:
            out.append(term)
    return out


def _entry_categories(e) -> list[str]:
    """Category tags carried by a single feed entry (<category>, itunes, dc:subject,
    media:keywords)."""
    cats = _norm_cats(e.get("tags")) + _norm_cats(e.get("categories"))
    mc = e.get("media_category") or {}
    if isinstance(mc, dict):
        cats += _norm_cats(mc.get("tags"))
    kw = e.get("keywords")
    if isinstance(kw, str):
        cats += _norm_cats(kw.split(","))
    return cats[:6]


def _collect_categories(parsed) -> list[str]:
    """Feed-level union used as display fallback: channel categories if declared,
    else the union of entry categories."""
    feed_cats = _norm_cats(parsed.feed.get("tags")) + _norm_cats(parsed.feed.get("categories"))
    kw = parsed.feed.get("media_category") or {}
    feed_cats += _norm_cats(kw.get("tags") if isinstance(kw, dict) else [])
    if feed_cats:
        return feed_cats[:8]
    entry_cats: list[str] = []
    for e in parsed.entries[:20]:
        for c in _entry_categories(e):
            if c.lower() not in [o.lower() for o in entry_cats]:
                entry_cats.append(c)
    return entry_cats[:8]


def entry_image(e) -> str | None:
    """Hero image URL an entry declares (free, no page fetch needed):
    media:thumbnail / media:content / image:frontpage / enclosure(image/*)
    / first <img> in inline content."""
    for key in ("media_thumbnail", "media_content"):
        for item in e.get(key) or []:
            u = item.get("url") if isinstance(item, dict) else None
            if u:
                return u
    img = e.get("image")
    if isinstance(img, dict) and img.get("url"):
        return img["url"]
    for enc in e.get("enclosures") or []:
        if str(enc.get("type", "")).startswith("image/") and enc.get("href"):
            return enc["href"]
    for c in e.get("content") or []:
        html = c.get("value") if isinstance(c, dict) else None
        if html:
            soup = BeautifulSoup(html, "html.parser")
            im = soup.find("img", src=True)
            if im:
                return im["src"]
    return None


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
            "categories": _entry_categories(e),
            "image": entry_image(e),
        })
    meta = {
        "title": (parsed.feed.get("title") or "").strip(),
        "description": (parsed.feed.get("description") or "").strip(),
        "categories": _collect_categories(parsed),
    }
    fingerprint = hashlib.sha256(
        "|".join(e["guid"] for e in entries).encode()).hexdigest()
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


COMMON_FEED_PATHS = ("feed", "rss.xml", "feed.xml", "atom.xml")
FEED_CONTENT_TYPES = ("application/rss+xml", "application/atom+xml",
                      "application/xml", "text/xml")
FEED_HREF_RE = re.compile(r"/(rss|feed|atom)([._/-].*)?(\.xml|/rss)?$", re.I)
FEED_LABEL_RE = re.compile(r"\b(rss|atom|feed)s?\b", re.I)


def _same_site(a: str, b: str) -> bool:
    from urllib.parse import urlparse
    ha = urlparse(a).netloc.lower().removeprefix("www.")
    hb = urlparse(b).netloc.lower().removeprefix("www.")
    return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)


def find_feeds(url: str) -> dict:
    """Given a URL that may or may not be a feed, discover the real feed.
    Returns {type: feed|page|unknown|error, candidates: [{url,title}],
             page_title?, error?}. No LLM used -- pure sniffing."""
    res = _get(url)
    if not res["ok"]:
        return {"type": "error", "error": f"HTTP {res['status']}", "candidates": []}
    if looks_like_feed(res["content"]):
        return {"type": "feed", "candidates": []}
    html = res["content"].decode("utf-8", "replace")
    if "html" not in res["content_type"].lower() and "<html" not in html[:600].lower():
        return {"type": "unknown", "candidates": []}
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    cands, seen = [], {url}

    def add(href, title=""):
        u = urljoin(url, href).split("#")[0]
        if u and u not in seen:
            seen.add(u)
            cands.append({"url": u, "title": (title or "").strip()})

    for link in soup.find_all("link", href=True):
        rel = link.get("rel") or ([link.get("rel")] if isinstance(link.get("rel"), str) else [])
        if "alternate" in [r.lower() for r in rel] \
           and (link.get("type", "").lower() in FEED_CONTENT_TYPES):
            add(link["href"], link.get("title", ""))
    declared = list(cands)  # publisher-declared <link rel=alternate>: trusted
    for a in soup.find_all("a", href=True):  # feed-ish anchors or feed-hosted urls
        href = urljoin(url, a["href"]).split("?")[0]
        label = " ".join(((a.get("title") or "") + " " + a.get_text(" ", strip=True)).split())
        host = urlparse(href).netloc.lower().removeprefix("www.").split(".")[0]
        if host in ("feed", "feeds", "rss", "atom") or FEED_HREF_RE.search(href) or \
           (FEED_LABEL_RE.search(label) and _same_site(href, url)):
            add(href, label[:60])
    if not cands:  # common well-known paths, root-relative
        root = "/".join(url.split("/")[:3])
        for p in COMMON_FEED_PATHS:
            probe_url = f"{root}/{p}"
            if probe_url == url or probe_url in seen:
                continue
            add(probe_url, "")
    # heuristic (non-declared) candidates must actually parse as feeds
    verified = list(declared)
    for c in cands:
        if len(verified) >= 6:
            break
        if c in declared:
            continue
        r = _get(c["url"])
        if r["ok"] and looks_like_feed(r["content"]):
            c["title"] = c["title"] or feedparser.parse(r["content"]).feed.get("title", "")
            verified.append(c)
    return {"type": "page", "candidates": verified[:6],
            "page_title": page_title(html)}


DISCOVER_PROMPT = (
    "You are examining a bare web page that has no RSS feed. From the candidate links "
    "below, select only the ones that are links to actual articles/posts (not menus, "
    "tags, categories, authors, archives, or navigation). Return a JSON array of "
    "objects {\"title\": ..., \"link\": ...} using the exact candidate data, newest/"
    "most prominent first, maximum 15 items. Respond with JSON only, no commentary.\n\n"
    "Candidates:\n{candidates}"
)


def discover_page_articles(html: str, base_url: str, llm, model: str = "") -> dict:
    """For pages without feeds: harvest candidate links, let the LLM pick the real
    articles. Falls back to raw candidates if the LLM call fails."""
    candidates = extract_candidate_links(html, base_url)
    if not candidates:
        return {"items": [], "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
    slim = [{"title": c["title"], "link": c["link"]} for c in candidates]
    try:
        prompt = DISCOVER_PROMPT.replace(
            "{candidates}", json.dumps(slim, ensure_ascii=False)[:16000])
        text, usage = llm.chat([{"role": "user", "content": prompt}], max_tokens=800,
                               model=model)
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
