# RSSgate HTTP API

Base: the app origin (default `http://localhost:8088`). All bodies JSON.
Pages: `GET /` viewer, `GET /admin` admin panel, `GET /static/<file>` assets.

## Reader

| Method/Path | Description |
|---|---|
| `GET /api/articles` | Keyset-paginated reverse-chronological articles. Query params: `before_ts` (ISO, exclusive *or* inclusive-newer in oldest mode), `before_id`, `limit` (≤100), `feed_id`, `category` (post tags) / `feed_category` (your feed labels) (repeatable; multi = OR; exact tag membership), `since_ts` (ISO floor, inclusive — powers “Since” mode; `from_date` YYYY-MM-DD accepted as sugar), `order` (`newest`|`oldest`, defaults to configured `ui.order`). Defaults to stored resume position when no cursor given (back-compat); `fresh=1` opts out and starts at newest - the viewer always sends it on boot. Returns `{items:[{id,title,link,summary,status,ts,feed_id,feed_title,feed_description,feed_summarize,categories[],auto_categories[],post_categories[], image, gallery[]}], has_more, next}`. |
| `POST /api/position` | Save read state. Body `{ts, id, reads?, global}` — `reads` is a `{feed_id: ts}` map advancing per-feed read cursors precisely; `global:true` also moves the New-view resume cursor (legacy `{ts,id}` or `{feeds:[ids]}` payloads still work). Resume cursor bounds only the unfiltered New first page; filtered/since views always start at newest. |
| `GET /api/resume` | `{resume_ts, resume_id, newest_ts, order}` — client boot call. |

## Feeds

| Method/Path | Description |
|---|---|
| `GET /api/feeds` | List feeds incl. `categories[]`, `auto_categories[]`, `article_count`, `last_status`. |
| `POST /api/feeds` | Add. Body `{url, type?: auto\|feed\|page, categories?: [str], refresh?: bool}`. 400 bad url, 409 duplicate. |
| `POST /api/feeds/probe` | Given any URL, find the real feed: `{type: feed\|page\|unknown\|error, candidates:[{url,title}], page_title?, error?}`. Heuristic candidates are verified (fetched + XML-parsed) before being returned; `<link rel=alternate>` declarations trusted as-is. No LLM used. |
| `GET /api/feeds/<id>/categories` | Per-feed category census: `[{name, count, allowed}]` (post tags + feed-declared, incl. hidden). |
| `PUT /api/feeds/<id>` | Update `{categories?: [str], enabled?: bool, type?: ..., hide_sponsored?: bool, summarize?: bool}`. `summarize: false` = raw mode (extracted text, no LLM); turning it back on re-queues raw items. Clearing `hide_sponsored` un-hides that feed's hidden articles. | Accepts `categories`, `enabled`, `summarize`, `hide_sponsored`, `digest_length` (default|terse|normal|detailed), `category_block` (list of blocked post categories; hides pre-LLM, retroactive).
| `DELETE /api/feeds/<id>` | Delete feed + its articles. |
| `POST /api/feeds/<id>/refresh` | Poll this feed immediately. |

## Categories

| Method/Path | Description |
|---|---|
| `GET /api/categories` | Admin scope: user-assigned categories with feed counts. `?viewer=1` returns the full union (post + user + feed-declared) with article counts — powers the sidebar chips. | `[{name, count}]` — distinct user-assigned categories. |
| `POST /api/categories/rename` | `{from, to}` — rename (implicitly creates `to`). Feeds that already had both are deduped. |
| `DELETE /api/categories/<name>` | Detach category from every feed (creating nothing). |

New categories are created simply by assigning them to a feed.

## Admin

| Method/Path | Description |
|---|---|
| `GET /api/config` | Full config, `llm.api_key` masked to `***` + `api_key_set` bool. |
| `PUT /api/config` | Deep-merge patch, persisted to `config.yaml`. `server`/`ui` sections read-only over HTTP; sending `api_key: "***"` leaves the key unchanged. `{length}` placeholder auto-appended to system prompt if missing. |
| `GET /api/models` | Models from the configured provider (`[]` + error for Anthropic). |
| `POST /api/llm/test` | Round-trip "Reply with exactly: OK" probe; usage is logged. |
| `GET /api/usage` | `{today, month, all_time}` total tokens. |
| `GET /api/llm/stats` | Performance snapshot: `{queue, queue_peak, errors, cache_hits, calls_today, tokens_today, avg_seconds, min_seconds, max_seconds, est_drain_minutes, last_call_ts}`. |
| `GET /api/workqueue` | Live work queue: `{current:[{id,title,started_at,feed_title,link}], recent:[{id,title,status,summarized_at,llm_ms,tokens_in,tokens_out,feed_title}], working, queue_ahead}`. |
| `GET /api/maintenance` | `{report, config}` — last maintenance run + retention/cache settings. |
| `POST /api/maintenance/run` | Run maintenance synchronously (retention delete, orphan prune, cache cap) and return the report. |
| `POST /api/images/backfill` | Background image backfill/enrichment over existing articles (missing heroes AND hero-only rows lacking galleries). Body `{"force": true}` re-extracts from article pages (repairs poisoned metadata); optional `limit`, `page_fetches` tune the budget. |
| `GET /image/<name>` | Serve a locally cached article image. Only hash-named cache files (`[0-9a-f]{24}.(jpg|png|webp|gif)`) resolve; everything else 404s. Immutable cache headers. |

| `GET /api/status` | `{version, feeds, pending, polling}`. |
| `POST /api/poll` | Refresh all enabled feeds in background. |

## Error shape
`{"error": "message"}` with 4xx/5xx status.
