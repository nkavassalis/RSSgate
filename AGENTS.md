# AGENTS.md — RSSgate codebase guide for AI agents

## What this is
Local-first RSS reader. Flask + SQLite, single YAML config, background poller
thread, LLM-generated article digests. Python ≥3.11. No frontend build step —
vanilla JS + one stylesheet + one CSS file. See CHANGELOG.md for release
history and the **versioning policy** at the bottom of this file.

## Layout
```
run.py                  entry point (config -> app -> scheduler -> flask)
rssgate/
  config.py             DEFAULTS dict, deep-merge load/save, env: secret
                        expansion, masked_config(); injects no-thinking
                        extra_body for provider=local unless configured.
                        ui.order = newest|oldest (stream direction, admin-editable)
  db.py                 ALL SQL lives here. Plain sqlite3, no ORM, WAL.
                        feeds: type auto|feed|page, categories (user, comma),
                        auto_categories (feed-declared), summarize (raw mode
                        flag), hide_sponsored, last_read_ts (read cursor)
                        category filtering: _cat_effective_sql() = exact
                        membership of POST tags or USER feed labels only
                        (feeds.auto_categories is display-fallback, filter-
                        inert); articles_page(category=) str or list (OR);
                        category_list() = filterable names + live counts
                        articles: status pending -> processing -> ready
                        |error|hidden; categories = per-post tags;
                        started_at/llm_ms = per-article LLM timing;
                        image_url (declared) + image (hero) + images (gallery,
                        comma filenames)
                        UNIQUE(feed_id, guid)
                        claim_pending() = atomic claim (UPDATE..RETURNING)
                        state table: resume position, queue_peak, cache_hits
                        _migrate() = additive ALTER TABLE migrations on init
  maint.py              run_all(): retention delete + orphan prune + size cap;
                        6h loop from run.py; report in state 'maint_report'
  imgstore.py           local image cache: store(url) magic-byte-sniffs and
                        writes data/images/<sha256[:24]>.<ext>; safe_path()
                        gates the /image route. init(dir) from create_app
  extract.py            article-text extraction (junk class/id regex that
                        NEVER strips <body>/<html>), candidate link harvest; extract_images() = og/twitter image + first plausible content <img>
  llm.py                LLMClient: chat(model= override)/list_models()/
                        resolve_model(). openai-compatible (local/openai/
                        openrouter) + anthropic. extra_body passthrough.
                        Pure network layer, no DB. Raises LLMError.
  fetcher.py            conditional GET (_get), feed parsing, per-entry +
                        feed-level categories, page fingerprinting,
                        discover_page_articles(), find_feeds() = URL probe
                        with verification fetches (no LLM)
  refresh.py            refresh_feed(); summarize_pending(): claim -> mark
                        processing -> (sponsored prefilter | raw mode |
                        hash cache | LLM) -> log usage+duration;
                        is_sponsored() heuristics; per-worker connections
  scheduler.py          Scheduler(thread): requeue stale processing,
                        poll due feeds (feed vs page timers), then
                        summarizer.concurrency parallel workers via
                        ThreadPoolExecutor; each worker opens its own db
  web.py                create_app(config_path, conn=None). Thin routes.
  templates/            viewer.html (sidebar: cat chips + feeds + New/Since),
                        admin.html
  static/               style.css (CSS vars + prefers-color-scheme),
                        viewer.js (modes/localStorage/beacon), admin.js
                        (probe-first add flow, category dropdowns, panels)
tests/                  pytest (84, hermetic); conftest fixtures: conn, cfg,
                        client (web.refresh_feed/refresh_all stubbed)
docs/API.md             HTTP API reference
CHANGELOG.md            version criteria + history — update on every release
```

## Invariants (do not break)
1. **Token discipline.** An article is LLM-processed only when (a) new, and
   (b) its extracted-text sha256 has no cached digest. Unchanged bare pages,
   raw-mode feeds and sponsored-filtered items must not trigger *any* LLM
   call. tests/test_refresh.py + test_sponsored.py + test_rawmode.py guard
   this with call-count assertions — extend, never weaken.
