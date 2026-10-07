# Testing RSSgate — why there are two tiers, and what each catches

## The incident history that forced this document

Every one of these shipped **through green CI** of the hermetic suite:

| Version | Bug class | Symptom |
|---------|-----------|---------|
| 0.25.1  | CSS beat `[hidden]` (`display:flex` on the modal veil) | re-process dialog permanently visible, dead buttons |
| 0.30.1  | markup inserted *inside* `<title>` | bookmark title became literal tag soup |
| 0.30.3  | `.readmore` rule **never existed** (append silently skipped, later "fixes" were string-replaces on absent anchors) | footer link wore browser-default blue for six releases while every "verification" passed |
| 0.31.1  | PTR pill added as a direct child of the flex `.layout` row | mobile layout gap; indicator invisible (self-clipped) |
| 0.32.0  | `/api/models`, `/api/llm/stats` raised when the LLM endpoint was down | admin page threw uncaught 500s (hidden for months because production's LLM was always healthy) |
| 0.32.0  | PTR `touchstart` assumed `e.target` is an Element | listener crash for window-targeted touch events |

The common thread: **the hermetic suite verifies strings and schemas; none of it
can see a computed style, a bounding box, or a console error.** Grepping the
served file for a class name proves the class is *mentioned*, never that it is
*styled, positioned, and visible*.

## Tier 1 — hermetic (`pytest`)

`tests/` minus the `ui` marker. No network, no browser, runs in seconds, covers
config/DB/fetch/refresh/API/contract rules. This is the default run:

```
.venv/bin/python -m pytest
```

The *contract* tests (`test_ui_contract.py`) are the cheap half of UI safety:
`[hidden]` must be authoritative, overlays fixed/absolute, ids wired,
`data-act`s handled, core classes styled. They catch structural mistakes but
still cannot see layout or paint.

## Tier 2 — real browser (`pytest -m ui`)

Playwright driving headless Chromium against **the actual app** booted on a
throwaway config/database (port 8977, seeded fixtures). It asserts:

- computed color of the read-more link in **both** color schemes
  (exact rgb() values — the 0.30.3 class of bug can't recur silently)
- mobile geometry: `#stream` bounding box x≤2px, no phantom flex siblings
  (the 0.31.1 class)
- the pull-to-refresh gesture end to end: synthesized `TouchEvent` sequence →
  pill opacity > 0 mid-drag, armed at threshold, `POST /api/poll` observed,
  spinner engaged
- admin modals `display: none` at page load (the 0.25.1 class)
- zero uncaught page errors on every page visited
- title integrity (nothing but text inside `<title>`)

```
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m playwright install chromium        # ~150 MB, once
sudo .venv/bin/python -m playwright install-deps chromium   # system libs
.venv/bin/python -m pytest -m ui
```

Excluded from the default run via `addopts = -m "not ui"` in pytest.ini; CI
(or you) opts in explicitly. The fixture evicts zombie servers on its port
before booting, so a killed test run never wedges the next.

## House rules for UI work (learned the hard way)

1. **Never claim a UI feature from patch exit codes.** Verification means:
   computed style / bounding box / screenshot on the *served* page — or in a
   pinch, `curl` the served asset and grep the exact selector, never the
   token in another file.
2. **Overlays are `position: fixed|absolute`, never in flow.** Anything that
   floats over the UI must not participate in flex/grid layout.
3. **Multi-file patches must fail loudly.** `str.replace()` on a missing
   anchor is a silent no-op; prefer asserting the anchor exists, or explicit
   line edits. (This is how the .readmore rule vanished six releases deep.)
4. **Degradation is part of the UI.** Endpoints the admin page calls on load
   must return JSON errors, not 500 — a down LLM must not break the panel.
5. **Watch your process tooling**: `pkill -f <pattern>` matches the invoking
   shell's own command line when the pattern text appears in your command.
   Several "mysterious SIGTERM 143s" during test bring-up were self-inflicted
   this way. The ui fixture's evictor keeps its pattern split so it can never
   match the pytest process or the invoking shell.

## Writing browser tests fast (helpers in `tests/test_ui_real.py`)

The `ui_server` fixture is **session-scoped**: one app, one database, one
image dir for the whole run (`UI_DB`, `UI_IMG_DIR`). That makes the tier
fast and makes isolation your job:

| Helper | Use |
|---|---|
| `_new_page(browser, **ctx)` | page with `pg.errors` collecting uncaught errors; end every test with `assert pg.errors == []` |
| `_mkfeed(prefix)` | brand-new uniquely-addressed feed, returns id. **Seed your own data**; never depend on another test's feed or on global counts |
| `_seed_article(fid, guid, title=, ts=, status=, summary=, image=, link=)` | one article in any status |
| `_feed_article(pg, fid)` | the API card for that feed, fetched in-page (what the viewer would render) |
| `RAIL_RECORDER` | init script recording `#stream-progress` class changes from document-start (DOMContentLoaded races the boot fetch) |
| `SHARE_PROBE` / `_share(pg, art, zones)` | render a share card and count dark/accent/orange/blue pixels in named zones given in logical card px |
| `share_page` fixture | light-scheme page for share tests; asserts no page errors on teardown |
| `_crop_png(seed)` | portrait 200x600 orange image with a blue band at rows 270-330: proves crop/scale behaviour by colour |

App-side test seams (keep them; they cost nothing in production):
`window.__renderCardPng(a, {style, width})`, `window.__lastShareGeo`
(layout rects/lines of the last render), `window.__SETTLE_MS` (shrinks the
2.5 s poll settle wait), `window.refreshPips()`.

Patterns:
- **Wait for state, not time**: `wait_for_selector("label:has(#x).cfg-ok")`,
  `wait_for_function(...)`; avoid `wait_for_timeout` except to prove
  something does NOT happen.
- **Spatial bugs get spatial assertions**: bounding boxes, computed styles,
  pixel counts inside layout-derived rects, wall-clock durations.
- **Parametrize over variants** (share styles, color schemes, viewports) so
  a new variant inherits every invariant.
- **Canary new guards**: temporarily break the code under test, see red,
  restore with a targeted edit.
- Playwright globs: `*` does not cross `/`; use `**/api/articles*`.
- `fill()` does not fire `change`; press Tab after it.

## Hermetic tier conventions
- Never touch the network: monkeypatch `rssgate.fetcher._get` /
  `requests.get`; the `client` fixture stubs LLM methods and
  `web.refresh_feed` / `web.refresh_all`.
- **API tests write through `client.conn`.** The `conn` fixture is a
  different database file than the one the app serves.
- `db.add_feed` returns the inserted Row (`row["id"]`).
- Token discipline is proven with FakeLLM call counts; extend, never weaken.
- Image tests must use hash-shaped filenames (`sha256(...)[:24] + ".png"`):
  `imgstore.safe_path` rejects anything else, and release silently skips it.

## What tier 2 still cannot do (honest limits)

- No pixel-diff/visual regression (baseline screenshots are a future step;
  `page.screenshot()` hooks are trivial to add to any test).
- No real network conditions (throttling, DNS failures).
- Touch is synthesized, not human — feel (resistance curves) still needs a
  thumb on glass.
