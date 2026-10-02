def test_llm_stats_endpoint(client):
    conn = client.conn
    from rssgate import db
    fid = db.add_feed(conn, "https://ex/feed")
    db.upsert_article(conn, fid, "g", "l", "t", None)
    db.log_usage(conn, "local", "m", 10, 5, duration_ms=4200, purpose="summarize")
    s = client.get("/api/llm/stats").get_json()
    assert s["queue"] == 1 and s["avg_seconds"] == 4.2
    assert s["calls_today"] == 1


def test_workqueue_endpoint(client):
    conn = client.conn
    from rssgate import db
    fid = db.add_feed(conn, "https://ex/feed", title="Ex Feed")
    db.upsert_article(conn, fid, "g", "l", "Waiting Article", None)
    wq = client.get("/api/workqueue").get_json()
    assert wq["queue_ahead"] == 1 and wq["working"] == 0
    assert wq["current"] == [] and wq["recent"] == []
