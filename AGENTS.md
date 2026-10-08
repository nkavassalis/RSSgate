# AGENTS.md - working on RSSgate

Read this before changing anything. It is the map, the rules, and the scars.
Companion docs: `docs/TESTING.md` (test tiers and helpers), `docs/API.md`
(HTTP contract), `CHANGELOG.md` (history; one entry per release).

## What it is
A local-first RSS reader: Flask + SQLite (WAL), one YAML config, a background
scheduler that polls feeds and asks an LLM for article digests, a vanilla-JS
reader and admin page. Python >= 3.11. No frontend build step: two scripts
(`viewer.js`, `admin.js`), one stylesheet, two Jinja templates.

```
.venv/bin/python run.py --config config.yaml   # serves the app (config sets host/port)
.venv/bin/python -m pytest                     # hermetic tier, seconds
.venv/bin/python -m pytest -m ui               # browser tier, ~1 min; REQUIRED for UI work
```
The live instance runs from `config.yaml` in the repo root (gitignored, has
`host: 0.0.0.0`). After editing Python or templates, restart it: Flask caches
templates and the scheduler holds imported code.

## Map
```
run.py              config -> create_app -> Scheduler -> maint loop -> flask
rssgate/
  config.py         DEFAULTS + deep-merge load/save, env: secrets, masked_config.
                    ui.*: order, snapshot_width, stream_width, read_delay,
                    hide_untranscribed (snapshot_width = WIDE share width).
  db.py             ALL SQL. Schema + additive _migrate(). Keyset paging
                    (articles_page), claim_pending (UPDATE..RETURNING),
                    read cursors, category SQL, delete_* return image names,
                    release_files() = immediate orphan reclaim.
                    Index idx_articles_sort matches _TS_EXPR exactly - if you
                    change the sort expression, change the index with it.
  refresh.py        refresh_feed / summarize_pending: claim -> processing ->
                    (sponsored prefilter | raw mode | hash cache | LLM).
                    _cache_image: extract -> store -> imgstore.dedupe.
  scheduler.py      thread: requeue stale, poll due feeds, N workers, each
                    with its OWN connection. Re-reads config.yaml every tick
                    (config_path), so admin edits apply without a restart.
  maint.py          run_all every 6h + at boot: retention, orphan prune,
                    dedupe_galleries (idempotent repair), cache cap.
  imgstore.py       cache dir, store(url) (magic-byte sniff, sha256 name),
                    safe_path, ahash (8x8 centre-crop average hash),
                    dedupe(names) = THE hero/gallery duplicate rule.
  extract.py        article text + image candidates (og first, article-root
                    scoped content images, avatar/junk filters).
  net.py            THE outbound HTTP path: get() adds the User-Agent
                    (fetch.user_agent or DEFAULT_UA) and paces requests per
                    host (fetch.per_host_interval). BLOCK_STATUSES = 403/429.
  fetcher.py        conditional GET, feed parsing, page fingerprint, probe.
  llm.py            provider-agnostic client; no DB; raises LLMError.
  web.py            create_app(config_path, conn=None). Thin routes only, no
                    SQL. Input hygiene: _body() (JSON object or 400), _int(),
                    _strs(); /api/* errors are JSON, never an HTML 500
                    (tests/test_input_hygiene.py fuzzes every endpoint).
  templates/        viewer.html, admin.html (assets stamped ?v={{app_version}})
  static/viewer.js  stream, read tracking, PTR, pips, share renderer
  static/admin.js   panels, autosave, feed table + cog modal, nav spy
  static/style.css  CSS variables, light/dark via prefers-color-scheme
tests/              hermetic tests + test_ui_contract.py + test_ui_real.py
```

## Invariants (never break; most are test-enforced)
1. **Token discipline.** LLM only for new articles whose extracted-text hash
   has no cached digest. Unchanged pages, raw-mode feeds, sponsored and
   category-blocked items cost zero calls. Call-count tests guard this.
2. **Config is the source of truth.** Admin writes go `PUT /api/config` ->
   `_merge` -> `save_config`. No config in the DB. API keys leave the server
   only masked.
3. **All SQL lives in `db.py`.** Routes stay thin.
4. **Keyset cursors `(ts, id)`, never OFFSET.** Read cursors only move forward.
5. **Image references are the GC root.** Code that deletes articles or
   replaces images must hand the old filenames to `db.release_files` (which
   keeps files another article still references); maintenance sweeps orphans.
6. **One SQLite connection per thread.** Background work (`/api/poll`,
   add-feed refresh, backfill, scheduler workers, maintenance) opens its own
   via `db.connect(db.path_of(conn))` - never a path rebuilt from config
   (under tests that pointed at a different database). Sharing the request
   connection with a thread corrupts commit state.
