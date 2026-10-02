"""SQLite persistence layer for RSSgate."""
from __future__ import annotations

import datetime
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    title TEXT DEFAULT '',
    description TEXT DEFAULT '',
    type TEXT NOT NULL DEFAULT 'auto',      -- auto | feed | page
    categories TEXT NOT NULL DEFAULT '',    -- user assigned, comma separated
    auto_categories TEXT NOT NULL DEFAULT '',-- declared by the feed itself
    enabled INTEGER NOT NULL DEFAULT 1,
    added_at TEXT NOT NULL,
    last_fetched_at TEXT,
    last_status TEXT,
    etag TEXT,
    last_modified TEXT,
    content_hash TEXT
);
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid TEXT NOT NULL,
    link TEXT NOT NULL,
    title TEXT DEFAULT '',
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    body_hash TEXT,
    summary TEXT,
    status TEXT NOT NULL DEFAULT 'pending', -- pending | ready | error
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    summarized_at TEXT,
    UNIQUE(feed_id, guid)
);
CREATE INDEX IF NOT EXISTS idx_articles_ts ON articles(published_at, id);
CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    ts TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if ":memory:" not in path:
        conn.execute("PRAGMA journal_mode=WAL")  # concurrent web + scheduler writes
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(feeds)")}
    if "hide_sponsored" not in cols:
        conn.execute("ALTER TABLE feeds ADD COLUMN hide_sponsored INTEGER NOT NULL DEFAULT 0")
    if "summarize" not in cols:
        conn.execute("ALTER TABLE feeds ADD COLUMN summarize INTEGER NOT NULL DEFAULT 1")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(articles)")}
    if "llm_ms" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN llm_ms INTEGER NOT NULL DEFAULT 0")
    if "started_at" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN started_at TEXT")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(token_usage)")}
    if "duration_ms" not in cols:
        conn.execute("ALTER TABLE token_usage ADD COLUMN duration_ms INTEGER NOT NULL DEFAULT 0")
    if "purpose" not in cols:
        conn.execute("ALTER TABLE token_usage ADD COLUMN purpose TEXT DEFAULT 'summarize'")
    conn.commit()


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- feeds

def parse_categories(raw: str) -> list[str]:
    return [c.strip() for c in (raw or "").split(",") if c.strip()]


