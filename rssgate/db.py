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
    if "last_read_ts" not in cols:
        conn.execute("ALTER TABLE feeds ADD COLUMN last_read_ts TEXT")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(articles)")}
    if "categories" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN categories TEXT NOT NULL DEFAULT ''")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(articles)")}
    if "llm_ms" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN llm_ms INTEGER NOT NULL DEFAULT 0")
    if "started_at" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN started_at TEXT")
    if "image_url" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN image_url TEXT")
    if "image" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN image TEXT")


def norm_ts(value):
    """Normalize any feed/HTTP timestamp (ISO with Z/+00:00/offsets, RFC 822)
    to canonical 'YYYY-MM-DDTHH:MM:SSZ' UTC — matching now_iso(). Every
    timestamp comparison in this app is a string comparison, so everything
    stored must share this one format. Unparseable values pass through."""
    if not value:
        return None
    s = str(value).strip()
    dt = None
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(s)
        except Exception:  # noqa: BLE001
            return s
    if dt is None:
        return s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(token_usage)")}
    if "duration_ms" not in cols:
        conn.execute("ALTER TABLE token_usage ADD COLUMN duration_ms INTEGER NOT NULL DEFAULT 0")
    if "purpose" not in cols:
        conn.execute("ALTER TABLE token_usage ADD COLUMN purpose TEXT DEFAULT 'summarize'")
    _normalize_timestamps(conn)
    conn.commit()


def _normalize_timestamps(conn: sqlite3.Connection) -> None:
    """One-time (idempotent) migration: rewrite every stored timestamp to
    canonical UTC. Fixes cross-format cursor comparisons."""
    for r in conn.execute("SELECT id, published_at FROM articles"
                          " WHERE published_at IS NOT NULL").fetchall():
        n = norm_ts(r["published_at"])
        if n and n != r["published_at"]:
            conn.execute("UPDATE articles SET published_at=? WHERE id=?", (n, r["id"]))
    for r in conn.execute("SELECT id, last_read_ts FROM feeds"
                          " WHERE last_read_ts IS NOT NULL").fetchall():
        n = norm_ts(r["last_read_ts"])
        if n and n != r["last_read_ts"]:
            conn.execute("UPDATE feeds SET last_read_ts=? WHERE id=?", (n, r["id"]))
    for key in ("resume_ts", "oldest_seen"):
        v = get_state(conn, key)
        if v:
            n = norm_ts(v)
            if n and n != v:
                set_state(conn, key, n)


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- feeds

def parse_categories(raw: str) -> list[str]:
    return [c.strip() for c in (raw or "").split(",") if c.strip()]


def add_feed(conn, url: str, type_: str = "auto", title: str = "",
             categories: list[str] | None = None) -> sqlite3.Row:
    """Insert and return the new feed row."""
    cur = conn.execute(
        "INSERT INTO feeds(url, type, title, categories, added_at) VALUES(?,?,?,?,?)",
        (url, type_, title, ",".join(categories or []), now_iso()))
    conn.commit()
    row = conn.execute("SELECT * FROM feeds WHERE id=?", (cur.lastrowid,)).fetchone()
    if row is None:  # defensive: shared-connection interleaving
        row = conn.execute("SELECT * FROM feeds WHERE url=?", (url,)).fetchone()
    if row is None:
        raise RuntimeError("feed insert failed")
    return row


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


def _cat_sql(col: str) -> str:
    """Exact-membership test for a comma-string category column."""
    return f"(',' || COALESCE({col},'') || ',') LIKE ('%,' || ? || ',%')"


def _cat_effective_sql() -> str:
    """Canonical category filter (2 bound params, same name each):
      1. the post's OWN tags,
      2. user-assigned feed labels (you labeled the whole feed — it matches).
    Feed-declared tags (feeds.auto_categories) are display-fallback only:
    a news feed's union tag soup (~40 tags) must never drag its untagged
    posts into every category view. Untagged posts live under 'All'.
    """
    return "(" + _cat_sql("a.categories") + " OR " + _cat_sql("f.categories") + ")"