7. **`hidden` is terminal but reversible** (sponsored/category toggles);
   hidden items never reach `/api/articles` or stats.
8. **UI contract** (`test_ui_contract.py`): `[hidden]{display:none
   !important}` exists and overlays ship hidden; every template id is used
   by its script; every emitted `data-act` has a handler; every `$('id')`
   resolves. If it fails, wire the thing - never weaken the test.
9. **Autosave doctrine.** Editable admin fields save themselves on
   `change` (single-field patch) and flash their label `cfg-saving` ->
   `cfg-ok`/`cfg-bad`; patches carry only the field that changed (a
   full-form Apply once re-queued every hidden post). Every global setting
   goes through admin.js `putConfig`/`autosave(id, toPatch)`; there is no
   Save button anywhere (the old one sent every field, so a stale tab could
   revert values saved elsewhere).
10. **Be polite to sites.** Every request to a feed/article/image host goes
   through `net.get` (UA + per-host pacing). A 403/429 from a site pauses
   the FEED (`db.feed_block`, doubling to 24h, cleared by a successful
   fetch or the cog modal's Resume now); paused feeds are skipped by the
   scheduler, refresh_all and claim_pending, and their posts stay `pending`
   instead of failing. Never retry around a block or rotate identities.
   A bot CHALLENGE (Cloudflare "Just a moment", `net.is_challenge`) is not
   a block - waiting never helps - so it does not pause the feed: the post
   is published from the feed's own text (`articles.feed_text`,
   `digest_source='excerpt'`, zero tokens, labelled "feed excerpt" on the
   card), or fails with "site requires a browser" if the feed had none.
   Headless Chromium was tried against guru3d and does not pass; don't
   add stealth/evasion tooling.

## Key flows
- **Add feed:** probe -> POST /api/feeds -> background refresh -> upsert ->
  summarize_pending drains -> viewer.
- **Bare page:** conditional GET / hash guard -> unchanged stops; changed ->
  candidate links -> LLM picks articles (usage `discovery`).
- **Images:** `_cache_image` stores candidates (filename = sha256(url)[:24]),
  then `imgstore.dedupe` collapses byte twins and perceptual twins (<=
  `TWIN_BITS` of 64 on the centre crop). A gallery twin of the hero REPLACES
  the hero (og images are usually cropped drafts). Stored `images` column =
  all kept names, hero first; the viewer hides the hero from the gallery.
  `images_mode` filters at read time (no re-digest).
- **Read state:** dwell engine marks the topmost >=55%-visible unread card
  after `ui.read_delay` s -> beacon -> per-feed cursors (+ global in New/all).
- **Content source** (`feeds.content_source`): `auto` = article page, with
  fallback to the feed's own text (`fetcher.entry_excerpt`, stored at ingest
  as `articles.feed_text`) when the page is a challenge or extracts to <120
  chars (`_use_excerpt`, >= `EXCERPT_MIN`); `feed` = never fetch article
  pages (teaser shown as-is, >= `FULLTEXT_MIN` chars digested by the LLM);
  `page` = never fall back. `articles.digest_source` records the outcome
  ('' page / 'excerpt' forced fallback / 'feed' chosen) and drives the card
  chips and the admin "N from feed excerpt" badge.
- **Auto adapts to bot checks.** Each detected check bumps
  `feeds.challenge_streak` (`db.feed_challenge`); at `STREAK_LIMIT` (3) Auto
  stops requesting article pages and uses the feed's text, allowing one page
  probe per `PROBE_HOURS` (24, `claim_page_probe` is atomic). Any real page
  resets it (`feed_pages_ok`). Excerpt fallbacks get `upgrade_at` slots
  (`UPGRADE_HOURS` 6/24/72); the scheduler runs ONE `upgrade_excerpts` per
  tick, which replaces the excerpt with a real digest in place (status stays
  `ready`, read state ignored). TechPowerUp's check is a browser-fingerprint
  firewall (WebGL renderer, GPU limits, webdriver): headless Chromium fails
  it by design. Do not add fingerprint spoofing or a disguised browser.
- **LLM outage = hold, not fail.** `refresh.llm_unavailable` (connection
  errors, timeouts, 502/503/504) puts the post back untouched, records the
  outage in state (`llm_down_since/reason/next_try`, probe 15s doubling to
  5m) and stops the batch; `summarize_pending` claims nothing until the
  probe time, and the first success clears it. Viewer pip turns amber
  "waiting · AI offline"; admin Status explains. A real rejection (400 etc.)
  still fails the article. `fail()` alone decides pending-vs-error; never
  override its status afterwards (that bug silently disabled retries).
