"""Refresh feeds/pages and summarize. All LLM work is guarded by change detection."""
from __future__ import annotations

import hashlib
import html  # noqa: F401  (kept for symmetry; unescape used in fetcher)
import logging
import re
import time

from . import db
from . import imgstore
from .config import LENGTH_TARGETS
from .extract import extract_article_text, extract_images, page_title
from .fetcher import fetch_feed, fetch_page, discover_page_articles, probe

log = logging.getLogger("rssgate.refresh")

LENGTH_TARGETS = LENGTH_TARGETS  # re-export for tests

SPONSORED_TITLE_RE = re.compile(
    r"\b(sponsored|promo\b|promotional|paid partnership|in partnership with"
    r"|partner content|gift guide|deal of the day|deal alert"
    r"|now (up to )?\d+% ?off|up to \d+% off|prime day|black friday"
    r"|cyber monday|save \d+%|on sale|discount code|best deal)\b", re.I)
SPONSORED_LINK_RE = re.compile(r"/(sponsored|deals?|partner-?content)/", re.I)
SPONSORED_DIGEST_RE = re.compile(
    r"(sponsored (promotional )?(piece|post|article|content)|"
    r"promotional piece|paid partnership|this (article|post) is sponsored)", re.I)


def is_sponsored(title: str, link: str) -> bool:
    """Deterministic, free pre-filter: obvious sponsored/sale posts by title or URL."""
    return bool(SPONSORED_TITLE_RE.search(title or "")
                or SPONSORED_LINK_RE.search(link or ""))


def _purpose_model(cfg, key: str) -> str:
    return (cfg.get("llm", {}).get(key) or "").strip()


def refresh_feed(conn, feed, cfg, llm=None) -> str:
    """Fetch one feed/page. Returns a status string. Never summarizes here;
    new articles are queued as 'pending' for the summarizer."""
    url, feed_id = feed["url"], feed["id"]
    ftype = feed["type"]
    if ftype == "auto":
        ftype = probe(url)
        db.update_feed(conn, feed_id, type=ftype)

    if ftype == "feed":
        res = fetch_feed(url, feed["etag"], feed["last_modified"])
        if not res["ok"]:
            status = f"error: {res.get('error', res.get('status'))}"
            db.update_feed(conn, feed_id, last_fetched_at=db.now_iso(), last_status=status)
            return status
        if res.get("not_modified"):
            db.update_feed(conn, feed_id, last_fetched_at=db.now_iso(),
                           last_status="not modified")
            return "not modified"
        meta = res["meta"]
        db.update_feed(conn, feed_id,
                       title=meta["title"] or feed["title"],
                       description=meta["description"] or feed["description"],
                       auto_categories=",".join(meta["categories"]),
                       etag=res.get("etag"), last_modified=res.get("last_modified"),
                       content_hash=res["fingerprint"],
                       last_fetched_at=db.now_iso(), last_status="ok")
        added = 0
        for e in res["entries"]:
            if db.upsert_article(conn, feed_id, e["guid"], e["link"], e["title"],
                                 e["published_at"], e.get("categories"),
                                 e.get("image")):
                added += 1
        log.info("feed %s: %d new articles", url, added)
        return f"ok ({added} new)"

    # bare page: skip everything unless the page content actually changed
    res = fetch_page(url, feed["etag"], feed["last_modified"])
    if not res["ok"]:
        status = f"error: {res.get('error', res.get('status'))}"
        db.update_feed(conn, feed_id, last_fetched_at=db.now_iso(), last_status=status)
        return status
    if res.get("not_modified") or res["fingerprint"] == feed["content_hash"]:
        db.update_feed(conn, feed_id, last_fetched_at=db.now_iso(),
                       last_status="not modified")
        return "not modified"
    if llm is None:
        return "error: LLM required for bare pages"
    disc_started = time.perf_counter()
    disc = discover_page_articles(res["html"], url, llm,
                                  model=_purpose_model(cfg, "model_discover"))
    disc_ms = int((time.perf_counter() - disc_started) * 1000)
    db.log_usage(conn, llm.provider, llm.model or "auto",
                 disc["usage"]["prompt_tokens"], disc["usage"]["completion_tokens"],
                 duration_ms=disc_ms, purpose="discovery")
    added = 0
    for item in disc["items"]:
        if db.upsert_article(conn, feed_id, item["link"], item["link"],
                             item.get("title", ""), None):
            added += 1
    db.update_feed(conn, feed_id,
                   title=page_title(res["html"]) or feed["title"],
                   etag=res.get("etag"), last_modified=res.get("last_modified"),
                   content_hash=res["fingerprint"],
                   last_fetched_at=db.now_iso(), last_status="ok")
    log.info("page %s: %d new articles", url, added)
    return f"ok ({added} new)"


def refresh_all(conn, cfg, llm=None) -> list[str]:
    results = []
    for feed in db.list_feeds(conn):
        if feed["enabled"]:
            results.append(f"{feed['url']}: {refresh_feed(conn, feed, cfg, llm)}")
    return results


def system_prompt(cfg) -> str:
    summ = cfg["summarizer"]
    target = LENGTH_TARGETS.get(summ.get("length", "medium"), "200-300 words")
    return summ["system_prompt"].replace("{length}", target)


