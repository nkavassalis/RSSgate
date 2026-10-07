"""Refresh feeds/pages and summarize. All LLM work is guarded by change detection."""
from __future__ import annotations

import hashlib
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
    r"|partner content|gift guide|deal of the day|deal alert|daily ?deals?"
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


SMART_BLOCK_RE = re.compile(
    r"^(partners?( |-)?content|partners?|sponsored( content)?|branded"
    r" content|promoted( content)?|paid content|advertorial(s)?)$", re.I)


def smart_category_block(conn, feed, declared: list[str]) -> int:
    """Smart default: feeds that DECLARE ad categories (Gizmodo's
    'Partners') get them pre-blocked on first ingest - zero tokens, visible
    in the gear census, and permanently user-owned once the admin touches
    the block list (explicit edits set smart_block=2 = user-owned).

    smart_block: 0 = untouched (auto may seed), 1 = seeded by smart default,
    2 = user took ownership (never auto-manage again, even when empty)."""
    if feed["category_block"] or feed["smart_block"] != 0:
        return 0
    hits = [c.strip() for c in declared if SMART_BLOCK_RE.match(c.strip())]
    if not hits:
        return 0
    db.update_feed(conn, feed["id"], category_block=",".join(hits),
                   smart_block=1)
    from .db import _cat_sql
    n = 0
    for h in hits:
        n += conn.execute(
            "UPDATE articles SET status='hidden' WHERE feed_id=?"
            " AND status IN ('pending','ready') AND " + _cat_sql("categories"),
            (feed["id"], h)).rowcount
    conn.commit()
    if n:
        log.info("smart category block %s on feed %s: %d hidden",
                 hits, feed["id"], n)
    return n


def _purpose_model(cfg, key: str) -> str:
    return (cfg.get("llm", {}).get(key) or "").strip()


def _backoff_minutes(cfg) -> float:
    try:
        return max(1.0, float(cfg.get("fetch", {}).get("block_backoff_minutes", 60)))
    except (TypeError, ValueError):
        return 60.0


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
        if not res["ok"] and res.get("status") in (403, 429):
            until = db.feed_block(conn, feed_id, _backoff_minutes(cfg))
            status = (f"blocked by site (HTTP {res['status']}),"
                      f" paused until {until}")
            db.update_feed(conn, feed_id, last_fetched_at=db.now_iso(),
                           last_status=status)
            return status
        if res["ok"] and feed["backoff_level"]:
            db.feed_unblock(conn, feed_id)        # site talks to us again
        if not res["ok"]:
            status = f"error: {res.get('error', res.get('status'))}"
            db.update_feed(conn, feed_id, last_fetched_at=db.now_iso(), last_status=status)
            return status
        if res.get("not_modified"):
            db.update_feed(conn, feed_id, last_fetched_at=db.now_iso(),
                           last_status="not modified")
            return "not modified"
        meta = res["meta"]
        smart_category_block(conn, feed, meta["categories"])
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
                                 e.get("image"), feed_text=e.get("excerpt")):
                added += 1
        if "category_block" in feed.keys() and feed["category_block"]:
            hidden = db.hide_blocked_categories(conn, feed_id)
            if hidden:
                log.info("feed %s: %d hidden by category filter", url, hidden)
        if "sync_deletes" in feed.keys() and feed["sync_deletes"] and res["entries"]:
            files = db.feed_files(conn, feed_id)
            gone = db.prune_vanished(conn, feed_id,
                                    {e["guid"] for e in res["entries"]})
            if gone:
                db.release_files(conn, files)
                log.info("feed %s: %d vanished entries pruned", url, gone)
        log.info("feed %s: %d new articles", url, added)
        return f"ok ({added} new)"

    # bare page: skip everything unless the page content actually changed
    res = fetch_page(url, feed["etag"], feed["last_modified"])
    if not res["ok"] and res.get("status") in (403, 429):
        until = db.feed_block(conn, feed_id, _backoff_minutes(cfg))
        status = f"blocked by site (HTTP {res['status']}), paused until {until}"
        db.update_feed(conn, feed_id, last_fetched_at=db.now_iso(), last_status=status)
        return status
    if res["ok"] and feed["backoff_level"]:
        db.feed_unblock(conn, feed_id)
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
    if "sync_deletes" in feed.keys() and feed["sync_deletes"] and disc["items"]:
        files = db.feed_files(conn, feed_id)
        gone = db.prune_vanished(conn, feed_id,
                                 {i["link"] for i in disc["items"]}, key="link")
        if gone:
            db.release_files(conn, files)
            log.info("page %s: %d vanished entries pruned", url, gone)
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
        if feed["enabled"] and not db.feed_paused(feed):
            results.append(f"{feed['url']}: {refresh_feed(conn, feed, cfg, llm)}")
    return results


