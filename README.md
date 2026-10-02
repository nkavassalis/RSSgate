# RSSgate

A self-hosted RSS feed manager with an AI-transcribed reader. Runs as a local
Python web app (default `http://0.0.0.0:8088`), fully configured from a single
YAML file. Desktop & mobile responsive, follows your OS dark-mode setting
automatically.

Two sides:

- **Viewer** (`/`) — an endless reverse-chronological scroller that *resumes
  where you left off*, with a feed-filter sidebar, per-feed unread counts and
  a New/Since date toggle. Every article is replaced by an LLM transcription
  of the linked page: important text kept, advertising and boilerplate
  stripped. When you reach the end you get **"You've seen it all!"** plus a
  friendly date picker to jump back through time.
- **Admin** (`/admin`, gear icon top-right of the viewer) — add feeds (RSS/Atom
  XML *and* bare web pages), manage categories, pick providers/models, tune
  polling and summarization, and watch your token spend.

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml     # optional; run.py generates defaults
.venv/bin/python run.py                # serves on 0.0.0.0:8088
```

## Feed types

| Type | Examples | Behavior |
|---|---|---|
| `feed` | `https://gizmodo.com/feed`, `https://feeds.arstechnica.com/arstechnica/index`, `https://feeds.npr.org/1001/rss.xml`, `https://baka.jp/feed.xml` | Parsed with `feedparser`. Conditional GET (ETag/Last-Modified) + guid dedupe. Feed-declared `<category>` tags are captured and shown as chips. |
| `page` (bare web page) | `https://lite.cnn.com/` (text-only CNN, no RSS anywhere on the page) | No structured feed: the page's article links are extracted with an LLM and treated like feed items. Skipped entirely when the page bytes haven't changed. Gets its own, longer poll timer. |
| `auto` | any URL | Probed once; result stored. |

## Token efficiency (the point of the design)

- An article is summarized **exactly once**; guid-dedupe means existing
  articles are never re-processed.
- Before any summarization, the article text is extracted and hashed. If an
  identical digest already exists (`body_hash` cache) it is reused — **zero
  tokens**.
- Bare pages are fetched with conditional GET and content-hash comparison; the
  discovery LLM call is skipped when the page is unchanged.
- `summarizer.max_input_chars` caps prompt size.
- Token usage per call is logged; the admin panel shows today / month / all-time,
  plus an LLM performance panel: digest queue depth & peak high-water mark,
  avg/min/max seconds per article, estimated drain time, cache-hit count
  (digests reused for zero tokens) and failed-digest counter. Stats auto-refresh
  every 15 s.
- **Work queue** (admin): live view of what the LLM is digesting right now,
  what it processed recently (status, finish time, wall-clock duration, tokens
  in/out) and how deep the backlog is. `GET /api/workqueue`. Items stuck in
  `processing` (e.g. after a crash) are auto-requeued after 15 minutes.
- **Parallel digesting**: `summarizer.concurrency` (default 2) runs that many
  digest workers in parallel. Reasoning-model endpoints spend most of their
  latency on hidden thinking tokens, so parallelism beats waiting.

## LLM providers

`llm.provider` in `config.yaml` (or the admin panel):

| provider | base_url | notes |
|---|---|---|
| `local` | e.g. `http://10.1.13.99:8000/v1` | Any OpenAI-compatible server. If it serves exactly **one model**, it is auto-selected; otherwise pick one in the admin panel. |
| `openai` | `https://api.openai.com/v1` | needs `api_key` |
| `openrouter` | `https://openrouter.ai/api/v1` | needs `api_key` |
| `anthropic` | – (Messages API) | needs `api_key` |

`llm.api_key` supports `env:VARNAME` so secrets can stay out of the file.
The key is never echoed to the browser (the API returns `***`).

### Tuning thinking tokens (local reasoning servers)

Digesting is extractive work — it does not benefit from chain-of-thought, and
thinking tokens dominate both cost and latency. For `provider: local`, RSSgate
by default sends `chat_template_kwargs: {enable_thinking: false}` with every
chat request (vLLM/SGLang-style servers), which on our hardware cut a gizmodo
digest from **51.8 s / 3,127 output tokens to 7.4 s / 405** with equal quality.
Set `llm.extra_body: {}` if your server rejects the parameter. Per-purpose
model overrides (`llm.model_summarize`, `llm.model_discover`) let a cheap
instruct model do the digesting while a reasoning model handles bare-page
discovery, when you have more than one model available.

## Reader UI: sidebar, New/Since modes

The viewer has a left sidebar (a slide-out drawer under 800 px):

- **Feeds list** — click a feed to filter the stream to it; “All feeds” to
  clear. Selection persists in the browser (localStorage), as do the mode and
  date. Resume-position saving only applies to the unfiltered New view.
- **New / Since toggle** — *New* is the classic flow: resume where you left off
  and scroll into the past until “You’ve seen it all!”. *Since* puts a floor
  under the stream: it shows everything from your chosen date onward and
  politely stops there (“That’s everything since 2026-09-25”). Preset chips
  (24h / 7d / 30d / 90d) make the common cases one tap; the “Jump to date”
  picker on the end banner simply switches into Since mode — one mental model.