def add_feed(conn, url: str, type_: str = "auto", title: str = "",
             categories: list[str] | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO feeds(url, type, title, categories, added_at) VALUES(?,?,?,?,?)",
        (url, type_, title, ",".join(categories or []), now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def list_feeds(conn) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM feeds ORDER BY id"))


def get_feed(conn, feed_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()


def update_feed(conn, feed_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE feeds SET {cols} WHERE id=?", (*fields.values(), feed_id))
    conn.commit()


def delete_feed(conn, feed_id: int) -> None:
    conn.execute("DELETE FROM articles WHERE feed_id=?", (feed_id,))
    conn.execute("DELETE FROM feeds WHERE id=?", (feed_id,))
    conn.commit()


def all_categories(conn) -> list[dict]:
    """Distinct user-assigned categories with usage counts."""
    counts: dict[str, int] = {}
    for row in conn.execute("SELECT categories FROM feeds"):
        for cat in parse_categories(row["categories"]):
            counts[cat] = counts.get(cat, 0) + 1
    return sorted(({"name": k, "count": v} for k, v in counts.items()),
                  key=lambda d: d["name"])


def rename_category(conn, old: str, new: str) -> int:
    """Rename (or create-and-reassign) a category across all feeds."""
    touched = 0
    for row in conn.execute("SELECT id, categories FROM feeds"):
        cats = parse_categories(row["categories"])
        if old in cats:
            cats = [new if c == old else c for c in cats]
            # dedupe, preserve order
            seen: list[str] = []
            for c in cats:
                if c not in seen:
                    seen.append(c)
            conn.execute("UPDATE feeds SET categories=? WHERE id=?",
                         (",".join(seen), row["id"]))
            touched += 1
    conn.commit()
    return touched


def remove_category(conn, name: str) -> int:
    touched = 0
    for row in conn.execute("SELECT id, categories FROM feeds"):
        cats = parse_categories(row["categories"])
        if name in cats:
            cats = [c for c in cats if c != name]
            conn.execute("UPDATE feeds SET categories=? WHERE id=?",
                         (",".join(cats), row["id"]))
            touched += 1
    conn.commit()
    return touched


# ---------------------------------------------------------------- articles

def upsert_article(conn, feed_id: int, guid: str, link: str, title: str,
                   published_at: str | None) -> int | None:
    """Insert a new article. Returns its id, or None if already known."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO articles(feed_id, guid, link, title, published_at,"
        " fetched_at, status) VALUES(?,?,?,?,?,?, 'pending')",
        (feed_id, guid, link, title, published_at, now_iso()),
    )
    conn.commit()
    return cur.lastrowid if cur.rowcount else None


_TS_EXPR = "COALESCE(published_at, fetched_at)"


def articles_page(conn, before_ts: str | None = None, before_id: int | None = None,
                  limit: int = 20, feed_id: int | None = None,
                  category: str | None = None) -> list[sqlite3.Row]:
    """Reverse-chronological page of articles older than (before_ts, before_id)."""
    where, params = ["a.status != 'hidden'"], []
    if before_ts is not None:
        where.append(f"({_TS_EXPR} < ? OR ({_TS_EXPR} = ? AND a.id < ?))")
        params += [before_ts, before_ts, before_id or 0]
    if feed_id:
        where.append("a.feed_id = ?")
        params.append(feed_id)
    if category:
        where.append("(f.categories LIKE ? OR f.auto_categories LIKE ?)")
        params += [f"%{category}%", f"%{category}%"]
    rows = conn.execute(
        f"""SELECT a.id, a.title, a.link, a.summary, a.status,
                   {_TS_EXPR.replace('published_at', 'a.published_at').replace('fetched_at', 'a.fetched_at')} AS ts,
                   a.published_at, a.fetched_at, a.tokens_in, a.tokens_out,
                   f.id AS feed_id, f.title AS feed_title, f.description AS feed_description,
                   f.categories AS categories, f.auto_categories AS auto_categories,
                   f.summarize AS feed_summarize
            FROM articles a JOIN feeds f ON f.id = a.feed_id
            WHERE {' AND '.join(where)}
            ORDER BY ts DESC, a.id DESC LIMIT ?""",
        (*params, limit),
    ).fetchall()
    return rows


def set_article(conn, article_id: int, **fields) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE articles SET {cols} WHERE id=?", (*fields.values(), article_id))
    conn.commit()


def pending_articles(conn, limit: int = 5) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM articles WHERE status='pending' ORDER BY id LIMIT ?", (limit,)))


def claim_pending(conn, limit: int = 1) -> list[sqlite3.Row]:
    """Atomically move up to `limit` pending articles to 'processing'.
    Safe with concurrent workers: a row can only be claimed once."""
    cur = conn.execute(
        "UPDATE articles SET status='processing', started_at=? WHERE id IN"
        " (SELECT id FROM articles WHERE status='pending' ORDER BY id LIMIT ?)"
        " RETURNING *",
        (now_iso(), limit))
    rows = cur.fetchall()
    conn.commit()
    return rows


def mark_processing(conn, article_id: int) -> None:
    conn.execute("UPDATE articles SET status='processing', started_at=? WHERE id=?",
                 (now_iso(), article_id))
    conn.commit()


def requeue_stale_processing(conn, older_than_minutes: int = 15) -> int:
    """Crashed mid-run items go back to the queue."""
    cur = conn.execute(
        "UPDATE articles SET status='pending' WHERE status='processing'"
        " AND started_at < datetime('now', ?)", (f'-{older_than_minutes} minutes',))
    conn.commit()
    return cur.rowcount


def workqueue_snapshot(conn, current_window_min: int = 2, recent_limit: int = 12) -> dict:
    """What the LLM is doing now, what it did recently, and what's next."""
    def rows(sql, params=()):
        return [dict(r) for r in conn.execute(sql, params)]
    current = rows(
        "SELECT a.id, a.title, a.started_at, a.link, f.title AS feed_title"
        " FROM articles a JOIN feeds f ON f.id=a.feed_id"
        " WHERE a.status='processing'")
    recent = rows(
        "SELECT a.id, a.title, a.status, a.summarized_at, a.llm_ms,"
        " a.tokens_in, a.tokens_out, f.title AS feed_title"
        " FROM articles a JOIN feeds f ON f.id=a.feed_id"
        " WHERE a.status IN ('ready','error')"
        " ORDER BY COALESCE(a.summarized_at, a.fetched_at) DESC LIMIT ?", (recent_limit,))
    ahead = conn.execute(
        "SELECT COUNT(*) c FROM articles WHERE status='pending'").fetchone()["c"]
    working = conn.execute(
        "SELECT COUNT(*) c FROM articles WHERE status='processing'").fetchone()["c"]
    return {"current": current, "recent": recent,
            "working": working, "queue_ahead": ahead}


def find_summary_by_hash(conn, body_hash: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT summary FROM articles WHERE body_hash=? AND summary IS NOT NULL "
        "AND status='ready' LIMIT 1", (body_hash,)).fetchone()


# ---------------------------------------------------------------- state / usage

def set_state(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO state(key,value) VALUES(?,?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def get_state(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def log_usage(conn, provider: str, model: str, prompt: int, completion: int,
              duration_ms: int = 0, purpose: str = "summarize") -> None:
    now = now_iso()
    conn.execute(
        "INSERT INTO token_usage(day, ts, provider, model, prompt_tokens,"
        " completion_tokens, total_tokens, duration_ms, purpose)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (now[:10], now, provider, model, prompt, completion, prompt + completion,
         duration_ms, purpose))
    conn.commit()


def bump_state_max(conn, key: str, value: int) -> int:
    """High-water mark state: keep the largest value ever seen."""
    cur = int(get_state(conn, key, "0") or 0)
    if value > cur:
        set_state(conn, key, str(value))
        return value
    return cur


def incr_state(conn, key: str, delta: int = 1) -> int:
    cur = int(get_state(conn, key, "0") or 0) + delta
    set_state(conn, key, str(cur))
    return cur


def llm_stats(conn) -> dict:
    """Performance snapshot for the admin panel."""
    def one(sql, params=()):
        return conn.execute(sql, params).fetchone()
    q = one("SELECT COUNT(*) c FROM articles WHERE status='pending'")["c"]
    errors = one("SELECT COUNT(*) c FROM articles WHERE status='error'")["c"]
    peak = bump_state_max(conn, "queue_peak", q)
    cache_hits = int(get_state(conn, "cache_hits", "0") or 0)
    s = one("SELECT COUNT(*) n, COALESCE(AVG(duration_ms),0) avg_ms,"
            " COALESCE(MIN(duration_ms),0) min_ms, COALESCE(MAX(duration_ms),0) max_ms"
            " FROM (SELECT duration_ms FROM token_usage"
            "       WHERE purpose='summarize' AND duration_ms > 0"
            "       ORDER BY id DESC LIMIT 50)")
    t = one("SELECT COUNT(*) calls, COALESCE(SUM(total_tokens),0) tokens"
            " FROM token_usage WHERE day = date('now')")
    avg_s = s["avg_ms"] / 1000.0
    last = one("SELECT ts FROM token_usage ORDER BY id DESC LIMIT 1")["ts"]
    return {
        "queue": q, "queue_peak": peak, "errors": errors,
        "cache_hits": cache_hits,
        "calls_today": t["calls"], "tokens_today": t["tokens"],
        "avg_seconds": round(avg_s, 1), "min_seconds": round(s["min_ms"] / 1000.0, 1),
        "max_seconds": round(s["max_ms"] / 1000.0, 1),
        "est_drain_minutes": round(q * avg_s / 60.0, 1) if q else 0,
        "last_call_ts": last,
    }


def usage_totals(conn) -> dict:
    def total(where: str, params: tuple = ()) -> int:
        row = conn.execute(
            f"SELECT COALESCE(SUM(total_tokens),0) t FROM token_usage WHERE {where}",
            params).fetchone()
        return row["t"]
    today = total("day = date('now')")
    month = total("substr(day,1,7) = strftime('%Y-%m','now')")
    alltime = total("1=1")
    return {"today": today, "month": month, "all_time": alltime}
