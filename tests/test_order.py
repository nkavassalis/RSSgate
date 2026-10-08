"""Configurable article order: ui.order = newest (default) | oldest."""
from rssgate import db


def seed(conn, n=3):
    fid = db.add_feed(conn, "https://ex/feed")["id"]
    for i in range(n):
        aid = db.upsert_article(conn, fid, f"g{i}", f"l{i}", f"A{i}",
                                f"2026-10-{i + 1:02d}T00:00:00Z")
        db.set_article(conn, aid, status="ready", summary="s")
    return fid


def test_oldest_order_keyset_forward(conn):
    seed(conn)
    rows = db.articles_page(conn, limit=2, order="oldest")
    assert [r["title"] for r in rows] == ["A0", "A1"]
    # cursor = last row -> strictly newer continuation
    last = rows[-1]
    rows = db.articles_page(conn, last["ts"], last["id"], 2, order="oldest")
    assert [r["title"] for r in rows] == ["A2"]
    # default stays newest-first
    rows = db.articles_page(conn, limit=3)
    assert [r["title"] for r in rows] == ["A2", "A1", "A0"]


def test_api_order_param_and_default(client):
    seed(client.conn)
    # explicit param
    r = client.get("/api/articles?limit=10&order=oldest").get_json()
    assert [i["title"] for i in r["items"]] == ["A0", "A1", "A2"]
    # default: config says newest
    assert [i["title"] for i in
            client.get("/api/articles?limit=10").get_json()["items"]
            if True][0] == "A2"
    # flip config default -> no param needed
    cfg = client.get("/api/config").get_json()
    cfg["ui"]["order"] = "oldest"
    client.put("/api/config", json=cfg)
    titles = [i["title"] for i in
              client.get("/api/articles?limit=10").get_json()["items"]]
    assert titles == ["A0", "A1", "A2"]
    assert client.get("/api/resume").get_json()["order"] == "oldest"


def test_oldest_mode_respects_resume_as_floor(client):
    """Boot in oldest mode with no cursor: continue at resume (server default)."""
    seed(client.conn)
    client.post("/api/position", json={"ts": "2026-10-01T00:00:00Z", "id": 1,
                                       "global": True})   # id 1 = first seeded row
    titles = [i["title"] for i in
              client.get("/api/articles?limit=10&order=oldest").get_json()["items"]]
    assert titles == ["A1", "A2"]          # strictly newer than resume
