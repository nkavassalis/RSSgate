# Changelog

All notable changes to RSSgate are documented here. Versions follow
**semver-with-attitude** — the criteria below are the contract for humans and
AI agents working on this repo.

## Versioning policy

Given `MAJOR.MINOR.PATCH` (tracked in `rssgate/__version__`, tagged `vX.Y.Z`,
one tag per released state):

- **MAJOR** — anything that can break an existing deployment or client:
  - HTTP endpoints or parameters removed or their semantics/shape changed
  - config keys renamed/removed without auto-migration
  - SQLite schema changes that are *not* achievable via additive
    `db._migrate()` (destructive rewrites, column semantics changed)
  - viewer/admin flows whose saved browser state (resume, cursors) becomes
    invalid
- **MINOR** — backwards-compatible capability:
  - new endpoints, new optional query/body params, new admin panels/UI
    features, new config keys with sane defaults, additive `ALTER TABLE`
    columns in `_migrate()`, new feed `PUT` flags
- **PATCH** — behavior-preserving:
  - bug fixes, perf, race/locking fixes, docs, tests, small UI tweaks that
    don't change an API contract

Rule of thumb: *could a working client or config file notice?* → MAJOR.
*Only new possibilities?* → MINOR. *Nothing user-visible contract-wise?* →
PATCH. When in doubt, cut the smaller number and fix forward.

Release checklist: tests green (`python -m pytest`), `__version__` bumped,
CHANGELOG entry added, `git tag -a vX.Y.Z`, `git push --follow-tags`.

## [1.0.0] — 2026-10-02 — "Caught up"

First stable baseline. Everything to date, grouped:

**Reader**
- Endless reverse-chronological stream with server-side resume cursor
  (`/api/resume`, `/api/position`), keyset pagination (no OFFSET)
- Feed-filter sidebar (drawer on mobile) with per-feed **unread counts**;
  per-feed read cursors (`feeds.last_read_ts`) advance from filtered *and*
  mixed scrolling; unread cards show an accent dot + left bar
- **New / Since** mode toggle with preset chips (24h/7d/30d/90d) and
  `since_ts` floor on `/api/articles`; end-of-stream banner defaults the
  date picker to yesterday

**Ingest & token discipline**
- RSS/Atom via feedparser with conditional GET; bare-page mode with LLM
  article discovery, skipped entirely when page bytes are unchanged
- Per-article sha256 body-hash digest cache (unchanged content = 0 tokens)
- Guid dedupe; articles summarized exactly once
- **Raw mode** per feed (`feeds.summarize=0`): readability-extracted text,
  zero LLM calls
- **Sponsored filter** per feed: free title/link heuristics before any LLM
  call, plus a zero-cost digest self-identification pass; hidden items
  excluded from viewer; toggle sweeps both directions
- Feed probe on add (`POST /api/feeds/probe`): finds real feeds behind any
  URL via alternate tags, feed-ish links, `feeds.`/`rss.` hosts, common
  paths — heuristic candidates verified by fetch+parse before offering;
  bare-page adds require explicit confirmation

**LLM layer**
- Providers: OpenAI-compatible (local), OpenAI, OpenRouter, Anthropic;
  single-model auto-detection; per-purpose model overrides
  (`model_summarize`, `model_discover`)
- Thinking-token suppression by default on local reasoning servers via
  `llm.extra_body` (`chat_template_kwargs.enable_thinking:false`) —
  measured 51.8 s → 7.4 s and −87 % output tokens per digest
- Parallel digest workers (`summarizer.concurrency`, ThreadPoolExecutor,
  atomic `UPDATE..RETURNING` claims), crash-recovery requeue of stale
  `processing` items, per-worker SQLite connections

**Admin**
- Feed table with type/categories/hidden counts; **category dropdown** per
  feed (create/rename/remove globally; datalist on add)
- **Work queue** panel: in-flight digestions with elapsed time, recent
  completions (status, duration, tokens in/out), queue depth — `GET
  /api/workqueue`
- Performance panel: queue peak, avg/min/max seconds (windowed to last 50),
  est. drain time, cache hits, per-day/month/all-time token totals
- LLM connection test, model picker fed from `/api/models`

**Categories**
- Per-**post** category tags stored on articles at fetch time; per-post
  chips with feed-union fallback; user-assigned chips distinct; global
  create/rename/remove; filtering matches all three sources

**UI / platform**
- Responsive vanilla JS, CSS-variable dark mode (auto/light/dark),
  zero frontend build step
- SQLite WAL + busy timeout; additive `_migrate()` column migrations

**Ops**
- Single YAML config (deep-merge defaults, `env:` secret expansion, masked
  over the wire); documented HTTP API (docs/API.md); agent guide
  (AGENTS.md); 84 hermetic tests

## Older

Pre-1.0 history lives in `git log` (initial commit through the v1
baseline).
