"""Outbound politeness: per-host pacing, configurable User-Agent, and
site-block backoff (403/429 pause the feed instead of failing its backlog).
Born from TechPowerUp IP-banning us after a ~100-page burst on feed add."""
import time

from rssgate import db, net, refresh
from rssgate.config import load_config


class R:
    def __init__(self, status=200, text="<html><body>"
                 + "<p>Real article text. </p>" * 20 + "</body></html>"):
        self.status_code, self.ok, self.text = status, status < 400, text
        self.content, self.headers = text.encode(), {"content-type": "text/html"}

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def iter_content(self, n): yield b""


def test_pacing_spaces_same_host_only(monkeypatch):
    seen = []
    monkeypatch.setattr("requests.get", lambda url, **kw: seen.append(
        (url, time.monotonic())) or R())
    net.configure({"fetch": {"per_host_interval": 0.25}})
    net.get("https://a.test/1"); net.get("https://a.test/2")
    net.get("https://b.test/1")
    mine = [s for s in seen if ".test/" in s[0] and s[0].split("/")[2]
            in ("a.test", "b.test")]   # other tests' threads may also call
    (_, t1), (_, t2), (_, t3) = mine
    assert t2 - t1 >= 0.2                 # same host waited
    assert t3 - t2 < 0.1                  # other host did not


def test_user_agent_configurable_with_default(monkeypatch):
    heads = []
    monkeypatch.setattr("requests.get",
                        lambda url, headers=None, **kw: heads.append(headers) or R())
    net.configure({"fetch": {"user_agent": "", "per_host_interval": 0}})
    net.get("https://ua.test/")
    assert heads[-1]["user-agent"] == net.DEFAULT_UA
    net.configure({"fetch": {"user_agent": "MyBrowser/1.0",
                             "per_host_interval": 0}})
    net.get("https://ua.test/")
    assert heads[-1]["user-agent"] == "MyBrowser/1.0"


def test_article_403_pauses_feed_and_keeps_post_queued(conn, cfg, monkeypatch):
    monkeypatch.setattr("requests.get", lambda url, **kw: R(403))
    fid = db.add_feed(conn, "https://blocked.test/feed", type_="feed")["id"]
    for i in range(3):
        db.upsert_article(conn, fid, f"g{i}", f"https://blocked.test/{i}",
                          "t", f"2026-10-0{i + 1}T00:00:00Z")

    class NoLLM:
        def chat(self, *a, **k):
            raise AssertionError("no LLM call for a blocked site")

    refresh.summarize_pending(conn, load_config(cfg), NoLLM(), limit=1)
    st = dict(conn.execute("SELECT status, COUNT(*) FROM articles"
                           " WHERE feed_id=? GROUP BY status", (fid,)).fetchall())
    assert st == {"pending": 3}            # nothing burned to error
    f = db.get_feed(conn, fid)
    assert f["backoff_level"] == 1 and db.feed_paused(f)
    assert db.claim_pending(conn, 5) == []  # paused feed is not claimed


def test_feed_403_doubles_and_success_resets(conn, cfg, monkeypatch):
    c = load_config(cfg)
    fid = db.add_feed(conn, "https://wall.test/feed", type_="feed")["id"]
    monkeypatch.setattr("requests.get", lambda url, **kw: R(429))
    s1 = refresh.refresh_feed(conn, db.get_feed(conn, fid), c)
    assert s1.startswith("blocked by site (HTTP 429)")
    conn.execute("UPDATE feeds SET backoff_until=NULL WHERE id=?", (fid,))
    refresh.refresh_feed(conn, db.get_feed(conn, fid), c)
    assert db.get_feed(conn, fid)["backoff_level"] == 2   # doubled
    rss = ("<?xml version='1.0'?><rss version='2.0'><channel><title>W</title>"
           "<item><guid>x</guid><link>https://wall.test/x</link>"
           "<title>X</title></item></channel></rss>")
    monkeypatch.setattr("requests.get", lambda url, **kw: R(200, rss))
    conn.execute("UPDATE feeds SET backoff_until=NULL WHERE id=?", (fid,))
    assert refresh.refresh_feed(conn, db.get_feed(conn, fid), c) != ""
    f = db.get_feed(conn, fid)
    assert f["backoff_level"] == 0 and not db.feed_paused(f)


