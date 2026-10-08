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
    # id must be a real seeded article: a resume pointer may not name a
    # vanished post (see test_bookmark_for_vanished_post_is_ignored)
    aid = client.conn.execute(
        "SELECT MIN(id) m FROM articles").fetchone()["m"]
    client.post("/api/position", json={"ts": "2026-10-01T00:00:00Z", "id": aid})
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


def test_filtered_view_ignores_resume_bounds(client):
    """Filtered/since views must start at newest: a stale global resume cursor
    used to hide unread articles inside feed views (pills that never clear)."""
    fid = seed_api(client)
    # user's global resume sits on the OLDEST article (2026-10-01T00:00Z)
    client.post("/api/position", json={"ts": "2026-10-01T00:00:00Z", "id": 1,
                                       "global": True})
    # unfiltered New view IS bounded (keyset is strictly-older than resume)
    titles = [i["title"] for i in
              client.get("/api/articles?limit=10").get_json()["items"]]
    assert titles == []
    # filtered view is NOT bounded: newest first, all 3 visible
    titles = [i["title"] for i in client.get(
        f"/api/articles?limit=10&feed_id={fid}").get_json()["items"]]
    assert titles == ["A2", "A1", "A0"]
    # since-mode is not bounded either
    titles = [i["title"] for i in client.get(
        "/api/articles?limit=10&since_ts=2026-09-01T00:00:00Z"
    ).get_json()["items"]]
    assert titles == ["A2", "A1", "A0"]


def test_reads_map_precise_per_feed_cursors(client):
    """Per-feed beacon precision: reads={feed: ts} marks each feed read at its
    OWN newest-passed card, and does not touch the global cursor."""
    fid = seed_api(client)
    fid2 = client.post("/api/feeds", json={"url": "https://ex/2"}).get_json()["id"]
    conn = client.conn
    aid = db.upsert_article(conn, fid2, "k1", "l", "B1", "2026-10-02T05:00:00Z")
    db.set_article(conn, aid, status="ready", summary="s")
    r = client.post("/api/position", json={
        "ts": "2026-10-01T00:00:00Z", "id": 1,
        "reads": {str(fid): "2026-10-03T00:00:00Z"},   # feed1 fully read
        "global": False})
    assert r.get_json()["ok"]
    feeds = {f["id"]: f for f in client.get("/api/feeds").get_json()}
    assert feeds[fid]["unread"] == 0
    assert feeds[fid2]["unread"] == 1          # untouched feed stays unread
    assert client.get("/api/resume").get_json()["resume_ts"] == ""


def test_fresh_param_bypasses_resume_bound(client):
    """New-mode boots at newest via ?fresh=1; legacy unbounded-free requests
    keep honoring the resume bound (backwards compatible)."""
    fid = seed_api(client)
    client.post("/api/position", json={"ts": "2026-10-01T00:00:00Z", "id": 1,
                                       "global": True})
    assert [i["title"] for i in
            client.get("/api/articles?limit=10").get_json()["items"]] == []
    titles = [i["title"] for i in
              client.get("/api/articles?limit=10&fresh=1").get_json()["items"]]
    assert titles == ["A2", "A1", "A0"]


def test_dropped_articles_never_hold_the_pill(conn):
    """feed_unread must mirror stream visibility: a dropped failure has no
    card to dwell on, so counting it makes the pill permanently sticky."""
    from rssgate import db
    fid = db.add_feed(conn, "https://drop.test/f", type_="feed")["id"]
    a1 = db.upsert_article(conn, fid, "d1", "https://drop.test/1", "Fine",
                           None, categories=[])
    db.set_article(conn, a1, status="ready", summary="ok")
    a2 = db.upsert_article(conn, fid, "d2", "https://drop.test/2", "Failed",
                           None, categories=[])
    db.set_article(conn, a2, status="error", error_msg="junk")
    assert db.feed_unread(conn, fid, None) == 2
    db.set_article(conn, a2, status="dropped")
    assert db.feed_unread(conn, fid, None) == 1
    db.set_article(conn, a2, status="hidden")      # sponsored hide likewise
    assert db.feed_unread(conn, fid, None) == 1


def _prio_seed(conn):
    from rssgate import db
    f1 = db.add_feed(conn, "https://p1.test/f", type_="feed")["id"]
    f2 = db.add_feed(conn, "https://p2.test/f", type_="feed")["id"]
    db.update_feed(conn, f1, last_read_ts="2026-10-05T12:00:00Z")
    a_read = db.upsert_article(conn, f1, "r", "https://p1.test/1", "Read NEW",
                               "2026-10-05T10:00:00Z")      # newer, but read
    db.set_article(conn, a_read, status="ready", summary="x")
    a_un = db.upsert_article(conn, f2, "u", "https://p2.test/1", "Unread OLD",
                             "2026-10-04T10:00:00Z")       # older, unread
    db.set_article(conn, a_un, status="ready", summary="y")
    return a_un, a_read