def _variant_map(rows) -> dict[str, str]:
    """casefold-key -> display variant: the spelling that appears most often
    across the given comma-strings (ties: case-sensitive alphabetical)."""
    from collections import Counter
    freq: Counter = Counter()
    for raw in rows:
        freq.update(parse_categories(raw))
    best: dict[str, tuple] = {}
    for name, n in freq.items():
        k = name.casefold()
        rank = (n, name[0].isupper(), name)   # frequent, then Title-case wins
        if k not in best or rank > best[k]:
            best[k] = rank
    return {k: v[2] for k, v in best.items()}


def category_list(conn) -> list[dict]:
    """Category names the filter can actually match (per-post tags and
    user-assigned feed labels) with visible-article counts. Case-insensitive:
    'Tech News' and 'tech news' share ONE entry (modal spelling) and one
    count. Exact comma membership, not substrings; zero-count: not listed."""
    variants = _variant_map(
        r["c"] for r in conn.execute(
            "SELECT categories c FROM feeds WHERE categories != ''"
            " UNION ALL SELECT categories FROM articles WHERE categories != ''"))
    out = []
    for k in sorted(variants):
        cnt = conn.execute(
            "SELECT COUNT(*) c FROM articles a JOIN feeds f ON f.id=a.feed_id"
            " WHERE a.status != 'hidden' AND " + _cat_effective_sql(),
            (k, k)).fetchone()["c"]
        out.append({"name": variants[k], "count": cnt}) if cnt else None
    return out


def all_categories(conn) -> list[dict]:
    """Distinct user-assigned categories with usage counts, case-insensitive
    grouping (modal spelling displayed)."""
    variants = _variant_map(
        r["categories"] for r in conn.execute(
            "SELECT categories FROM feeds WHERE categories != ''"))
    counts = {k: 0 for k in variants}
    for row in conn.execute("SELECT categories FROM feeds"):
        for cat in {c.casefold() for c in parse_categories(row["categories"])}:
            counts[cat] += 1
    return sorted(({"name": variants[k], "count": counts[k]}
                   for k in variants if counts[k]),
                  key=lambda d: d["name"].casefold())


def rename_category(conn, old: str, new: str) -> int:
    """Rename (or create-and-reassign) a category across all feeds.
    Case-insensitive on both ends: every stored variant of `old` is replaced."""
    old_cf, new_cf = old.casefold(), new.casefold()
    touched = 0
    for row in conn.execute("SELECT id, categories FROM feeds"):
        cats = parse_categories(row["categories"])
        if any(c.casefold() == old_cf for c in cats):
            cats = [new if c.casefold() == old_cf else c for c in cats]
            # dedupe (case-insensitively), preserve order
            seen: list[str] = []
            seen_cf = set()
            for c in cats:
                if c.casefold() not in seen_cf:
                    seen.append(c)
                    seen_cf.add(c.casefold())
            conn.execute("UPDATE feeds SET categories=? WHERE id=?",
                         (",".join(seen), row["id"]))
            touched += 1
    conn.commit()
    return touched


def remove_category(conn, name: str) -> int:
    """Remove a category everywhere, case-insensitively (all stored variants)."""
    name_cf = name.casefold()
    touched = 0
    for row in conn.execute("SELECT id, categories FROM feeds"):
        cats = parse_categories(row["categories"])
        if any(c.casefold() == name_cf for c in cats):
            cats = [c for c in cats if c.casefold() != name_cf]
            conn.execute("UPDATE feeds SET categories=? WHERE id=?",
                         (",".join(cats), row["id"]))
            touched += 1
    conn.commit()
    return touched


# ---------------------------------------------------------------- articles

