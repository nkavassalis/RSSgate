import time
from rssgate import db


def seed_pending(conn, n=3):
    fid = db.add_feed(conn, "https://ex/feed")["id"]
    for i in range(n):
        db.upsert_article(conn, fid, f"g{i}", f"https://ex.com/{i}", f"A{i}", None)
    return fid


def test_claim_pending_is_atomic(conn):
    seed_pending(conn, 3)
    first = db.claim_pending(conn, 2)
    second = db.claim_pending(conn, 2)
    assert len(first) == 2 and len(second) == 1
    assert not {r["id"] for r in first} & {r["id"] for r in second}
    assert db.claim_pending(conn, 5) == []
    assert all(r["status"] == "processing" and r["started_at"] for r in first)


def test_stale_processing_requeued(conn):
    seed_pending(conn, 1)
    db.claim_pending(conn, 1)
    conn.execute("UPDATE articles SET started_at=datetime('now','-30 minutes')")
    conn.commit()
    assert db.requeue_stale_processing(conn) == 1
    assert db.workqueue_snapshot(conn)["working"] == 0


def test_workqueue_snapshot_shape(conn):
    seed_pending(conn, 2)
    claimed = db.claim_pending(conn, 1)
    db.set_article(conn, claimed[0]["id"], status="ready", summary="s",
                   summarized_at=db.now_iso(), llm_ms=4200,
                   tokens_in=100, tokens_out=30)
    snap = db.workqueue_snapshot(conn)
    assert snap["queue_ahead"] == 1 and len(snap["recent"]) == 1
    assert snap["recent"][0]["llm_ms"] == 4200
    assert snap["recent"][0]["feed_title"] == ""  # title empty when feed meta unknown
    assert snap["current"] == []


def test_summarize_records_duration_and_processing_state(conn, cfg, monkeypatch):
    import rssgate.refresh as refresh
    import requests

    html = "<html><body><article>" + "".join(
        f"<p>Unique paragraph {i} with plenty of article substance here.</p>"
        for i in range(15)) + "</article></body></html>"

    class R: ok = True; status_code = 200; text = html
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())

    class SlowLLM:
        provider = "local"; model = "m"
        def chat(self, messages, max_tokens=1200, model=""):
            time.sleep(0.05)
            return "digest", {"prompt_tokens": 10, "completion_tokens": 2}

    from rssgate.config import load_config
    seed_pending(conn, 1)
    assert refresh.summarize_pending(conn, load_config(cfg), SlowLLM()) == 1
    art = conn.execute("SELECT * FROM articles").fetchone()
    assert art["status"] == "ready"
    assert art["llm_ms"] >= 40          # measured wall time
    assert art["started_at"] is not None  # was marked processing first


def test_stale_requeue_canonical_formats(conn):
    """Regression: comparing canonical ...T...Z started_at against
    datetime('now') strings silently never matched (v0.28.1)."""
    import datetime as dt
    from rssgate import db as _db
    fid = _db.add_feed(conn, "https://ex/stale", type_="feed")["id"]
    aid = _db.upsert_article(conn, fid, "g", "https://x/1", "t", None)
    old = (dt.datetime.now(dt.timezone.utc)
           - dt.timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _db.set_article(conn, aid, status="processing", started_at=old)
    assert _db.requeue_stale_processing(conn) == 1
    assert _db.get_article(conn, aid)["status"] == "pending" if hasattr(_db, "get_article") \
        else conn.execute("SELECT status FROM articles WHERE id=?", (aid,)).fetchone()[0] == "pending"
    # fresh processing (5 min) is left alone
    recent = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE articles SET status='processing', started_at=? WHERE id=?",
                 (recent, aid))
    assert _db.requeue_stale_processing(conn) == 0