- **Not-an-article guards:** `looks_like_botcheck` catches interstitials
  served as HTTP 200 (TechPowerUp's "drag the handle"); `is_refusal`
  catches the model saying the input wasn't an article (matched only in the
  first 300 chars - real digests can end with caveats). Refusals are never
  published and never served from the hash cache; maintenance
  `requeue_refusals` repairs old ones.
- **Digest queue:** `claim_pending` takes the NEWEST pending post first
  (across feeds, skipping paused feeds), so a new feed's latest posts
  digest first and its backlog drips in at the paced rate.
- **Refresh:** button and pull-to-refresh share `doRefreshWork()`: POST
  /api/poll -> settle wait unless throttled -> restart stream -> refresh
  pips. The top rail (`#stream-progress`, min 400 ms) is the one busy signal.

## Share card (viewer.js) - read before touching
```
renderCardPng(a, {style, width})   load hero + QR bitmaps, build env, DPR=2
  -> shareLayout(a, env)           PURE: measures text, returns
                                   {W, H, ops, geo:{rects, lines, ...}}
       layoutBanner | layoutFloat  one function per style; each card has
                                   two buttons: tall = banner @ TALL_W 640,
                                   wide = float @ ui.snapshot_width
                                   (SHARE_FORMATS in viewer.js)
  -> paintShare(ctx, L, assets)    dumb interpreter of ops (fill/img/text)
  -> window.__lastShareGeo         geometry seam for tests
```
- Layout code never draws; paint code never measures. A new style is a new
  `layoutX` returning the same shape; register it in `shareLayout`.
- `geo.rects` must include `hero`, `qr`, `caption`, `via`, `title`, `meta`
  when present, and `geo.lines` every digest line box. The browser suite
  checks every style for zero overlaps between text and graphics, pixels
  inside the rects (photo, QR modules, accent), and visible paragraph gaps.
- **banner** (the TALL button): full-bleed hero (natural ratio clamped 16:9..2.4:1,
  centre cover crop), title, meta, one digest column, hairline, footer
  (QR + "Read the full article" + domain | via RSSgate). Type scales with
  card width so phone-sized views stay readable.
- **float**: hero floats top-right of the digest; QR + caption sink to the
  digest's bottom-left via a fixed-point loop (place, re-wrap, re-place until
  stable); via bottom-right.
- The agent usually cannot SEE the PNG. Verify with geometry + pixel probes,
  and when the user judges the look, change one thing per iteration and ask.

## Admin page (admin.html / admin.js)
- Sections are `section.panel#sec-*`, listed in `.sec-nav` under group labels
  (Overview / Reading / Sources / Processing). Keep nav order == section order.
  The nav spy is visual only; it never scrolls the page.
- Deep links `/admin#sec-*` are re-applied after the initial async renders
  (page height changes); viewer pips link to `#sec-queue` / `#sec-failures`.
- Card meta row: `.card-meta` is a wrapping flex row; the timestamp and both
  share buttons live in `.meta-actions` (one group, `order:1`, `flex:none`).
  Anything new on that row goes inside the group or before it - never
  between, or it splits the group again (test_share_buttons_stay_on_one_line_on_mobile).
- Default digest prompt = `_VOICE_RULE` + the rest in config.DEFAULTS; the
  rule keeps digests reporting content, not the document. A feed's custom
  `system_prompt` REPLACES the global prompt (only digest_length is always
  appended), so don't "restore" the voice rule there.
- Work queue wording lives server-side: `db.workqueue_snapshot` builds the
  `summary` sentence (counts are GLOBAL across feeds - never assert a fixed
  number in a browser test, the tier shares one session DB).
- Unread pill == stream visibility. `db.feed_unread(..., hide_statuses)` must
  receive the SAME tuple `/api/articles` withholds (web.py `_hide_statuses(cfg)`,
  from ui.hide_untranscribed); a queued post has no card, so it must not hold
  a pill. `error` posts DO render, so they count.
- `/api/pulse` counts MUST mirror `db.feed_unread` (read cursor + the same
  _TS_EXPR): two unread numbers on one page that disagree is a bug (it read
  714 where the UI said 1). tests/test_api.py::test_pulse_unread_matches_the_sidebar_pills.
- Boot failure (backend down/restarting): viewer shows `#boot-error` + retries
  via `retryPulse`; `bootFailed` suppresses the initial load and PTR. Never
  leave the reader as a bare logo + empty sidebar.
