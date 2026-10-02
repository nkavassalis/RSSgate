import rssgate.fetcher as fetcher
from rssgate.fetcher import looks_like_feed, fetch_feed, fetch_page, _collect_categories
import feedparser

RSS_WITH_CATS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Tech Weekly</title>
<description>Weekly tech news</description>
<item><title>Post about rockets</title>
 <link>https://ex.com/rockets</link><guid>https://ex.com/rockets</guid>
 <pubDate>Tue, 30 Sep 2026 10:00:00 GMT</pubDate>
 <category>Space</category><category>Rockets</category></item>
<item><title>Post about GPUs</title>
 <link>https://ex.com/gpus</link><guid>https://ex.com/gpus</guid>
 <pubDate>Mon, 29 Sep 2026 10:00:00 GMT</pubDate>
 <category>Hardware</category></item>
</channel></rss>"""


class Resp:
    status_code = 200
    ok = True
    headers = {"content-type": "application/rss+xml"}
    def __init__(self, content): self.content = content
    text = ""


def test_sniff_feed_vs_html():
    assert looks_like_feed(RSS_WITH_CATS)
    assert looks_like_feed(b"  <?xml version='1.0'?><feed/>")
    assert not looks_like_feed(b"<!doctype html><html>")


def test_entry_categories_collected(monkeypatch):
    monkeypatch.setattr(fetcher, "_get", lambda url, e=None, l=None: {
        "ok": True, "not_modified": False, "status": 200,
        "content": RSS_WITH_CATS, "content_type": "", "etag": None, "last_modified": None})
    res = fetch_feed("https://ex.com/feed")
    assert res["ok"]
    assert sorted(res["meta"]["categories"]) == ["Hardware", "Rockets", "Space"]
    assert {e["guid"] for e in res["entries"]} == {"https://ex.com/rockets",
                                                  "https://ex.com/gpus"}
    assert res["entries"][0]["published_at"] == "2026-09-30T10:00:00Z"
    # per-post categories ride along
    assert res["entries"][0]["categories"] == ["Space", "Rockets"]
    assert res["entries"][1]["categories"] == ["Hardware"]


def test_channel_categories_preferred(monkeypatch):
    xml = RSS_WITH_CATS.replace(b"<channel>",
        b"<channel><category>Science</category>")
    monkeypatch.setattr(fetcher, "_get", lambda url, e=None, l=None: {
        "ok": True, "not_modified": False, "status": 200,
        "content": xml, "content_type": "", "etag": None, "last_modified": None})
    assert fetch_feed("u")["meta"]["categories"] == ["Science"]


def test_feed_304_not_modified(monkeypatch):
    monkeypatch.setattr(fetcher, "_get", lambda url, e=None, l=None: {
        "ok": True, "not_modified": True, "status": 304, "content": b"",
        "content_type": "", "etag": None, "last_modified": None})
    res = fetch_feed("u", etag='"abc"')
    assert res["ok"] and not res["changed"] and res["not_modified"]


def test_page_unchanged_detected_by_hash(monkeypatch):
    monkeypatch.setattr(fetcher, "_get", lambda url, e=None, l=None: {
        "ok": True, "not_modified": False, "status": 200,
        "content": b"<html>hi</html>", "content_type": "text/html",
        "etag": '"e1"', "last_modified": None})
    res = fetch_page("https://ex.com/news")
    assert res["changed"] and res["fingerprint"]
    # same content on next poll must produce the same fingerprint
    assert fetch_page("https://ex.com/news")["fingerprint"] == res["fingerprint"]


def test_discover_page_articles_parses_llm_json(monkeypatch):
    html = """<html><body>
      <a href="https://ex.com/posts/real-article-one-with-long-title">Real Article One With Long Title</a>
      <a href="https://ex.com/about-us-page-with-long-text">About Us Page With Long Text</a>
    </body></html>"""
    class FakeLLM:
        provider = "local"; model = "m"
        def chat(self, messages, max_tokens=1200, model=""):
            return ('here you go:\n[{"title": "Real Article One With Long Title", '
                    '"link": "https://ex.com/posts/real-article-one-with-long-title"}] junk',
                    {"prompt_tokens": 10, "completion_tokens": 2})
    out = fetcher.discover_page_articles(html, "https://ex.com/", FakeLLM())
    assert len(out["items"]) == 1
    assert out["items"][0]["link"] == "https://ex.com/posts/real-article-one-with-long-title"
    assert out["usage"]["prompt_tokens"] == 10


def test_discover_falls_back_without_llm(monkeypatch):
    html = '<html><body><a href="https://ex.com/posts/looooong-article-title">Looooong Article Title Text</a></body></html>'
    class DeadLLM:
        provider = "local"; model = "m"
        def chat(self, *a, **k): raise RuntimeError("llm down")
    out = fetcher.discover_page_articles(html, "https://ex.com/", DeadLLM())
    assert out["items"] and out["usage"]["prompt_tokens"] == 0
