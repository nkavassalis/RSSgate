# RSSgate

A self-hosted RSS feed manager with an AI-transcribed reader. Runs as a local
Python web app (default `http://0.0.0.0:8088`), fully configured from a single
YAML file. Desktop & mobile responsive, follows your OS dark-mode setting
automatically.

Two sides:

- **Viewer** (`/`) — an endless reverse-chronological scroller that *resumes
  where you left off*. Every article is replaced by an LLM transcription of the
  linked page: important text kept, advertising and boilerplate stripped. When
  you reach the end you get **"You've seen it all!"** plus a friendly date
  picker to jump back through time.
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
| `feed` | `https://gizmodo.com/feed`, `https://baka.jp/feed.xml` | Parsed with `feedparser`. Conditional GET (ETag/Last-Modified) + guid dedupe. Feed-declared `<category>` tags are captured and shown as chips. |
| `page` (bare web page) | `https://arstechnica.com/rss-feeds/` | No structured feed: the page's article links are extracted with an LLM and treated like feed items. Skipped entirely when the page bytes haven't changed. Gets its own, longer poll timer. |
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
- Token usage per call is logged; the admin panel shows today / month / all-time.

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

## Categories

Feeds can be categorized two ways, both displayed as chips next to the feed
title in the viewer:

- **Feed-declared** — `<category>`/`itunes:category`/`dc:subject` tags found in
  the feed itself (read-only, refreshed with each poll).
- **User-assigned** — managed entirely from the admin panel: assign per feed,
  and globally **create / rename / remove** categories.

## Endless reader & resume

The viewer requests pages from `GET /api/articles`; your furthest-seen position
is stored server-side (`POST /api/position`, sent via `sendBeacon`) and the next
visit continues from there. `GET /api/resume` exposes it. After the first load,
scrolling hits the end banner where a date picker loads anything published on
or before the chosen date.

## Development & tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest        # 45 tests, no network required
```

See [docs/API.md](docs/API.md) for the HTTP surface and
[AGENTS.md](AGENTS.md) for an agent-oriented map of the codebase.