DIGEST_DIRECTIVES = {
    "terse": " OVERRIDE: the digest must be ONE sentence of at most 20 words"
             " stating only what happened. No context, no nuance.",
    "normal": " OVERRIDE: write a digest of about 150 words.",
    "detailed": " OVERRIDE: write a thorough digest of 300-500 words with"
                " concrete facts, numbers and named entities.",
}


TRANSIENT_RE = re.compile(
    r"(429|rate.?limit|timeout|timed out|connection|reset by peer|"
    r"temporar|try again|502|503|504|overloaded)", re.I)


def fail(conn, cfg, article_id: int, why: str) -> str:
    """Record a transcription failure. Counts attempts; TRANSIENT failures
    (429s, timeouts, 5xx, connection resets) go back to 'pending' for an
    automatic retry on the next tick - the 30s scheduler tick is the back-
    off - until summarizer.max_retries is exhausted, then 'error'.
    With troubleshooting.log_llm_failures the reason is stored. Returns the
    resulting status."""
    conn.execute("UPDATE articles SET attempts=attempts+1 WHERE id=?",
                 (article_id,))
    attempts = conn.execute("SELECT attempts FROM articles WHERE id=?",
                            (article_id,)).fetchone()["attempts"]
    max_retries = int(cfg.get("summarizer", {}).get("max_retries", 2))
    retry = bool(TRANSIENT_RE.search(str(why))) and attempts <= max_retries
    status = "pending" if retry else "error"
    fields = {"status": status}
    if cfg.get("troubleshooting", {}).get("log_llm_failures"):
        fields["error_msg"] = (f"{str(why)[:400]} "
                               f"[attempt {attempts}"
                               f"{', retrying' if retry else ''}]")
    db.set_article(conn, article_id, **fields)
    return status


def system_prompt(cfg, feed=None) -> str:
    """Global prompt, or the feed's CUSTOM one when set (optional override;
    {length} works in both). The per-feed digest-length directive is always
    appended so the select keeps authority even over custom prompts."""
    summ = cfg["summarizer"]
    target = LENGTH_TARGETS.get(summ.get("length", "medium"), "200-300 words")
    custom = ""
    if feed is not None and "system_prompt" in feed.keys():
        custom = (feed["system_prompt"] or "").strip()
    base = (custom or summ["system_prompt"]).replace("{length}", target)
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
        pairs = [(u, n) for u in urls if (n := imgstore.store(u))]
        if not pairs:
            return None
        # hero/gallery dedupe: one rule set, shared with maintenance
        url_of = {n: u for u, n in pairs}
        kept, orphan_extra = imgstore.dedupe([n for _, n in pairs])
        keep = [(url_of[n], n) for n in kept]
        urls = [u for u, _ in keep]
        names = [n for _, n in keep]
        old = set()
        if art["image"]:
            old.add(art["image"])
        for n in (art["images"] or "").split(","):
            if n and n != "-":
                old.add(n)
        db.set_article(conn, art["id"], image=names[0], image_url=urls[0],
                       images=",".join(names))
        db.release_files(conn, (old - set(names))
                           | set(orphan_extra))  # reclaim
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
    from . import net
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
            resp = net.get(art["link"], timeout=25)
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


