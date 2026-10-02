"""Refresh feeds/pages and summarize. All LLM work is guarded by change detection."""
from __future__ import annotations

import hashlib
import html  # noqa: F401  (kept for symmetry; unescape used in fetcher)
import logging
import time

from . import db
from .config import LENGTH_TARGETS
from .extract import extract_article_text, page_title
from .fetcher import fetch_feed, fetch_page, discover_page_articles, probe

log = logging.getLogger("rssgate.refresh")

LENGTH_TARGETS = LENGTH_TARGETS  # re-export for tests


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
                                 e["published_at"]):
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
    disc = discover_page_articles(res["html"], url, llm)
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


def summarize_pending(conn, cfg, llm, limit: int = 5) -> int:
    """Run claimed articles through the LLM exactly once, unless their content
    hash shows we already have a digest for identical text (cache hit = 0 tokens).
    Claims atomically, so multiple worker threads may call this concurrently."""
    done = 0
    rows = db.claim_pending(conn, limit)
    for art in rows:
        try:
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
            body_hash = hashlib.sha256(text.encode()).hexdigest()
            cached = db.find_summary_by_hash(conn, body_hash)
            if cached:
                db.set_article(conn, art["id"], summary=cached["summary"],
                               status="ready", body_hash=body_hash,
                               summarized_at=db.now_iso(), llm_ms=0)
                db.incr_state(conn, "cache_hits")  # zero-token reuse counter
                done += 1
                continue
            feed = db.get_feed(conn, art["feed_id"])
            user = (f"Feed: {feed['title'] or feed['url']}\n"
                    f"Article: {art['title']}\nSource: {art['link']}\n\n{text}")
            t0 = time.perf_counter()
            digest, usage = llm.chat(
                [{"role": "system", "content": system_prompt(cfg)},
                 {"role": "user", "content": user}],
                max_tokens=int(cfg["summarizer"].get("max_output_tokens", 4000)))
            dur_ms = int((time.perf_counter() - t0) * 1000)
            db.log_usage(conn, llm.provider, llm.model or "auto",
                         usage["prompt_tokens"], usage["completion_tokens"],
                         duration_ms=dur_ms, purpose="summarize")
            if not digest.strip():
                # reasoning models can burn the whole budget thinking
                db.set_article(conn, art["id"], status="error")
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
