# Changelog

All notable changes to RSSgate are documented here. Versions follow
**semver-with-attitude** — the criteria below are the contract for humans and
AI agents working on this repo.

## Versioning policy

Pre-1.0: this project is versioned 0.x — every minor may add features and
fix bugs freely; breaking changes get a minor bump and a migration note.
1.0.0 is reserved for a declared-stable release.

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

## [0.70.0] — 2026-10-08

### Added
- Auto content source adapts to sites that keep serving bot checks: after
  3 in a row it stops requesting article pages (uses the feed's text) and
  probes ONE page per day; any real page resets it.
- Excerpt posts are upgraded later: their page is retried after 6h, 24h
  and 3 days (one retry per scheduler tick); when a real page comes back a
  proper digest replaces the excerpt in place - read or not.
- Feed row and cog modal spell out Auto's state ("Auto: using feed text -
  site shows a bot check (next page probe ...)", "Auto: N bot checks in a
  row") and count excerpt posts awaiting a page retry. /api/feeds adds
  challenge_streak, page_probe_at, upgrade_pending.

### Notes
- TechPowerUp's check is a browser-fingerprinting firewall (WebGL renderer,
  GPU limits, webdriver flag); it passes in a real browser without any
  click, and rejects headless Chromium by design. RSSgate doesn't disguise
  automation; it detects, falls back, and upgrades when the site allows.

## [0.69.0] — 2026-10-07

Rough-edges pass over the whole codebase.

### Fixed
- Admin setting changes (digest prompt, summary length, poll intervals,
  workers, input cap, retries, backoff...) did not reach background work
  until a restart: the scheduler kept its startup config. It now re-reads
  config.yaml every tick.
- Adding a feed ran its first refresh in a thread on the request's shared
  database connection (the invariant whose breach once stranded posts in
  'processing'). All background jobs now open their own connection to the
  app's actual database file (db.path_of).
- 12 malformed-input crashes (list bodies, id "abc", limit=abc, non-list
  category fields, non-string urls) returned HTML 500 tracebacks; now JSON
  400s, plus a JSON error handler for all /api routes. A fuzz test covers
  every endpoint.
- The "Save settings" button sent EVERY field on the page, so a stale tab
  could revert values autosaved elsewhere. Language model and Polling
  fields now autosave like everything else; the button is gone.

### Changed
- Share buttons are drawn icons (upright card / slightly-wide card) in
  30px tap targets; the old glyphs were tiny and very wide respectively.

### Internal
- No SQL left in web.py (status counts, retry/drop, sponsored sweep,
  newest_ts moved to db.py); dead code removed (feed_ready_count,
  pending_articles, mark_processing, unused extract constants); the two
  duplicate autosave flash helpers collapsed onto putConfig.

## [0.68.0] — 2026-10-07