EXCERPT_MIN = 80     # shorter feed text isn't worth a card on its own
FULLTEXT_MIN = 600   # feed text at least this long is digested like a page


def _use_excerpt(conn, art) -> bool:
    """Publish the feed-provided text as the post (zero tokens) when the
    article page is unusable. Returns False if the feed carried too little."""
    ft = (art["feed_text"] if "feed_text" in art.keys() else "") or ""
    if len(ft) < EXCERPT_MIN:
        return False
    db.set_article(conn, art["id"], summary=ft, status="ready",
                   digest_source="excerpt", summarized_at=db.now_iso(),
                   body_hash=hashlib.sha256(ft.encode()).hexdigest(), llm_ms=0)
    return True


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
            from . import net
            src = (feed["content_source"] if feed and "content_source" in
                   feed.keys() else "auto") or "auto"
            cap = cfg["summarizer"]["max_input_chars"]
            if feed and "max_input_chars" in feed.keys() and feed["max_input_chars"]:
                cap = min(cap, feed["max_input_chars"])   # per-feed tighter cap
            if src == "feed":
                # feed text only: never touch the article page
                ft = (art["feed_text"] or "") if "feed_text" in art.keys() else ""
                if len(ft) < EXCERPT_MIN:
                    fail(conn, cfg, art["id"], "feed provides no text"
                         " (content source: feed text only)")
                    continue
                db.set_article(conn, art["id"], digest_source="feed")
                if len(ft) < FULLTEXT_MIN:            # a teaser: show as-is
                    db.set_article(conn, art["id"], summary=ft, status="ready",
                                   summarized_at=db.now_iso(), llm_ms=0,
                                   body_hash=hashlib.sha256(ft.encode()).hexdigest())
                    _cache_image(conn, art, "")
                    done += 1
                    continue
                text, page_html = ft[:cap], ""
            else:
                resp = net.get(art["link"], timeout=30)
                if net.is_challenge(resp):
                    # browser-only page: the feed's own text is all we can get
                    if src == "auto" and _use_excerpt(conn, art):
                        done += 1
                    else:
                        fail(conn, cfg, art["id"], "site requires a browser"
                             " (bot challenge)" + ("" if src == "page" else
                                                   " and the feed has no text"))
                    continue
                if resp.status_code in net.BLOCK_STATUSES:
                    # the SITE refuses us: pause the whole feed, keep the post
                    # queued (not failed) - burning the backlog deepens bans
                    until = db.feed_block(conn, art["feed_id"], _backoff_minutes(cfg))
                    db.set_article(conn, art["id"], status="pending", started_at=None)
                    log.warning("feed %s blocked by site (HTTP %s); paused until %s",
                                art["feed_id"], resp.status_code, until)
                    continue
                if not resp.ok:
                    transient = resp.status_code == 429 or resp.status_code >= 500
                    fail(conn, cfg, art["id"],
                         f"fetch HTTP {resp.status_code}"
                         + (" (transient)" if transient else ""))
                    continue
                text = extract_article_text(resp.text, cap)
                if len(text) < 120:
                    if src == "auto" and _use_excerpt(conn, art):
                        done += 1                     # page empty, feed has text
                        continue
                    fail(conn, cfg, art["id"], f"extracted text too short ({len(text)} ch)")
                    continue
                page_html = resp.text
                if "digest_source" in art.keys() and art["digest_source"]:
                    db.set_article(conn, art["id"], digest_source="")
            _cache_image(conn, art, page_html)
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
            fail(conn, cfg, art["id"], f"{type(exc).__name__}: {exc}")
            log.warning("summarize failed for %s: %s", art["link"], exc)
            db.set_article(conn, art["id"], status="error")
    return done
