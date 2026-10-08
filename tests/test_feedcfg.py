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


def test_custom_prompt_override_and_merge(client):
    from rssgate.config import load_config
    cfg = load_config("__no_such__.yaml")
    feed = {"digest_length": "terse", "system_prompt": "You are HF. {length} max."}
    p = refresh.system_prompt(cfg, feed)
    assert p.startswith("You are HF.")
    assert "ONE sentence" in p                       # digest length still applies
    # empty custom prompt falls back to global (optional by design)
    assert refresh.system_prompt(cfg, {"digest_length": "default",
                                       "system_prompt": "  "}) \
        == refresh.system_prompt(cfg)


def test_system_prompt_persists(client, fake_feed):
    client.put(f"/api/feeds/{fake_feed}",
               json={"system_prompt": "one short bullet only"})
    f = [x for x in client.get("/api/feeds").get_json()
         if x["id"] == fake_feed][0]
    assert f["system_prompt"] == "one short bullet only"
    client.put(f"/api/feeds/{fake_feed}", json={"system_prompt": None})
    f = [x for x in client.get("/api/feeds").get_json()
         if x["id"] == fake_feed][0]
    assert f["system_prompt"] == ""


def test_redigest_endpoint(client, fake_feed):
    conn = client.conn
    for i in range(3):
        aid = db.upsert_article(conn, fake_feed, f"g{i}", f"https://x/{i}",
                                "t", None)
        db.set_article(conn, aid, status="ready", summary="s", body_hash="h" * 64,
                       llm_ms=50)
    r = client.post(f"/api/feeds/{fake_feed}/redigest").get_json()
    assert r["requeued"] == 3
    row = conn.execute("SELECT status, body_hash, llm_ms FROM articles"
                       " LIMIT 1").fetchone()
    assert (row[0], row[1], row[2]) == ("pending", None, 0)
    assert client.post(f"/api/feeds/{fake_feed}/redigest").get_json()["requeued"] == 0
    assert client.post("/api/feeds/999/redigest").status_code == 404


def test_ready_count_in_feeds_payload(client, fake_feed):
    conn = client.conn
    aid = db.upsert_article(conn, fake_feed, "g", "https://x/1", "t", None)
    db.set_article(conn, aid, status="ready", summary="s")
    f = [x for x in client.get("/api/feeds").get_json() if x["id"] == fake_feed][0]
    assert f["ready_count"] == 1


def _fake_page(monkeypatch, html):
    class R:
        ok = True; status_code = 200; text = html
    monkeypatch.setattr("requests.get", lambda *a, **k: R())


