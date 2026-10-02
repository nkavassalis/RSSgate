import rssgate.refresh as refresh
from rssgate import db
from rssgate.config import load_config


class CountingLLM:
    provider = "local"; model = "m"; calls = 0
    def chat(self, messages, max_tokens=1200, model=""):
        CountingLLM.calls += 1
        return "AI DIGEST", {"prompt_tokens": 5, "completion_tokens": 1}


HTML = ("<html><body><article>" + "".join(
    f"<p>Unique extracted paragraph {i} that should appear verbatim in raw mode.</p>"
    for i in range(15)) + "</article></body></html>")


def stub(conn, monkeypatch, summarize_flag):
    import requests
    class R: ok = True; status_code = 200; text = HTML
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    fid = db.add_feed(conn, "https://ex/feed", type_="feed")
    db.update_feed(conn, fid, summarize=summarize_flag)
    aid = db.upsert_article(conn, fid, "g", "https://ex.com/p", "T", None)
    return fid, aid


def test_raw_mode_never_calls_llm(conn, cfg, monkeypatch):
    fid, aid = stub(conn, monkeypatch, 0)
    CountingLLM.calls = 0
    assert refresh.summarize_pending(conn, load_config(cfg), CountingLLM()) == 1
    assert CountingLLM.calls == 0
    art = conn.execute("SELECT * FROM articles WHERE id=?", (aid,)).fetchone()
    assert art["status"] == "ready" and art["llm_ms"] == 0
    assert "Unique extracted paragraph 3" in art["summary"]


def test_llm_mode_still_digests(conn, cfg, monkeypatch):
    fid, aid = stub(conn, monkeypatch, 1)
    CountingLLM.calls = 0
    refresh.summarize_pending(conn, load_config(cfg), CountingLLM())
    assert CountingLLM.calls == 1


def test_api_raw_flag_and_requeue(client, monkeypatch):
    fid = client.post("/api/feeds", json={"url": "https://a/feed"}).get_json()["id"]
    conn = client.conn
    from rssgate import db as _db
    aid = _db.upsert_article(conn, fid, "g", "https://a/1", "t", None)
    _db.set_article(conn, aid, status="ready", summary="raw text", llm_ms=0)
    r = client.put(f"/api/feeds/{fid}", json={"summarize": False}).get_json()
    assert r["summarize"] == 0
    # back to LLM mode: raw-digested items (llm_ms=0) are re-queued
    client.put(f"/api/feeds/{fid}", json={"summarize": True})
    assert conn.execute("SELECT status FROM articles WHERE id=?", (aid,)).fetchone()["status"] == "pending"
    assert client.get("/api/articles").get_json()["items"][0]["feed_summarize"] is True
