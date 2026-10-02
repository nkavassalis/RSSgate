"""Flask web app: viewer API + admin API."""
from __future__ import annotations

import os
import threading

from flask import Flask, jsonify, render_template, request

from . import db
from .config import load_config, save_config, masked_config, DEFAULTS, _merge
from .llm import LLMClient, LLMError, TEST_PROMPT
from .refresh import refresh_all, refresh_feed


def create_app(config_path: str, conn=None, scheduler=None) -> Flask:
    app = Flask(__name__)
    cfg = load_config(config_path)
    data_dir = cfg["server"]["data_dir"]
    os.makedirs(data_dir, exist_ok=True)
    if conn is None:
        conn = db.connect(os.path.join(data_dir, "rssgate.sqlite"))
        db.init_db(conn)

    def llm() -> LLMClient:
        return LLMClient(load_config(config_path))

    # ------------------------------------------------------------- pages

    @app.route("/")
    def viewer():
        cfg = load_config(config_path)
        return render_template("viewer.html", cfg=masked_config(cfg))

    @app.route("/admin")
    def admin():
        cfg = load_config(config_path)
        return render_template("admin.html", cfg=masked_config(cfg))

    # ------------------------------------------------------------- articles

    def _cursor_from_request():
        before_ts = request.args.get("before_ts")
        before_id = int(request.args.get("before_id", 0) or 0)
        if before_ts is None:
            ts = db.get_state(conn, "resume_ts")
            aid = int(db.get_state(conn, "resume_id", "0") or 0)
            if ts:
                before_ts, before_id = ts, aid
        return before_ts, before_id

    @app.route("/api/articles")
    def api_articles():
        cfg = load_config(config_path)
        limit = min(int(request.args.get("limit", cfg["ui"]["items_per_page"])), 100)
        before_ts, before_id = _cursor_from_request()
        rows = db.articles_page(
            conn, before_ts, before_id, limit + 1,
            feed_id=request.args.get("feed_id", type=int),
            category=request.args.get("category"))
        has_more = len(rows) > limit
        items = []
        for r in rows[:limit]:
            from .db import parse_categories
            items.append({
                "id": r["id"], "title": r["title"], "link": r["link"],
                "summary": r["summary"], "status": r["status"], "ts": r["ts"],
                "feed_id": r["feed_id"], "feed_title": r["feed_title"],
                "feed_description": r["feed_description"],
                "feed_summarize": bool(r["feed_summarize"]),
                "categories": parse_categories(r["categories"]),
                "auto_categories": parse_categories(r["auto_categories"]),
                "post_categories": parse_categories(r["post_categories"]),
            })
        return jsonify({"items": items, "has_more": has_more,
                        "next": items[-1] if items else None})

    @app.route("/api/position", methods=["POST"])
    def api_position():
        data = request.get_json(force=True)
        ts, aid = data.get("ts"), int(data.get("id", 0))
        if ts and aid:
            db.set_state(conn, "resume_ts", ts)
            db.set_state(conn, "resume_id", str(aid))
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "ts and id required"}), 400

    @app.route("/api/resume")
    def api_resume():
        newest = conn.execute(
            "SELECT COALESCE(published_at, fetched_at) ts FROM articles "
            "ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT 1").fetchone()
        return jsonify({"resume_ts": db.get_state(conn, "resume_ts"),
                        "resume_id": db.get_state(conn, "resume_id", "0"),
                        "newest_ts": newest["ts"] if newest else None})

    # ------------------------------------------------------------- feeds

    def _feed_dict(f):
        d = dict(f)
        d["categories"] = db.parse_categories(d.get("categories", ""))
        d["auto_categories"] = db.parse_categories(d.get("auto_categories", ""))
        return d

    @app.route("/api/feeds", methods=["GET"])
    def api_feeds():
        out = []
        for f in db.list_feeds(conn):
            n = conn.execute("SELECT COUNT(*) c FROM articles WHERE feed_id=?",
                             (f["id"],)).fetchone()["c"]
            hid = conn.execute(
                "SELECT COUNT(*) c FROM articles WHERE feed_id=? AND status='hidden'",
                (f["id"],)).fetchone()["c"]
            out.append({k: f[k] for k in f.keys() if k not in ("etag", "last_modified")}
                       | {"article_count": n, "hidden_count": hid,
                          "categories": db.parse_categories(f["categories"]),
                          "auto_categories": db.parse_categories(f["auto_categories"])})
        return jsonify(out)

    @app.route("/api/feeds", methods=["POST"])
    def api_add_feed():
        data = request.get_json(force=True)
        url = (data.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return jsonify({"error": "url must start with http(s)://"}), 400
        cats = [c.strip() for c in (data.get("categories") or []) if c.strip()]
        try:
            fid = db.add_feed(conn, url, data.get("type", "auto"), "", cats)
        except Exception as exc:  # unique constraint etc.
            return jsonify({"error": f"feed already exists or invalid: {exc}"}), 409
        if data.get("refresh", True):
            def _bg():
                try:
                    refresh_feed(conn, db.get_feed(conn, fid), load_config(config_path), llm())
                except Exception:  # noqa: BLE001
                    pass
            threading.Thread(target=_bg, daemon=True).start()
        return jsonify(_feed_dict(db.get_feed(conn, fid))), 201

    @app.route("/api/feeds/<int:fid>", methods=["PUT"])
    def api_update_feed(fid):
        feed = db.get_feed(conn, fid)
        if not feed:
            return jsonify({"error": "not found"}), 404
        data = request.get_json(force=True)
        fields = {}
        if "categories" in data:
            fields["categories"] = ",".join(
                c.strip() for c in data["categories"] if c.strip())
        if "enabled" in data:
            fields["enabled"] = 1 if data["enabled"] else 0
        if "summarize" in data:
            want = 1 if data["summarize"] else 0
            if want and not feed["summarize"]:   # raw -> LLM: re-digest the feed
                conn.execute("UPDATE articles SET status='pending'"
                             " WHERE feed_id=? AND status='ready' AND llm_ms=0", (fid,))
            fields["summarize"] = want
        if "hide_sponsored" in data:
            fields["hide_sponsored"] = 1 if data["hide_sponsored"] else 0
            if not data["hide_sponsored"]:  # un-hide everything when flag goes off
                conn.execute("UPDATE articles SET status='pending'"
                             " WHERE feed_id=? AND status='hidden'", (fid,))
            else:  # sweep existing items too (free: titles + stored digests)
                from .refresh import is_sponsored, SPONSORED_DIGEST_RE
                for a in conn.execute("SELECT id, title, link, summary FROM articles"
                                      " WHERE feed_id=? AND status='ready'", (fid,)):
                    if (is_sponsored(a["title"], a["link"]) or
                            SPONSORED_DIGEST_RE.search((a["summary"] or "")[:400])):
                        conn.execute("UPDATE articles SET status='hidden' WHERE id=?",
                                     (a["id"],))
                conn.commit()
        if "type" in data and data["type"] in ("auto", "feed", "page"):
            fields["type"] = data["type"]
        db.update_feed(conn, fid, **fields)
        return jsonify(_feed_dict(db.get_feed(conn, fid)))

    @app.route("/api/feeds/<int:fid>", methods=["DELETE"])
    def api_delete_feed(fid):
        db.delete_feed(conn, fid)
        return jsonify({"ok": True})

    @app.route("/api/feeds/<int:fid>/refresh", methods=["POST"])
    def api_refresh_feed(fid):
        feed = db.get_feed(conn, fid)
        if not feed:
            return jsonify({"error": "not found"}), 404
        return jsonify({"result": refresh_feed(conn, feed, load_config(config_path), llm())})

    # ------------------------------------------------------------- categories

    @app.route("/api/categories")
    def api_categories():
        return jsonify(db.all_categories(conn))

    @app.route("/api/categories/rename", methods=["POST"])
    def api_rename_category():
        data = request.get_json(force=True)
        old = (data.get("from") or "").strip()
        new = (data.get("to") or "").strip()
        if not old or not new:
            return jsonify({"error": "from and to are required"}), 400
        n = db.rename_category(conn, old, new)
        return jsonify({"ok": True, "feeds_updated": n})

    @app.route("/api/categories/<name>", methods=["DELETE"])
    def api_remove_category(name):
        n = db.remove_category(conn, name.strip())
        return jsonify({"ok": True, "feeds_updated": n})

    # ------------------------------------------------------------- admin

    @app.route("/api/config")
    def api_get_config():
        return jsonify(masked_config(load_config(config_path)))

    @app.route("/api/config", methods=["PUT"])
    def api_put_config():
        patch = request.get_json(force=True)
        for section in ("server", "ui"):
            patch.pop(section, None)  # host/port/theme changes via file only
        patch.get("llm", {}).pop("api_key_set", None)
        if patch.get("llm", {}).get("api_key") == "***":
            del patch["llm"]["api_key"]
        if "system_prompt" in patch.get("summarizer", {}) and \
                "{length}" not in patch["summarizer"]["system_prompt"]:
            patch["summarizer"]["system_prompt"] += " Length target: {length}."
        current = load_config(config_path)
        merged = _merge(current, patch)
        merged.setdefault("llm", {}).pop("api_key_set", None)
        save_config(merged, config_path)
        return jsonify(masked_config(merged))

    @app.route("/api/models")
    def api_models():
        try:
            return jsonify({"models": llm().list_models(),
                            "current": load_config(config_path)["llm"].get("model", "")})
        except LLMError as exc:
            return jsonify({"models": [], "error": str(exc)}), 502

    @app.route("/api/llm/test", methods=["POST"])
    def api_llm_test():
        client = llm()
        try:
            text, usage = client.chat(TEST_PROMPT, max_tokens=128)
            db.log_usage(conn, client.provider, client.model or "auto",
                         usage["prompt_tokens"], usage["completion_tokens"],
                         purpose="test")
            return jsonify({"ok": True, "reply": text.strip()[:40],
                            "model": client.resolve_model()})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)}), 502

    @app.route("/api/usage")
    def api_usage():
        return jsonify(db.usage_totals(conn))

    @app.route("/api/llm/stats")
    def api_llm_stats():
        return jsonify(db.llm_stats(conn))

    @app.route("/api/workqueue")
    def api_workqueue():
        return jsonify(db.workqueue_snapshot(conn))

    @app.route("/api/poll", methods=["POST"])
    def api_poll():
        results = []
        def _bg():
            nonlocal results
            results = refresh_all(conn, load_config(config_path), llm())
        threading.Thread(target=_bg, daemon=True).start()
        return jsonify({"started": True})

    @app.route("/api/status")
    def api_status():
        cfg = load_config(config_path)
        n_feeds = conn.execute("SELECT COUNT(*) c FROM feeds").fetchone()["c"]
        n_pending = conn.execute(
            "SELECT COUNT(*) c FROM articles WHERE status='pending'").fetchone()["c"]
        return jsonify({"feeds": n_feeds, "pending": n_pending,
                        "polling": cfg["polling"]})

    return app
