"""Post images: declared hero URLs, page extraction, local cache + serving."""
from rssgate import db, imgstore, refresh
from rssgate.config import load_config

JPEG = (b"\xff\xd8\xff\xe0" + b"\x00" * 40)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


class FakeResp:
    def __init__(self, body=b"", text=None, ok=True, ctype="image/jpeg"):
        self.body, self._text, self.ok, self.ctype = body, text, ok, ctype
        self.headers = {"content-type": ctype}
    @property
    def text(self):
        return self._text if self._text is not None else self.body.decode()
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def iter_content(self, n): yield self.body


ARTICLE_HTML = """<html><head>
<meta property="og:image" content="/hero.jpg">
<meta name="twitter:image" content="https://cdn.x/tw.png">
</head><body><article>
<img src="/hero.jpg">
<img src="https://cdn.x/big-photo.png" width="800" height="600">
<img class="ad-banner" src="/ad.gif">
<img src="data:image/png;base64,AAA">
<img src="/tiny.png" width="40">
<p>Some decent article body text goes here for extraction purposes. Some decent article body text goes here for extraction purposes. Some decent article body text goes here for extraction purposes. Some decent article body text goes here for extraction purposes. Some decent article body text goes here for extraction purposes. Some decent article body text goes here for extraction purposes. </p>
</article></body></html>"""


def test_extract_images_priority_and_filters():
    from rssgate.extract import extract_images
    urls = extract_images(ARTICLE_HTML, "https://blog.example/post")
    assert urls[0] == "https://blog.example/hero.jpg"        # og:image first
    assert "https://cdn.x/tw.png" in urls                    # twitter:image
    assert "https://cdn.x/big-photo.png" in urls             # content img
    assert all("ad.gif" not in u for u in urls)              # junk filtered
    assert all(not u.startswith("data:") for u in urls)
    assert all("tiny" not in u for u in urls)                # tiny filtered


