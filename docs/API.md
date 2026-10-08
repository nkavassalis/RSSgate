# RSSgate HTTP API

Base: the app origin (default `http://127.0.0.1:8088`). Bodies are JSON.
Pages: `GET /` viewer, `GET /admin` admin, `GET /static/<file>?v=<version>`
assets (the `?v=` cache-buster is stamped by the templates on every release).
Errors: `{"error": "message"}` with a 4xx/5xx status. Endpoints the admin page
calls on load must degrade to JSON errors, never raise (a down LLM must not
break the panel).

## Reader

| Method / path | Description |
|---|---|
| `GET /api/articles` | Keyset-paginated articles. Params: `before_ts` + `before_id` (cursor; strictly older in newest mode, strictly newer in oldest mode), `limit` (<=100), `feed_id`, `category` (post tags) / `feed_category` (your feed labels) - repeatable, multi = OR, exact membership, `since_ts` (inclusive floor; `from_date=YYYY-MM-DD` sugar), `order` (`newest`\|`oldest`, default `ui.order`), `prio=1` (unread first; cursor becomes `before_u, before_ts, before_id`), `fresh=1` (start at newest; the viewer always sends it - without it the legacy resume cursor bounds the first page). Hidden statuses and (with `ui.hide_untranscribed`) pending/processing/error never appear. Returns `{items:[{id,title,link,summary,status,ts,unread,feed_id,feed_title,feed_description,feed_summarize,categories[],auto_categories[],post_categories[],image,gallery[],digest_source}], has_more, next}`. `digest_source` is `""` (article page), `"excerpt"` (forced fallback to the feed's text) or `"feed"` (the feed is set to feed text only). `image`/`gallery` honour the feed's `images_mode` at read time. |
| `POST /api/position` | Read-state beacon `{ts, id, reads?: {feed_id: ts}, global?: bool}`. Per-feed cursors advance forward only; `global:true` (New + all feeds) also moves the resume cursor. |
| `GET /api/resume` | Viewer boot: `{resume_ts, resume_id, newest_ts, order, snapshot_width, stream_width, read_delay}` (`snapshot_width` = the WIDE share card's width; the tall card is fixed at 640). |
| `POST /api/poll` | Poll all enabled feeds now in a background thread (own DB connection), throttled to once per 60 s: `{ok, started, throttled?, why?}`. The viewer skips its settle wait when `throttled` is true. |
| `GET /api/qr.png?u=<url>` | Local QR PNG for http(s) URLs (<=500 chars; 400 otherwise). Used by share cards. |
| `GET /image/<name>` | Cached image. Only hash names `[0-9a-f]{24}.(jpg\|png\|webp\|gif)` resolve. A missing hero is re-fetched from the article's `image_url` and hash-verified (name = sha256(url)[:24]) before serving. Immutable cache headers. |
| `GET /api/status` | `{version, uptime_min, feeds, feeds_enabled, pending, processing, errors, db_mb, cache_mb, llm_down_since, llm_down_reason, llm_next_try, polling}`. `llm_down_since` is non-null while the AI backend is unreachable and the queue is held. Powers the admin Status panel and the viewer's queue/failure pips. |
| `GET /api/pulse` | `{newest_ts, ready_total, unread_total, unread_since_total, since_ts, every_minutes, feeds:[{feed_id,title,unread,unread_since,ts}]}`. READ-ONLY "anything waiting?" for the reader's pill: it never fetches feeds (pacing rule). `unread_total` mirrors the sidebar pills exactly (ready+error past the feed cursor, never pending/processing). `unread_since_total` is the pill's own number: those same posts restricted to ones newer than the reader's last-read marker (`state.resume_ts`, or the feed cursor when later); `since_ts` echoes the marker used, so the pill can say "since your last read" only when that is true. `feeds` is sorted by `unread_since` desc so `feeds[0]` is the feed with most to see. |

## Feeds

| Method / path | Description |
|---|---|
| `GET /api/feeds` | All feeds with `categories[]`, `auto_categories[]`, counts (`article_count`, `ready_count`, `unread`, `hidden_count`), `last_status`, settings (`summarize`, `hide_sponsored`, `images_mode`, `digest_length`, `system_prompt`, `max_input_chars`, `sync_deletes`, `smart_block` 0 untouched / 1 auto-seeded / 2 user-owned), site-block state `backoff_until` (ISO or null) / `backoff_level`, `content_source` (auto\|feed\|page), `excerpt_count` (posts that fell back to the feed's text), `upgrade_pending` (excerpt posts with a page retry scheduled), `challenge_streak` / `page_probe_at` (Auto's bot-check streak and next allowed page probe). |
| `POST /api/feeds` | Add `{url, type?: auto\|feed\|page, categories?: [str], refresh?: bool}`. 400 bad URL, 409 duplicate. |
| `POST /api/feeds/probe` | Find the real feed for any URL: `{type: feed\|page\|unknown\|error, candidates:[{url,title}], page_title?, error?}`. Candidates are verified by fetching; no LLM. |
| `PUT /api/feeds/<id>` | Field-wise patch; only keys present change: `categories`, `enabled`, `custom_title`, `summarize` (false = raw mode), `hide_sponsored` (true sweeps existing items to hidden; false re-queues them), `images_mode` (auto\|hero\|off), `digest_length` (default\|terse\|normal\|detailed), `system_prompt`, `max_input_chars`, `sync_deletes`, `category_block` (list; hides pre-LLM, retroactive), `content_source` (auto\|feed\|page). Never re-digests by itself. |
| `PUT /api/feeds/<id>` `{unpause: true}` | Clear a site-block pause (`backoff_until`/`backoff_level`) and try the site again. |
| `DELETE /api/feeds/<id>` | Delete feed, its articles, and images only they used. |
| `POST /api/feeds/<id>/refresh` | Poll one feed now. |
| `POST /api/feeds/<id>/redigest` | User-confirmed re-processing with cleared hashes: `{ok, requeued}`. |
| `GET /api/feeds/<id>/categories` | Per-feed category census `[{name, count, allowed}]`. |

## Categories

| Method / path | Description |
|---|---|
| `GET /api/categories` | Admin scope: user labels with feed counts `[{name, count}]`. `?viewer=1`: filterable names (post tags + user labels) with live article counts for the sidebar chips. |
| `POST /api/categories/rename` | `{from, to}` rename/merge across feeds (case-insensitive, deduped). |
| `DELETE /api/categories/<name>` | Remove the label from every feed. |

## Processing and maintenance

| Method / path | Description |
|---|---|
| `GET /api/workqueue` | `{current:[...], recent:[...], working, queue_ahead}`. |
| `GET /api/feed-errors` | Latest failed articles `[{feed_title,title,link,error_msg}]`; the reason is always recorded. |
| `POST /api/articles/retry-failed` | Re-queue every failed article with a fresh attempt budget: `{requeued}`. |
| `POST /api/articles/<id>/retry` | Re-queue a failed article, reset attempts. |
| `POST /api/articles/<id>/drop` | Hide a failed article permanently. |
| `POST /api/articles/clear-failed` | Delete every `error` article and release images only they used: `{deleted, images_released}`. |
| `GET /api/maintenance` | `{report, config}` of the last maintenance run. |
| `POST /api/maintenance/run` | Run now (retention, orphan prune, gallery dedupe repair, cache cap); returns the report. |
| `POST /api/images/backfill` | Background image backfill. `{force?: true, limit?, page_fetches?}`. |

## Config and LLM

| Method / path | Description |
|---|---|
| `GET /api/config` | Full config; `llm.api_key` masked to `***` plus `api_key_set`. |
| `PUT /api/config` | Deep-merge patch persisted to `config.yaml`. `server` is file-only. `ui` accepts only validated keys: `order`, `snapshot_width` (360-1440), `stream_width` (480-1600), `hide_untranscribed`, `read_delay` (0-60). `fetch` accepts `user_agent` (printable, <=400 chars; "" = default), `per_host_interval` (0-60 s), `block_backoff_minutes` (1-1440); applied live. `api_key: "***"` keeps the key. `{length}` is appended to a system prompt that lacks it. |
| `GET /api/models` | Provider model list (`[]` + error when unavailable). |
| `POST /api/llm/test` | "Reply with exactly: OK" round trip; usage logged. |
| `GET /api/usage` | `{today, month, all_time}` tokens. |
| `GET /api/llm/stats` | `{queue, queue_peak, errors, cache_hits, calls_today, tokens_today, avg_seconds, min_seconds, max_seconds, est_drain_minutes, last_call_ts}`. |
