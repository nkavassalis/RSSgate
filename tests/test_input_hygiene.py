"""Every API endpoint survives malformed input: no tracebacks, no 5xx.
Found 12 crash sites in the rough-edges audit (list bodies, 'abc' ids,
limit=abc, non-list category fields, non-string urls)."""


def test_malformed_input_never_crashes(client):
    from rssgate import db
    fid = db.add_feed(client.conn, "https://fz.test/f", type_="feed")["id"]
    bad_bodies = [None, "not json", "[]", "{}", '{"x": 1}', '{"ts": 5, "id": "abc"}',
                  '{"read_ids": "nope"}', '{"read_ids": [1, "x", null]}',
                  '{"categories": "notalist"}', '{"category_block": 7}',
                  '{"ui": "str"}', '{"fetch": []}', '{"url": 12}', '{"from": null}']
    routes = [("POST","/api/position"),("PUT",f"/api/feeds/{fid}"),("PUT","/api/config"),
              ("POST","/api/feeds"),("POST","/api/feeds/probe"),("POST","/api/categories/rename"),
              ("PUT","/api/feeds/999999"),("POST","/api/articles/999999/retry"),
              ("POST","/api/articles/999999/drop"),("POST","/api/feeds/999999/redigest")]
    gets = ["/api/articles?limit=abc","/api/articles?before_ts=garbage&before_id=x",
            "/api/articles?feed_id=abc","/api/articles?since_ts=zzz","/api/articles?order=sideways",
            "/api/articles?prio=1&before_u=7","/api/qr.png","/api/qr.png?u=javascript:alert(1)",
            "/image/../../etc/passwd","/image/abc.png","/api/feeds/999999/categories",
            "/api/articles?category=&feed_category="]
    fails = []
    for m, u in routes:
        for b in bad_bodies:
            try:
                r = client.open(u, method=m, data=b, content_type="application/json")
                if r.status_code >= 500: fails.append((m, u, b, r.status_code))
            except Exception as e:
                fails.append((m, u, b, type(e).__name__ + ": " + str(e)[:60]))
    for u in gets:
        try:
            r = client.get(u)
            if r.status_code >= 500: fails.append(("GET", u, None, r.status_code))
        except Exception as e:
            fails.append(("GET", u, None, type(e).__name__ + ": " + str(e)[:60]))
    for f in fails: print("FAIL|", f[0], f[1], "|", f[3], "| body:", f[2])
    assert fails == [], fails
    assert True


def test_malformed_body_is_a_json_400(client):
    r = client.post("/api/position", data="[]", content_type="application/json")
    assert r.status_code == 400 and "JSON object" in r.get_json()["error"]
    r = client.get("/api/no-such-endpoint")
    assert r.status_code == 404 and "error" in r.get_json()
