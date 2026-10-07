"""Maintenance: retention months, orphan pruning, image cache size cap."""
import json
import time
from rssgate import db, imgstore, maint

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 5000


def _file(d, name):
    f = d / name
    f.write_bytes(PNG)
    return f


def seed_article(conn, fid, guid, published_at, image=None, images=""):
    aid = db.upsert_article(conn, fid, guid, "l", "t", published_at,
                            image_url=None)
    db.set_article(conn, aid, status="ready", summary="s",
                   image=image, images=images)
    return aid


def test_delete_old_articles_returns_files(conn):
    fid = db.add_feed(conn, "https://ex/m")["id"]
    A, B = "aa" * 12 + ".png", "bb" * 12 + ".png"
    old = seed_article(conn, fid, "o", "2020-01-01T00:00:00Z",
                       image=A, images=A + "," + B)
    seed_article(conn, fid, "n", "2026-10-01T00:00:00Z")
    n, files = db.delete_old_articles(conn, 12)
    assert n == 1
    assert files == {A, B}
    assert conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"] == 1
    # months=0 -> forever
    assert db.delete_old_articles(conn, 0) == (0, set())


def test_maint_run_all(conn, tmp_path, monkeypatch):
    img = tmp_path / "images"; img.mkdir()
    monkeypatch.setattr(imgstore, "_dir", img)
    fid = db.add_feed(conn, "https://ex/m")["id"]
    keep = "keep" + "0" * 20 + ".png"
    seed_article(conn, fid, "k", "2026-10-01T00:00:00Z", image=keep)
    _file(img, keep)                       # referenced
    orphan = _file(img, "or" + "0" * 22 + ".png")   # unreferenced
    cfg = {"maintenance": {"retention_months": 0, "images_max_mb": 0}}
    rep = maint.run_all(conn, cfg)
    assert rep["ok"] and rep["orphans_removed"] == 1
    assert orphan.exists() is False and (img / keep).exists()
    assert json.loads(db.get_state(conn, "maint_report"))["ok"]


def test_cache_size_cap_clears_refs(conn, tmp_path, monkeypatch):
    img = tmp_path / "images"; img.mkdir()
    monkeypatch.setattr(imgstore, "_dir", img)
    fid = db.add_feed(conn, "https://ex/m")["id"]
    n1, n2 = "a" * 24 + ".png", "b" * 24 + ".png"
    seed_article(conn, fid, "1", "2026-10-01T00:00:00Z", image=n1,
                 images=n1 + "," + n2)
    f1 = _file(img, n1)
    f1.stat()
    _file(img, n2)
    # make n1 clearly the older file
    os_environ_time = time.time()
    import os
    os.utime(f1, (os_environ_time - 100, os_environ_time - 100))
    rep = maint.run_all(conn, {"maintenance": {"retention_months": 0,
                                               "images_max_mb": 0.001}})
    assert rep["cache_trimmed"] >= 1
    assert not f1.exists()                       # oldest pruned first
    art = conn.execute("SELECT image, images FROM articles").fetchone()
    assert art["image"] is None                  # dangling ref cleaned
    assert n1 not in (art["images"] or "")


def test_api_maintenance(client, monkeypatch):
    monkeypatch.setattr(imgstore, "_dir", None)  # no dir -> safe no-ops
    r = client.post("/api/maintenance/run").get_json()
    assert r["ok"] is True
    g = client.get("/api/maintenance").get_json()
    assert g["report"]["ok"] is True
    assert g["config"]["retention_months"] == 0
    # retention setting round-trips through admin config save
    cfg = client.get("/api/config").get_json()
    cfg["maintenance"]["retention_months"] = 6
    client.put("/api/config", json=cfg)
    assert client.get("/api/maintenance").get_json()["config"][
        "retention_months"] == 6


def test_status_cache_mb_is_live(conn, tmp_path):
    from rssgate import imgstore
    d = tmp_path / "images"; d.mkdir()
    (d / "a" * 1).write_bytes(b"x" * 3_000_000) if False else \
        (d / "a").write_bytes(b"x" * 3_000_000)
    imgstore.init(str(d))
    assert imgstore.cache_mb() == 3.0


def test_dedupe_galleries_repairs_resized_twins(conn, tmp_path):
    from rssgate import db, imgstore, maint
    from PIL import Image
    import io
    imgstore.init(str(tmp_path / "images"))
    d = tmp_path / "images"

    def base(size, kind):
        im = Image.new("L", (32, 32))
        for y in range(32):
            for x in range(32):
                im.putpixel((x, y), 0 if kind == "checker"
                            and (x // 8 + y // 8) % 2 else
                            (x * 7 + y * 3) % 256)
        if size != (32, 32):
            im = im.resize(size, Image.BILINEAR)
        return im

    def png(name, size=(32, 32), kind="gradient"):
        buf = io.BytesIO(); base(size, kind).save(buf, "PNG")
        (d / name).write_bytes(buf.getvalue())
        return name
    import hashlib
    def hname(url):
        return hashlib.sha256(url.encode()).hexdigest()[:24] + ".png"
    fid = db.add_feed(conn, "https://ah.test/f", type_="feed")["id"]
    aid = db.upsert_article(conn, fid, "a1", "https://ah.test/1", "T", None)
    hero = png(hname("https://ah.test/hero"))
    twin = png(hname("https://ah.test/twin"), size=(64, 64))
    other = png(hname("https://ah.test/other"), kind="checker")
    db.set_article(conn, aid, image=hero,
                   images=",".join([hero, twin, other]))
    assert maint.dedupe_galleries(conn) == 1
    row = conn.execute("SELECT image, images FROM articles WHERE id=?",
                       (aid,)).fetchone()
    assert row["image"] == twin                    # twin promoted to hero
    gal = [x for x in row["images"].split(",")
           if x and x != "-" and x != row["image"]]
    assert gal == [other]                          # distinct stays in gallery
    assert maint.dedupe_galleries(conn) == 0       # idempotent: no churn
    assert (d / other).exists() and (d / twin).exists()
    assert not (d / hero).exists()                 # og draft reclaimed
