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
        if "category_block" in feed.keys() and feed["category_block"]:
            hidden = db.hide_blocked_categories(conn, feed_id)
            if hidden:
                log.info("feed %s: %d hidden by category filter", url, hidden)
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


DIGEST_DIRECTIVES = {
    "terse": " OVERRIDE: the digest must be ONE sentence of at most 20 words"
             " stating only what happened. No context, no nuance.",
    "normal": " OVERRIDE: write a digest of about 150 words.",
    "detailed": " OVERRIDE: write a thorough digest of 300-500 words with"
                " concrete facts, numbers and named entities.",
}


def system_prompt(cfg, feed=None) -> str:
    summ = cfg["summarizer"]
    target = LENGTH_TARGETS.get(summ.get("length", "medium"), "200-300 words")
    base = summ["system_prompt"].replace("{length}", target)
    dl = feed["digest_length"] if (feed is not None
                                   and "digest_length" in feed.keys()) else "default"
    return base + DIGEST_DIRECTIVES.get(dl, "")


def _cache_image(conn, art, page_html: str):
    """Cache an article's images: page-extracted candidates first (og:image
    + plausible content imgs, avatar-filtered); the feed-declared URL is only
    a fallback when the page yields nothing (feeds can declare site icons).
    Writes image (hero), image_url and images (comma list; a trailing '-'
    entry marks 'page tried, single image only'). Returns the stored name
    list or None. Never raises."""
    try:
        decl = art["image_url"] if "image_url" in art.keys() else None
        urls = extract_images(page_html or "", art["link"])[:imgstore.per_post()]
        if not urls and decl:
            urls = [decl]
        if not urls:
            return None
        names = [n for n in (imgstore.store(u) for u in urls) if n]
        if not names:
            return None
        db.set_article(conn, art["id"], image=names[0], image_url=urls[0],
                       images=",".join(names))
        return names
    except Exception:  # noqa: BLE001 - images are decorative
        return None


def backfill_images(conn, cfg, limit: int = 150, page_fetches: int = 40,
                    force: bool = False) -> int:
    """Zero-token catch-up/enrichment for stored articles.

    Non-force: declared heroes are re-saved for free; articles WITHOUT
    declared metadata get their page fetched (budgeted) for og/content
    images; tried pages are marked so the budget always advances.
    Force: page-extract everything, repairing poisoned image_url values
    (e.g. avatars saved as heroes by pre-v1.9 backfills)."""
    import requests
    from .fetcher import UA
    imgstore.set_per_post(cfg.get("maintenance", {}).get("images_per_post", 4))
    cond = "status IN ('ready','error')" if force else (
        "status IN ('ready','error')"
        " AND (image IS NULL OR COALESCE(images,'') IN ('','-')"
        "      OR images NOT LIKE '%,%')")          # singles w/o marker retry
    rows = conn.execute(
        f"SELECT * FROM articles WHERE {cond}"
        " ORDER BY COALESCE(published_at, fetched_at) DESC, id DESC LIMIT ?",
        (limit,)).fetchall()
    stored = fetched = 0
    for art in rows:
        imgs = art["images"] or ""
        decl = art["image_url"] or None
        if not force:
            if imgs == "-" or imgs.endswith(",-"):
                continue                      # done, nothing more available
            if decl:
                if art["image"] or "," in imgs:
                    continue                  # hero (or gallery) already stored
                if _cache_image(conn, art, ""):
                    stored += 1               # free declared hero
                continue
        if not art["link"]:
            continue
        if fetched >= page_fetches:
            break
        try:
            fetched += 1
            resp = requests.get(art["link"], headers={"user-agent": UA},
                                timeout=25)
            names = _cache_image(conn, art, resp.text if resp.ok else "")
            if names and len(names) > 1:
                stored += 1
            elif names:                        # single: mark done-no-gallery
                db.set_article(conn, art["id"], images=names[0] + ",-")
                stored += 1
            else:
                db.set_article(conn, art["id"], images="-")   # no retry
        except Exception:  # noqa: BLE001
            continue
    log.info("image backfill: %d enriched (%d pages fetched)", stored, fetched)
    return stored


def summarize_pending(conn, cfg, llm, limit: int = 5) -> int:
    """Run claimed articles through the LLM exactly once, unless their content
    hash shows we already have a digest for identical text (cache hit = 0 tokens).
    Claims atomically, so multiple worker threads may call this concurrently."""
    imgstore.set_per_post(cfg.get("maintenance", {}).get("images_per_post", 4))
    done = 0
    rows = db.claim_pending(conn, limit)
    for art in rows:
        try:
            feed = db.get_feed(conn, art["feed_id"])
            if feed and feed["category_block"]:
                acats = {c.casefold() for c in db.parse_categories(art["categories"])}
                blocked = {c.casefold() for c in
                           db.parse_categories(feed["category_block"])}
                if acats & blocked:      # blocked since claim: don't spend tokens
                    db.set_article(conn, art["id"], status="hidden")
                    continue
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
                [{"role": "system", "content": system_prompt(cfg, feed)},
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
