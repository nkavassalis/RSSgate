"""Sidebar category boxes: Post Categories (post tags) and Feed Categories
(user feed labels) are INDEPENDENT filters; multi-select ORs within a box,
ANDs across boxes; feed-declared tags stay filter-inert."""
from rssgate import db


def seed(conn):
    f1 = db.add_feed(conn, "https://ex/tech", categories=["technology"])["id"]
    f2 = db.add_feed(conn, "https://ex/food", categories=["cooking"])["id"]
    db.add_feed(conn, "https://ex/empty", categories=["ghost"])["id"]  # no posts
    conn.execute("UPDATE feeds SET auto_categories='science,health' WHERE id=?", (f1,))
    a1 = db.upsert_article(conn, f1, "g1", "l1", "Tagged AI post", None,
                           categories=["ai", "tech"])
    a2 = db.upsert_article(conn, f2, "g2", "l2", "Untagged food post", None)
    a3 = db.upsert_article(conn, f1, "g3", "l3", "Tagged Internet post", None,
                           categories=["internet"])
    a4 = db.upsert_article(conn, f1, "g4", "l4", "Untagged tech-feed post", None)
    for a in (a1, a2, a3, a4):
        db.set_article(conn, a, status="ready", summary="s")
    return f1, f2


def test_feed_declared_tags_are_filter_inert(conn):
    """THE regression: feed-declared union tags (e.g. Gizmodo's ~40-tag
    'health' in the soup) must not match ANYTHING in either box."""
    seed(conn)
    assert db.articles_page(conn, limit=10, category="science") == []
    assert db.articles_page(conn, limit=10, feed_category="science") == []
    assert db.articles_page(conn, limit=10, category="health") == []
    # exact membership, not substrings
    assert [r["title"] for r in
            db.articles_page(conn, limit=10, category="tech")] == ["Tagged AI post"]
    assert db.articles_page(conn, limit=10, category="ch") == []


def test_post_box_is_own_tags_only_feed_box_is_labels(conn):
    seed(conn)
    # Post Categories box: only articles carrying the tag themselves
    assert [r["title"] for r in
            db.articles_page(conn, limit=10, category="technology")] == []
    # Feed Categories box: every post of a labeled feed
    titles = {r["title"] for r in
              db.articles_page(conn, limit=10, feed_category="technology")}
    assert titles == {"Tagged AI post", "Tagged Internet post",
                      "Untagged tech-feed post"}


def test_multi_or_within_box_and_across_boxes(conn):
    seed(conn)
    titles = {r["title"] for r in
              db.articles_page(conn, limit=10, category=["ai", "internet"])}
    assert titles == {"Tagged AI post", "Tagged Internet post"}
    titles = {r["title"] for r in
              db.articles_page(conn, limit=10, feed_category=["cooking", "technology"])}
    assert len(titles) == 4                       # both feeds' posts
    # AND across boxes: tech-labeled feed AND internet-tagged post
    titles = [r["title"] for r in db.articles_page(
        conn, limit=10, feed_category="technology", category="internet")]
    assert titles == ["Tagged Internet post"]
    # composes with feed filter
    titles = [r["title"] for r in db.articles_page(
        conn, limit=10, category=["ai", "internet"], feed_id=2)]
    assert titles == []


def test_category_list_split_payload_counts_match_the_filter(conn):
    seed(conn)
    data = db.category_list(conn)
    post = {d["name"]: d["count"] for d in data["post"]}
    feed = {d["name"]: d["count"] for d in data["feed"]}
    assert post == {"ai": 1, "tech": 1, "internet": 1}
    assert feed == {"technology": 3, "cooking": 1}   # 'ghost': zero posts, hidden
    assert "science" not in post and "science" not in feed   # auto-only: never


def test_api(client):
    seed(client.conn)
    r = client.get("/api/articles?limit=10&category=ai&category=internet")
    assert {i["title"] for i in r.get_json()["items"]} == {
        "Tagged AI post", "Tagged Internet post"}
    r = client.get("/api/articles?limit=10&feed_category=cooking")
    assert [i["title"] for i in r.get_json()["items"]] == ["Untagged food post"]
    r = client.get("/api/articles?limit=10&category=science")
    assert r.get_json()["items"] == []
    data = client.get("/api/categories?viewer=1").get_json()
    assert {"name": "ai", "count": 1} in data["post"]
    assert {"name": "technology", "count": 3} in data["feed"]
    admin = client.get("/api/categories").get_json()            # scope intact
    assert {d["name"] for d in admin} == {"technology", "cooking", "ghost"}


def test_case_insensitive_chips_and_ops(conn):
    # same category stored with two spellings (feed label lc, post tag tc)
    f1 = db.add_feed(conn, "https://ex/mixed", categories=["tech news"])["id"]
    a1 = db.upsert_article(conn, f1, "g1", "l1", "T1", None, categories=["Tech News"])
    a2 = db.upsert_article(conn, f1, "g2", "l2", "T2", None, categories=["tech news"])
    for a in (a1, a2):
        db.set_article(conn, a, status="ready", summary="s")
    data = db.category_list(conn)
    lst = [d for d in data["post"] if d["name"].casefold() == "tech news"]
    assert len(lst) == 1 and lst[0]["count"] == 2       # ONE merged chip
    flst = [d for d in data["feed"] if d["name"].casefold() == "tech news"]
    assert len(flst) == 1 and flst[0]["count"] == 2
    # filter works from either spelling
    assert len(db.articles_page(conn, limit=10, category="Tech News")) == 2
    assert len(db.articles_page(conn, limit=10, category="tech news")) == 2
    # admin list merges user-assigned variants too
    adm = [d for d in db.all_categories(conn) if d["name"].casefold() == "tech news"]
    assert len(adm) == 1 and adm[0]["count"] == 1       # one FEED uses it
    # remove kills every spelling
    assert db.remove_category(conn, "Tech News") == 1
    assert db.get_feed(conn, f1)["categories"] == ""