def test_scheduler_and_refresh_all_skip_paused(conn, cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(refresh, "refresh_feed",
                        lambda conn, feed, cfg, llm=None: calls.append(feed["id"]))
    a = db.add_feed(conn, "https://ok.test/f", type_="feed")["id"]
    b = db.add_feed(conn, "https://paused.test/f", type_="feed")["id"]
    db.feed_block(conn, b, 60)
    refresh.refresh_all(conn, load_config(cfg))
    assert calls == [a]


def test_claim_is_newest_first(conn):
    fid = db.add_feed(conn, "https://order.test/f", type_="feed")["id"]
    old = db.upsert_article(conn, fid, "o", "https://order.test/o", "old",
                            "2020-01-01T00:00:00Z")
    new = db.upsert_article(conn, fid, "n", "https://order.test/n", "new",
                            "2026-01-01T00:00:00Z")
    assert [r["id"] for r in db.claim_pending(conn, 1)] == [new]
    assert [r["id"] for r in db.claim_pending(conn, 1)] == [old]


def test_config_put_validates_fetch_keys(client):
    r = client.put("/api/config", json={"fetch": {
        "user_agent": "Mozilla/5.0 Test", "per_host_interval": 5,
        "block_backoff_minutes": 30}})
    assert r.status_code == 200
    f = client.get("/api/config").get_json()["fetch"]
    assert f["user_agent"] == "Mozilla/5.0 Test" and f["per_host_interval"] == 5
    client.put("/api/config", json={"fetch": {
        "user_agent": "x" * 500, "per_host_interval": 999,
        "block_backoff_minutes": 0}})
    f = client.get("/api/config").get_json()["fetch"]
    assert f["user_agent"] == "Mozilla/5.0 Test"     # rejected, unchanged
    assert f["per_host_interval"] == 5 and f["block_backoff_minutes"] == 30
    assert net.user_agent() == "Mozilla/5.0 Test"    # applied live


# ---- browser challenges (Cloudflare "Just a moment...") vs bans -----------

CF_PAGE = ("<html><head><title>Just a moment...</title></head><body>"
           "Checking your browser. Cloudflare <script src='/cdn-cgi/"
           "challenge-platform/x.js'></script></body></html>")


class CF(R):
    def __init__(self):
        super().__init__(403, CF_PAGE)
        self.headers = {"content-type": "text/html", "cf-mitigated": "challenge",
                        "server": "cloudflare"}


class NoLLM:
    calls = 0

    def chat(self, *a, **k):
        NoLLM.calls += 1
        raise AssertionError("excerpt path must not call the LLM")


def test_challenge_detection_is_not_a_ban():
    assert net.is_challenge(CF())
    assert not net.is_challenge(R(403, "<h1>403 - Access Denied</h1>"))  # a ban
    assert not net.is_challenge(R(200))


def test_challenge_uses_feed_excerpt_zero_tokens_no_pause(conn, cfg, monkeypatch):
    monkeypatch.setattr("requests.get", lambda url, **kw: CF())
    fid = db.add_feed(conn, "https://cf.test/feed", type_="feed")["id"]
    text = "NVIDIA's platform gives the laptop native CUDA support. " * 4
    aid = db.upsert_article(conn, fid, "c1", "https://cf.test/1", "t",
                            "2026-10-07T00:00:00Z", feed_text=text)
    assert refresh.summarize_pending(conn, load_config(cfg), NoLLM(), limit=1) == 1
    a = conn.execute("SELECT status, summary, digest_source FROM articles"
                     " WHERE id=?", (aid,)).fetchone()
    assert a["status"] == "ready" and a["digest_source"] == "excerpt"
    assert a["summary"] == text.strip() or a["summary"] == text
    f = db.get_feed(conn, fid)
    assert f["backoff_level"] == 0 and not db.feed_paused(f)


def test_challenge_without_excerpt_fails_with_plain_reason(conn, cfg, monkeypatch):
    monkeypatch.setattr("requests.get", lambda url, **kw: CF())
    fid = db.add_feed(conn, "https://cf2.test/feed", type_="feed")["id"]
    aid = db.upsert_article(conn, fid, "c1", "https://cf2.test/1", "t", None)
    c = load_config(cfg)
    c.setdefault("troubleshooting", {})["log_llm_failures"] = True
    refresh.summarize_pending(conn, c, NoLLM(), limit=1)
    a = conn.execute("SELECT status, error_msg FROM articles WHERE id=?",
                     (aid,)).fetchone()
    assert a["status"] in ("error", "pending")   # retry policy may re-queue
    if a["error_msg"]:
        assert "requires a browser" in a["error_msg"]
    assert not db.feed_paused(db.get_feed(conn, fid))


def test_too_short_page_falls_back_to_excerpt(conn, cfg, monkeypatch):
    monkeypatch.setattr("requests.get",
                        lambda url, **kw: R(200, "<html><body>tiny</body></html>"))
    fid = db.add_feed(conn, "https://thin.test/feed", type_="feed")["id"]
    aid = db.upsert_article(conn, fid, "t1", "https://thin.test/1", "t", None,
                            feed_text="A real teaser paragraph from the feed. " * 3)
    refresh.summarize_pending(conn, load_config(cfg), NoLLM(), limit=1)
    assert conn.execute("SELECT digest_source FROM articles WHERE id=?",
                        (aid,)).fetchone()[0] == "excerpt"


def test_entry_excerpt_parses_and_backfills_known_posts(conn):
    from rssgate.fetcher import entry_excerpt
    e = {"summary": "<p>First &amp; best.</p><p>Second<br>line</p>",
         "content": [{"value": "<p>short</p>"}]}
    assert entry_excerpt(e) == "First & best.\n\nSecond\n\nline"
    fid = db.add_feed(conn, "https://bf.test/feed", type_="feed")["id"]
    aid = db.upsert_article(conn, fid, "g", "https://bf.test/g", "t", None)
    db.upsert_article(conn, fid, "g", "https://bf.test/g", "t", None,
                      feed_text="filled later")            # known post
    db.upsert_article(conn, fid, "g", "https://bf.test/g", "t", None,
                      feed_text="never clobbers")
    assert conn.execute("SELECT feed_text FROM articles WHERE id=?",
                        (aid,)).fetchone()[0] == "filled later"


# ---- per-feed content source: auto | feed | page --------------------------

class CountLLM:
    provider, model = "fake", "fake"

    def __init__(self):
        self.calls, self.last = 0, ""

    def chat(self, messages, **k):
        self.calls += 1
        self.last = messages[-1]["content"]
        return "digest of feed text", {"prompt_tokens": 1, "completion_tokens": 1}


def _feed_with(conn, url, src, text):
    fid = db.add_feed(conn, url, type_="feed")["id"]
    db.update_feed(conn, fid, content_source=src)
    aid = db.upsert_article(conn, fid, "x", url + "/x", "t", None, feed_text=text)
    return fid, aid


def test_feed_only_never_fetches_page_and_shows_teaser(conn, cfg, monkeypatch):
    hits = []
    monkeypatch.setattr("requests.get", lambda url, **kw: hits.append(url) or R())
    llm = CountLLM()
    _, aid = _feed_with(conn, "https://fo.test", "feed", "A teaser line. " * 8)
    assert refresh.summarize_pending(conn, load_config(cfg), llm, limit=1) == 1
    a = conn.execute("SELECT status, digest_source, summary FROM articles"
                     " WHERE id=?", (aid,)).fetchone()
    assert a["status"] == "ready" and a["digest_source"] == "feed"
    assert llm.calls == 0 and hits == []          # no page, no tokens


def test_feed_only_long_text_gets_llm_digest(conn, cfg, monkeypatch):
    monkeypatch.setattr("requests.get", lambda url, **kw: (_ for _ in ()).throw(
        AssertionError("article page must not be fetched")))
    llm = CountLLM()
    full = "A full article paragraph carried by the feed itself. " * 20
    _, aid = _feed_with(conn, "https://fl.test", "feed", full)
    refresh.summarize_pending(conn, load_config(cfg), llm, limit=1)
    a = conn.execute("SELECT status, digest_source, summary FROM articles"
                     " WHERE id=?", (aid,)).fetchone()
    assert llm.calls == 1 and full[:200] in llm.last
    assert a["status"] == "ready" and a["digest_source"] == "feed"
    assert a["summary"] == "digest of feed text"


def test_page_only_never_falls_back(conn, cfg, monkeypatch):
    monkeypatch.setattr("requests.get", lambda url, **kw: CF())
    _, aid = _feed_with(conn, "https://po.test", "page", "Plenty of feed text. " * 9)
    refresh.summarize_pending(conn, load_config(cfg), NoLLM(), limit=1)
    a = conn.execute("SELECT status, digest_source FROM articles WHERE id=?",
                     (aid,)).fetchone()
    assert a["digest_source"] != "excerpt" and a["status"] in ("error", "pending")


def test_content_source_put_and_excerpt_count(client):
    conn = client.conn
    fid = db.add_feed(conn, "https://cs.test/feed", type_="feed")["id"]
    aid = db.upsert_article(conn, fid, "a", "https://cs.test/a", "t", None)
    db.set_article(conn, aid, status="ready", digest_source="excerpt")
    assert client.put(f"/api/feeds/{fid}", json={"content_source": "bogus"}
                      ).status_code == 200
    f = next(x for x in client.get("/api/feeds").get_json() if x["id"] == fid)
    assert f["content_source"] == "auto" and f["excerpt_count"] == 1
    client.put(f"/api/feeds/{fid}", json={"content_source": "feed"})
    f = next(x for x in client.get("/api/feeds").get_json() if x["id"] == fid)
    assert f["content_source"] == "feed"


# ---- browser-check detection flag -----------------------------------------

def test_challenge_flag_recorded_on_feed(conn, cfg, monkeypatch):
    """Every challenge we see flags the feed (for the admin warning)."""
    monkeypatch.setattr("requests.get", lambda url, **kw: CF())
    fid = db.add_feed(conn, "https://flg.test/feed", type_="feed")["id"]
    db.upsert_article(conn, fid, "c1", "https://flg.test/1", "t", None)
    refresh.summarize_pending(conn, load_config(cfg), NoLLM(), limit=1)
    f = db.get_feed(conn, fid)
    assert f["challenge_hits"] == 1 and f["challenge_at"]
    db.upsert_article(conn, fid, "c2", "https://flg.test/2", "t", None)
    refresh.summarize_pending(conn, load_config(cfg), NoLLM(), limit=1)
    f = db.get_feed(conn, fid)
    assert f["challenge_hits"] == 2
    assert not db.feed_paused(f)               # challenges don't back off


def test_bare_page_challenge_flags_feed(conn, cfg, monkeypatch):
    monkeypatch.setattr("requests.get", lambda url, **kw: CF())
    fid = db.add_feed(conn, "https://blg.test/news", type_="page")["id"]
    status = refresh.refresh_feed(conn, db.get_feed(conn, fid), load_config(cfg))
    assert "browser check" in status
    assert db.get_feed(conn, fid)["challenge_hits"] == 1


def test_challenge_flag_visible_in_feeds_api(client):
    conn = client.conn
    fid = db.add_feed(conn, "https://vis.test/feed", type_="feed")["id"]
    db.feed_challenge(conn, fid)
    f = next(x for x in client.get("/api/feeds").get_json() if x["id"] == fid)
    assert f["challenge_at"] and f["challenge_hits"] == 1
    other = db.add_feed(conn, "https://vis.test/other", type_="feed")["id"]
    f2 = next(x for x in client.get("/api/feeds").get_json() if x["id"] == other)
    assert not f2.get("challenge_at")
