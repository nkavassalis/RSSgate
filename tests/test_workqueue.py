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


def test_transient_auto_retry_and_drop(client, monkeypatch):
    from rssgate.config import load_config
    from rssgate import db as _db, refresh as _ref
    fid = _db.add_feed(client.conn, "https://ex/retry", type_="feed")["id"]
    aid = _db.upsert_article(client.conn, fid, "g", "https://blog.example/p",
                             "t", "2026-10-01T00:00:00Z")
    cfg = load_config("__no_such__.yaml")
    cfg["troubleshooting"]["log_llm_failures"] = True
    cfg["summarizer"]["max_retries"] = 2
    _db.update_feed(client.conn, fid, digest_length="default")
    # non-transient failure: straight to error
    st = _ref.fail(client.conn, cfg, aid, "extracted text too short (9 ch)")
    assert st == "error"
    # transient: auto-requeue until budget, then error
    _db.set_article(client.conn, aid, status="pending", attempts=0)
    st = _ref.fail(client.conn, cfg, aid, "LLMError: HTTP 429 rate limited")
    assert st == "pending"
    _db.set_article(client.conn, aid, status="processing")
    st = _ref.fail(client.conn, cfg, aid, "LLMError: HTTP 429 rate limited")
    assert st == "pending"
    _db.set_article(client.conn, aid, status="processing")
    st = _ref.fail(client.conn, cfg, aid, "LLMError: HTTP 429 rate limited")
    assert st == "error"
    row = client.conn.execute("SELECT attempts FROM articles WHERE id=?",
                              (aid,)).fetchone()
    assert row["attempts"] == 3
    errs = client.get("/api/feed-errors").get_json()
    assert errs[0]["attempts"] == 3 and "429" in errs[0]["error_msg"]
    # manual retry resets the counter
    client.post(f"/api/articles/{aid}/retry")
    row = client.conn.execute("SELECT status, attempts FROM articles"
                              " WHERE id=?", (aid,)).fetchone()
    assert (row["status"], row["attempts"]) == ("pending", 0)
    # drop hides it from feed, errors list, everywhere
    client.post(f"/api/articles/{aid}/drop")
    assert client.get("/api/articles?limit=10").get_json()["items"] == []
    assert client.get("/api/feed-errors").get_json() == []


def test_scheduler_picks_up_config_edits_without_restart(conn, cfg, monkeypatch):
    """Admin edits (prompt, intervals, workers...) used to reach background
    work only after a restart: the scheduler kept its startup copy."""
    from rssgate import scheduler as S
    from rssgate.config import load_config, save_config
    monkeypatch.setattr(S, "summarize_pending", lambda *a, **k: 0)
    monkeypatch.setattr(S, "refresh_feed", lambda *a, **k: "ok")
    sch = S.Scheduler(conn, load_config(cfg), lambda: None, config_path=cfg)
    c = load_config(cfg)
    c["summarizer"]["system_prompt"] = "EDITED PROMPT {length}"
    c["polling"]["feed_interval_minutes"] = 77
    save_config(c, cfg)
    sch.poll_due()
    assert sch.cfg["summarizer"]["system_prompt"].startswith("EDITED PROMPT")
    assert sch.cfg["polling"]["feed_interval_minutes"] == 77


def test_workqueue_explains_what_is_waiting(client):
    """'30 queued' -> which feeds, why, and what runs next (same order and
    filters as claim_pending)."""
    from rssgate import db
    conn = client.conn
    a = db.add_feed(conn, "https://wa.test/feed", type_="feed")["id"]
    p = db.add_feed(conn, "https://wp.test/feed", type_="feed")["id"]
    off = db.add_feed(conn, "https://wo.test/feed", type_="feed")["id"]
    for fid, n in ((a, 3), (p, 2), (off, 1)):
        for i in range(n):
            db.upsert_article(conn, fid, f"g{i}", f"https://x.test/{fid}/{i}",
                              f"post {fid}-{i}", f"2026-10-0{i + 1}T00:00:00Z")
    db.feed_block(conn, p, 60)
    db.update_feed(conn, off, enabled=0)
    wq = client.get("/api/workqueue").get_json()
    by = {w["feed_id"]: w for w in wq["waiting"]}
    assert by[a]["n"] == 3 and by[p]["n"] == 2 and by[off]["n"] == 1
    assert by[p]["backoff_until"] and by[off]["enabled"] == 0
    nxt = [u["title"] for u in wq["up_next"]]
    assert nxt[0] == f"post {a}-2"                       # newest first
    assert all(t.startswith(f"post {a}-") for t in nxt)  # paused/off skipped
    claimed = db.claim_pending(conn, 1)[0]
    assert claimed["title"] == nxt[0]                    # same order as workers