- New-posts pill (viewer.js `pulseTick`): reads `GET /api/pulse` (read-only,
  never `/api/poll`) on `ui.pulse_minutes`; baseline = per-feed unread at boot,
  so "new" means arrived-since-boot, not globally-newest. It NEVER scrolls or
  inserts; the click does `restart(true)` + `refreshPips()` + `pulseClear()`.
  Browser tests must not assert global counts - the tier shares one DB.
- Theme: `ui.theme` (auto|light|dark) is admin-editable (`cfg-theme`) and
  server-rendered into `data-theme`. Anything that differs by theme MUST be a
  CSS variable (the `--ok/--warn/--danger/--accent/--link` set) - a
  `@media (prefers-color-scheme)` rule cannot be overridden by the attribute.
  Only the `:root` OS block may use the media query.
- Status panel cells: add new items to the single `cells` list in
  `loadStatus` (reading order); it splits into two balanced rows itself.
  Manual refresh buttons use `withSpin(btn, work, dimEl)`.
- Destructive actions go in a `.danger-zone` with a plain-language note and
  a confirm().
- Feed rows are slim; per-feed settings live in the cog modal (`#cfg-modal`),
  autosaving per field. The feed filter appears past 8 feeds.

## Workflow and release checklist
1. Make the change. For patch scripts: assert every anchor exists
   (`assert old in s`) - a silent `str.replace` miss shipped fake fixes here.
2. `node --check` edited JS; `pyflakes rssgate tests` (one known warning:
   `web.refresh_all` is imported only as a conftest stub seam).
3. Hermetic tier green. For any template/CSS/JS change: browser tier green
   **twice** (a one-off failure that never repeats is a watch item; write
   it down).
4. Bump `__version__`, add the CHANGELOG entry (say what actually changed),
   commit, `git tag -a vX.Y.Z`, `git push --follow-tags`.
5. Restart the live server; confirm `/api/status` shows the version and,
   for UI changes, curl the served asset for a marker of the new code.

Versioning: MAJOR = breaks a client/config; MINOR = new capability (endpoint,
param, admin feature, config key with default, additive column); PATCH = fix,
perf, docs, tests, polish. When unsure, cut smaller.

## Hard-won lessons
1. **Release from disk, not intent.** v0.52.0 shipped notes for a rewrite
   whose patch script died mid-chain. Grep the file and curl the served
   asset before writing the changelog.
2. **"No difference after reload" means doubt your logic first.** Assets are
   cache-busted; the v0.55.0 title span was dead because its escape clause
   fired on every 16:9 photo.
3. **Test the shape that breaks.** A span test without a photo passed while
   every real card failed. Use production-shaped fixtures (16:9 og image,
   long title, long digest).
4. **Derive probe coordinates from layout, never hand-tune them.** Share
   tests read `__lastShareGeo`; hard-coded pixels had to be re-aimed on
   every layout tweak and hid real regressions.
5. **Prove a test can fail.** Break the guarded code once (and restore it
   with a targeted edit, NOT `git checkout`, which wipes uncommitted work)
   and watch the test go red.
6. **Shared session DB = shared fate.** Browser tests that read global
   counts or another test's feed broke four times in one day. Seed with
   `_mkfeed()` and query by your own feed id.
7. **Wait for state, not time.** `wait_for_timeout(350)` and "any label is
   cfg-ok" were flaky; wait for the specific selector/condition.
8. **Read the real API shape.** `/api/status` is flat (`pending`, `errors`);
   a guessed nested `queue` object rendered pips that always showed 0.
9. **Idempotency is a feature.** A repair pass that rewrote every row on
   every run reported "232 repaired" for weeks; assert a second run is a no-op.
10. **CSS has no `//` comments.** One swallowed the next rule and hid the
    lightbox for five releases.
11. **Bursts get you banned.** Adding a 100-entry feed fetched every page
    back-to-back and TechPowerUp blocked the IP (feed included). Pacing and
    backoff are now in `net`/`db`; keep new fetch code on that path.
12. **Unchecked edits fail silently - for months.** Every CHANGELOG entry
    from v0.32 to v0.63 was lost: the release script's `str.replace`
    anchor no longer existed. `tests/test_release.py` now fails unless the
    newest CHANGELOG entry is `__version__`. Assert anchors in every edit.
13. **Same-size canary edits can run stale bytecode.** A revert that keeps
    the file size within the same second reuses the `.pyc`; clear
    `__pycache__` (or change the size) after a canary.

## Known follow-ups (not started)
- systemd user unit for the live server.
- Per-feed smart_block indicator in the cog modal; "Retry all failed".
- The browser tier is serial (~1 min); per-worker servers would allow xdist.
