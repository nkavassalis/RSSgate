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