2. **Config is the source of truth.** Admin edits go PUT /api/config →
   `_merge` → `save_config`. Don't store config in the DB. API keys never
   leave the server except masked (`***`).
3. **All SQL in `db.py`**; routes stay thin.
4. **Cursors are (ts, id) keyset** — strictly-older pagination (newest mode)
   or strictly-newer (oldest mode), never OFFSET. Read cursors (global +
   per-feed) advance FORWARD only. The resume cursor bounds ONLY legacy
   unfiltered no-fresh requests; the viewer boots fresh in newest mode and
   at-resume in oldest mode.
5. **Image refs are the cache's GC root**: any code deleting articles must
   return their image filenames (db.delete_old_articles does) and maintenance
   prunes orphans afterwards. Config `maintenance.*` governs retention/cap/images_per_post.
6. **Threading + sqlite**: each worker opens its OWN connection; a shared
   handle across threads corrupts commit state (this stranded articles in
   'processing' once — see stale-requeue).
6. Status `hidden` is terminal-but-reversible via the sponsored toggle;
   hidden items never appear in /api/articles or usage stats.

## Key data flows
- **Add feed**: admin probes URL (`/api/feeds/probe` → find_feeds: feed |
  verified candidates | confirm bare page) → POST /api/feeds → background
  refresh_feed → upsert entries with per-post categories → summarize_pending
  drains (claim → processing → ready/error/hidden) → viewer.
- **Bare page poll**: fetch_page (304/hash guard) → unchanged: stop →
  changed: candidates → LLM picks articles (usage logged as 'discovery') →
  pending items.
- **Read state**: cards send `{ts,id,feeds[],global}` beacons →
  POST /api/position → mark_feed_read per feed (+ global cursor only when
  `global:true`, i.e. New + All-feeds view). Sidebar pills from
  `db.feed_unread`; card dots from `unread` column in /api/articles.
- **Viewer boot**: order from /api/resume. Newest mode: at newest
  (`?fresh=1`), resume shown as "Continue reading" button. Oldest mode:
  boot AT resume (catch-up), end banner says "all caught up".
  GET /api/articles (cursor + feed_id + since_ts + order) →
  IntersectionObserver → beacons `{ts,id,reads:{feed:ts},global}` →
  per-feed cursors precisely; global resume = deepest passed (newest mode)
  or frontier (oldest mode).

## Versioning policy (cut releases the same way every time)
Given `MAJOR.MINOR.PATCH`, tagged `vX.Y.Z`, `__version__` in
`rssgate/__init__.py`, CHANGELOG entry per release:
- **MAJOR**: breaks a working client/config — endpoint removals/semantic
  changes, config keys without migration, non-additive schema changes.
- **MINOR**: backwards-compatible capability — new endpoints/params, admin
  features, config keys with defaults, additive `_migrate()` columns.
- **PATCH**: bug fixes, perf, docs, tests, UI polish without contract change.
Rule of thumb: could an existing client/config notice? → MAJOR. Only new
possibilities? → MINOR. Nothing contract-visible? → PATCH. When unsure, cut
smaller and fix forward. Release checklist: pytest green → bump → changelog
→ `git tag -a vX.Y.Z -m "..."` → `git push --follow-tags`.

## Testing conventions
- Never hit the network: monkeypatch `rssgate.fetcher._get`, `requests.get`,
  use the `client` fixture (LLM methods stubbed class-wide in conftest).
- FakeLLM pattern asserts call *counts* to prove token efficiency.
- db.add_feed returns the inserted Row (`row["id"]`), not an int.

## Running
```
.venv/bin/python run.py --config config.yaml   # http://0.0.0.0:8088
.venv/bin/python -m pytest                     # hermetic suite
```