### Changed
- Two share buttons on every post instead of one style setting: TALL
  (banner card, fixed 640px - phone-friendly; type scales with it) and
  WIDE (magazine layout at ui.snapshot_width, relabelled "Wide share card
  width"). Answers "is share width obeyed?": it always set the PNG size,
  but the banner scaled everything proportionally, so it looked identical
  at any width; width now drives the wide card, where it changes layout.

### Removed
- ui.share_style setting, its admin select and the /api/resume field
  (an old value in config.yaml is ignored).

## [0.67.0] — 2026-10-07

### Fixed
- Posts were marked read while mostly off screen: the scroll handler
  marked every card whose TOP had crossed 60% of the viewport (ignoring the
  read delay). Now scrolling marks a card only once it has scrolled
  ENTIRELY past the top of the screen; the dwell timer still needs most of
  the card on screen (or, for cards taller than the screen, the card
  filling most of it - those used to never count).
- Reading a feed's newest post silently read its whole backlog on the
  server (read state was one per-feed "read up to" timestamp). Read state
  is now per article (articles.read_at); the old cursor still covers
  history from before this release.

### Added
- Clicking anywhere on an unread card marks it read instantly.
- POST /api/position accepts {read_ids: [...]} (ts/id optional when only
  marking); legacy payloads still work.

## [0.66.1] — 2026-10-07

### Fixed
- The outage hold only recognised connection errors and 502-504, but the
  real maintenance window answered "models request failed (404)". Now
  every backend-level LLM error (404 route/model missing, 401/403 auth,
  5xx, connection, timeout, no model available) holds the queue; only
  request-specific rejections (400, 413, 422) fail an article.

## [0.66.0] — 2026-10-07

### Added
- LLM backend outages hold the queue instead of failing it: unreachable /
  timing-out / 502-504 backends put the post back untouched and pause
  digesting, probing again at 15s doubling to 5m; the first success
  resumes. Viewer queue pip turns amber "waiting · AI offline" (links to
  Status); admin Status shows "AI backend: offline since HH:MM" with the
  reason and next check. /api/status adds llm_down_since/reason/next_try.
- "Retry all failed" button (POST /api/articles/retry-failed).
- Bot checks served as normal HTTP 200 pages are detected by their text
  (TechPowerUp's "Automated bot check... Drag the handle") and handled like
  JS challenges: feed flagged, feed-text fallback, no LLM call.
- The model's "this isn't an article" replies are never published or
  reused from the content-hash cache; maintenance re-queues old ones.

### Changed
- Failure reasons are always recorded; the troubleshooting checkbox
  (troubleshooting.log_llm_failures) is gone.

### Fixed
- Automatic retries for transient failures never ran: the digest error
  handler forced status=error after fail() had chosen pending. That's why
  LLM maintenance produced 46 failures instead of retries.
- 120 TechPowerUp posts shared one cached "this page is a bot check"
  digest (identical challenge text -> identical hash -> cache hit).
- Admin Status "Refresh" button and update note were never wired
  ($('#id') with an id-only helper).

## [0.65.0] — 2026-10-07

### Added
- Browser-check detection flag: when a site answers with a Cloudflare-style
  JS challenge (feed, bare-page or article fetch), the feed is flagged
  (feeds.challenge_at / challenge_hits). Admin shows a "browser-check
  site" badge on the row and a warning right at the Content source
  control recommending "Feed text only"; feed status explains itself.
- Challenges are informational only: never a block, never a backoff.

### Watch item
- One hermetic test failed once in five full runs (name lost to the
  re-run) and never recurred; likely the known cross-test thread/timing
  interference class. Being watched, not ignored.

## [0.64.0] — 2026-10-07

### Added
- Per-feed Content source (cog modal, autosave; feeds.content_source):
  auto (article page, fall back to the feed's text - default), feed (never
  fetch article pages; teasers shown as-is, feed text >= 600 chars gets an
  LLM digest like a page), page (never fall back; failures stay visible).
- Fallback is flagged: cards show "⚠ feed excerpt" (forced fallback) or
  "from feed" (chosen source); feed rows show "N from feed excerpt" and a
  non-default source badge; the cog stats line counts fallbacks.
  GET /api/feeds adds content_source and excerpt_count.

### Internal
- /api/feeds counts moved into db.feed_counts (one query; SQL back in db.py).

## Reconstructed history (v0.32.0 - v0.63.0)

From v0.32 onward the release script inserted entries with an
unchecked str.replace on an anchor that no longer existed, so none were
written. Entries below were rebuilt on 2026-10-07 from tags and commit
subjects; today's releases carry full notes. Releases now assert the
entry exists before tagging (see AGENTS.md).

## [0.63.0] — 2026-10-07

### Added
- Feed-excerpt fallback: each entry's feed text is stored at ingest
  (articles.feed_text; filled into known posts, never overwritten). When an
  article page is a browser-only bot challenge, or extracts to almost
  nothing, the post is published from that text - zero tokens,
  digest_source=excerpt, labelled "feed excerpt" on the card.
- net.is_challenge(): Cloudflare-style JS challenges are told apart from
  bans and never pause the feed; with no feed text the failure reads
  "site requires a browser (bot challenge)".
### Why
- guru3d sits behind Cloudflare "Just a moment..."; headless Chromium was
  tested and does not pass it.

## [0.62.0] — 2026-10-07

### Added
- rssgate/net.py: one outbound path for feed/article/image hosts - the
  User-Agent (fetch.user_agent) and per-host pacing (fetch.per_host_interval,
  default 3s, shared by all workers).
- Site-block backoff: 403/429 pauses the FEED (fetch.block_backoff_minutes,
  default 60, doubling, max 24h; feeds.backoff_until/backoff_level). Paused
  feeds are skipped by polling, refresh-all and the digest queue; their
  posts stay queued. A successful fetch clears it; cog modal "Resume now".
- Admin "Fetching politely": UA field with "Use this browser's" / "Reset",
  pacing and backoff settings (autosave, live).
### Changed
- Digest queue takes the newest pending post first.
### Why
- Adding TechPowerUp fetched ~100 pages back-to-back; the site IP-banned us.

## [0.61.1] — 2026-10-07

### Fixed
- Removed a dead duplicate POST /api/poll handler (it shared the request
  connection with a thread); /api/poll reports throttled: true so the
  viewer skips its settle wait.
### Docs
- AGENTS.md, docs/TESTING.md and docs/API.md rewritten.

## [0.61.0] — 2026-10-07

### Changed
- Admin nav grouped (Overview / Reading / Sources / Processing), sections
  reordered to match; feed cog settings autosave per field (Done replaces
  Apply); Danger zone for "Clear all failed posts".
### Added
- Feed filter box (past 8 feeds).
### Fixed
- Saving feed settings with "Hide sponsored" off re-queued every hidden post.
- Admin deep links landed on stale positions after async renders.

## [0.60.0] — 2026-10-07

### Added
- ui.share_style = banner (default) | float. Banner: full-bleed hero, title,
  meta, one readable digest column, footer with QR + "Read the full
  article" + domain | via RSSgate.
### Internal
- Share renderer split into shareLayout (pure geometry -> ops + geo) and
  paintShare; share tests parametrized over styles, driven by
  window.__lastShareGeo. /api/resume loads config once.

## [0.59.1] — 2026-10-07

### Performance
- Expression index on COALESCE(published_at, fetched_at), id for the stream.
### Fixed
- maint.dedupe_galleries rewrote most rows every run; now idempotent.
### Internal
- imgstore.dedupe(): one duplicate rule for ingest and repair; lint clean;
  browser-test helpers (_mkfeed, SHARE_PROBE, RAIL_RECORDER, __SETTLE_MS).

## [0.59.0] — 2026-10-07

- queue/failure pips with deep links; clear-all-failed with image release

## [0.58.2] — 2026-10-07

- QR scaled to 80% (72-120px)

## [0.58.1] — 2026-10-07

- QR fixed-point placement - truly the last line

## [0.58.0] — 2026-10-07

- QR sinks to digest bottom-left with caption; text wraps both floats

## [0.57.0] — 2026-10-07

- QR hero-height twin at band's left with caption; center-channel text flow

## [0.56.3] — 2026-10-07

- via to bottom-right corner; air below the graphics band

## [0.56.2] — 2026-10-07

- text yields to QR/via lines; canary test with proven lethality

## [0.56.1] — 2026-10-07

- paragraph gap carries explicit y; pixel-measured gap test

## [0.56.0] — 2026-10-07

- full-width meta, digest flows beside graphics, via on the band line, sag post-mortem in AGENTS.md

## [0.55.1] — 2026-10-07

- image drops below the full-width title (the span now survives reality)

## [0.55.0] — 2026-10-07

- full-width title flow above the bottom-flush masthead graphics

## [0.54.0] — 2026-10-07

- gallery twin promotes to hero; centre-crop 8x8 hash; hero size halfway

## [0.53.0] — 2026-10-07

- small flush-right hero, bottom-aligned with QR

## [0.52.4] — 2026-10-07

- share hero 40% -> 33% width

## [0.52.3] — 2026-10-07

- version-stamped static assets

## [0.52.2] — 2026-10-07

- actually ship the flush-aligned share masthead (missed in 0.52.0)

## [0.52.1] — 2026-10-07

- perceptual threshold 10->20 (og crop variants)

## [0.52.0] — 2026-10-07

- perceptual dedupe + gallery repair pass; flush-aligned share QR

## [0.51.4] — 2026-10-07

- gallery-ratio feed thumbs + 100px share QR

## [0.51.3] — 2026-10-06

- QR between text and hero under the date; hero at gallery ratio; minimal footer

## [0.51.2] — 2026-10-06

- QR nests under the date beside the hero; label-only footer

## [0.51.1] — 2026-10-06

- title-only beside hero, full-width body below, natural-aspect thumbs

## [0.51.0] — 2026-10-06

- share card float layout + content-level gallery dedupe

## [0.50.0] — 2026-10-06

- single-current read marking + configurable read_delay (default 5s)

## [0.49.1] — 2026-10-06

- deterministic visual-only nav spy (pass-over geometry, no auto-jump)

## [0.49.0] — 2026-10-06

- /image lazily revives trimmed heroes (hash-verified refetch)

## [0.48.3] — 2026-10-06

- two-row status panel + explicit cache-cap enforcement docs

## [0.48.2] — 2026-10-06

- live image-cache size in Status (flat state JSON was mis-parsed -> 0)

## [0.48.1] — 2026-10-06

- type badge moves into modal stats line; rows end clean

## [0.48.0] — 2026-10-06

- lightbox CSS-comment fix, Enabled column, type under cog, posts in modal, full-page feed-settings modal

## [0.47.0] — 2026-10-06

- feed rows slim - LLM/Ads/Images behind the cog; row refresh/delete into panel

## [0.46.0] — 2026-10-06

- per-feed images_mode (auto/hero/off), read-time filter, row autosave select

## [0.45.2] — 2026-10-06

- IMG_JUNK_RE kills loader gifs + Most Read widget images in galleries

## [0.45.1] — 2026-10-06

- snapshot paragraphs keep their breaks; README canonizes the home-screen-over-VPN pattern

## [0.45.0] — 2026-10-06

- native share sheet first for snapshots (standalone/https); clipboard+download fallback

## [0.44.3] — 2026-10-06

- PTR hands work indication to the top rail; one refresh animation everywhere

## [0.44.2] — 2026-10-06

- refresh button spins + rail covers full poll window; no more delayed hard reload

## [0.44.1] — 2026-10-06

- 400ms min-dwell loading rail + duration-measured browser test

## [0.44.0] — 2026-10-06

- hide untranscribed by default (admin toggle) + stream loading rail

## [0.43.1] — 2026-10-06

- Feeds section follows Categories

## [0.43.0] — 2026-10-06

- Status panel + admin reorder, X-home close button, authoritative nav feedback, 800/1280 defaults

## [0.42.2] — 2026-10-06

- admin grid right-column wrapper (sections no longer wrap into nav gutter) + geometry test

## [0.42.1] — 2026-10-06

- two-pane admin nav, Display & sharing panel completes (img-per-post joins, all autosave)

## [0.42.0] — 2026-10-06

- admin-configurable snapshot + reading column widths (clamped, resume-delivered, CSS-var driven)

## [0.41.0] — 2026-10-06

- unread-first all-feeds view (tuple keyset + client dedupe); snapshot absolute local timestamp

## [0.40.1] — 2026-10-06

- snapshot footer on one baseline row, QR flush right, no dead space

## [0.40.0] — 2026-10-06

- QR footer on share snapshots, local /api/qr.png encoder

## [0.39.1] — 2026-10-06

- feed_unread mirrors stream visibility (dropped can't hold a pill hostage)

## [0.39.0] — 2026-10-06

- copy-snapshot card PNG (clipboard with download fallback), browser-verified

## [0.38.0] — 2026-10-06

- smart default blocking of declared ad categories (user-ownership tri-state)

## [0.37.3] — 2026-10-05

- 'daily deal' sponsored pattern (Techdirt leak)

## [0.37.2] — 2026-10-04

- PTR stands down on horizontally-led gestures (drawer swipes no longer reload)

## [0.37.1] — 2026-10-04

- admin back arrow pops history (no ghost page under browser edge-back)

## [0.37.0] — 2026-10-04

- restore missing On checkbox (unbreaks every Save), checkbox autosave, data-role contract guard

## [0.36.0] — 2026-10-04

- decisive-intent axis lock - scrolling no longer peeks the drawer

## [0.35.2] — 2026-10-04

- extraction threshold actually applied; thin posts extract

## [0.35.1] — 2026-10-04

- extraction thresholds fit thin posts

## [0.35.0] — 2026-10-04

- two-tier junk pruning - hyphenated CSS class names no longer eat whole articles

## [0.34.0] — 2026-10-03

- edge-swipe drawer (finger-tracking reveal, swipe-left dismiss, axis lock) + browser tests

## [0.33.0] — 2026-10-03

- category chips autosave with visible confirmation (add + remove), browser-tier coverage

## [0.32.0] — 2026-10-03

- real-browser UI test tier (Playwright), LLM-down endpoint degradation, PTR target guard, docs/TESTING.md

## [0.31.1] — 2026-10-03

### Fixed
- Pull-to-refresh broke the mobile layout (the pill was a direct child of
  the flex .layout container, becoming a third flex item - the left gap)
  and was invisible (self-clipping height:0). Now a position:fixed pill
  that slides in with proper visibility states.
- Layout-class contract test: overlays must be fixed/absolute, and the
  flex container's child set is pinned - this exact bug class is now
  CI-forbidden.

## [0.31.0] — 2026-10-03

### Added
- **Pull-to-refresh on mobile** (viewer): rubber-banded drag with
  resistance, armed-state arrow, release spinner. Pulling POSTs
  /api/poll - a background pass that polls every enabled feed RIGHT NOW
  (60s throttle, disabled feeds respected, LLM queue drains itself after)
  - then the stream restarts at the newest. Native browser PTR disabled
  via overscroll-behavior so only ours fires.

## [0.30.3] — 2026-10-03

### Fixed
- **The .readmore CSS rule never existed.** The v1.8.0 append lived in a
  script that died early on an unrelated path error, so the footer link
  shipped UNSTYLED (browser-default blue) - and every follow-up 'fix' was
  a string-replace against an anchor that was never in the file, i.e. a
  silent no-op, and my verifications grepped viewer.js (which has the
  class name) instead of style.css (which never did). Rule restored;
  contract test now requires core markup classes to have CSS rules.

## [0.30.2] — 2026-10-03

### Changed
- Full-article link color: white on dark, dark grey on light (was blue -
  poor contrast for the reader's most-used link).

## [0.30.1] — 2026-10-03

### Fixed
- Icon links were inserted INSIDE <title>, making browsers use the literal
  tag soup as the page (bookmark) title. Re-anchored after </title>; UI
  contract test now forbids any markup inside the title element.

## [0.30.0] — 2026-10-03

### Added
- **App icon**: RSS-signal glyph reimagined as a gate arch over the feed
  dot, indigo gradient rounded tile. Served as SVG favicon (crisp at any
  zoom, auto no-emoji-tab), 32px PNG fallback, apple-touch-icon, and a
  web manifest so installed/home-screen bookmarks get a real branded tile
  instead of the generic globe.

## [0.29.0] — 2026-10-03

### Added
- **Transient failures now retry themselves.** 429s, timeouts, connection
  resets and 5xx go back to the queue automatically (the 30s scheduler
  tick is the back-off) up to `summarizer.max_retries` (default 2); the
  stored error reason gains an `[attempt N]` suffix. Persistent failures
  (paywalls, junk pages, 404s) fail immediately - retrying those is
  someone else's problem (yours, via the buttons below).
- **Attempts are counted** (`articles.attempts`) and shown as a badge.
- **Retry / Drop buttons** on every row of the admin failure list:
  retry re-queues with a fresh budget; drop hides the article for good
  (new `dropped` status: gone from feed, counts, and error list).
  `POST /api/articles/<id>/retry` / `.../drop`.

## [0.28.1] — 2026-10-03

### Fixed
- **Stale-processing recovery never ran.** The crash-recovery sweep
  compared canonical `...T...Z` timestamps against sqlite
  `datetime('now')` format (`... ...`) - as STRINGS, `'T' > ' '` makes the
  comparison false forever, so an article killed mid-processing (e.g. by a
  server restart) sat 'processing' indefinitely and the admin work-queue
  cheerfully counted its uptime ('running 48 min'). Cutoff now built in
  the same canonical format; regression test added.

## [0.28.0] — 2026-10-03

### Added
- **Prune vanished entries** (per-feed `sync_deletes`, gear panel): for
  SNAPSHOT sources - trending lists, breaking-news pages - entries absent
  from the latest SUCCESSFUL, NON-EMPTY fetch are deleted (their images
  released immediately). Off by default: normal RSS keeps history, since
  item lists legitimately fluctuate. Reappearing entries re-add cheaply
  (content-hash cache usually returns the old digest for free).

## [0.27.1] — 2026-10-03

### Fixed
- Sidebar feed list ignored renames (showed the raw feed title while cards
  showed the friendly name). `/api/feeds` now provides `display_title`
  (custom > feed's own > url) and the viewer uses it.

## [0.27.0] — 2026-10-03

### Added
- **Failure troubleshooting mode** (`troubleshooting.log_llm_failures`,
  default off): stores WHY each article failed to transcribe (HTTP code,
  too-short extraction, LLM error class) on the article; the admin panel
  gains a 'Transcription failures' section (GET /api/feed-errors).
- **Per-feed max input chars** (gear panel): tighter cap than the global
  one for verbose sources (model cards, PDFs) - smaller prompts, faster
  digests. `feeds.max_input_chars`, 0 = global.
- **Proactive orphan prevention**: replaced images (backfill/re-digest)
  and feed deletion release their cache files IMMEDIATELY when no other
  article references them; maintenance remains the belt-and-braces sweeper.
- Feed rename pencils + disable toggle shipped alongside (0.26.0 dev).

### Notes
- Admin feed table: 'On' checkbox disables polling AND digesting without
  losing any settings/articles (dimmed row; resume anytime).
- Feed titles: inline pencil rename (custom_title); the feed's own title
  keeps refreshing underneath, 'revert' clears the override.

## [0.25.3] — 2026-10-03

### Removed
- 'feed says: ...' auto-category echo in the feed table - redundant now
  that the gear panel's per-feed category census (with counts and
  allow/deny) covers it properly.

## [0.25.2] — 2026-10-03

### Added
- **UI contract tests** (`tests/test_ui_contract.py`): hermetic static
  guards for the JS/CSS layer — the bug class pytest couldn't see. They
  enforce: `[hidden]` is authoritative, overlays ship hidden, every
  template control id is referenced by its script, every `data-act` has a
  handler, every `$('id')` hook resolves.

### Fixed (all three found BY the new tests on first run)
- The feed-type select (`#new-type`) was decorative dead markup: choosing
  feed/page now forces that type and skips the auto-probe.
- The admin 'Max images per article' input was never wired to config
  load/save (shipped unwired in v0.24.0).
- Re-asserted the modal regression guard as a permanent test, not a fix.

## [0.25.1] — 2026-10-03

### Fixed
- Re-process dialog showed itself whenever the admin panel opened (the
  veil's `display:flex` beat the `hidden` attribute, and its buttons only
  get handlers when deliberately summoned). Added a global
  `[hidden] { display:none !important }` so the attribute always wins —
  this also immunizes every other hidden-classed element in the app.

## [0.25.0] — 2026-10-03

### Added
- **Re-process confirmation popup**: changing a feed's LLM on/off or its
  digest settings now asks whether to regenerate its stored digests.
  Confirming (`POST /api/feeds/<id>/redigest`) re-queues every ready/error
  article with body hashes cleared (so the hash cache cannot serve stale
  settings), then a toast reports the queue count. Declining keeps
  everything as-is; feeds with nothing digested never prompt.
- **Optional custom system prompt per feed** (config panel textarea):
  replaces the global digest prompt for that feed; `{length}` token works;
  empty = global. The digest-length directive still layers on top.

### Changed
- `PUT /api/feeds/<id>` no longer auto-requeues raw articles when LLM is
  toggled on (the confirm dialog owns that decision now).
- `/api/feeds` payload gains `ready_count`.

## [0.24.0] — 2026-10-03

### Fixed
- Gear button on feed rows never opened the config panel (handler patch
  had missed its anchor).

### Changed
- Category rename is inline: the per-category pencil turns the row into an
  editable field (Enter saves, Esc cancels, datalist offers existing names
  so typing one merges). The two-textbox Rename/Remove form is gone;
  removal was already one click (the row's x).

## [0.23.0] — 2026-10-03

### Added
- **Per-feed post-category filter** (`category_block`): admin panel shows
  every category a feed declares or its posts carry, checked by default
  (new arrivals auto-allowed). Unchecking hides matching articles BEFORE
  the LLM (zero tokens); retroactive sweep on save, unblocking re-queues.
- **Per-feed digest length** (`digest_length`: default|terse|normal|detailed):
  terse = one sentence <=20 words; detailed = 300-500 words. Overrides the
  global length knob per feed.
- **Expandable per-feed config** (gear icon) in the admin feed table.

### Changed
- **Version reset**: the project is not mature enough for 1.x promises.
  The entire v1.0.0..v1.10.0 history was renumbered v0.1.0..v0.22.0
  (chronological); this release is v0.23.0. 1.0.0 will be declared when
  the feature set and API stabilize, per the versioning policy below.

## [0.22.0] — 2026-10-03

### Added
- **Sidebar split into three boxes**: Feed Categories (your feed labels),
  Feeds by Name, Post Categories (tags declared by the articles
  themselves). Independent filters: OR within a box, AND across boxes.
- `GET /api/articles` gains `feed_category=` (user feed labels);
  `category=` now means POST TAGS ONLY (was: post tags OR feed labels).
- `GET /api/categories?viewer=1` returns `{post:[...], feed:[...]}`.

### Fixed
- Sidebar now scrolls independently (tall category lists no longer scroll
  off the page - `sticky` + own `overflow-y`).
- Unread pills **count down** as you read articles instead of vanishing on
  the first read card; server truth re-syncs (debounced) afterwards.
  Clicking a feed pill no longer wipes it (no eager cursor jump).

## [0.21.0] — 2026-10-03

### Fixed
- **Queue backlog after adding a feed**: the summarizer ran only 4 rounds
  of `concurrency` articles per 30s tick, so a freshly added 100-article
  feed showed 'waiting' for 15+ minutes. Now drains up to 32 rounds per
  tick under a wall-clock budget (LLM queues drain proportionally too).
- Raw-mode feeds show 'preparing...' instead of 'waiting for AI
  transcription' (no AI is involved); error cards say 'transcribe'.

## [0.20.0] — 2026-10-02

### Fixed
- **Sidebar/related-post image leaks**: junk markers (related, promo,
  sidebar, newsletter...) are now checked in the img's 4-ancestor chain,
  not just the tag itself; content-image scanning is scoped to the
  article root (the <article>/<main>/entry-content containing the <h1>)
  when identifiable, chrome tags (aside/nav/header/footer) always rejected,
  and whole-page fallback enforces an after-<h1> positional floor.
  Fixes other posts' thumbnails entering galleries (Gizmodo).

## [0.19.0] — 2026-10-02

### Fixed
- **Poisoned `image_url` repair**: pre-v1.9 backfills saved page-extracted
  (sometimes avatar) URLs back into `image_url`, and later passes trusted
  them as feed-declared heroes — blocking real galleries forever (baka.jp).
  The image pipeline now always prefers fresh page extraction; declared
  URLs are a fallback. `force` re-extracts from the article page.
- Backfill completion marking: `images` may end in a `-` sentinel
  ('page tried, single image only'); such rows never re-consume the page
  budget. Sentinels are stripped from API galleries and GC references.

## [0.18.0] — 2026-10-02

### Fixed
- Backfill route accepts `limit`/`page_fetches` (force passes now reach
  older articles like baka.jp instead of burning the 40-page budget on the
  newest rows first).
- Maintenance clears references to missing cache files (stale heroes after
  pruning showed as broken/absent images).
- Read-the-full-article link uses a dedicated `--link` color: high-contrast
  blue in both dark and light themes, underlined.

## [0.17.0] — 2026-10-02

### Added
- **Lightbox**: clicking a card thumbnail or gallery image opens the
  locally cached original full-size (never re-downloaded; click or Esc to
  close). Displays were small — the cached bytes always were the full
  `og:image`/content source.
- **`force` option on POST /api/images/backfill**: re-extracts even
  already-enriched articles (used to replace avatar-contaminated galleries).

### Fixed
- **Avatar/icon extraction**: the image filter now inspects alt text and up
  to 4 ancestor class/id levels (author cards, comment blocks, bylines),
  rejects gravatar-style URLs & size params, emoji/sprite/logo paths, and
  square-and-small declared dimensions. Avatars no longer enter galleries.

## [0.16.0] — 2026-10-02

### Fixed
- Backfill starvation: pages fetched without usable images are marked
  (sentinel in `articles.images`, sanitized out of API and GC-reference
  sets) so the per-pass page budget advances toward older articles
  (baka.jp) instead of re-fetching imageless pages forever.

## [0.15.0] — 2026-10-02

### Added
- **`maintenance.images_per_post`** (1-8, default 4): configurable image
  budget per article (hero included); admin number input in Maintenance.
- **"Read the full article at <feed> ↗"** footer link on every card.

### Fixed
- Image backfill now re-visits hero-only articles (v1.6-era rows, e.g.
  baka.jp) so pre-gallery articles catch up; previously `image IS NOT NULL`
  meant they were skipped forever.

## [0.14.0] — 2026-10-02

### Added
- **In-body galleries**: hero + up to 3 content images cached per article
  (same token-free pipeline); rendered as a strip under the digest linking
  to the original.
- **Maintenance** (admin panel + `GET/POST /api/maintenance[/run]`):
  article retention in months (default forever) with image-ref cleanup,
  image-cache size cap (oldest-first, dangling refs cleared), orphan-file
  pruning. Runs at startup and every 6 h; report surfaced in the panel.
- New config section `maintenance: {retention_months, images_max_mb}`.

## [0.13.0] — 2026-10-02

### Fixed
- `/image/*` 500s when `server.data_dir` is relative: Flask resolves relative
  `send_file` paths against the package directory. The image cache directory
  is now canonicalized to an absolute path at init.

## [0.12.0] — 2026-10-02

### Added
- **Image backfill**: startup pass (and `POST /api/images/backfill`) caches
  hero images for articles predating v1.5.0 — feed-declared URLs cost
  nothing but a download; at most 40 article pages are re-fetched per pass.
  Zero LLM tokens, same cache, same safety rules.

## [0.11.0] — 2026-10-02

### Added
- **Post images, cached and served locally.** Hero image per article from
  feed-declared URLs (media:thumbnail/og:image/enclosures — free) or the
  page HTML already fetched during digest/raw extraction (zero LLM tokens).
  Downloads are magic-byte verified, size-capped (5 MB), stored hash-named
  under `<data_dir>/images/` and served via `GET /image/<name>` (whitelist
  regex, immutable caching, no traversal/SSRF surface). Articles gained
  `image_url`/`image` columns (additive migration); `/api/articles` items
  expose `image`; reader cards render a lazy thumbnail.

## [0.10.0] — 2026-10-02

### Changed
- **Category filtering is post-precise.** Clicking `Health` returned every
  Gizmodo post because the feed's declared tag soup (~40 tags, `Health`
  included) OR-matched all of the feed's articles. Filter semantics are
  now: **post's own tags**, or **labels you assigned to its feed** (those
  label the whole feed). `feeds.auto_categories` remains a display-fallback
  chip for untagged cards but is filter-inert; untagged posts live under
  “All”. Sidebar chip counts/visibility use the same rule (zero-count and
  auto-only names are no longer offered as chips).

## [0.9.0] — 2026-10-02

### Changed
- Category chips: most-used first, selected chips always visible, and the
  long tail (your DB has 100+ names) collapsed behind a "more (N) …"
  expander instead of flooding the sidebar.

## [0.8.0] — 2026-10-02

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

## [0.7.0] — 2026-10-02

### Added
- **Viewed = read**: dwelling on an unread card (~1s, ≥55% in viewport)
  marks it read immediately — flips the card, decrements the pill, and
  beacons that one card's cursor. Previously only *scrolling past* the
  fold counted, so an unread card sitting in view (or a re-click of the
  same view) never cleared.

## [0.6.0] — 2026-10-02

### Fixed
- Read-state visual sync: cards scrolled past now flip from unread to
  read *immediately* (class + dot removed, same code path that sends the
  read beacon), instead of keeping their blue frame until a re-fetch.
  Sidebar pills re-render on every feed switch and whenever cards flip,
  so pill counts and card styling can no longer disagree.

## [0.5.0] — 2026-10-02

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

## [0.4.0] — 2026-10-02

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

## [0.3.0] — 2026-10-02

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

## [0.2.0] — 2026-10-02

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

## [0.1.0] — 2026-10-02 — "Caught up"

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
