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

## [1.4.0] — 2026-10-02

### Changed
- **Category filtering is post-precise.** Clicking `Health` returned every
  Gizmodo post because the feed's declared tag soup (~40 tags, `Health`
  included) OR-matched all of the feed's articles. Filter semantics are
  now: **post's own tags**, or **labels you assigned to its feed** (those
  label the whole feed). `feeds.auto_categories` remains a display-fallback
  chip for untagged cards but is filter-inert; untagged posts live under
  “All”. Sidebar chip counts/visibility use the same rule (zero-count and
  auto-only names are no longer offered as chips).

## [1.3.1] — 2026-10-02

### Changed
- Category chips: most-used first, selected chips always visible, and the
  long tail (your DB has 100+ names) collapsed behind a "more (N) …"
  expander instead of flooding the sidebar.

## [1.3.0] — 2026-10-02

### Added
- **Sidebar category filter** — multi-select chips above the feed list,
  default "All". ORs across selections, composes with feed filter and
  New/Since; persisted in localStorage; chip counts from
  `GET /api/categories?viewer=1` (union of post/user/feed-declared names
  with article counts). `GET /api/articles` now accepts repeated
  `category=` params.

### Changed
- Category matching is now EXACT comma-membership (post/user/auto columns)
  instead of raw LIKE substrings — `tech` no longer matches `technology`.

## [1.2.2] — 2026-10-02

### Added
- **Viewed = read**: dwelling on an unread card (~1s, ≥55% in viewport)
  marks it read immediately — flips the card, decrements the pill, and
  beacons that one card's cursor. Previously only *scrolling past* the
  fold counted, so an unread card sitting in view (or a re-click of the
  same view) never cleared.

## [1.2.1] — 2026-10-02

### Fixed
- Read-state visual sync: cards scrolled past now flip from unread to
  read *immediately* (class + dot removed, same code path that sends the
  read beacon), instead of keeping their blue frame until a re-fetch.
  Sidebar pills re-render on every feed switch and whenever cards flip,
  so pill counts and card styling can no longer disagree.

## [1.2.0] — 2026-10-02

### Added
- **Configurable stream order** — admin panel → Polling & summarizer →
  *Article order*: *Newest first* (default, unchanged) or *Oldest first*,
  which turns the reader into a chronological catch-up list: boot at your
  saved position, read forward to now, new arrivals at the end.
  - `ui.order` config key (`newest`|`oldest`); `PUT /api/config` now
    accepts `ui.order` specifically (rest of `ui`/`server` remain file-only)
  - `articles_page(order=…)` direction-aware keyset: in oldest mode the
    cursor is a strictly-newer lower bound; resume acts as the boot floor
  - `/api/articles?order=…` explicit override; `/api/resume` now reports
    `order` so the viewer boots with the right direction and beacons
  - read beacons are direction-proof: per-feed `reads` map is max-ts;
    global resume tracks the deepest card (newest mode) or the frontier
    card (oldest mode)

## [1.1.0] — 2026-10-02

### Changed
- **The New view now always boots at the newest article.** Previously the
  stream resumed at your deepest scroll point, so everything newer lived
  "above the ceiling": invisible in the All-feeds stream while feed pills
  counted them — reading everything in All never cleared the feeds. The
  saved position is now offered as an explicit "⤓ Continue reading from
  <date>" button (same keyset `(ts, id)` semantics, same beacons).

### Added
- `GET /api/articles?fresh=1` — explicit at-newest opt-out of the server's
  resume bound (the viewer sends it on fresh boots; legacy requests are
  unchanged).

## [1.0.2] — 2026-10-02

### Fixed
- **Unread pills that never cleared.** Three compounding bugs:
  1. The server applied the global resume cursor as the page bound to
     *every* `/api/articles` request — feed-filtered, category and Since
     views silently started at your stale resume point, hiding (and
     preventing read-marking of) everything newer. Resume now bounds only
     the plain New/all-feeds first page.
  2. Read beacons sent one position (the *oldest* card past the fold), so
     fast scrolling marked an old cutoff and left newer scrolled-past cards
     unread forever. Beacons now send `reads: {feed_id: newest_passed_ts}`
     — a precise per-feed cursor from the actual cards you passed.
  3. Newly arrived articles sit *above* the resume point in the New view
     and were unreachable there. A "↑ New articles above" button now
     appears when `newest_ts > resume_ts` and jumps the stream to newest
     (saving your deep position first).
- Verified live: CNN filtered view went from an invisible-empty stream
  (29 unread, no dots) to newest-first with dots; pills clear on scroll.

## [1.0.1] — 2026-10-02

### Fixed
- **Read-state never caught up (the bug users saw):** feeds publish
  timestamps in inconsistent formats (`Z`, `+00:00`, `-04:00`, RFC 822).
  Since all cursor/unread comparisons are string comparisons, mixed formats
  made scrolled-past articles stay unread and some cursors never advance.
  Every stored timestamp is now normalized to canonical
  `YYYY-MM-DDTHH:MM:SSZ` UTC at the DB chokepoints (`upsert_article`,
  `mark_feed_read`, `set_state`) with an idempotent startup migration for
  existing rows (`db._normalize_timestamps`). Regression tests cover all
  input formats and cross-format cursor advancement.

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
