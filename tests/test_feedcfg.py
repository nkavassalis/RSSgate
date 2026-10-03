"""Per-feed post-category allow list + per-feed digest length."""
import pytest

from rssgate import db, refresh


RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><guid>g-op</guid><link>https://blog.example/op</link>
 <title>Opinion piece</title><category>opinion</category>
 <pubDate>Mon, 29 Sep 2026 10:00:00 GMT</pubDate></item>
<item><guid>g-ai</guid><link>https://blog.example/ai</link>
 <title>AI news</title><category>ai</category>
 <pubDate>Mon, 29 Sep 2026 11:00:00 GMT</pubDate></item>
</channel></rss>"""


@pytest.fixture
def fake_feed(client, monkeypatch):
    """Serve the canned RSS without network; returns feed id."""
    return db.add_feed(client.conn, "https://ex/cfg", type_="feed")["id"]


def _ingest(client, monkeypatch, block=None):
    import feedparser  # noqa: F401  (sanity: rss parses with real parser)
    monkeypatch.setattr("rssgate.refresh.fetch_feed",
                        lambda url, etag, lm: {
                            "ok": True, "not_modified": False,
                            "meta": {"title": "T", "description": "",
                                     "categories": ["opinion", "ai"]},
                            "entries": [
                                {"guid": "g-op", "link": "https://blog.example/op",
                                 "title": "Opinion piece",
                                 "published_at": "2026-09-29T10:00:00Z",
                                 "categories": ["opinion"], "image": None},
                                {"guid": "g-ai", "link": "https://blog.example/ai",
                                 "title": "AI news",
                                 "published_at": "2026-09-29T11:00:00Z",
                                 "categories": ["ai"], "image": None},
                            ], "etag": None, "last_modified": None,
                            "fingerprint": "fp2"})
    fid = db.add_feed(client.conn, "https://ex/block", type_="feed")["id"]
    if block is not None:
        client.put(f"/api/feeds/{fid}", json={"category_block": block})
    conn = client.conn
    feed = db.get_feed(conn, fid)
    monkeypatch.setattr("rssgate.refresh.fetch_feed",
                        lambda url, etag, lm: {
                            "ok": True, "not_modified": False,
                            "meta": {"title": "T", "description": "",
                                     "categories": ["opinion", "ai"]},
                            "entries": [
                                {"guid": "g-op", "link": "https://blog.example/op",
                                 "title": "Opinion piece",
                                 "published_at": "2026-09-29T10:00:00Z",
                                 "categories": ["opinion"], "image": None},
                                {"guid": "g-ai", "link": "https://blog.example/ai",
                                 "title": "AI news",
                                 "published_at": "2026-09-29T11:00:00Z",
                                 "categories": ["ai"], "image": None},
                            ], "etag": None, "last_modified": None,
                            "fingerprint": "fp3"})
    refresh.refresh_feed(conn, db.get_feed(conn, fid),
                         _cfg_with(block), llm=None)
    return fid


def _cfg_with(block):
    from rssgate.config import load_config
    cfg = load_config("__no_such__.yaml")
    return cfg


def test_blocked_category_hidden_at_ingest_zero_tokens(client, monkeypatch):
    class NoLLM:  # any call fails the test
        provider = "x"; model = "y"
        def chat(self, *a, **k): raise AssertionError("LLM must not run")
    fid = _ingest(client, monkeypatch, block=["opinion"])
    conn = client.conn
    st = {r["title"]: r["status"] for r in conn.execute(
        "SELECT title, status FROM articles WHERE feed_id=?", (fid,))}
    assert st["Opinion piece"] == "hidden"      # never queued -> never summarized
    assert st["AI news"] == "pending"


def test_allow_by_default_and_retroactive_sweep(client, monkeypatch):
    fid = _ingest(client, monkeypatch, block=None)
    conn = client.conn
    n = conn.execute("SELECT COUNT(*) FROM articles WHERE feed_id=?",
                     (fid,)).fetchone()[0]
    assert n == 2 and conn.execute("SELECT COUNT(*) FROM articles WHERE"
                                   " feed_id=? AND status='hidden'",
                                   (fid,)).fetchone()[0] == 0
    # blocking later hides what is already stored (pre-queue only)
    client.put(f"/api/feeds/{fid}", json={"category_block": ["opinion"]})
    assert conn.execute("SELECT status FROM articles WHERE title='Opinion piece'"
                        ).fetchone()[0] == "hidden"
    # unblocking re-queues it
    client.put(f"/api/feeds/{fid}", json={"category_block": []})
    assert conn.execute("SELECT status FROM articles WHERE title='Opinion piece'"
                        ).fetchone()[0] == "pending"


def test_feed_categories_endpoint_state(client, monkeypatch):
    fid = _ingest(client, monkeypatch, block=["opinion"])
    data = client.get(f"/api/feeds/{fid}/categories").get_json()
    d = {c["name"]: c for c in data}
    assert d["opinion"]["allowed"] is False and d["opinion"]["count"] == 1
    assert d["ai"]["allowed"] is True
    assert {c["name"] for c in data} == {"opinion", "ai"}   # declared + tagged


def test_digest_length_directive(client, fake_feed):
    from rssgate.config import load_config
    cfg = load_config("__no_such__.yaml")
    base = refresh.system_prompt(cfg)
    assert refresh.system_prompt(cfg, {"digest_length": "default"}) == base
    terse = refresh.system_prompt(cfg, {"digest_length": "terse"})
    assert "ONE sentence" in terse and terse != base
    assert "300-500 words" in refresh.system_prompt(
        cfg, {"digest_length": "detailed"})


def test_digest_length_persists_and_validates(client, fake_feed):
    client.put(f"/api/feeds/{fake_feed}", json={"digest_length": "terse"})
    f = [x for x in client.get("/api/feeds").get_json()
         if x["id"] == fake_feed][0]
    assert f["digest_length"] == "terse"
    client.put(f"/api/feeds/{fake_feed}", json={"digest_length": "wat"})
    f = [x for x in client.get("/api/feeds").get_json()
         if x["id"] == fake_feed][0]
    assert f["digest_length"] == "default"