def test_prio_orders_unread_first_and_pages_correctly(conn):
    from rssgate import db
    a_un, a_read = _prio_seed(conn)
    rows = db.articles_page(conn, unread_first=True, limit=10)
    assert [r["title"] for r in rows] == ["Unread OLD", "Read NEW"]
    # plain mode keeps pure chrono (read NEW first)
    rows = db.articles_page(conn, limit=10)
    assert [r["title"] for r in rows] == ["Read NEW", "Unread OLD"]
    # keyset page 2 with (unread, ts, id) cursor lands past the unread row
    page1 = db.articles_page(conn, unread_first=True, limit=1)
    cur = page1[0]
    page2 = db.articles_page(conn, before_ts=cur["ts"], before_id=cur["id"],
                             before_u=cur["unread"], unread_first=True,
                             limit=10)
    assert [r["title"] for r in page2] == ["Read NEW"]


def test_per_article_read_marks_do_not_read_the_backlog(client):
    """Reading a feed's newest post marks THAT post only (the old per-feed
    cursor read every older post the moment you saw the newest)."""
    from rssgate import db
    conn = client.conn
    fid = db.add_feed(conn, "https://pa.test/feed", type_="feed")["id"]
    ids = []
    for i in range(3):
        aid = db.upsert_article(conn, fid, f"g{i}", f"https://pa.test/{i}", "t",
                                f"2026-10-0{i + 1}T00:00:00Z")
        db.set_article(conn, aid, status="ready", summary="s")
        ids.append(aid)
    newest = ids[-1]
    r = client.post("/api/position", json={"read_ids": [newest]})
    assert r.get_json() == {"ok": True, "marked": 1}
    items = client.get(f"/api/articles?feed_id={fid}&fresh=1").get_json()["items"]
    unread = {i["id"]: i["unread"] for i in items}
    assert unread[newest] is False
    assert unread[ids[0]] is True and unread[ids[1]] is True
    assert db.feed_unread(conn, fid, db.get_feed(conn, fid)["last_read_ts"]) == 2
    # legacy cursor history still counts as read
    db.mark_feed_read(conn, fid, "2026-10-02T00:00:00Z")
    assert db.feed_unread(conn, fid, db.get_feed(conn, fid)["last_read_ts"]) == 0


def test_bookmark_for_vanished_post_is_ignored(client):
    """A beacon naming a since-deleted post (redigest/sync_deletes race an
    open reader tab) must not 500 the beacon or lose the rest of its payload."""
    conn = client.conn
    fid = seed_api(client)                     # articles A0..A2, status ready
    gone, keep = conn.execute(
        "SELECT MIN(id) g, MAX(id) k FROM articles").fetchone()
    r = client.post("/api/position", json={
        "id": gone, "ts": "2027-01-01T00:00:00Z",
        "reads": {str(fid): "2027-01-01T00:00:00Z"}, "read_ids": [keep]})
    assert r.status_code == 200, r.get_json()
    assert conn.execute("SELECT value FROM state WHERE key='resume_id'").fetchone() is None
    # ...and a beacon for a LIVE post does set it (the guard isn't blanket)
    assert client.post("/api/position", json={"id": keep,
                       "ts": "2027-01-01T00:00:00Z", "global": True}).status_code == 200
    assert conn.execute("SELECT value FROM state WHERE key='resume_id'").fetchone()[0] == str(keep)
    assert conn.execute("SELECT read_at IS NOT NULL FROM articles WHERE id=?",
                        (keep,)).fetchone()[0] == 1
    # and the beacon is not rejected outright even once the row is gone
    conn.execute("DELETE FROM articles WHERE id=?", (gone,))
    conn.commit()
    assert client.post("/api/position", json={"id": gone,
                       "ts": "2027-01-01T00:00:00Z"}).status_code == 200


def test_queued_posts_do_not_hold_the_pill(client):
    """The pill must count only posts the stream will actually show: with
    ui.hide_untranscribed on (default), pending/processing posts have no
    card to dwell-mark, so counting them means 'N unread' with nothing to
    read (a 144-post re-run showed exactly that)."""
    from rssgate import db
    conn = client.conn
    fid = db.add_feed(conn, "https://pillqueue.test/feed", type_="feed")["id"]

    def art(guid, ts, status):
        a = db.upsert_article(conn, fid, guid, f"https://x.test/{guid}",
                              f"q {guid}", ts)
        db.set_article(conn, a, status=status,
                       summary="digest" if status == "ready" else None)
        return a

    art("p1", "2026-10-05T00:00:00Z", "pending")
    art("p2", "2026-10-04T00:00:00Z", "processing")
    art("e1", "2026-10-03T00:00:00Z", "error")      # error posts DO get a card
    art("r1", "2026-10-02T00:00:00Z", "ready")

    pill = next(f["unread"] for f in client.get("/api/feeds").get_json()
                if f["id"] == fid)
    items = client.get(f"/api/articles?feed_id={fid}&limit=50").get_json()["items"]
    assert pill == 2                                    # ready + error, not queued
    assert {i["status"] for i in items} == {"ready", "error"}
    assert pill == len([i for i in items if not i.get("read_at")])

    # same filter applied explicitly, i.e. the stream's toggle honoured
    assert db.feed_unread(conn, fid, None, ()) == 4                      # toggle off
    assert db.feed_unread(conn, fid, None, ("pending", "processing")) == 2
