"""Smart default blocking of feed-declared ad categories (v0.38.0)."""
import rssgate.refresh as R
from rssgate import db


def _monkey_fetch(monkeypatch, cats, entries):
    monkeypatch.setattr("rssgate.refresh.fetch_feed", lambda *a, **k: {
        "ok": True, "changed": True,
        "meta": {"title": "T", "description": "", "categories": cats},
        "entries": entries, "etag": None, "last_modified": None,
        "fingerprint": "fp"})


def test_partner_category_auto_blocked(conn, monkeypatch, cfg):
    fid = db.add_feed(conn, "https://gd.test/feed", type_="feed")["id"]
    a1 = db.upsert_article(conn, fid, "g1", "https://gd.test/1",
                           "Robot vacuum promo", None,
                           categories=["Partners"])
    db.set_article(conn, a1, status="ready", summary="was digested")
    a2 = db.upsert_article(conn, fid, "g2", "https://gd.test/2",
                           "Another ad", None,
                           categories=["Partners"])
    db.set_article(conn, a2, status="pending")
    _monkey_fetch(monkeypatch, ["Partners", "Gadgets"], [])
    R.refresh_feed(conn, db.get_feed(conn, fid), cfg, None)
    f = db.get_feed(conn, fid)
    assert f["category_block"] == "Partners" and f["smart_block"] == 1
    st = dict(conn.execute("SELECT guid, status FROM articles WHERE feed_id=?",
                           (fid,)).fetchall())
    assert st == {"g1": "hidden", "g2": "hidden"}   # retro + pre-claim, 0 tokens


def test_user_ownership_disables_smart_default(conn, monkeypatch, cfg):
    fid = db.add_feed(conn, "https://gd2.test/feed", type_="feed")["id"]
    db.update_feed(conn, fid, category_block="WeirdTag", smart_block=0)
    _monkey_fetch(monkeypatch, ["Partners"], [])
    R.refresh_feed(conn, db.get_feed(conn, fid), cfg, None)
    f = db.get_feed(conn, fid)
    assert f["category_block"] == "WeirdTag"          # untouched
    assert f["smart_block"] == 0
    # explicit opt-out (cleared via admin checkboxes) also sticks:
    db.update_feed(conn, fid, category_block="", smart_block=2)
    R.refresh_feed(conn, db.get_feed(conn, fid), cfg, None)
    assert db.get_feed(conn, fid)["category_block"] == ""


def test_editorial_deals_category_not_blocked(conn, monkeypatch, cfg):
    fid = db.add_feed(conn, "https://kd.test/feed", type_="feed")["id"]
    _monkey_fetch(monkeypatch, ["Deals", "Reviews"], [])
    R.refresh_feed(conn, db.get_feed(conn, fid), cfg, None)
    f = db.get_feed(conn, fid)
    assert f["category_block"] == "" and f["smart_block"] == 0
