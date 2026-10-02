import rssgate.refresh as refresh
from rssgate import db
from rssgate.config import load_config
from rssgate.refresh import is_sponsored


def test_is_sponsored_heuristics():
    assert is_sponsored("Keep up with the people you love — it's a breeze with Hypershell tech, now up to 30% off", "https://gizmodo.com/x")
    assert is_sponsored("Our biggest deal of the day picks", "https://x/y")
    assert is_sponsored("Normal headline", "https://site.com/sponsored/post")
    assert is_sponsored("Sponsored: A word from our partner", "https://x/y")
    assert not is_sponsored("Scientists invent underwater umbrellas", "https://gizmodo.com/coral")
    assert not is_sponsored("The dealer caught big losses at the track", "https://x/y")
    assert not is_sponsored("A promoter's downfall: political story", "https://x/y")


class FakeLLM:
    provider = "local"; model = "m"; calls = 0
    reply = "DIGEST"
    def chat(self, messages, max_tokens=1200, model=""):
        FakeLLM.calls += 1
        return self.reply, {"prompt_tokens": 5, "completion_tokens": 1}


def setup(conn, html, flag=1):
    fid = db.add_feed(conn, "https://ex/feed", type_="feed")
    db.update_feed(conn, fid, hide_sponsored=flag)
    aid = db.upsert_article(conn, fid, "g", "https://ex.com/post", "Regular Title", None)
    import requests
    class R: ok = True; status_code = 200; text = html
    requests.get = lambda *a, **k: R()
    return fid, aid


HTML = "<html><body><article>" + "".join(
    f"<p>Substantive unique paragraph {i} for extraction sanity.</p>"
    for i in range(15)) + "</article></body></html>"


def test_sponsored_hidden_before_llm_zero_tokens(conn, cfg, monkeypatch):
    FakeLLM.calls = 0
    fid, aid = setup(conn, HTML)
    db.set_article(conn, aid, title="Hypershell exoskeleton now up to 30% off for prime day")
    n = refresh.summarize_pending(conn, load_config(cfg), FakeLLM())
    assert n == 0 and FakeLLM.calls == 0            # hidden pre-LLM, free
    assert conn.execute("SELECT status FROM articles WHERE id=?", (aid,)).fetchone()["status"] == "hidden"
    assert db.articles_page(conn) == []              # not visible to viewer


def test_nonsponsored_passes_through(conn, cfg):
    FakeLLM.calls = 0
    fid, aid = setup(conn, HTML)
    assert refresh.summarize_pending(conn, load_config(cfg), FakeLLM()) == 1
    assert FakeLLM.calls == 1


def test_llm_detected_sponsored_hidden_after_digest(conn, cfg):
    fid, aid = setup(conn, HTML)
    FakeLLM.reply = "This article is a sponsored promotional piece for Hypershell."
    refresh.summarize_pending(conn, load_config(cfg), FakeLLM())
    art = conn.execute("SELECT status, summary FROM articles WHERE id=?", (aid,)).fetchone()
    assert art["status"] == "hidden"
    assert art["summary"].startswith("This article")   # kept for audit
    FakeLLM.reply = "DIGEST"


def test_unhiding_when_flag_disabled(conn, cfg):
    fid, aid = setup(conn, HTML, flag=1)
    db.set_article(conn, aid, status="hidden")
    db.update_feed(conn, fid, hide_sponsored=0)
    conn.execute("UPDATE articles SET status='pending' WHERE feed_id=? AND status='hidden'", (fid,))
    assert conn.execute("SELECT status FROM articles WHERE id=?", (aid,)).fetchone()["status"] == "pending"


# ------------------------------------------------------------------ API side

def _add(client, url="https://a/feed"):
    return client.post("/api/feeds", json={"url": url}).get_json()["id"]


def test_api_toggle_and_hidden_count(client):
    fid = _add(client)
    conn = client.conn
    db.upsert_article(conn, fid, "g", "https://a/1", "Sponsored: big sale", None)
    conn.execute("UPDATE articles SET status='ready', summary='fine' WHERE feed_id=?", (fid,))
    conn.commit()
    # turning the flag ON sweeps already-digested sponsored items
    r = client.put(f"/api/feeds/{fid}", json={"hide_sponsored": True}).get_json()
    assert r["hide_sponsored"] == 1
    assert client.get("/api/feeds").get_json()[0]["hidden_count"] == 1
    r = client.put(f"/api/feeds/{fid}", json={"hide_sponsored": True}).get_json()
    assert r["hide_sponsored"] == 1
    # turning it off re-queues hidden items
    client.put(f"/api/feeds/{fid}", json={"hide_sponsored": False})
    assert conn.execute("SELECT status FROM articles WHERE feed_id=?", (fid,)).fetchone()["status"] == "pending"
    assert client.get("/api/articles").get_json()["items"][0]["status"] == "pending"
