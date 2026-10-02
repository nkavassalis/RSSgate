import hashlib
import rssgate.refresh as refresh
import rssgate.fetcher as fetcher
from rssgate import db
from rssgate.config import load_config

ARTICLE_HTML = ("<html><body><article>" +
    "".join(f"<p>Sentence number {i} about the quarterly earnings report, fact {i}.</p>"
            for i in range(20)) +
    "</article></body></html>")

PAGE_HTML = ("<html><body>" +
    "".join(f'<a href="https://ex.com/posts/article-{i}-with-sufficiently-long-title">'
            f'Article {i} with a sufficiently long title</a>' for i in range(3)) +
    "</body></html>")


class FakeLLM:
    provider = "local"
    model = "m"
    calls = 0
    def chat(self, messages, max_tokens=1200, model=""):
        FakeLLM.calls += 1
        return ("DIGEST TEXT " + str(FakeLLM.calls)), {"prompt_tokens": 50, "completion_tokens": 10}


def stub_network(monkeypatch, page_html=ARTICLE_HTML, page_bytes=None):
    content = page_bytes or page_html.encode()
    monkeypatch.setattr(fetcher, "_get", lambda url, e=None, l=None: {
        "ok": True, "not_modified": False, "status": 200, "content": content,
        "content_type": "text/html", "etag": None, "last_modified": None})
    class R:
        ok = True; status_code = 200
        text = page_html
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())


def test_summarize_once_and_log_usage(conn, cfg, monkeypatch):
    stub_network(monkeypatch)
    fid = db.add_feed(conn, "https://ex.com/feed", type_="feed")["id"]
    aid = db.upsert_article(conn, fid, "g", "https://ex.com/post", "T", None)
    from rssgate.config import load_config
    import os
    n = refresh.summarize_pending(conn, load_config(cfg), FakeLLM())
    assert n == 1
    art = conn.execute("SELECT * FROM articles WHERE id=?", (aid,)).fetchone()
    assert art["status"] == "ready" and art["summary"].startswith("DIGEST")
    assert db.usage_totals(conn)["today"] == 60
    # nothing pending anymore -> no second LLM call
    before = FakeLLM.calls
    assert refresh.summarize_pending(conn, load_config(cfg), FakeLLM()) == 0
    assert FakeLLM.calls == before


def test_identical_article_reuses_summary_cache(conn, cfg, monkeypatch):
    stub_network(monkeypatch)
    from rssgate.config import load_config
    f1 = db.add_feed(conn, "https://a/feed", type_="feed")["id"]
    f2 = db.add_feed(conn, "https://b/feed", type_="feed")["id"]
    db.upsert_article(conn, f1, "g1", "https://ex.com/post", "T", None)
    refresh.summarize_pending(conn, load_config(cfg), FakeLLM())
    calls_after_first = FakeLLM.calls
    db.upsert_article(conn, f2, "g2", "https://ex.com/post", "T", None)
    assert refresh.summarize_pending(conn, load_config(cfg), FakeLLM()) == 1
    assert FakeLLM.calls == calls_after_first  # served from cache, zero tokens
    assert db.get_state(conn, "cache_hits") == "1"


def test_bare_page_unchanged_skips_llm(conn, cfg, monkeypatch):
    stub_network(monkeypatch, page_html=PAGE_HTML)
    from rssgate.config import load_config
    FakeLLM.calls = 0
    fid = db.add_feed(conn, "https://ex.com/news", type_="page")["id"]
    feed = db.get_feed(conn, fid)
    # first refresh: LLM discovers articles
    status = refresh.refresh_feed(conn, feed, load_config(cfg), FakeLLM())
    assert status.startswith("ok")
    calls_first = FakeLLM.calls
    assert calls_first >= 1
    # second refresh with identical bytes -> "not modified", no LLM discovery
    feed = db.get_feed(conn, fid)
    status = refresh.refresh_feed(conn, feed, load_config(cfg), FakeLLM())
    assert status == "not modified"
    assert FakeLLM.calls == calls_first  # no extra discovery call


def test_feed_304_skips_everything(conn, cfg, monkeypatch):
    monkeypatch.setattr(fetcher, "_get", lambda url, e=None, l=None: {
        "ok": True, "not_modified": True, "status": 304, "content": b"",
        "content_type": "", "etag": None, "last_modified": None})
    from rssgate.config import load_config
    fid = db.add_feed(conn, "https://ex.com/feed", type_="feed")["id"]
    feed = db.get_feed(conn, fid)
    assert refresh.refresh_feed(conn, feed, load_config(cfg), FakeLLM()) == "not modified"
    assert conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"] == 0
