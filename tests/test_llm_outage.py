"""LLM backend outages hold the queue (never fail articles), transient
errors really retry, bot-check pages served as HTTP 200 are caught, and the
model's "this isn't an article" replies are never published or cached.
Born from: 46 failures during LLM maintenance; 120 TechPowerUp posts that
all got the same cached "this is a bot check" digest."""
import requests

from rssgate import db, maint, refresh
from rssgate.config import load_config
from rssgate.llm import LLMError

ARTICLE = ("<html><body><article>"
           + "".join(f"<p>Paragraph {i}: real reporting about the studio, "
                     f"its {i * 3} staff and the game's launch plans.</p>"
                     for i in range(30))
           + "</article></body></html>")
BOTCHECK = ("<html><body><h2>Automated bot check in progress</h2><p>This "
            "should only take a few seconds. If you have issues, please do "
            "contact us, we want to learn about any problems.</p><p>Drag the "
            "handle to the target</p></body></html>")


class Page:
    def __init__(self, html, status=200):
        self.text, self.status_code, self.ok = html, status, status < 400
        self.content, self.headers = html.encode(), {"content-type": "text/html"}

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def iter_content(self, n): yield b""


class LLM:
    provider, model = "fake", "fake"

    def __init__(self, fail=None, reply="A faithful digest of the article."):
        self.fail, self.reply, self.calls = fail, reply, 0

    def chat(self, messages, **k):
        self.calls += 1
        if self.fail:
            raise self.fail
        return self.reply, {"prompt_tokens": 10, "completion_tokens": 5}


def _one(conn, html=ARTICLE, feed_text=None, url="https://o.test"):
    fid = db.add_feed(conn, url + "/feed", type_="feed")["id"]
    aid = db.upsert_article(conn, fid, "a", url + "/a", "t",
                            "2026-10-07T00:00:00Z", feed_text=feed_text)
    return fid, aid


def _row(conn, aid):
    return conn.execute("SELECT * FROM articles WHERE id=?", (aid,)).fetchone()


def test_backend_down_holds_queue_then_resumes(conn, cfg, monkeypatch):
    hits = []
    monkeypatch.setattr("requests.get",
                        lambda url, **kw: hits.append(url) or Page(ARTICLE))
    c = load_config(cfg)
    _, aid = _one(conn)
    down = LLM(fail=requests.ConnectionError("Connection refused"))
    refresh.summarize_pending(conn, c, down, limit=1)
    r = _row(conn, aid)
    assert r["status"] == "pending" and r["attempts"] == 0   # not its fault
    st = refresh.llm_down_state(conn)
    assert st["since"] and st["next_try"]
    # while down: nothing is claimed, no pages are fetched
    n_hits = len(hits)
    assert refresh.summarize_pending(conn, c, down, limit=1) == 0
    assert len(hits) == n_hits and down.calls == 1
    # probe time arrives and the backend is back
    db.set_state(conn, "llm_next_try", "2000-01-01T00:00:00Z")
    up = LLM()
    assert refresh.summarize_pending(conn, c, up, limit=1) == 1
    assert _row(conn, aid)["status"] == "ready"
    assert not refresh.llm_down_state(conn)["since"]


def test_backend_errors_hold_and_only_request_errors_fail():
    """The real incident: 'models request failed (404)' during maintenance."""
    hold = ["models request failed (404): unknown route", "chat failed (503): x",
            "chat failed (401): bad key", "chat failed (502): gateway",
            "no model configured and auto-selection failed"]
    for msg in hold:
        assert refresh.llm_unavailable(LLMError(msg)), msg
    for msg in ["chat failed (400): bad", "chat failed (413): too large",
                "chat failed (422): invalid"]:
        assert not refresh.llm_unavailable(LLMError(msg)), msg
    assert not refresh.llm_unavailable(ValueError("parse error"))


def test_gateway_errors_count_as_down_but_bad_requests_fail(conn, cfg, monkeypatch):
    monkeypatch.setattr("requests.get", lambda url, **kw: Page(ARTICLE))
    c = load_config(cfg)
    _, a1 = _one(conn, url="https://gw.test")
    refresh.summarize_pending(conn, c, LLM(fail=LLMError("chat failed (503): x")), 1)
    assert _row(conn, a1)["status"] == "pending"
    assert refresh.llm_down_state(conn)["since"]
    refresh.mark_llm_up(conn)
    _, a2 = _one(conn, url="https://br.test")
    db.set_article(conn, a1, status="ready")           # out of the way
    refresh.summarize_pending(conn, c, LLM(fail=LLMError("chat failed (400): bad")), 1)
    r = _row(conn, a2)
    assert r["status"] == "error" and "400" in r["error_msg"]
    assert not refresh.llm_down_state(conn)["since"]   # backend is fine


