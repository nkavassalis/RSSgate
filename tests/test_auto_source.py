"""Auto content source adapts to sites that keep serving bot checks:
3 checks in a row -> stop requesting pages (feed text instead), probe one
page per day; a real page resets the streak. Excerpt posts retry their page
at 6h / 24h / 3d and are upgraded in place to a real digest when it works."""
from rssgate import db, refresh
from rssgate.config import load_config

REAL = ("<html><body><article>" + "".join(
    f"<p>Paragraph {i}: the studio laid off {i * 7} people before launch.</p>"
    for i in range(25)) + "</article></body></html>")
CHECK = ("<html><body><h2>Automated bot check in progress</h2><p>This should "
         "only take a few seconds. If you have issues, please do contact us, "
         "we want to learn about any problems.</p><p>Drag the handle to the "
         "target</p></body></html>")
EXCERPT = "Feed teaser: the studio laid off most of its staff this week. " * 3


class Page:
    def __init__(self, html):
        self.text, self.status_code, self.ok = html, 200, True
        self.content, self.headers = html.encode(), {"content-type": "text/html"}

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def iter_content(self, n): yield b""


class LLM:
    provider, model = "fake", "fake"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, **k):
        self.calls += 1
        return "A real digest of the full article.", {"prompt_tokens": 9,
                                                       "completion_tokens": 9}


def _serve(monkeypatch, html_for):
    hits = []

    def get(url, **kw):
        hits.append(url)
        return Page(html_for(url))
    monkeypatch.setattr("requests.get", get)
    return hits


def _posts(conn, fid, n):
    return [db.upsert_article(conn, fid, f"g{i}", f"https://ab.test/{i}", "t",
                              f"2026-10-{10 + i:02d}T00:00:00Z", feed_text=EXCERPT)
            for i in range(n)]


def test_streak_switches_auto_to_feed_text_and_probes_daily(conn, cfg, monkeypatch):
    hits = _serve(monkeypatch, lambda u: CHECK)
    c = load_config(cfg)
    fid = db.add_feed(conn, "https://ab.test/feed", type_="feed")["id"]
    ids = _posts(conn, fid, 6)
    for _ in range(3):                                   # 3 checks in a row
        refresh.summarize_pending(conn, c, LLM(), limit=1)
    f = db.get_feed(conn, fid)
    assert f["challenge_streak"] == 3 and f["page_probe_at"]
    assert db.feed_pages_skipped(f)
    n = len(hits)
    refresh.summarize_pending(conn, c, LLM(), limit=1)   # 4th: no page request
    assert len(hits) == n
    st = dict(conn.execute("SELECT digest_source, COUNT(*) FROM articles WHERE"
                           " feed_id=? AND status='ready' GROUP BY 1", (fid,)))
    assert st.get("excerpt") == 4
    # probe becomes due: exactly one page request, then skipping resumes
    conn.execute("UPDATE feeds SET page_probe_at='2000-01-01T00:00:00Z'"
                 " WHERE id=?", (fid,))
    refresh.summarize_pending(conn, c, LLM(), limit=1)
    refresh.summarize_pending(conn, c, LLM(), limit=1)
    assert len(hits) == n + 1
    assert db.get_feed(conn, fid)["page_probe_at"] > db.now_iso()


def test_real_page_resets_the_streak(conn, cfg, monkeypatch):
    _serve(monkeypatch, lambda u: REAL)
    fid = db.add_feed(conn, "https://rs.test/feed", type_="feed")["id"]
    conn.execute("UPDATE feeds SET challenge_streak=2 WHERE id=?", (fid,))
    db.upsert_article(conn, fid, "a", "https://rs.test/a", "t", None)
    refresh.summarize_pending(conn, load_config(cfg), LLM(), limit=1)
    f = db.get_feed(conn, fid)
    assert f["challenge_streak"] == 0 and not f["page_probe_at"]


def test_excerpt_upgrades_in_place_when_page_comes_back(conn, cfg, monkeypatch):
    c = load_config(cfg)
    pages = {"html": CHECK}
    _serve(monkeypatch, lambda u: pages["html"])
    fid = db.add_feed(conn, "https://up.test/feed", type_="feed")["id"]
    aid = db.upsert_article(conn, fid, "a", "https://up.test/a", "t", None,
                            feed_text=EXCERPT)
    refresh.summarize_pending(conn, c, LLM(), limit=1)
    a = conn.execute("SELECT * FROM articles WHERE id=?", (aid,)).fetchone()
    assert a["digest_source"] == "excerpt" and a["upgrade_at"]
    assert a["upgrade_tries"] == 0
    # not due yet: nothing happens
    assert refresh.upgrade_excerpts(conn, c, LLM()) == 0
    # due, site still checking: try counted, rescheduled further out
    db.set_article(conn, aid, upgrade_at="2000-01-01T00:00:00Z")
    assert refresh.upgrade_excerpts(conn, c, LLM()) == 0
    a = conn.execute("SELECT * FROM articles WHERE id=?", (aid,)).fetchone()
    assert a["upgrade_tries"] == 1 and a["upgrade_at"] > db.now_iso()
    assert a["status"] == "ready"                  # card never disappears
    # due again and the site now serves the article: upgraded in place
    pages["html"] = REAL
    conn.execute("UPDATE feeds SET challenge_streak=0, page_probe_at=NULL")
    db.set_article(conn, aid, upgrade_at="2000-01-01T00:00:00Z")
    llm = LLM()
    assert refresh.upgrade_excerpts(conn, c, llm) == 1
    a = conn.execute("SELECT * FROM articles WHERE id=?", (aid,)).fetchone()
    assert a["status"] == "ready" and a["digest_source"] == ""
    assert a["summary"] == "A real digest of the full article."
    assert a["upgrade_at"] is None and llm.calls == 1


def test_upgrades_stop_after_the_last_slot(conn, cfg, monkeypatch):
    _serve(monkeypatch, lambda u: CHECK)
    c = load_config(cfg)
    fid = db.add_feed(conn, "https://ls.test/feed", type_="feed")["id"]
    aid = db.upsert_article(conn, fid, "a", "https://ls.test/a", "t", None,
                            feed_text=EXCERPT)
    db.set_article(conn, aid, status="ready", digest_source="excerpt",
                   summary=EXCERPT)
    for tries in range(len(db.UPGRADE_HOURS)):
        conn.execute("UPDATE feeds SET challenge_streak=0, page_probe_at=NULL")
        db.set_article(conn, aid, upgrade_at="2000-01-01T00:00:00Z",
                       upgrade_tries=tries)
        refresh.upgrade_excerpts(conn, c, LLM())
    a = conn.execute("SELECT * FROM articles WHERE id=?", (aid,)).fetchone()
    assert a["upgrade_at"] is None                 # gave up, politely
    assert a["digest_source"] == "excerpt" and a["status"] == "ready"


def test_feeds_api_exposes_auto_state(client):
    conn = client.conn
    fid = db.add_feed(conn, "https://st.test/feed", type_="feed")["id"]
    for _ in range(3):
        db.feed_challenge(conn, fid)
    aid = db.upsert_article(conn, fid, "a", "https://st.test/a", "t", None)
    db.set_article(conn, aid, status="ready", digest_source="excerpt",
                   upgrade_at="2999-01-01T00:00:00Z")
    f = next(x for x in client.get("/api/feeds").get_json() if x["id"] == fid)
    assert f["challenge_streak"] == 3 and f["page_probe_at"]
    assert f["excerpt_count"] == 1 and f["upgrade_pending"] == 1
