"""Maintenance: article retention + image-cache hygiene.

run_all() is safe to call anytime (idempotent, bounded work): it enforces
maintenance.retention_months, prunes orphaned cache files, and trims the
image cache to maintenance.images_max_mb. Report is kept in state under
'maint_report' for the admin panel.
"""
from __future__ import annotations

import json
import logging
import os
import threading

from . import db, imgstore

log = logging.getLogger("rssgate.maint")


def _cache_files() -> list[os.stat_result | str]:
    d = imgstore.directory()
    if not d:
        return []
    return sorted(
        (f for f in d.iterdir() if f.is_file() and not f.name.endswith(".part")),
        key=lambda f: f.stat().st_mtime)


def prune_orphans(conn) -> tuple[int, int]:
    """Delete cache files no article references. Returns (count, bytes)."""
    ref = db.referenced_images(conn)
    d = imgstore.directory()
    n = size = 0
    if not d:
        return 0, 0
    for f in _cache_files():
        if f.name not in ref:
            try:
                size += f.stat().st_size
                f.unlink()
                n += 1
            except OSError:
                pass
    return n, size


def requeue_refusals(conn) -> int:
    """Ready posts whose "digest" is the model saying the input wasn't an
    article (bot-check pages, error pages) go back through digestion; the
    pipeline now detects those pages and falls back to the feed's text."""
    from .refresh import is_refusal
    rows = conn.execute("SELECT id, summary FROM articles WHERE status='ready'"
                        " AND digest_source=''").fetchall()
    bad = [r["id"] for r in rows if is_refusal(r["summary"] or "")]
    return db.requeue_articles(conn, bad)


def dedupe_galleries(conn) -> int:
    """Repair pass: apply imgstore.dedupe to stored articles (rows written
    before the current rules). Idempotent - a clean row is never rewritten.
    Returns the number of rows changed."""
    n = 0
    rows = list(conn.execute(
        "SELECT id, image, images FROM articles"
        " WHERE image IS NOT NULL AND images IS NOT NULL AND images != '-'"))
    for r in rows:
        raw = [x for x in (r["images"] or "").split(",") if x]
        marker = raw and raw[-1] == "-"
        gal = [x for x in raw if x != "-" and x != r["image"]]
        if not gal:
            continue
        kept, dropped = imgstore.dedupe([r["image"], *gal])
        if kept[0] == r["image"] and kept[1:] == gal:
            continue
        images = ",".join(kept) + (",-" if marker and len(kept) == 1 else "")
        db.set_article(conn, r["id"], image=kept[0], images=images)
        db.release_files(conn, dropped)
        n += 1
    return n


def enforce_cache_size(max_mb: float) -> tuple[int, int]:
    """Delete oldest cache files until under the cap. Returns (count, bytes)."""
    d = imgstore.directory()
    if not d or max_mb <= 0:
        return 0, 0
    cap = float(max_mb) * 1_000_000
    files = _cache_files()                      # oldest first
    total = sum(f.stat().st_size for f in files)
    n = freed = 0
    for f in files:
        if total <= cap:
            break
        try:
            sz = f.stat().st_size
            f.unlink()
            total -= sz
            freed += sz
            n += 1
        except OSError:
            break
    return n, freed


def run_all(conn, cfg) -> dict:
    report = {"ts": db.now_iso(), "deleted_articles": 0,
              "articles_freed_images": 0, "orphans_removed": 0,
              "orphans_freed_mb": 0.0, "cache_trimmed": 0,
              "cache_trimmed_mb": 0.0, "cache_mb": 0.0}
    try:
        months = int(cfg.get("maintenance", {}).get("retention_months", 0) or 0)
        n, files = db.delete_old_articles(conn, months)
        report["deleted_articles"] = n
        report["articles_freed_images"] = len(files)

        # references pointing at missing files (pruned/lost) -> clear them
        d0 = imgstore.directory()
        if d0:
            present = {f.name for f in _cache_files()}
            stale = {n for n in db.referenced_images(conn) if n not in present}
            db.clear_image_refs(conn, stale)

        orphans, osize = prune_orphans(conn)
        report["orphans_removed"] = orphans
        report["orphans_freed_mb"] = round(osize / 1e6, 2)

        # size cap may delete referenced files -> clean dangling refs too
        report["galleries_deduped"] = dedupe_galleries(conn)
        report["refusals_requeued"] = requeue_refusals(conn)
        max_mb = float(cfg.get("maintenance", {}).get("images_max_mb", 0) or 0)
        d = imgstore.directory()
        if d and max_mb > 0:
            ref = db.referenced_images(conn)
            before = {f.name for f in _cache_files()}
            trimmed, tsize = enforce_cache_size(max_mb)
            gone = {f for f in before - {p.name for p in _cache_files()} if f in ref}
            db.clear_image_refs(conn, gone)
            report["cache_trimmed"] = trimmed
            report["cache_trimmed_mb"] = round(tsize / 1e6, 2)
        if d:
            report["cache_mb"] = round(
                sum(f.stat().st_size for f in _cache_files()) / 1e6, 2)
        report["ok"] = True
    except Exception as exc:  # noqa: BLE001
        log.exception("maintenance failed")
        report["ok"] = False
        report["error"] = str(exc)
    db.set_state(conn, "maint_report", json.dumps(report))
    log.info("maintenance: %s", report)
    return report


def last_report(conn) -> dict:
    try:
        return json.loads(db.get_state(conn, "maint_report", "{}"))
    except json.JSONDecodeError:
        return {}


def loop(conn_factory, config_path, stop: "threading.Event | None" = None,
         every_s: int = 6 * 3600) -> None:
    """Background loop: maintenance every `every_s` (default 6h)."""
    import contextlib
    from .config import load_config
    stop = stop or threading.Event()
    while not stop.wait(every_s):
        with contextlib.closing(conn_factory()) as conn:
            run_all(conn, load_config(config_path))