def test_transient_errors_really_retry(conn, cfg, monkeypatch):
    """The except-handler used to force status=error after fail() chose
    pending, so automatic retries never ran."""
    def boom(url, **kw):
        raise requests.Timeout("read timed out")
    monkeypatch.setattr("requests.get", boom)
    _, aid = _one(conn, url="https://tr.test")
    refresh.summarize_pending(conn, load_config(cfg), LLM(), limit=1)
    r = _row(conn, aid)
    assert r["status"] == "pending" and r["attempts"] == 1
    assert "retrying" in r["error_msg"]


def test_http200_botcheck_page_uses_excerpt_and_flags(conn, cfg, monkeypatch):
    monkeypatch.setattr("requests.get", lambda url, **kw: Page(BOTCHECK))
    llm = LLM()
    fid, aid = _one(conn, feed_text="The studio laid off most of its staff. " * 4,
                    url="https://bc.test")
    refresh.summarize_pending(conn, load_config(cfg), llm, limit=1)
    r = _row(conn, aid)
    assert r["status"] == "ready" and r["digest_source"] == "excerpt"
    assert llm.calls == 0                          # never sent to the model
    assert db.get_feed(conn, fid)["challenge_hits"] == 1


def test_refusal_digest_not_published_or_cached(conn, cfg, monkeypatch):
    monkeypatch.setattr("requests.get", lambda url, **kw: Page(ARTICLE))
    refusal = ("The provided source text does not contain a news article and "
               "cannot be summarized.")
    c = load_config(cfg)
    fid, a1 = _one(conn, feed_text="Feed teaser about the layoffs. " * 4,
                   url="https://rf.test")
    refresh.summarize_pending(conn, c, LLM(reply=refusal), limit=1)
    r = _row(conn, a1)
    assert r["summary"] != refusal and r["digest_source"] == "excerpt"
    # a stored refusal (pre-fix data) must not be served by the hash cache
    a2 = db.upsert_article(conn, fid, "b", "https://rf.test/b", "t2", None)
    import hashlib
    from rssgate.extract import extract_article_text
    h = hashlib.sha256(extract_article_text(ARTICLE, 24000).encode()).hexdigest()
    db.set_article(conn, a1, summary=refusal, body_hash=h, digest_source="")
    llm = LLM()
    refresh.summarize_pending(conn, c, llm, limit=1)
    assert llm.calls == 1 and _row(conn, a2)["summary"] != refusal


def test_real_digest_with_trailing_caveat_is_kept():
    real = ("Reddit is limiting Old Reddit. " * 20 + "The published article "
            "may contain more, but it is not present in the supplied scrape "
            "and therefore cannot be summarized.")
    assert not refresh.is_refusal(real)
    assert refresh.is_refusal("The provided text does not contain a news "
                              "article; no digest can be generated.")


def test_retry_failed_endpoint(client):
    conn = client.conn
    fid = db.add_feed(conn, "https://rt.test/feed", type_="feed")["id"]
    ids = [db.upsert_article(conn, fid, f"g{i}", f"https://rt.test/{i}", "t",
                             None) for i in range(3)]
    for i in ids:
        db.set_article(conn, i, status="error", attempts=3)
    assert client.post("/api/articles/retry-failed").get_json() == {"requeued": 3}
    rows = conn.execute("SELECT status, attempts FROM articles WHERE feed_id=?",
                        (fid,)).fetchall()
    assert all(r["status"] == "pending" and r["attempts"] == 0 for r in rows)


def test_status_reports_llm_outage(client):
    refresh.mark_llm_down(client.conn, "ConnectionError: refused")
    s = client.get("/api/status").get_json()
    assert s["llm_down_since"] and "refused" in s["llm_down_reason"]
    refresh.mark_llm_up(client.conn)
    assert client.get("/api/status").get_json()["llm_down_since"] is None


def test_maintenance_requeues_published_refusals(conn):
    fid = db.add_feed(conn, "https://mr.test/feed", type_="feed")["id"]
    bad = db.upsert_article(conn, fid, "x", "https://mr.test/x", "t", None)
    good = db.upsert_article(conn, fid, "y", "https://mr.test/y", "t", None)
    db.set_article(conn, bad, status="ready", body_hash="h",
                   summary="The provided source text does not contain a news "
                           "article and cannot be summarized.")
    db.set_article(conn, good, status="ready", summary="A real digest. " * 20)
    assert maint.requeue_refusals(conn) == 1
    assert _row(conn, bad)["status"] == "pending" and not _row(conn, bad)["body_hash"]
    assert _row(conn, good)["status"] == "ready"
    assert maint.requeue_refusals(conn) == 0          # idempotent
