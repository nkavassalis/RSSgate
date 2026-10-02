from rssgate import db


def seed(conn, n=3):
    fid = db.add_feed(conn, "https://ex/feed")["id"]
    for i in range(n):
        aid = db.upsert_article(conn, fid, f"g{i}", f"l{i}", f"A{i}",
                                f"2026-10-{i + 1:02d}T00:00:00Z")
        db.set_article(conn, aid, status="ready", summary="s")
    return fid


def test_mark_feed_read_forward_only(conn):
    fid = seed(conn)
    db.mark_feed_read(conn, fid, "2026-10-02T00:00:00Z")
    assert db.feed_unread(conn, fid, db.get_feed(conn, fid)["last_read_ts"]) == 1
    db.mark_feed_read(conn, fid, "2026-10-01T00:00:00Z")   # backwards: ignored
    assert db.get_feed(conn, fid)["last_read_ts"] == "2026-10-02T00:00:00Z"
    db.mark_feed_read(conn, fid, "2026-10-03T00:00:00Z")
    assert db.feed_unread(conn, fid, "2026-10-03T00:00:00Z") == 0
    # never-read feeds count everything
    fid2 = db.add_feed(conn, "https://b/feed")["id"]
    db.upsert_article(conn, fid2, "x", "l", "t", "2026-10-01T00:00:00Z")
    assert db.feed_unread(conn, fid2, None) == 1


def test_unread_flag_on_articles_page(conn):
    fid = seed(conn, 3)
    rows = db.articles_page(conn, limit=10)
    assert [r["unread"] for r in rows] == [1, 1, 1]        # never read
    db.mark_feed_read(conn, fid, "2026-10-02T12:00:00Z")
    rows = db.articles_page(conn, limit=10)
    assert [r["unread"] for r in rows] == [1, 0, 0]        # only newest is new


# ------------------------------------------------------------------ API

def seed_api(client):
    fid = client.post("/api/feeds", json={"url": "https://ex/feed"}).get_json()["id"]
    conn = client.conn
    for i in range(3):
        aid = db.upsert_article(conn, fid, f"g{i}", f"l{i}", f"A{i}",
                                f"2026-10-{i + 1:02d}T00:00:00Z")
        db.set_article(conn, aid, status="ready", summary="s")
    return fid


def test_position_feeds_cursor_and_counts(client):
    fid = seed_api(client)
    assert client.get("/api/feeds").get_json()[0]["unread"] == 3
    r = client.post("/api/position", json={
        "ts": "2026-10-02T00:00:00Z", "id": 2, "feeds": [str(fid)],
        "global": False})
    assert r.get_json()["ok"]
    f = client.get("/api/feeds").get_json()[0]
    assert f["unread"] == 1                      # two consumed
    assert client.get("/api/resume").get_json()["resume_ts"] == ""  # global untouched
    items = client.get("/api/articles?limit=10").get_json()["items"]
    assert [i["unread"] for i in items] == [True, False, False]


def test_position_legacy_payload_still_saves_global(client):
    fid = seed_api(client)
    client.post("/api/position", json={"ts": "2026-10-01T00:00:00Z", "id": 9})
    assert client.get("/api/resume").get_json()["resume_ts"] == "2026-10-01T00:00:00Z"


def test_norm_ts_all_formats():
    from rssgate.db import norm_ts
    canon = "2026-10-02T16:00:00Z"
    for raw in ["2026-10-02T16:00:00Z", "2026-10-02T16:00:00+00:00",
                "2026-10-02T12:00:00-04:00", "2026-10-02T16:00:00.123456Z",
                "Fri, 02 Oct 2026 16:00:00 +0000",
                "Fri, 02 Oct 2026 12:00:00 -0400",
                "Fri, 02 Oct 2026 16:00:00 GMT"]:
        assert norm_ts(raw) == canon, raw
    assert norm_ts(None) is None
    assert norm_ts("garbage") == "garbage"  # passthrough, never crashes


def test_mixed_format_timestamps_compare_correctly(client, conn):
    """The original bug: an article stored with an offset or RFC-822 date and
    a cursor in a different format must still count as read after scrolling."""
    from rssgate import db
    fid = db.add_feed(conn, "https://ex/mixed")["id"]
    db.upsert_article(conn, fid, "g1", "u1", "RFC",
                      "Fri, 02 Oct 2026 12:00:00 -0400")   # = 16:00Z
    db.upsert_article(conn, fid, "g2", "u2", "Newer",
                      "2026-10-02T18:00:00+00:00")
    stored = {r["title"]: r["published_at"] for r in conn.execute(
        "SELECT title, published_at FROM articles")}
    assert stored["RFC"] == "2026-10-02T16:00:00Z"
    assert stored["Newer"] == "2026-10-02T18:00:00Z"
    # cursor lands on the RFC article (beacon ts arrives in yet another form)
    db.mark_feed_read(conn, fid, "2026-10-02T16:00:00+00:00")
    assert db.feed_unread(conn, fid, "2026-10-02T16:00:00Z") == 1  # only Newer
    # forward-only still holds across formats
    db.mark_feed_read(conn, fid, "2026-10-01T00:00:00Z")
    assert conn.execute("SELECT last_read_ts FROM feeds WHERE id=?",
                        (fid,)).fetchone()["last_read_ts"] == "2026-10-02T16:00:00Z"
