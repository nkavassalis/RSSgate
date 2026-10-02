"""Sidebar category filter: multi-select, exact comma-membership, counts."""
from rssgate import db


def seed(conn):
    f1 = db.add_feed(conn, "https://ex/tech", categories=["technology"])["id"]
    f2 = db.add_feed(conn, "https://ex/food", categories=["cooking"])["id"]
    conn.execute("UPDATE feeds SET auto_categories='science,health' WHERE id=?", (f1,))
    a1 = db.upsert_article(conn, f1, "g1", "l1", "Post tagged AI", None,
                           categories=["ai", "tech"])
    a2 = db.upsert_article(conn, f2, "g2", "l2", "Untagged food post", None)
    a3 = db.upsert_article(conn, f1, "g3", "l3", "Internet-y", None,
                           categories=["internet"])
    for a in (a1, a2, a3):
        db.set_article(conn, a, status="ready", summary="s")
    return f1, f2


def test_exact_membership_not_substring(conn):
    seed(conn)
    # 'tech' matches only the post tag; NOT the feed tag 'technology'
    titles = [r["title"] for r in db.articles_page(conn, limit=10, category="tech")]
    assert titles == ["Post tagged AI"]
    # substring would have leaked 'technology'; exact does not
    assert db.articles_page(conn, limit=10, category="ch") == []
    # feed-level tag filters its articles via fallback (f1 -> tech feed)
    titles = [r["title"] for r in
              db.articles_page(conn, limit=10, category="technology")]
    assert set(titles) == {"Post tagged AI", "Internet-y"}


def test_multi_select_is_or(conn):
    seed(conn)
    titles = [r["title"] for r in
              db.articles_page(conn, limit=10, category=["ai", "cooking"])]
    assert set(titles) == {"Post tagged AI", "Untagged food post"}
    # combined with feed filter
    titles = [r["title"] for r in
              db.articles_page(conn, limit=10, category=["ai", "cooking"],
                               feed_id=1)]
    assert titles == ["Post tagged AI"]


def test_category_list_union_counts(conn):
    seed(conn)
    lst = {d["name"]: d["count"] for d in db.category_list(conn)}
    assert lst["ai"] == 1          # post tag
    assert lst["technology"] == 2  # user feed tag -> 2 articles of f1
    assert lst["science"] == 2     # feed-declared (auto) -> f1 articles
    assert lst["cooking"] == 1


def test_api_multi_category_and_viewer_list(client):
    seed(client.conn)
    r = client.get("/api/articles?limit=10&category=ai&category=cooking")
    assert {i["title"] for i in r.get_json()["items"]} == {
        "Post tagged AI", "Untagged food post"}
    # single param still works (back-compat)
    r = client.get("/api/articles?limit=10&category=ai")
    assert [i["title"] for i in r.get_json()["items"]] == ["Post tagged AI"]
    # viewer category list shape
    data = client.get("/api/categories?viewer=1").get_json()
    assert {"name": "ai", "count": 1} in data
    # admin scope unchanged (user-assigned only)
    admin = client.get("/api/categories").get_json()
    assert {d["name"] for d in admin} == {"technology", "cooking"}