## Categories

Categories appear as chips on each article card, in three flavors:

- **Post categories** (solid chips, hover: “post categories”) — the category
  tags the *entry itself* carries in the feed XML, stored per article
  (`articles.categories`) at fetch time. Shown first when present.
- **Feed categories** (same chips, hover: “feed categories”) — the feed-declared
  union (`<category>`/`itunes:category`/`dc:subject` at channel level, else the
  union over recent entries). Used as fallback for articles that carry no tags
  of their own, and for bare-page items (which never have tags).
- **Your categories** (filled accent chips) — user-assigned per feed, managed
  entirely from the admin panel: assign per feed, and globally **create /
  rename / remove**. Category filtering matches post tags, your feed tags, or
  the feed union.

## Feed discovery when the URL isn't a feed

In the admin panel you can paste *any* URL (a homepage, a newsroom page).
Clicking **Add feed** first probes it (`POST /api/feeds/probe`, no LLM):

1. URL itself parses as RSS/Atom → added directly.
2. Not a feed → the HTML is sniffed for `<link rel="alternate">` declarations
   (trusted), feed-ish links (`/rss`, `/feed.xml`, anchors labeled "RSS"),
   hosts named `feeds.`/`rss.` (subdomain heuristic — finds Ars Technica's
   real feeds from its info page), and finally well-known paths
   (`/feed`, `/rss.xml`, `/feed.xml`, `/atom.xml`). **Every heuristic
   candidate is verified by fetching it and parsing as XML** before being
   offered — so when RSSgate says "found a feed", it is one.
3. Nothing found → you're asked to confirm adding the URL as a **bare page**
   (LLM article discovery), or cancel. A bare page is never added silently.

## Raw mode (per feed, zero LLM tokens)

Some feeds don't need an AI digest — a plain blog or a text-only site is
already clean reading. Uncheck **LLM** for a feed in the admin panel (or
`{"summarize": false}` to `PUT /api/feeds/<id>`) and RSSgate still does
everything that matters for free: fetches the page, strips ads/chrome with the
extractor, dedupes by content hash, and shows the cleaned text in the reader
with a dashed `raw` chip. Re-checking the box re-queues that feed's raw items
for proper digesting. Sponsored-filter behavior applies in both modes.

## Sponsored content filter (per feed)

Off by default. Tick **Ad filter** on a feed in the admin panel (or send
`{"hide_sponsored": true}` to `PUT /api/feeds/<id>`) and sponsored/sale posts
never enter your reader — and never reach the LLM either:

1. **Free pre-filter**: at digest time the item's title/link is checked against
   heuristics (`sponsored`, `now up to N% off`, `deal of the day`, `prime day`,
   `/sponsored/` URL paths…). Matches are marked `hidden` before the page is
   even fetched — zero tokens.
2. **Zero-cost second pass**: if the digest text itself starts by calling the
   piece a sponsored/promotional post, the article is hidden too (the digest is
   kept in the DB for audit).

Hidden articles are excluded from the viewer and stats; un-ticking the flag
re-queues them. Detection is intentionally conservative — it errs toward
*showing* articles.

## Endless reader, resume & read state

The stream **always opens at the newest article** — "caught up" means what you
see. Three layers of state:

- **Continue reading** — your deepest scroll position `(ts, id)` is stored
  server-side (`POST /api/position` via `sendBeacon`). When it's older than
  the newest article, a "⤓ Continue reading from <date>" button appears to
  drop you back where you left off (`GET /api/resume` exposes it; `?fresh=1`
  on `/api/articles` is the at-newest opt-out of the resume bound).
- **Per-feed read cursors** (`feeds.last_read_ts`) — each read beacon sends
  `reads: {feed_id: newest_passed_card_ts}`, so every feed's cursor advances
  at the exact card you passed, in filtered *and* mixed views. The sidebar
  shows an unread pill per feed; unpassed cards carry a blue dot + accent
  bar and calm down once read.
- **Client prefs** — mode (New/Since), since-date and feed filter live in
  localStorage; view preferences, not read state.

Since mode floors the stream at `GET /api/articles?since_ts=…`; the date
picker on the end banner simply switches into Since mode.

## Development & tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest        # 84 tests, no network required
```

See [docs/API.md](docs/API.md) for the HTTP surface,
[AGENTS.md](AGENTS.md) for an agent-oriented map of the codebase, and
[CHANGELOG.md](CHANGELOG.md) for release history and the versioning policy
(major/minor/patch criteria — bump `rssgate.__version__` and tag every
release).

## Versioning

Current: **v1.0.0** — see [CHANGELOG.md](CHANGELOG.md) for the policy and
history. Tags follow `vX.Y.Z`.

## Security note

RSSgate has **no authentication** and binds `0.0.0.0` by default — it is meant
for trusted networks. Set `server.host: 127.0.0.1` in `config.yaml` or put it
behind a reverse proxy with auth if others can reach the port. The admin panel
(and the LLM spend behind it) is open to anyone who can reach the port.

## License

MIT — see [LICENSE](LICENSE).