def test_failure_reason_always_recorded(client, monkeypatch):
    """Failure reasons are always stored (the opt-in checkbox is gone)."""
    from rssgate.config import load_config
    fid = db.add_feed(client.conn, "https://ex/fail", type_="feed")["id"]
    aid = db.upsert_article(client.conn, fid, "g", "https://blog.example/p",
                            "t", "2026-10-01T00:00:00Z")
    _fake_page(monkeypatch, "<html><body><p>tiny</p></body></html>")  # <120 chars
    cfg = load_config("__no_such__.yaml")
    refresh.summarize_pending(client.conn, cfg, None)
    row = client.conn.execute("SELECT status, error_msg FROM articles"
                              " WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "error" and "too short" in row["error_msg"]
    errs = client.get("/api/feed-errors").get_json()
    assert errs[0]["feed_id"] == fid and "too short" in errs[0]["error_msg"]


def test_per_feed_input_cap(client, monkeypatch, fake_feed):
    from rssgate.config import load_config
    captured = {}
    class RecLLM:
        provider = "local"; model = "m"
        def chat(self, msgs, **kw):
            captured["user"] = msgs[1]["content"]
            return "ok", {"prompt_tokens": 1, "completion_tokens": 1}
    long_html = "<article>" + "".join(
        f"<p>{'word%d ' * 40 % tuple([i] * 40)}</p>" for i in range(80)) + "</article>"
    _fake_page(monkeypatch, long_html)
    fid = fake_feed
    aid = db.upsert_article(client.conn, fid, "g", "https://blog.example/p",
                            "t", "2026-10-01T00:00:00Z")
    cfg = load_config("__no_such__.yaml")
    cfg["summarizer"]["min_text_chars"] = 10
    refresh.summarize_pending(client.conn, cfg, RecLLM())
    full = len(captured["user"])
    client.put(f"/api/feeds/{fid}", json={"max_input_chars": 1500})
    captured.clear()
    db.set_article(client.conn, aid, status="pending", body_hash=None)
    db.update_feed(client.conn, fid, categories="")
    feed = db.get_feed(client.conn, fid)
    refresh.summarize_pending(client.conn, cfg, RecLLM())
    assert len(captured["user"]) < full          # per-feed cap applied


def _entries(*guids):
    return [{"guid": g, "link": f"https://blog.example/{g}", "title": g,
             "published_at": "2026-10-01T00:00:00Z", "categories": [],
             "image": None} for g in guids]


def _serve(monkeypatch, entries):
    monkeypatch.setattr("rssgate.refresh.fetch_feed",
                        lambda url, etag, lm: {
                            "ok": True, "not_modified": False,
                            "meta": {"title": "T", "description": "",
                                     "categories": []},
                            "entries": entries, "etag": None,
                            "last_modified": None,
                            "fingerprint": "fp" + str(len(entries))})


def test_sync_deletes_prunes_vanished_only_when_enabled(client, monkeypatch):
    fid = db.add_feed(client.conn, "https://ex/snap", type_="feed")["id"]
    _serve(monkeypatch, _entries("a", "b", "c"))
    refresh.refresh_feed(client.conn, db.get_feed(client.conn, fid),
                         {"summarizer": {"max_input_chars": 24000}}, llm=None)
    assert client.conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 3
    # off (default): a shrinking feed keeps history
    _serve(monkeypatch, _entries("a", "b"))
    refresh.refresh_feed(client.conn, db.get_feed(client.conn, fid),
                         {"summarizer": {"max_input_chars": 24000}}, llm=None)
    assert client.conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 3
    # on: the vanished entry goes
    client.put(f"/api/feeds/{fid}", json={"sync_deletes": True})
    refresh.refresh_feed(client.conn, db.get_feed(client.conn, fid),
                         {"summarizer": {"max_input_chars": 24000}}, llm=None)
    assert {r[0] for r in client.conn.execute("SELECT guid FROM articles")} == {"a", "b"}


def test_sync_deletes_never_prunes_on_empty_or_unchanged(client, monkeypatch):
    fid = db.add_feed(client.conn, "https://ex/snap2", type_="feed")["id"]
    _serve(monkeypatch, _entries("a", "b"))
    refresh.refresh_feed(client.conn, db.get_feed(client.conn, fid),
                         {"summarizer": {"max_input_chars": 24000}}, llm=None)
    db.update_feed(client.conn, fid, sync_deletes=1)
    _serve(monkeypatch, [])          # parse hiccup / empty feed: keep everything
    refresh.refresh_feed(client.conn, db.get_feed(client.conn, fid),
                         {"summarizer": {"max_input_chars": 24000}}, llm=None)
    assert client.conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 2


def test_images_mode_read_time_filter(client):
    conn = client.conn
    from rssgate import db
    fid = db.add_feed(conn, "https://im.test/f", type_="feed")["id"]
    aid = db.upsert_article(conn, fid, "g1", "https://im.test/1", "T", None)
    conn.execute("UPDATE articles SET status='ready', summary='s',"
                 " image='h.png', images='a.png,b.png' WHERE id=?", (aid,))
    conn.commit()
    item = lambda: next(i for i in client.get("/api/articles").get_json()
                        ["items"] if i["id"] == aid)
    assert item()["gallery"] == ["a.png", "b.png"] and item()["image"]
    assert client.put(f"/api/feeds/{fid}",
                      json={"images_mode": "hero"}).status_code == 200
    assert item()["gallery"] == [] and item()["image"]
    client.put(f"/api/feeds/{fid}", json={"images_mode": "off"})
    assert item()["gallery"] == [] and item()["image"] is None
    client.put(f"/api/feeds/{fid}", json={"images_mode": "bogus"})   # ignored
    assert item()["image"] is None                                   # stays off
    client.put(f"/api/feeds/{fid}", json={"images_mode": "auto"})
    assert item()["gallery"] == ["a.png", "b.png"]