def _cache_image(conn, art, page_html: str) -> str | None:
    """Best-effort hero image for one article: prefer the URL the feed
    declared (free); else the first candidate from the page HTML we already
    fetched. Cached locally forever. Never raises; returns filename."""
    try:
        url = art["image_url"] if "image_url" in art.keys() else None
        if not url:
            cands = extract_images(page_html or "", art["link"])
            url = cands[0] if cands else None
        if not url:
            return None
        fname = imgstore.store(url)
        if fname:
            db.set_article(conn, art["id"], image=fname, image_url=url)
        return fname
    except Exception:  # noqa: BLE001 - images are decorative
        return None


def backfill_images(conn, cfg, limit: int = 150, page_fetches: int = 40) -> int:
    """Zero-token catch-up: cache hero images for articles stored before
    images existed. Feed-declared URLs are free; at most `page_fetches`
    article pages are re-fetched for og/content images."""
    import requests
    from .fetcher import UA
    rows = conn.execute(
        "SELECT * FROM articles WHERE image IS NULL AND status IN ('ready','error')"
        " ORDER BY COALESCE(published_at, fetched_at) DESC, id DESC LIMIT ?",
        (limit,)).fetchall()
    stored = fetched = 0
    for art in rows:
        if art["image_url"]:
            if _cache_image(conn, art, ""):
                stored += 1
            continue
        if fetched >= page_fetches or not art["link"]:
            continue
        try:
            fetched += 1
            resp = requests.get(art["link"], headers={"user-agent": UA},
                                timeout=25)
            if resp.ok and _cache_image(conn, art, resp.text):
                stored += 1
        except Exception:  # noqa: BLE001
            continue
    log.info("image backfill: %d stored (%d pages fetched)", stored, fetched)
    return stored


def summarize_pending(conn, cfg, llm, limit: int = 5) -> int:
    """Run claimed articles through the LLM exactly once, unless their content
    hash shows we already have a digest for identical text (cache hit = 0 tokens).
    Claims atomically, so multiple worker threads may call this concurrently."""
    done = 0
    rows = db.claim_pending(conn, limit)
    for art in rows:
        try:
            feed = db.get_feed(conn, art["feed_id"])
            if feed and feed["hide_sponsored"] and is_sponsored(art["title"], art["link"]):
                db.set_article(conn, art["id"], status="hidden")  # 0 tokens spent
                continue
            import requests
            from .fetcher import UA
            resp = requests.get(art["link"], headers={"user-agent": UA}, timeout=30)
            if not resp.ok:
                db.set_article(conn, art["id"], status="error")
                continue
            text = extract_article_text(resp.text, cfg["summarizer"]["max_input_chars"])
            if len(text) < 120:
                db.set_article(conn, art["id"], status="error", summary=None)
                continue
            _cache_image(conn, art, resp.text)
            body_hash = hashlib.sha256(text.encode()).hexdigest()
            if feed and not feed["summarize"]:
                # raw mode: cleaned extracted text IS the digest (zero tokens)
                if feed["hide_sponsored"] and SPONSORED_DIGEST_RE.search(text[:400]):
                    db.set_article(conn, art["id"], status="hidden")
                    continue
                db.set_article(conn, art["id"], summary=text, status="ready",
                                body_hash=body_hash, summarized_at=db.now_iso(),
                                llm_ms=0)
                done += 1
                continue
            cached = db.find_summary_by_hash(conn, body_hash)
            if cached:
                db.set_article(conn, art["id"], summary=cached["summary"],
                               status="ready", body_hash=body_hash,
                               summarized_at=db.now_iso(), llm_ms=0)
                db.incr_state(conn, "cache_hits")  # zero-token reuse counter
                done += 1
                continue
            if feed and not feed["summarize"]:
                # raw mode: cleaned extracted text IS the digest (zero tokens)
                if feed["hide_sponsored"] and SPONSORED_DIGEST_RE.search(text[:400]):
                    db.set_article(conn, art["id"], status="hidden")
                    continue
                db.set_article(conn, art["id"], summary=text,
                               status="ready", body_hash=body_hash,
                               summarized_at=db.now_iso(), llm_ms=0)
                done += 1
                continue
            user = (f"Feed: {feed['title'] or feed['url']}\n"
                    f"Article: {art['title']}\nSource: {art['link']}\n\n{text}")
            t0 = time.perf_counter()
            digest, usage = llm.chat(
                [{"role": "system", "content": system_prompt(cfg)},
                 {"role": "user", "content": user}],
                max_tokens=int(cfg["summarizer"].get("max_output_tokens", 4000)),
                model=_purpose_model(cfg, "model_summarize"))
            dur_ms = int((time.perf_counter() - t0) * 1000)
            db.log_usage(conn, llm.provider, llm.model or "auto",
                         usage["prompt_tokens"], usage["completion_tokens"],
                         duration_ms=dur_ms, purpose="summarize")
            if not digest.strip():
                # reasoning models can burn the whole budget thinking
                db.set_article(conn, art["id"], status="error")
                continue
            if feed and feed["hide_sponsored"] and SPONSORED_DIGEST_RE.search(digest[:400]):
                # the model itself flagged it as sponsored: hide, keep digest
                # on file for audit, never render in the viewer
                db.set_article(conn, art["id"], summary=digest.strip(),
                               status="hidden", body_hash=body_hash,
                               llm_ms=dur_ms, summarized_at=db.now_iso())
                continue
            db.set_article(conn, art["id"], summary=digest.strip(), status="ready",
                           body_hash=body_hash, tokens_in=usage["prompt_tokens"],
                           tokens_out=usage["completion_tokens"], llm_ms=dur_ms,
                           summarized_at=db.now_iso())
            done += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("summarize failed for %s: %s", art["link"], exc)
            db.set_article(conn, art["id"], status="error")
    return done
