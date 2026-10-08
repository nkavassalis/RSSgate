#!/bin/sh
# Add this example feed to a running RSSgate with its settings.
#   ./add-feed.sh                      # RSSgate on http://127.0.0.1:8088
#   RSSGATE_URL=https://box.tailnet.ts.net ./add-feed.sh
set -eu
BASE="${RSSGATE_URL:-http://127.0.0.1:8088}"
DIR="$(cd "$(dirname "$0")" && pwd)"
python3 - "$BASE" "$DIR/feed.json" <<'PY'
import json, sys, urllib.request

base, path = sys.argv[1].rstrip("/"), sys.argv[2]
spec = json.load(open(path))

def call(method, url, body):
    req = urllib.request.Request(base + url, method=method,
                                 data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {url} failed: {e.code} {e.read().decode()[:200]}")

feed = call("POST", "/api/feeds", {"url": spec["url"], "type": spec["type"],
                                   "categories": spec.get("categories", [])})
call("PUT", f"/api/feeds/{feed['id']}", spec.get("settings", {}))
print(f"added feed {feed['id']}: {spec['url']}")
PY
