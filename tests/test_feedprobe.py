import rssgate.fetcher as fetcher
from rssgate.fetcher import find_feeds

def _res(content, ct="text/html; charset=utf-8", status=200):
    return {"ok": 200 == status, "not_modified": False, "status": status,
            "content": content if isinstance(content, bytes) else content.encode(),
            "content_type": ct, "etag": None, "last_modified": None}

XML = b'<?xml version="1.0"?><rss><channel><title>x</title></channel></rss>'
HTML_ALT = ('<html><head><title>My Blog</title>'
            '<link rel="alternate" type="application/rss+xml" title="Blog Feed" '
            'href="/feed.xml"></head><body>hello</body></html>')
HTML_BARE = '<html><head><title>Dead site</title></head><body>nothing here</body></html>'


def stub_get(monkeypatch, mapping):
    def fake(url, e=None, l=None):
        return mapping.get(url, _res(b"<html></html>", status=404))
    monkeypatch.setattr(fetcher, "_get", fake)


def test_direct_feed(monkeypatch):
    stub_get(monkeypatch, {"https://x.com/feed": _res(XML, "application/rss+xml")})
    assert find_feeds("https://x.com/feed") == {"type": "feed", "candidates": []}


def test_link_alternate_discovery(monkeypatch):
    stub_get(monkeypatch, {"https://blog.example/": _res(HTML_ALT)})
    out = find_feeds("https://blog.example/")
    assert out["type"] == "page"
    assert out["page_title"] == "My Blog"
    assert out["candidates"] == [{"url": "https://blog.example/feed.xml",
                                  "title": "Blog Feed"}]


def test_common_path_fallback(monkeypatch):
    stub_get(monkeypatch, {"https://x.com/": _res(HTML_BARE),
                           "https://x.com/rss.xml": _res(XML, "application/rss+xml")})
    out = find_feeds("https://x.com/")
    assert out["candidates"][0]["url"] == "https://x.com/rss.xml"


def test_feed_host_anchor_discovery(monkeypatch):
    """Ars-style page: anchors point at feeds.arstechnica.com, no 'RSS' wording."""
    html = ('<html><body>'
            '<a href="https://feeds.arstechnica.com/arstechnica/index">'
            'Ars Technica - All Content</a>'
            '<a href="https://arstechnica.com/about/">About us</a>'
            '</body></html>')
    stub_get(monkeypatch, {
        "https://arstechnica.com/rss-feeds/": _res(html),
        "https://feeds.arstechnica.com/arstechnica/index":
            _res(XML, "application/rss+xml")})
    out = find_feeds("https://arstechnica.com/rss-feeds/")
    assert len(out["candidates"]) == 1          # About link ignored; verified
    assert out["candidates"][0]["url"] == "https://feeds.arstechnica.com/arstechnica/index"


def test_unverified_anchors_are_dropped(monkeypatch):
    html = '<html><body><a href="https://feeds.x.com/ghost">Feed</a></body></html>'
    stub_get(monkeypatch, {"https://x.com/blog": _res(html)})  # ghost -> 404
    assert find_feeds("https://x.com/blog")["candidates"] == []


def test_error_status(monkeypatch):
    stub_get(monkeypatch, {})
    out = find_feeds("https://x.com/nothing")
    assert out["type"] == "error" and "404" in out["error"]


def test_probe_endpoint(client, monkeypatch):
    stub_get(monkeypatch, {"https://blog.test/": _res(HTML_ALT)})
    r = client.post("/api/feeds/probe", json={"url": "https://blog.test/"})
    d = r.get_json()
    assert d["type"] == "page" and d["candidates"][0]["title"] == "Blog Feed"
    assert client.post("/api/feeds/probe", json={"url": "junk"}).status_code == 400