def test_feed_entry_image(monkeypatch):
    import rssgate.fetcher as f
    rss = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
    <item><title>i1</title><link>https://x/1</link>
    <media:thumbnail xmlns:media="http://search.yahoo.com/mrss/"
      url="https://img.x/thumb.jpg"/>
    </item></channel></rss>"""
    monkeypatch.setattr(f, "_get", lambda url, e=None, l=None: {
        "ok": True, "not_modified": False, "content": rss.encode(),
        "etag": None, "last_modified": None, "status": 200,
        "content_type": "application/rss+xml"})
    out = f.fetch_feed("https://x/feed")
    assert out["entries"][0]["image"] == "https://img.x/thumb.jpg"


def test_digest_caches_declared_image(conn, cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(imgstore, "_dir", tmp_path / "images")
    (tmp_path / "images").mkdir(exist_ok=True)
    calls = []
    def fake_get(url, **kw):
        calls.append(url)
        if url.endswith(".jpg"):
            return FakeResp(JPEG)
        return FakeResp(text=ARTICLE_HTML)
    monkeypatch.setattr("requests.get", fake_get)
    fid = db.add_feed(conn, "https://ex/img", type_="feed")["id"]
    aid = db.upsert_article(conn, fid, "g1", "https://blog.example/post",
                            "T", "2026-10-02T00:00:00Z", ["ai"],
                            image_url="https://blog.example/hero.jpg")
    n = refresh.summarize_pending(conn, load_config(cfg), FakeImgLLM(), limit=1)
    assert n == 1
    art = conn.execute("SELECT image, image_url, status FROM articles"
                       " WHERE id=?", (aid,)).fetchone()
    assert art["status"] == "ready"
    assert art["image"] and imgstore.safe_path(art["image"])
    assert (tmp_path / "images" / art["image"]).read_bytes().startswith(b"\xff\xd8")
    # image downloaded exactly once from its declared URL
    assert calls.count("https://blog.example/hero.jpg") == 1


class FakeImgLLM:
    provider = "local"
    model = "fake"
    def chat(self, messages, max_tokens=1200, model=""):
        return "IMG DIGEST " * 20, {"prompt_tokens": 40, "completion_tokens": 8}


def test_image_route_and_traversal(client, tmp_path, monkeypatch):
    monkeypatch.setattr(imgstore, "_dir", tmp_path / "images")
    (tmp_path / "images").mkdir(exist_ok=True)
    import hashlib
    name = hashlib.sha256(b"x").hexdigest()[:24] + ".jpg"
    (tmp_path / "images" / name).write_bytes(JPEG)
    r = client.get(f"/image/{name}")
    assert r.status_code == 200 and r.data.startswith(b"\xff\xd8")
    assert client.get("/image/deadbeef.jpg").status_code == 404
    assert client.get("/image/..%2Fconftest.py").status_code == 404
    assert client.get("/image/notes.txt.jpg").status_code == 404


def test_api_articles_exposes_image(client, tmp_path, monkeypatch):
    monkeypatch.setattr(imgstore, "_dir", tmp_path / "images")
    (tmp_path / "images").mkdir(exist_ok=True)
    fid = client.post("/api/feeds", json={"url": "https://ex/i"}).get_json()["id"]
    conn = client.conn
    aid = db.upsert_article(conn, fid, "g", "l", "T", "2026-10-01T00:00:00Z")
    db.set_article(conn, aid, status="ready", summary="s", image="ab" * 12 + ".png")
    item = client.get("/api/articles?limit=1").get_json()["items"][0]
    assert item["image"] == "ab" * 12 + ".png"


def test_backfill_uses_declared_urls_free(conn, cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(imgstore, "_dir", tmp_path / "images")
    (tmp_path / "images").mkdir(exist_ok=True)
    page_calls = []
    def fake_get(url, **kw):
        page_calls.append(url)
        if url.endswith(".jpg"):
            return FakeResp(JPEG)
        return FakeResp(text=ARTICLE_HTML)   # page -> og:image /hero.jpg
    monkeypatch.setattr("requests.get", fake_get)
    fid = db.add_feed(conn, "https://ex/bf")["id"]
    a1 = db.upsert_article(conn, fid, "b1", "https://x/p1", "T1",
                           "2026-10-01T00:00:00Z", image_url="https://i/1.jpg")
    a2 = db.upsert_article(conn, fid, "b2", "https://x/p2", "T2",
                           "2026-10-02T00:00:00Z")           # no declared url
    for a in (a1, a2):
        db.set_article(conn, a, status="ready", summary="s" * 40)
    n = refresh.backfill_images(conn, load_config(cfg), page_fetches=1)
    assert n == 2
    assert "https://i/1.jpg" in page_calls                  # free, declared
    assert "https://x/p2" in page_calls                     # capped page fetch
    both = conn.execute("SELECT image FROM articles WHERE image IS NOT NULL"
                        " ").fetchall()
    assert len(both) == 2


def test_gallery_stored_and_exposed(client, tmp_path, monkeypatch):
    monkeypatch.setattr(imgstore, "_dir", tmp_path / "images")
    (tmp_path / "images").mkdir(exist_ok=True)
    html = ARTICLE_HTML.replace(
        "<p>", '<img src="https://cdn.x/second.png"><p>')
    def fake_get(url, **kw):
        if url.endswith((".jpg", ".png")):
            return FakeResp(PNG if url.endswith(".png") else JPEG)
        return FakeResp(text=html)
    monkeypatch.setattr("requests.get", fake_get)
    fid = client.post("/api/feeds", json={"url": "https://ex/g"}).get_json()["id"]
    conn = client.conn
    aid = db.upsert_article(conn, fid, "g", "https://blog.example/post",
                            "T", "2026-10-02T00:00:00Z")
    n = refresh.summarize_pending(conn, load_config("__no_such__.yaml"),
                                  FakeImgLLM(), limit=1)   # DEFAULTS suffice
    assert n == 1
    item = client.get("/api/articles?limit=1").get_json()["items"][0]
    assert item["image"] and len(item["gallery"]) >= 2
    for g in item["gallery"]:
        assert client.get(f"/image/{g}").status_code == 200