def upsert_article(conn, feed_id: int, guid: str, link: str, title: str,
                   published_at: str | None, categories: list[str] | None = None,
                   image_url: str | None = None) -> int | None:
    """Insert a new article (with its own category tags and any declared hero
    image URL). Returns its id, or None if already known -- known articles get
    refreshed category tags only."""
    cats = ",".join(categories or [])
    published_at = norm_ts(published_at) if published_at else None
    known = conn.execute("SELECT 1 FROM articles WHERE feed_id=? AND guid=?",
                         (feed_id, guid)).fetchone()
    if known:
        if cats:
            conn.execute("UPDATE articles SET categories=? WHERE feed_id=? AND guid=?",
                         (cats, feed_id, guid))
            conn.commit()
        return None
    cur = conn.execute(
        "INSERT INTO articles(feed_id, guid, link, title, published_at, fetched_at,"
        " categories, image_url, status) VALUES(?,?,?,?,?,?,?, ?, 'pending')",
        (feed_id, guid, link, title, published_at, now_iso(), cats, image_url))
    conn.commit()
    return cur.lastrowid


_TS_EXPR = "COALESCE(published_at, fetched_at)"


def articles_page(conn, before_ts: str | None = None, before_id: int | None = None,
                  limit: int = 20, feed_id: int | None = None,
                  category: str | None = None, since_ts: str | None = None,
                  order: str = "newest") -> list[sqlite3.Row]:
    """Page of articles. order='newest': reverse-chronological, cursor is an
    exclusive UPPER bound (older-than). order='oldest': chronological, cursor
    is an exclusive LOWER bound (newer-than) — the catch-up flow.
    Optionally floored at since_ts (inclusive)."""
    where, params = ["a.status != 'hidden'"], []
    if since_ts is not None:
        where.append(f"{_TS_EXPR} >= ?")
        params.append(since_ts)
    if before_ts is not None:
        if order == "oldest":
            where.append(f"({_TS_EXPR} > ? OR ({_TS_EXPR} = ? AND a.id > ?))")
        else:
            where.append(f"({_TS_EXPR} < ? OR ({_TS_EXPR} = ? AND a.id < ?))")
        params += [before_ts, before_ts, before_id or 0]
    if feed_id:
        where.append("a.feed_id = ?")
        params.append(feed_id)
    if category:
        cats = [category] if isinstance(category, str) else [c for c in category if c]
        if cats:
            ors = []
            for c in cats:
                ors.append(_cat_effective_sql())
                params += [c, c]
            where.append("(" + " OR ".join(ors) + ")")
    rows = conn.execute(
        f"""SELECT a.id, a.title, a.link, a.summary, a.status,
                   {_TS_EXPR.replace('published_at', 'a.published_at').replace('fetched_at', 'a.fetched_at')} AS ts,
                   a.published_at, a.fetched_at, a.tokens_in, a.tokens_out,
                   f.id AS feed_id, f.title AS feed_title, f.description AS feed_description,
                   f.categories AS categories, f.auto_categories AS auto_categories,
                   a.categories AS post_categories, a.image AS image,
                   f.summarize AS feed_summarize,
                   CASE WHEN COALESCE(f.last_read_ts, '') = '' THEN 1
                        WHEN {_TS_EXPR} > f.last_read_ts THEN 1 ELSE 0 END AS unread
            FROM articles a JOIN feeds f ON f.id = a.feed_id
            WHERE {' AND '.join(where)}
            ORDER BY ts {'ASC' if order == 'oldest' else 'DESC'},
                     a.id {'ASC' if order == 'oldest' else 'DESC'} LIMIT ?""",
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
    if key in ("resume_ts", "oldest_seen") and value:
        value = norm_ts(value)
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


def mark_feed_read(conn, feed_id: int, ts: str) -> None:
    """Advance one feed's read cursor (canonical UTC strings); forward only."""
    ts = norm_ts(ts)
    if not ts or not feed_id:
        return
    conn.execute("UPDATE feeds SET last_read_ts=? WHERE id=?"
                 " AND (last_read_ts IS NULL OR last_read_ts < ?)", (ts, feed_id, ts))
    conn.commit()


def feed_unread(conn, feed_id: int, last_read_ts: str | None) -> int:
    """Articles newer than the feed's read cursor (never-read feeds: all)."""
    return conn.execute(
        "SELECT COUNT(*) c FROM articles WHERE feed_id=? AND status!='hidden'"
        " AND " + _TS_EXPR + " > COALESCE(?, '')",
        (feed_id, last_read_ts)).fetchone()["c"]


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
