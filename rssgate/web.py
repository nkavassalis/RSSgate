"""Flask web app: viewer API + admin API."""
from __future__ import annotations

import os
import threading

from flask import (
    Flask, Response, abort, jsonify, make_response, render_template,
    request, send_file)

from . import db
from .config import load_config, save_config, masked_config, _merge
from .llm import LLMClient, LLMError, TEST_PROMPT
import time as _time_mod
from .refresh import refresh_all, refresh_feed  # noqa: F401 (refresh_all: conftest stub seam)

START_TIME = _time_mod.time()


def create_app(config_path: str, conn=None, scheduler=None) -> Flask:
    app = Flask(__name__)

    from . import __version__ as _ver

    @app.context_processor
    def _inject_version():
        return {"app_version": _ver}

    # ---- input hygiene: malformed requests get a JSON 400, never a 500 ----
    def _body() -> dict:
        """The JSON request body, which must be an object."""
        data = request.get_json(force=True, silent=True)
        if data is None and not request.data:
            return {}
        if not isinstance(data, dict):
            abort(400, description="request body must be a JSON object")
        return data

    def _int(v, default=0, lo=None, hi=None):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return default
        if lo is not None:
            n = max(lo, n)
        if hi is not None:
            n = min(hi, n)
        return n

    def _strs(v) -> list[str]:
        """A list of strings (anything else -> [])."""
        if not isinstance(v, list):
            return []
        return [s for s in v if isinstance(s, str)]

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(405)
    def _http_err(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": getattr(e, "description", str(e))}), e.code
        return e

    @app.errorhandler(Exception)
    def _crash(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return _http_err(e)
        import logging
        logging.getLogger("rssgate.web").exception("unhandled error on %s",
                                                   request.path)
        if request.path.startswith("/api/"):
            return jsonify({"error": f"internal error: {type(e).__name__}"}), 500
        raise e

    cfg = load_config(config_path)
    data_dir = cfg["server"]["data_dir"]
    os.makedirs(data_dir, exist_ok=True)
    from . import imgstore
    imgstore.init(os.path.abspath(os.path.join(data_dir, "images")))
    if conn is None:
        conn = db.connect(db.path_of(conn))
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
        """Keyset upper bound. The stored resume cursor ONLY bounds the plain
        New/all-feeds first page — filters and Since mode start at newest."""
        before_ts = request.args.get("before_ts")
        before_id = _int(request.args.get("before_id"), 0, lo=0)
        unfiltered = (not request.args.get("feed_id")
                      and not request.args.get("category")
                      and not request.args.get("since_ts"))
        if before_ts is None and unfiltered and not request.args.get("fresh"):
            ts = db.get_state(conn, "resume_ts")
            aid = int(db.get_state(conn, "resume_id", "0") or 0)
            if ts:
                before_ts, before_id = ts, aid
        return before_ts, before_id

    def _hide_statuses(cfg) -> tuple:
        """Statuses the stream withholds; the unread pill must use the SAME
        value or queued posts show a pill with no card to clear it."""
        return (("pending", "processing")
                if cfg["ui"].get("hide_untranscribed", True) else ())

    @app.route("/api/articles")
    def api_articles():
        cfg = load_config(config_path)
        limit = _int(request.args.get("limit"), cfg["ui"]["items_per_page"],
                     lo=1, hi=100)
        order = request.args.get("order") or cfg["ui"].get("order", "newest")
        if order not in ("newest", "oldest"):
            order = "newest"
        before_ts, before_id = _cursor_from_request()
        bu = request.args.get("before_u")
        hide_st = _hide_statuses(cfg)
        rows = db.articles_page(
            conn, before_ts, before_id, limit + 1,
            feed_id=request.args.get("feed_id", type=int),
            category=request.args.getlist("category") or None,
            feed_category=request.args.getlist("feed_category") or None,
            since_ts=request.args.get("since_ts"), order=order,
            unread_first=request.args.get("prio") == "1",
            before_u=int(bu) if bu in ("0", "1") else None,
            hide_statuses=hide_st)
        has_more = len(rows) > limit
        items = []
        for r in rows[:limit]:
            from .db import parse_categories
            item = {
                "id": r["id"], "title": r["title"], "link": r["link"],
                "summary": r["summary"], "status": r["status"], "ts": r["ts"],
                "feed_id": r["feed_id"], "feed_title": r["feed_title"],
                "feed_description": r["feed_description"],
                "feed_summarize": bool(r["feed_summarize"]),
                "categories": parse_categories(r["categories"]),
                "auto_categories": parse_categories(r["auto_categories"]),
                "post_categories": parse_categories(r["post_categories"]),
                "image": r["image"],
                "gallery": [g for g in (r["gallery"] or "").split(",")
                         if g and g != "-"],
                "unread": bool(r["unread"]),
                "digest_source": r["digest_source"] or "",
            }
            mode = r["images_mode"]
            if mode == "off":
                item["image"] = None
                item["gallery"] = []
            elif mode == "hero":
                item["gallery"] = []
            items.append(item)
        return jsonify({"items": items, "has_more": has_more,
                        "next": items[-1] if items else None})

    @app.route("/api/position", methods=["POST"])
    def api_position():
        data = _body()
        ts, aid = data.get("ts"), _int(data.get("id"), 0)
        if not isinstance(ts, str):
            ts = None
        rid = data.get("read_ids")
        marked = db.mark_articles_read(conn, rid) if isinstance(rid, list) else 0
        if not (ts and aid):
            if isinstance(rid, list):              # read marks only
                return jsonify({"ok": True, "marked": marked})
            return jsonify({"ok": False, "error": "ts and id required"}), 400
        # precise per-feed cursors: {"reads": {"<feed_id>": "<ts>", ...}}
        reads = data.get("reads")
        if isinstance(reads, dict) and reads:
            for fid, rts in reads.items():
                try:
                    db.mark_feed_read(conn, int(fid), rts)
                except (TypeError, ValueError):
                    continue
        else:  # back-compat: {feeds: [ids], ts} or legacy global-only payload
            for fid in data.get("feeds") or []:
                try:
                    db.mark_feed_read(conn, int(fid), ts)
                except (TypeError, ValueError):
                    continue
        if data.get("global", not data.get("feeds") and not reads):  # legacy {ts,id} = global
            # The bookmark may name a since-deleted post (redigest and
            # sync_deletes remove rows under an open reader tab). Validate it
            # exists before storing: a stale pointer must not become the
            # reader's resume point. Other feeds' cursors and per-article
            # reads above are still valid, so the beacon stays a 200.
            if db.article_exists(conn, aid):
                db.set_state(conn, "resume_ts", ts)
                db.set_state(conn, "resume_id", str(aid))
        return jsonify({"ok": True})

    @app.route("/api/pulse")
    def api_pulse():
        """Read-only 'anything new?' for the reader's quiet pill, plus how
        often the reader may ask. Never fetches feeds."""
        cfg = load_config(config_path)
        out = db.pulse(conn, since_ts=db.get_state(conn, "resume_ts", ""),
                       hide_statuses=_hide_statuses(cfg))
        out["every_minutes"] = cfg["ui"].get("pulse_minutes", 1)
        return jsonify(out)

    @app.route("/api/resume")
    def api_resume():
        newest = db.newest_ts(conn)
        ui = load_config(config_path)["ui"]
        return jsonify({"resume_ts": db.get_state(conn, "resume_ts"),
                        "resume_id": db.get_state(conn, "resume_id", "0"),
                        "newest_ts": newest,
                        "order": ui.get("order", "newest"),
                        "snapshot_width": ui.get("snapshot_width", 800),
                        "stream_width": ui.get("stream_width", 1280),
                        "read_delay": ui.get("read_delay", 5)})

    # ------------------------------------------------------------- feeds

    def _feed_dict(f):
        d = dict(f)
        d["categories"] = db.parse_categories(d.get("categories", ""))
        d["auto_categories"] = db.parse_categories(d.get("auto_categories", ""))
        return d

    @app.route("/api/feeds", methods=["GET"])
    def api_feeds():
        out = []
        hide_st = _hide_statuses(load_config(config_path))
        for f in db.list_feeds(conn):
            out.append({k: f[k] for k in f.keys() if k not in ("etag", "last_modified")}
                       | db.feed_counts(conn, f["id"])
                       | {"display_title": (f["custom_title"] or f["title"] or f["url"]),
                          "unread": db.feed_unread(conn, f["id"], f["last_read_ts"],
                                                   hide_st),
                          "categories": db.parse_categories(f["categories"]),
                          "auto_categories": db.parse_categories(f["auto_categories"])})
        return jsonify(out)

    @app.route("/api/feeds", methods=["POST"])
    def api_add_feed():
        data = _body()
        url = str(data.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return jsonify({"error": "url must start with http(s)://"}), 400
        cats = [c.strip().lower() for c in _strs(data.get("categories")) if c.strip()]
        try:
            feed_row = db.add_feed(conn, url, data.get("type", "auto"), "", cats)
        except Exception as exc:  # unique constraint etc.
            return jsonify({"error": f"feed already exists or invalid: {exc}"}), 409
        fid = feed_row["id"]
        if data.get("refresh", True):
            dbfile = db.path_of(conn)

            def _bg():
                import contextlib
                try:      # own connection: threads never share the request's
                    with contextlib.closing(db.connect(dbfile)) as bconn:
                        refresh_feed(bconn, db.get_feed(bconn, fid),
                                     load_config(config_path), llm())
                except Exception:  # noqa: BLE001
                    pass
            threading.Thread(target=_bg, daemon=True).start()
        return jsonify(_feed_dict(feed_row)), 201

    @app.route("/api/feeds/probe", methods=["POST"])
    def api_probe_feed():
        from .fetcher import find_feeds
        url = str(_body().get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return jsonify({"error": "url must start with http(s)://"}), 400
        return jsonify(find_feeds(url))

    @app.route("/api/feeds/<int:fid>", methods=["PUT"])
    def api_update_feed(fid):
        feed = db.get_feed(conn, fid)
        if not feed:
            return jsonify({"error": "not found"}), 404
        data = _body()
        fields = {}
        if "categories" in data:
            fields["categories"] = ",".join(
                c.strip().lower() for c in _strs(data["categories"]) if c.strip())
        if "enabled" in data:
            fields["enabled"] = 1 if data["enabled"] else 0
        if "summarize" in data:
            # no auto re-digest: the admin UI asks first, then calls
            # POST /api/feeds/<id>/redigest if you confirm
            fields["summarize"] = 1 if data["summarize"] else 0
        if "sync_deletes" in data:
            fields["sync_deletes"] = 1 if data["sync_deletes"] else 0
        if data.get("images_mode") in ("auto", "hero", "off"):
            fields["images_mode"] = data["images_mode"]
        if "max_input_chars" in data:
            try:
                fields["max_input_chars"] = _int(data["max_input_chars"], 0, 0, 200000)
            except (TypeError, ValueError):
                fields["max_input_chars"] = 0
        if "custom_title" in data:
            ct = data["custom_title"]
            fields["custom_title"] = (str(ct)[:200].strip()
                                      if isinstance(ct, str) else "")
        if "system_prompt" in data:
            sp = data["system_prompt"]
            fields["system_prompt"] = (str(sp)[:4000].strip()
                                       if isinstance(sp, str) else "")
        if "digest_length" in data:
            val = data["digest_length"]
            fields["digest_length"] = (val if val in
                                       ("default", "terse", "normal",
                                        "detailed") else "default")
        if "category_block" in data:
            names = [c.strip().lower() for c in _strs(data["category_block"])
                     if c.strip()]
            fields["category_block"] = ",".join(names)
        if data.get("content_source") in ("auto", "feed", "page"):
            fields["content_source"] = data["content_source"]
        if data.get("unpause"):
            db.feed_unblock(conn, fid)        # user overrides a site-block pause
        if "hide_sponsored" in data:
            fields["hide_sponsored"] = 1 if data["hide_sponsored"] else 0
            if not data["hide_sponsored"]:  # un-hide everything when flag goes off
                db.unhide_feed(conn, fid)
            else:  # sweep existing items too (free: titles + stored digests)
                from .refresh import is_sponsored, SPONSORED_DIGEST_RE
                db.set_status(conn, [
                    a["id"] for a in db.feed_ready_articles(conn, fid)
                    if is_sponsored(a["title"], a["link"])
                    or SPONSORED_DIGEST_RE.search((a["summary"] or "")[:400])],
                    "hidden")
        if "type" in data and data["type"] in ("auto", "feed", "page"):
            fields["type"] = data["type"]
        db.update_feed(conn, fid, **fields)
        if "category_block" in fields:
            db.hide_blocked_categories(conn, fid)   # pre-LLM: zero tokens
            db.requeue_unblocked(conn, fid)
        return jsonify(_feed_dict(db.get_feed(conn, fid)))

    @app.route("/api/feeds/<int:fid>", methods=["DELETE"])
    def api_delete_feed(fid):
        files = db.feed_files(conn, fid)
        db.delete_feed(conn, fid)
        db.release_files(conn, files)          # proactive orphan reclaim
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
        if request.args.get("viewer"):
            return jsonify(db.category_list(conn))
        return jsonify(db.all_categories(conn))

    @app.route("/api/articles/<int:aid>/retry", methods=["POST"])
    def api_article_retry(aid):
        if not db.retry_article(conn, aid):
            return jsonify({"error": "not found"}), 404
        return jsonify({"ok": True})

    @app.route("/api/articles/<int:aid>/drop", methods=["POST"])
    def api_article_drop(aid):
        if not db.drop_article(conn, aid):
            return jsonify({"error": "not found"}), 404
        return jsonify({"ok": True})

    @app.route("/api/poll", methods=["POST"])
    def api_poll_now():
        """Pull-to-refresh: poll every enabled feed NOW (background thread),
        throttled so frantic pulling can't hammer sources."""
        import contextlib, threading, time as _t
        last = db.get_state(conn, "last_manual_poll", "")
        if last and (_t.time() - _t.mktime(_t.strptime(
                last, "%Y-%m-%dT%H:%M:%SZ"))) < 60:
            return jsonify({"ok": True, "started": False, "throttled": True,
                            "why": "throttled"})
        db.set_state(conn, "last_manual_poll", db.now_iso())
        cfg = load_config(config_path)

        def _go():
            import rssgate.refresh as R
            with contextlib.closing(db.connect(
                    db.path_of(conn))) as pconn:
                for feed in db.list_feeds(pconn):
                    if feed["enabled"]:
                        try:
                            R.refresh_feed(pconn, feed, cfg, llm())
                        except Exception:  # noqa: BLE001
                            pass
        threading.Thread(target=_go, daemon=True).start()
        return jsonify({"ok": True, "started": True})

    @app.route("/api/qr.png")
    def api_qr():
        """Local QR encoder for share snapshots: encodes a URL, fetches
        nothing, leaks nothing. PNG, immutable-cacheable by content."""
        import io
        u = (request.args.get("u") or "").strip()
        if not u or not u.startswith(("http://", "https://")) or len(u) > 500:
            return jsonify({"error": "url required (http(s), <=500 chars)"}), 400
        import qrcode
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                           box_size=10, border=1)
        qr.add_data(u)
        qr.make(fit=True)
        buf = io.BytesIO()
        qr.make_image(fill_color="#111111", back_color="#ffffff").save(buf, "PNG")
        return Response(buf.getvalue(), mimetype="image/png",
                        headers={"Cache-Control": "public, max-age=31536000"})

    @app.route("/api/feed-errors")
    def api_feed_errors():
        rows = db.recent_errors(conn, _int(request.args.get("limit"), 20, 1, 100))
        return jsonify([dict(r) for r in rows])

    @app.route("/api/feeds/<int:fid>/redigest", methods=["POST"])
    def api_redigest_feed(fid):
        if not db.get_feed(conn, fid):
            return jsonify({"error": "not found"}), 404
        n = db.requeue_ready(conn, fid)
        return jsonify({"ok": True, "requeued": n})

    @app.route("/api/feeds/<int:fid>/categories")
    def api_feed_categories(fid):
        return jsonify(db.feed_category_state(conn, fid))

    @app.route("/api/categories/rename", methods=["POST"])
    def api_rename_category():
        data = _body()
        old = (data.get("from") or "").strip()
        new = (data.get("to") or "").strip().lower()
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
        patch = _body()
        patch.pop("server", None)   # host/port changes via file only
        ui = patch.pop("ui", None)
        ui_patch = {}
        if isinstance(ui, dict):
            if ui.get("order") in ("newest", "oldest"):
                ui_patch["order"] = ui["order"]
            sw = ui.get("snapshot_width")
            if isinstance(sw, (int, float)) and 360 <= sw <= 1440:
                ui_patch["snapshot_width"] = int(sw)
            tw = ui.get("stream_width")
            if isinstance(tw, (int, float)) and 480 <= tw <= 1600:
                ui_patch["stream_width"] = int(tw)
            if isinstance(ui.get("hide_untranscribed"), bool):
                ui_patch["hide_untranscribed"] = ui["hide_untranscribed"]
            rd = ui.get("read_delay")
            if isinstance(rd, (int, float)) and 0 <= rd <= 60:
                ui_patch["read_delay"] = int(rd)
            if ui.get("theme") in ("auto", "light", "dark"):
                ui_patch["theme"] = ui["theme"]
            pm = ui.get("pulse_minutes")
            if isinstance(pm, (int, float)) and 0 <= pm <= 120:
                ui_patch["pulse_minutes"] = int(pm)
        if ui_patch:
            patch["ui"] = ui_patch        # rest of ui: file only
        fetch = patch.pop("fetch", None)
        if isinstance(fetch, dict):
            fp = {}
            ua = fetch.get("user_agent")
            if isinstance(ua, str) and len(ua) <= 400 and ua.isprintable():
                fp["user_agent"] = ua.strip()
            iv = fetch.get("per_host_interval")
            if isinstance(iv, (int, float)) and 0 <= iv <= 60:
                fp["per_host_interval"] = iv
            bo = fetch.get("block_backoff_minutes")
            if isinstance(bo, (int, float)) and 1 <= bo <= 1440:
                fp["block_backoff_minutes"] = bo
            if fp:
                patch["fetch"] = fp
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
        from . import net
        net.configure(merged)             # UA/pacing apply without restart
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
        try:
            return jsonify(db.llm_stats(conn))
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc), "avg_duration_s": None,
                            "windows": []}, 200)

    @app.route("/api/workqueue")
    def api_workqueue():
        from .refresh import llm_down_state
        snap = db.workqueue_snapshot(conn)
        down = llm_down_state(conn)
        snap["llm_down_since"] = down["since"] or None
        snap["llm_next_try"] = down["next_try"] or None
        return jsonify(snap)

    @app.route("/image/<name>")
    def image_route(name):
        """Serve a locally cached article image (hash-named files only)."""
        p = imgstore.safe_path(name)
        if p is None:
            # trimmed by the cache cap? heroes whose declared URL we kept
            # are content-addressed: refetch verifies against the name.
            url = db.article_needing_image(conn, name)
            if url and name.split(".")[0] == \
                    __import__("hashlib").sha256(url.encode()).hexdigest()[:24] \
                    and imgstore.store(url, timeout=10):
                p = imgstore.safe_path(name)
            if p is None:
                abort(404)
        resp = make_response(send_file(p))
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

    @app.route("/api/images/backfill", methods=["POST"])
    def api_image_backfill():
        import contextlib
        import threading
        data = _body()
        force = bool(data.get("force"))
        limit = _int(data.get("limit"), 150, 1, 5000)
        page_fetches = _int(data.get("page_fetches"), 40, 0, 1000)

        def _go():
            with contextlib.closing(db.connect(
                    db.path_of(conn))) as bconn:
                from .refresh import backfill_images
                backfill_images(bconn, load_config(config_path),
                                limit=limit, page_fetches=page_fetches,
                                force=force)
        threading.Thread(target=_go, daemon=True).start()
        return jsonify({"ok": True, "started": True})

    @app.route("/api/maintenance")
    def api_maintenance():
        from . import maint
        return jsonify({"report": maint.last_report(conn),
                        "config": load_config(config_path)["maintenance"]})

    @app.route("/api/maintenance/run", methods=["POST"])
    def api_maintenance_run():
        from . import maint
        return jsonify(maint.run_all(conn, load_config(config_path)))

    @app.route("/api/articles/retry-failed", methods=["POST"])
    def api_retry_failed():
        return jsonify({"requeued": db.retry_failed(conn)})

    @app.route("/api/articles/clear-failed", methods=["POST"])
    def api_clear_failed():
        n, files = db.delete_articles_status(conn, "error")
        released = db.release_files(conn, files)
        return jsonify({"deleted": n, "images_released": released})

    @app.route("/api/status")
    def api_status():
        import os, time as _t
        from . import __version__
        cfg = load_config(config_path)
        sc = db.status_counts(conn)
        n_feeds, n_on, q = sc["feeds"], sc["feeds_enabled"], sc["by_status"]
        db_mb = 0.0
        try:
            db_mb = round(sum(
                os.path.getsize(os.path.join(data_dir, f)) / 1e6
                for f in os.listdir(data_dir)
                if f.startswith("rssgate.sqlite")), 1)
        except OSError:
            pass
        from . import imgstore
        from .refresh import llm_down_state
        cache_mb = imgstore.cache_mb()
        down = llm_down_state(conn)
        return jsonify({"version": __version__, "feeds": n_feeds,
                        "feeds_enabled": n_on, "pending": q.get("pending", 0),
                        "processing": q.get("processing", 0),
                        "errors": q.get("error", 0),
                        "uptime_min": round((_t.time() - START_TIME) / 60),
                        "db_mb": db_mb, "cache_mb": cache_mb,
                        "llm_down_since": down["since"] or None,
                        "llm_down_reason": down["reason"] or None,
                        "llm_next_try": down["next_try"] or None,
                        "polling": cfg["polling"]})

    return app
