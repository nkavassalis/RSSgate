from rssgate import db
from urllib.parse import quote


def seed(client, n=5, cats="tech"):
    r = client.post("/api/feeds", json={"url": "https://ex.com/feed", "type": "feed",
                                        "categories": [cats]})
    assert r.status_code == 201
    fid = r.get_json()["id"]
    conn = client.conn
    from rssgate import db
    for i in range(n):
        aid = db.upsert_article(conn, fid, f"g{i}", f"https://ex.com/{i}",
                                f"Article {i}", f"2026-10-{i + 1:02d}T00:00:00Z")
        db.set_article(conn, aid, status="ready", summary=f"digest {i}")
    return fid


def test_viewer_and_admin_pages(client):
    assert client.get("/").status_code == 200
    assert b"RSSgate" in client.get("/").data
    assert client.get("/admin").status_code == 200


def test_articles_reverse_chrono_and_pagination(client):
    seed(client, 5)
    r = client.get("/api/articles?limit=3").get_json()
    assert [i["title"] for i in r["items"]] == ["Article 4", "Article 3", "Article 2"]
    assert r["has_more"] is True
    last = r["items"][-1]
    r2 = client.get(f"/api/articles?limit=3&before_ts={last['ts']}&before_id={last['id']}"
                    ).get_json()
    assert [i["title"] for i in r2["items"]] == ["Article 1", "Article 0"]
    assert r2["has_more"] is False


def test_resume_position_roundtrip(client):
    seed(client, 3)
    assert client.get("/api/resume").get_json()["resume_ts"] == ""
    items = client.get("/api/articles?limit=2").get_json()["items"]
    r = client.post("/api/position", json={"ts": items[-1]["ts"], "id": items[-1]["id"]})
    assert r.get_json()["ok"]
    # next default load resumes strictly older than the saved position
    nxt = client.get("/api/articles?limit=10").get_json()["items"]
    assert [i["title"] for i in nxt] == ["Article 0"]


def test_article_cards_carry_feed_categories(client):
    seed(client, 1, cats="tech, science")
    conn = client.conn
    conn.execute("UPDATE feeds SET auto_categories='Space,Rockets'")
    conn.execute("UPDATE articles SET categories='Physics,Gene Editing'")
    conn.commit()
    item = client.get("/api/articles").get_json()["items"][0]
    assert item["categories"] == ["tech", "science"]        # user-assigned (feed)
    assert item["auto_categories"] == ["Space", "Rockets"]  # feed-level fallback
    assert item["post_categories"] == ["Physics", "Gene Editing"]  # the post's own


def test_add_feed_validates_and_stores_categories(client):
    r = client.post("/api/feeds", json={"url": "not-a-url"})
    assert r.status_code == 400
    r = client.post("/api/feeds", json={"url": "https://a/feed",
                                        "categories": ["ai", " robots "]})
    f = r.get_json()
    assert f["categories"] == ["ai", "robots"]
    # duplicate URL rejected
    assert client.post("/api/feeds", json={"url": "https://a/feed"}).status_code == 409


def test_category_management_endpoints(client):
    seed(client, 1, cats="gadgets")
    client.post("/api/feeds", json={"url": "https://ex2.com/feed", "categories": ["gadgets"]})
    cats = client.get("/api/categories").get_json()
    assert {"name": "gadgets", "count": 2} in cats
    r = client.post("/api/categories/rename", json={"from": "gadgets", "to": "hardware"})
    assert r.get_json()["feeds_updated"] == 2
    assert client.delete("/api/categories/hardware").get_json()["feeds_updated"] == 2
    assert client.get("/api/categories").get_json() == []


def test_update_feed_categories_and_delete(client):
    fid = seed(client, 2)
    r = client.put(f"/api/feeds/{fid}", json={"categories": ["ai", "tech"]})
    assert r.get_json()["categories"] == ["ai", "tech"]
    assert client.delete(f"/api/feeds/{fid}").get_json()["ok"]
    assert client.get("/api/articles").get_json()["items"] == []


def test_config_api_masks_key_and_persists(client, tmp_path):
    cfg = client.put("/api/config", json={"llm": {"provider": "openrouter",
                                                  "api_key": "sk-key", "model": "x"},
                                          "polling": {"feed_interval_minutes": 10}}).get_json()
    assert cfg["llm"]["api_key"] == "***" and cfg["llm"]["api_key_set"] is True
    assert cfg["polling"]["feed_interval_minutes"] == 10
    plain = open([p for p in __import__("glob").glob(str(tmp_path / "*.yaml"))][0]).read()
    assert "sk-key" in plain  # stored on disk, masked to browser


