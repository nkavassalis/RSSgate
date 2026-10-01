# RSSgate HTTP API

Base: the app origin (default `http://localhost:8088`). All bodies JSON.
Pages: `GET /` viewer, `GET /admin` admin panel, `GET /static/<file>` assets.

## Reader

| Method/Path | Description |
|---|---|
| `GET /api/articles` | Keyset-paginated reverse-chronological articles. Query params: `before_ts` (ISO, exclusive), `before_id`, `limit` (≤100), `feed_id`, `category`, `from_date` (YYYY-MM-DD alias of before_ts=23:59 of that day). Defaults to stored resume position. Returns `{items:[{id,title,link,summary,status,ts,feed_id,feed_title,feed_description,categories[],auto_categories[]}], has_more, next}`. |
| `POST /api/position` | Save resume position. Body `{ts, id}` (the oldest article card the user passed). |
| `GET /api/resume` | `{resume_ts, resume_id, newest_ts}` — client boot call. |

## Feeds

| Method/Path | Description |
|---|---|
| `GET /api/feeds` | List feeds incl. `categories[]`, `auto_categories[]`, `article_count`, `last_status`. |
| `POST /api/feeds` | Add. Body `{url, type?: auto\|feed\|page, categories?: [str], refresh?: bool}`. 400 bad url, 409 duplicate. |
| `PUT /api/feeds/<id>` | Update `{categories?: [str], enabled?: bool, type?: ...}`. |
| `DELETE /api/feeds/<id>` | Delete feed + its articles. |
| `POST /api/feeds/<id>/refresh` | Poll this feed immediately. |

## Categories

| Method/Path | Description |
|---|---|
| `GET /api/categories` | `[{name, count}]` — distinct user-assigned categories. |
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
| `GET /api/status` | `{feeds, pending, polling}`. |
| `POST /api/poll` | Refresh all enabled feeds in background. |

## Error shape
`{"error": "message"}` with 4xx/5xx status.
