# AGENTS.md — RSSgate codebase guide for AI agents

## What this is
Local-first RSS reader. Flask + SQLite, single YAML config, background poller
thread, LLM-generated article digests. Python ≥3.11. No frontend build step —
vanilla JS + one stylesheet.

## Layout
```
run.py                  entry point (config -> app -> scheduler -> flask)
rssgate/
  config.py             DEFAULTS dict, deep-merge load/save, env: secret expansion,
                        masked_config() for browser responses
  db.py                 ALL SQL lives here. Plain sqlite3, no ORM.
                        feeds.categories = user-assigned (comma string)
                        feeds.auto_categories = feed-declared (comma string)
                        articles.status: pending -> processing -> ready|error
                        articles.started_at/llm_ms = per-article LLM timing
                        claim_pending() = atomic queue claim (UPDATE..RETURNING)
                        state table: resume position, queue_peak, cache_hits
                        _migrate() = additive ALTER TABLE migrations on init
  extract.py            BeautifulSoup article-text extraction (junk class/id regex),
                        candidate link harvest for bare pages
  llm.py                LLMClient: chat()/list_models()/resolve_model().
                        openai-compatible (local/openai/openrouter) + anthropic.
                        Pure network layer, no DB. Raises LLMError.
  fetcher.py            conditional GET (_get), feed parsing (feedparser),
                        category collection, page fingerprinting,
                        discover_page_articles() = candidates -> LLM JSON pick
  refresh.py            refresh_feed(): per-feed orchestration;
                        summarize_pending(): fetch link -> extract -> sha256 ->
                        hash-cache -> LLM -> log usage
  scheduler.py          Scheduler(thread): every 30s requeue stale processing,
                        poll due feeds (feed vs page timers differ), then run
                        `summarizer.concurrency` parallel digest workers via
                        ThreadPoolExecutor (claims are atomic)
  web.py                create_app(config_path, conn=None). Flask routes only;
                        delegates to db/refresh/llm.
  templates/            viewer.html, admin.html (server-rendered shells)
  static/               style.css (CSS vars + prefers-color-scheme), viewer.js, admin.js
tests/                  pytest; conftest fixtures: conn, cfg, client (all network stubbed)
docs/API.md             HTTP API reference
```

## Invariants (do not break)
1. **Token discipline.** An article is LLM-processed only when (a) new, and
   (b) its extracted-text sha256 has no cached digest. Unchanged bare pages must
   not trigger *any* LLM call. Tests in `tests/test_refresh.py` guard this.
2. **Config is the source of truth.** Admin edits go through `PUT /api/config`
   → `_merge` → `save_config` (file rewrite). Don't store config in the DB.
3. **API keys never leave the server** except masked (`***`).
4. **All SQL in `db.py`**; routes stay thin.
5. Resume position is `(ts, id)` — a strictly-older cursor, keyset pagination,
   never OFFSET.

## Key data flows
- **Add feed**: POST /api/feeds → db.add_feed(status pending) → background
  refresh_feed → type auto-probe → entries upserted (pending) →
  summarize_pending drains queue → articles ready → viewer.
- **Bare page poll**: fetch_page (304/hash guard) → unchanged: stop →
  changed: extract_candidate_links → LLM picks real articles → pending items.
- **Viewer boot**: GET /api/resume → GET /api/articles (keyset older-than) →
  IntersectionObserver → POST /api/position as you scroll.

## Testing conventions
- Never hit the network: monkeypatch `rssgate.fetcher._get`, `requests.get`,
  and use the `client` fixture (LLM methods are stubbed class-wide).
- `FakeLLM` pattern in test_refresh.py asserts call *counts* to prove token
  efficiency; extend rather than weaken those.

## Running
```
.venv/bin/python run.py --config config.yaml   # http://0.0.0.0:8088
.venv/bin/python -m pytest                     # hermetic test suite
```