def test_usage_and_status(client):
    conn = client.conn
    from rssgate import db
    db.log_usage(conn, "local", "m", 10, 5)
    assert client.get("/api/usage").get_json()["today"] == 15
    assert client.get("/api/status").get_json()["feeds"] == 0


def test_llm_test_endpoint(client):
    r = client.post("/api/llm/test").get_json()
    assert r["ok"] and r["reply"] == "OK"


def test_models_endpoint(client):
    assert client.get("/api/models").get_json()["models"] == ["alpha", "beta"]


def test_poll_now_throttles(client, monkeypatch):
    db.add_feed(client.conn, "https://poll.test/feed", type_="feed")
    calls = []
    monkeypatch.setattr("rssgate.refresh.refresh_feed",
                        lambda *a, **k: calls.append(1) or "ok")
    r = client.post("/api/poll").get_json()
    assert r == {"ok": True, "started": True}
    import time
    for _ in range(50):                     # thread is daemon; wait a beat
        time.sleep(0.05)
        if calls:
            break
    assert calls                             # feeds actually poked
    r2 = client.post("/api/poll").get_json()
    assert r2["started"] is False and r2["why"] == "throttled"


def test_qr_route_encodes_locally(client):
    r = client.get("/api/qr.png?u=" + quote("https://example.com/a?b=1&c=2"))
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert r.data[:8] == bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
    assert r.headers["Cache-Control"].startswith("public")
    assert client.get("/api/qr.png").status_code == 400
    assert client.get("/api/qr.png?u=javascript:alert(1)").status_code == 400
    assert client.get("/api/qr.png?u=" + "x" * 600).status_code == 400


def test_hide_untranscribed_default_and_toggle(client):
    from rssgate import db
    conn = client.conn
    fid = db.add_feed(conn, "https://ht.test/f", type_="feed")["id"]
    a = db.upsert_article(conn, fid, "rdy", "https://ht.test/1", "Ready one",
                          None)
    conn.execute("UPDATE articles SET status='ready', summary='d' WHERE id=?", (a,))
    conn.commit()
    db.upsert_article(conn, fid, "wait", "https://ht.test/2", "Waiting", None)
    conn.commit()
    titles = lambda: sorted(x["title"] for x in
                            client.get("/api/articles").get_json()["items"])
    assert titles() == ["Ready one"]                    # hidden by default
    body = client.get("/api/config").get_json()
    body["ui"]["hide_untranscribed"] = False
    client.put("/api/config", json=body)
    assert titles() == ["Ready one", "Waiting"]


def test_trimmed_hero_revives_lazily(client, tmp_path, monkeypatch):
    import hashlib
    from rssgate import db
    conn = client.conn
    fid = db.add_feed(conn, "https://rv.test/f", type_="feed")["id"]
    url = "https://rv.test/hero.png"
    stem = hashlib.sha256(url.encode()).hexdigest()[:24]
    name = stem + ".png"
    aid = db.upsert_article(conn, fid, "rv1", "https://rv.test/1", "T", None)
    db.set_article(conn, aid, image=name, image_url=url)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    def fake_get(u, **kw):
        class R:
            ok = True
            headers = {"content-type": "image/png"}
            def iter_content(self, n=8192):
                yield png
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()
    monkeypatch.setattr("requests.get", fake_get)
    assert client.get(f"/image/{name}").status_code == 200   # revived


def test_clear_failed_endpoint(client):
    from rssgate import db
    conn = client.conn
    fid = db.add_feed(conn, "https://clear.test/f", type_="feed")["id"]
    for i in range(3):
        aid = db.upsert_article(conn, fid, f"e{i}", f"https://clear.test/{i}",
                                "failing", None)
        conn.execute("UPDATE articles SET status='error' WHERE id=?", (aid,))
    ok = db.upsert_article(conn, fid, "k", "https://clear.test/ok",
                           "survivor", None)
    conn.commit()
    r = client.post("/api/articles/clear-failed")
    assert r.status_code == 200 and r.get_json()["deleted"] == 3
    left = conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"]
    assert left == 1
    assert conn.execute("SELECT id FROM articles WHERE id=?",
                        (ok,)).fetchone()


def test_poll_is_single_route_and_reports_throttle(client, monkeypatch):
    """One /api/poll handler (a duplicate once lurked, sharing the
    request connection with a thread), and the throttle is a boolean the
    viewer can read (it checks j.throttled to skip the settle wait)."""
    import threading
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    rules = [r for r in client.application.url_map.iter_rules()
             if r.rule == "/api/poll"]
    assert len(rules) == 1
    first = client.post("/api/poll").get_json()
    assert first.get("throttled") in (None, False)
    second = client.post("/api/poll").get_json()
    assert second["throttled"] is True and second["started"] is False
