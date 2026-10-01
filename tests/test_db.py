from rssgate import db


def seed(conn, n=5):
    fid = db.add_feed(conn, "https://example.com/feed")
    ids = []
    for i in range(n):
        ids.append(db.upsert_article(
            conn, fid, f"guid-{i}", f"https://example.com/{i}", f"Article {i}",
            f"2026-10-{i + 1:02d}T12:00:00Z"))
    conn.execute("UPDATE articles SET status='ready', summary='digest'")
    conn.commit()
    return fid, ids


def test_article_dedup_by_guid(conn):
    fid = db.add_feed(conn, "https://example.com/feed")
    assert db.upsert_article(conn, fid, "g1", "l1", "T", None) is not None
    assert db.upsert_article(conn, fid, "g1", "l1", "T", None) is None  # duplicate
    # same guid on another feed is a different article
    fid2 = db.add_feed(conn, "https://other.example/feed")
    assert db.upsert_article(conn, fid2, "g1", "l1", "T", None) is not None


def test_reverse_chronological_cursor_pages(conn):
    seed(conn, 5)
    page1 = db.articles_page(conn, limit=3)
    assert [r["title"] for r in page1] == ["Article 4", "Article 3", "Article 2"]
    last = page1[-1]
    page2 = db.articles_page(conn, last["ts"], last["id"], limit=3)
    assert [r["title"] for r in page2] == ["Article 1", "Article 0"]


def test_newest_first_when_no_cursor(conn):
    seed(conn, 3)
    rows = db.articles_page(conn, limit=10)
    assert rows[0]["title"] == "Article 2"
    assert rows[-1]["title"] == "Article 0"


def test_category_helpers(conn):
    db.add_feed(conn, "https://a/feed", categories=["tech", "ai"])
    db.add_feed(conn, "https://b/feed", categories=["ai"])
    assert db.all_categories(conn) == [{"count": 2, "name": "ai"},
                                       {"count": 1, "name": "tech"}]
    assert db.rename_category(conn, "ai", "ai2") == 2
    counts = {c["name"]: c["count"] for c in db.all_categories(conn)}
    assert counts == {"tech": 1, "ai2": 2}
    assert db.remove_category(conn, "tech") == 1
    assert db.all_categories(conn) == [{"count": 2, "name": "ai2"}]


def test_category_filter_in_articles_page(conn):
    fid, _ = seed(conn, 2)
    db.update_feed(conn, fid, categories="tech")
    assert len(db.articles_page(conn, category="tech")) == 2
    assert db.articles_page(conn, category="cooking") == []


def test_usage_totals(conn):
    db.log_usage(conn, "local", "m", 100, 20)
    db.log_usage(conn, "local", "m", 5, 5)
    u = db.usage_totals(conn)
    assert u["today"] == 130 and u["all_time"] == 130 and u["month"] == 130


def test_resume_state_roundtrip(conn):
    assert db.get_state(conn, "resume_ts", "") == ""
    db.set_state(conn, "resume_ts", "2026-10-01T00:00:00Z")
    db.set_state(conn, "resume_ts", "2026-10-02T00:00:00Z")
    assert db.get_state(conn, "resume_ts") == "2026-10-02T00:00:00Z"


def test_summary_cache_lookup_by_hash(conn):
    fid, ids = seed(conn, 1)
    db.set_article(conn, ids[0], body_hash="deadbeef", status="ready", summary="cached")
    assert db.find_summary_by_hash(conn, "deadbeef")["summary"] == "cached"
    assert db.find_summary_by_hash(conn, "unknown") is None
