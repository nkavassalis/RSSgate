from rssgate import db


def test_log_usage_records_duration_and_purpose(conn):
    db.log_usage(conn, "local", "m", 10, 5, duration_ms=1234, purpose="summarize")
    row = conn.execute("SELECT * FROM token_usage").fetchone()
    assert row["duration_ms"] == 1234 and row["purpose"] == "summarize"


def test_bump_state_max_and_incr_state(conn):
    assert db.bump_state_max(conn, "queue_peak", 7) == 7
    assert db.bump_state_max(conn, "queue_peak", 3) == 7   # never decreases
    assert db.bump_state_max(conn, "queue_peak", 12) == 12
    assert db.incr_state(conn, "cache_hits") == 1
    assert db.incr_state(conn, "cache_hits") == 2


def test_llm_stats_snapshot(conn):
    fid = db.add_feed(conn, "https://ex/feed")["id"]
    for i in range(3):
        db.upsert_article(conn, fid, f"g{i}", f"l{i}", "t", None)
    db.log_usage(conn, "local", "m", 100, 20, duration_ms=30000, purpose="summarize")
    db.log_usage(conn, "local", "m", 100, 20, duration_ms=60000, purpose="summarize")
    s = db.llm_stats(conn)
    assert s["queue"] == 3
    assert s["queue_peak"] == 3
    assert s["avg_seconds"] == 45.0
    assert s["min_seconds"] == 30.0 and s["max_seconds"] == 60.0
    assert s["est_drain_minutes"] == round(3 * 45 / 60, 1)
    assert s["calls_today"] == 2 and s["tokens_today"] == 240
    assert s["cache_hits"] == 0 and s["errors"] == 0
