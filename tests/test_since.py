def test_since_floor_api_and_db(conn):
    from rssgate import db
    fid = db.add_feed(conn, "https://ex/feed")["id"]
    for i, day in enumerate(("2026-09-01", "2026-09-10", "2026-10-01")):
        aid = db.upsert_article(conn, fid, f"g{i}", f"l{i}", f"A{i}", f"{day}T12:00:00Z")
        db.set_article(conn, aid, status="ready", summary="s")
    rows = db.articles_page(conn, limit=10, since_ts="2026-09-05T00:00:00Z")
    assert [r["ts"][:10] for r in rows] == ["2026-10-01", "2026-09-10"]
    # floor + older-than cursor compose
    last = rows[-1]
    rows2 = db.articles_page(conn, last["ts"], last["id"], 10,
                             since_ts="2026-09-05T00:00:00Z")
    assert rows2 == []


def test_since_param_on_endpoint(client):
    conn = client.conn
    from rssgate import db
    fid = db.add_feed(conn, "https://ex/feed", title="Ex")["id"]
    for i, day in enumerate(("2026-08-01", "2026-09-15")):
        aid = db.upsert_article(conn, fid, f"g{i}", f"l{i}", "t", f"{day}T00:00:00Z")
        db.set_article(conn, aid, status="ready", summary="s")
    d = client.get("/api/articles?since_ts=2026-09-01T00:00:00Z&limit=5").get_json()
    assert [i["ts"][:10] for i in d["items"]] == ["2026-09-15"]
    assert d["has_more"] is False
    d2 = client.get("/api/articles?since_ts=2026-01-01T00:00:00Z").get_json()
    assert len(d2["items"]) == 2
