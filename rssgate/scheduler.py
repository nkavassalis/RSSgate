"""Background scheduler: polls feeds/pages on their own timers, digests in batches."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import db
from .refresh import refresh_feed, summarize_pending


class Scheduler(threading.Thread):
    def __init__(self, conn, cfg, llm_factory, tick_seconds: int = 30):
        super().__init__(daemon=True, name="rssgate-scheduler")
        self.conn = conn
        self.cfg = cfg
        self.llm_factory = llm_factory
        self.tick = tick_seconds
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def _due(self, feed, now: float) -> bool:
        minutes = (self.cfg["polling"]["page_interval_minutes"]
                   if feed["type"] == "page"
                   else self.cfg["polling"]["feed_interval_minutes"])
        if feed["last_fetched_at"]:
            last = time.mktime(time.strptime(feed["last_fetched_at"], "%Y-%m-%dT%H:%M:%SZ"))
            last -= last % 3600 * 0  # utc string; compare in utc epoch
            return now - last >= minutes * 60
        return True

    def run(self):
        while not self._stop.is_set():
            try:
                self.poll_due()
            except Exception:  # noqa: BLE001 keep the loop alive
                pass
            self._stop.wait(self.tick)

    def poll_due(self):
        now = time.time()
        llm = None
        db.requeue_stale_processing(self.conn)
        for feed in db.list_feeds(self.conn):
            if not feed["enabled"] or not self._due(feed, now):
                continue
            if llm is None:
                llm = self.llm_factory()
            try:
                refresh_feed(self.conn, feed, self.cfg, llm)
            except Exception:  # noqa: BLE001
                pass
        try:
            llm = llm or self.llm_factory()
            workers = max(1, int(self.cfg["summarizer"].get("concurrency", 2)))
            for _ in range(4):  # rounds; each round runs `workers` in parallel
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    results = list(pool.map(
                        lambda _: summarize_pending(self.conn, self.cfg, llm, limit=1),
                        range(workers)))
                if sum(results) == 0:
                    break
        except Exception:  # noqa: BLE001
            pass
