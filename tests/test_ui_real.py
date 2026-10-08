"""Real-browser UI tests (Playwright + headless Chromium).

Why this file exists: for six weeks the hermetic suite verified strings and
schemas while three separate USER-VISIBLE bugs (modal veil beating
[hidden], an unstyled .readmore link, a PTR div that became a flex item
breaking mobile layout) sailed through green CI. Text greps cannot see
pixels. This tier boots the actual app against a throwaway database and
drives a real browser: computed styles, element geometry, synthesized
touch gestures, console errors.

Run explicitly:  .venv/bin/python -m pytest -m ui
Auto-skips when playwright/chromium is not installed.
"""
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui

try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except ImportError:  # pragma: no cover
    HAVE_PW = False

PORT = 8977
ROOT = Path(__file__).resolve().parents[1]

CONFIG = f"""
server:
  host: 127.0.0.1
  port: {PORT}
  data_dir: /tmp/rssgate-ui-test/data
llm:
  provider: local
  base_url: http://127.0.0.1:9/v1
polling:
  feed_interval_minutes: 9999
  page_interval_minutes: 9999
  fetch_on_start: false
troubleshooting:
  log_llm_failures: true
"""


def _wait_port(port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.25)
    raise RuntimeError("ui test server did not start")


UI_DB: str = ""
UI_IMG_DIR: Path | None = None


@pytest.fixture(scope="session")
def ui_server(tmp_path_factory):
    """Boot the real app (run.py) with its own config/data; seed content."""
    if not HAVE_PW:
        pytest.skip("playwright not installed")
    data = Path("/tmp/rssgate-ui-test")
    if data.exists():
        subprocess.run(["rm", "-rf", str(data)], check=False)
    (data / "data").mkdir(parents=True, exist_ok=True)
    cfg = data / "config.yaml"
    cfg.write_text(CONFIG)
    try:                                          # evict zombie servers
        subprocess.run(["fuser", "-k", f"{PORT}/tcp"], capture_output=True)
    except FileNotFoundError:
        tag = "rssgate-ui" + "-test"     # split so pkill never matches us
        subprocess.run(["pkill", "-f", tag], capture_output=True)
    time.sleep(0.5)
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "run.py"), "--config", str(cfg)],
        cwd=str(ROOT), stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    try:
        _wait_port(PORT)
        sys.path.insert(0, str(ROOT))
        from rssgate import db
        conn = db.connect(str(data / "data" / "rssgate.sqlite"))
        db.init_db(conn)                              # idempotent, race-proof
        fid = db.add_feed(conn, "https://ui.test/feed", type_="feed",
                          title="UI Test Feed")["id"]
        aid = db.upsert_article(conn, fid, "u1", "https://ui.test/post-1",
                                "A Real Headline For Pixels", None,
                                categories=["testing"])
        db.set_article(conn, aid, status="ready",
                       summary="A digest that definitely exists.")
        for extra_i, extra_guid in enumerate(("u2", "u3"), start=2):
            eid = db.upsert_article(
                conn, fid, extra_guid, f"https://ui.test/post-{extra_i}",
                f"Extra post {extra_i}", "2024-01-0" + str(extra_i) + "T10:00:00Z")
            db.set_article(conn, eid, status="ready",
                           summary="Filler digest for scroll tests.")

        db.set_article(conn, aid, image=None)
        # second feed left disabled on purpose (layout + poll semantics)
        db.update_feed(conn, fid, summarize=1)
        conn.close()
        global UI_DB, UI_IMG_DIR
        UI_DB = str(data / "data" / "rssgate.sqlite")
        UI_IMG_DIR = data / "data" / "images"
        UI_IMG_DIR.mkdir(parents=True, exist_ok=True)
        yield f"http://127.0.0.1:{PORT}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture()
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _new_page(browser, **ctx_kw):
    ctx = browser.new_page(**ctx_kw)
    ctx.on("pageerror", lambda e: ctx.errors.append(str(e)))
    ctx.errors = []
    return ctx


# ---------------------------------------------------------------- helpers
# The browser tier shares ONE server + DB per session. Tests that need data
# must create their OWN feed via _mkfeed() (unique URL per call) and query by
# feed_id - never rely on global counts or another test's seed.

RAIL_RECORDER = """
  window.__rail = [];
  new MutationObserver(rs => { for (const r of rs)
    if (r.type === 'attributes' && r.target.id === 'stream-progress')
      window.__rail.push([r.target.className, performance.now()]);
  }).observe(document, { childList: true, subtree: true,
                         attributes: true, attributeFilter: ['class'] });
"""   # attach at document-start: DOMContentLoaded races the boot fetch

_feed_seq = [0]


def _mkfeed(prefix: str = "t", url: bool = False):
    """Fresh, uniquely-addressed feed in the session DB; returns its id
    (or (id, url) when url=True, for tests that must recognise their own
    feed among the tier's shared data)."""
    from rssgate import db
    _feed_seq[0] += 1
    link = f"https://{prefix}-{_feed_seq[0]}-{time.time_ns()}.test/feed"
    conn = db.connect(UI_DB)
    fid = db.add_feed(conn, link, type_="feed")["id"]
    conn.commit(); conn.close()
    return (fid, link) if url else fid


def _seed_article(fid: int, guid: str, title: str = "probe",
                  ts: str = "2027-01-01T00:00:00Z", status: str = "ready",
                  summary: str = "probe digest", image: str | None = None,
                  link: str | None = None) -> int:
    from rssgate import db
    conn = db.connect(UI_DB)
    aid = db.upsert_article(conn, fid, guid,
                            link or f"https://x.test/{fid}/{guid}", title, ts)
    conn.execute("UPDATE articles SET status=?, summary=? WHERE id=?",
                 (status, summary, aid))
    if image:
        db.set_article(conn, aid, image=image)
    conn.commit(); conn.close()
    return aid


def _feed_article(pg, fid: int) -> dict:
    """The (first) API card for a feed, fetched in-page."""
    art = pg.evaluate("""(fid) => fetch('/api/articles?feed_id=' + fid +
        '&fresh=1&limit=50').then(r => r.json())
        .then(d => d.items.find(i => i.feed_id === fid))""", fid)
    assert art, f"feed {fid} has no visible article"
    return art


# In-page share renderer: returns layout geometry (window.__lastShareGeo)
# plus pixel stats for named zones. zones = {name: [x, y, w, h]} in LOGICAL
# card px (DPR handled here); each zone reports dark/accent/orange/blue counts.
SHARE_PROBE = """async ([a, zones]) => {
  const png = await window.__renderCardPng(a);
  const geo = window.__lastShareGeo || {};
  const bmp = await createImageBitmap(png);
  const cv = document.createElement('canvas');
  cv.width = bmp.width; cv.height = bmp.height;
  const g = cv.getContext('2d'); g.drawImage(bmp, 0, 0);
  const D = geo.dpr || 2, out = { geo, w: bmp.width / D, h: bmp.height / D };
  for (const [k, [x, y, w, h]] of Object.entries(zones || {})) {
    const z = g.getImageData(Math.round(x * D), Math.round(y * D),
                             Math.max(1, Math.round(w * D)),
                             Math.max(1, Math.round(h * D))).data;
    const s = { dark: 0, accent: 0, orange: 0, blue: 0, n: z.length / 4 };
    for (let i = 0; i < z.length; i += 4) {
      const r = z[i], gg = z[i+1], b = z[i+2];
      if (r < 110 && gg < 110 && b < 110) s.dark++;
      if (b > 200 && r < 190 && gg < 160) s.accent++;
      if (r > 200 && gg > 100 && gg < 170 && b < 60) s.orange++;
      if (b > 200 && r < 60 && gg < 60) s.blue++;
    }
    out[k] = s;
  }
  return out;
}"""


def _share(pg, art: dict, zones: dict | None = None) -> dict:
    return pg.evaluate(SHARE_PROBE, [art, zones or {}])


def test_desktop_renders_with_real_ink(ui_server, browser):
    """Cards visible, readmore link actually colored (not default blue),
    zero page errors, honest document title."""
    pg = _new_page(browser, viewport={"width": 1280, "height": 900},
                   color_scheme="light")
    pg.goto(ui_server, wait_until="networkidle")
    assert pg.title() == "RSSgate"
    assert pg.locator(".card").count() >= 1
    color = pg.eval_on_selector(".readmore a",
                               "el => getComputedStyle(el).color")
    assert color == "rgb(63, 65, 71)", f"light readmore: {color}"
    # dark scheme flips it to white
    pg2 = _new_page(browser, viewport={"width": 1280, "height": 900},
                    color_scheme="dark")
    pg2.goto(ui_server, wait_until="networkidle")
    assert pg2.eval_on_selector(".readmore a",
                                "el => getComputedStyle(el).color") \
        == "rgb(255, 255, 255)"
    assert pg.errors == [] and pg2.errors == []
    pg.close(); pg2.close()


def test_mobile_layout_has_no_phantom_flex_gap(ui_server, browser):
    """The v0.31.0 incident: an overlay div riding the .layout flex row
    pushed the stream right. Geometry can't lie."""
    pg = _new_page(browser, viewport={"width": 390, "height": 844},
                   has_touch=True)
    pg.goto(ui_server, wait_until="networkidle")
    box = pg.locator("#stream").bounding_box()
    assert box["x"] <= 2, f"stream offset x={box['x']}px (phantom sibling?)"
    assert box["width"] >= 360, f"stream squeezed to {box['width']}px"
    # the PTR pill must be out of the flex flow entirely
    pos = pg.eval_on_selector("#ptr", "el => getComputedStyle(el).position")
    assert pos == "fixed"
    assert pg.errors == []
    pg.close()


def test_pull_to_refresh_gesture_drives_state(ui_server, browser):
    """Synthesized touch: pill visible while dragging, arms at threshold;
    release retreats the pill and hands the work indication to the top
    rail (v0.44.3 - rail is the single work indicator on all refresh
    paths)."""
    pg = _new_page(browser, viewport={"width": 390, "height": 844},
                   has_touch=True)
    def slow_poll(route):
        time.sleep(0.8)
        route.continue_()
    pg.route("**/api/poll", slow_poll)
    poll_calls = []
    pg.on("request", lambda r: poll_calls.append(r.url)
          if r.url.endswith("/api/poll") else None)
    pg.goto(ui_server, wait_until="networkidle")
    state = pg.evaluate("""() => {
      const t = (y) => new Touch({identifier: 1, target: document.body,
                                  clientY: y});
      const fire = (type, y) => window.dispatchEvent(new TouchEvent(type, {
        touches: type === 'touchend' ? [] : [t(y)],
        changedTouches: [t(y)], bubbles: true, cancelable: true }));
      const ptr = document.getElementById('ptr');
      const mid = {};
      fire('touchstart', 10);
      fire('touchmove', 110);                    // ~50px after resistance
      mid.opacity_mid = ptr.style.opacity;
      fire('touchmove', 260);                    // past threshold
      mid.opacity_armed = ptr.style.opacity;
      mid.armed = ptr.classList.contains('ready');
      fire('touchend', 260);
      return mid; }""")
    assert float(state["opacity_mid"]) > 0.5, "pill invisible while dragging"
    assert float(state["opacity_armed"]) > 0.9
    assert state["armed"] is True, "never armed at threshold"
    # pill retreats; the rail lights instead (work phase)
    pg.wait_for_selector("#stream-progress.on", timeout=3000)
    assert pg.eval_on_selector("#ptr", "el => el.style.opacity") == "0"
    assert poll_calls, "release did not POST /api/poll"
    assert pg.errors == []
    pg.close()


def test_admin_modal_is_hidden_until_summoned(ui_server, browser):
    """The v0.25.1 incident class, verified in pixels this time."""
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server + "/admin", wait_until="networkidle")
    assert pg.title() == "RSSgate Admin"
    assert pg.locator("#rd-modal").is_hidden()
    disp = pg.eval_on_selector("#rd-modal",
                               "el => getComputedStyle(el).display")
    assert disp == "none"
    assert pg.errors == []
    pg.close()


def test_status_api_and_app_agree(ui_server, browser):
    pg = _new_page(browser)
    pg.goto(ui_server)
    status = json.loads(pg.evaluate(
        "() => fetch('/api/status').then(r => r.text())"))
    assert status["feeds"] >= 1 and "version" in status
    pg.close()


def test_inline_category_add_persists(ui_server, browser):
    """The reported bug: chips appeared but PUT failed silently (no
    try/catch meant stale DOM masquerading as success). Add a feed, give
    it an inline category via the prompt flow, Save, then verify against
    BOTH the re-rendered UI and the API - and after a full reload."""
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.on("dialog", lambda d: d.accept("inbox"))
    pg.goto(ui_server + "/admin", wait_until="networkidle")
    pg.fill("#new-url", "https://inline.test/feed")
    pg.select_option("#new-type", "feed")
    pg.click("#add-feed-btn")
    pg.wait_for_selector("tr[data-id]", timeout=5000)
    fid = pg.eval_on_selector("tr[data-id]", "el => el.dataset.id")
    row = pg.locator(f"tr[data-id='{fid}']")
    row.locator("select[data-role=catadd]").select_option("__new")
    row.locator(".chip.cat", has_text="inbox").wait_for()
    # autosave: no Save click needed; wait for the saved-flash class
    pg.wait_for_selector("td.cats.saved", timeout=5000)
    # chips must survive the post-save re-render (which refetches the server)
    assert row.locator(".chip.cat", has_text="inbox").count() == 1, \
        "chip vanished after save = not persisted"
    api_cats = pg.evaluate("() => fetch('/api/feeds').then(r => r.json())"
                           ".then(f => f.find(x => x.id === "
                           f"{fid}).categories)")
    assert api_cats == ["inbox"]
    pg.reload(wait_until="networkidle")                 # fresh page, fresh JS
    assert pg.locator(".chip.cat", has_text="inbox").count() == 1
    # chip REMOVAL must autosave too (same lie otherwise)
    row.locator(".chip.cat b").first.click()
    pg.wait_for_selector("td.cats.saved", timeout=5000)
    api_cats = pg.evaluate("() => fetch('/api/feeds').then(r => r.json())"
                           ".then(f => f.find(x => x.id === "
                           f"{fid}).categories)")
    assert api_cats == []
    assert pg.errors == []
    pg.close()


def test_edge_swipe_opens_and_closes_drawer(ui_server, browser):
    """Left-edge swipe reveals the drawer tracking the finger; left swipe
    over the veil closes it. Vertical drags must not trigger it."""
    pg = _new_page(browser, viewport={"width": 390, "height": 844},
                   has_touch=True)
    pg.goto(ui_server, wait_until="networkidle")
    assert pg.locator("#sidebar").bounding_box()["x"] < -100  # hidden

    def swipe(x0, y0, x1, steps=6):
        pg.evaluate("""([x0, y0, x1, steps]) => {
          const T = (x, y) => new Touch({identifier: 1, target: document.body,
                                         clientX: x, clientY: y});
          const fire = (type, x, y) => window.dispatchEvent(
            new TouchEvent(type, {
              touches: type === 'touchend' ? [] : [T(x, y)],
              changedTouches: [T(x, y)], bubbles: true, cancelable: true }));
          fire('touchstart', x0, y0);
          for (let i = 1; i <= steps; i++)
            fire('touchmove', x0 + (x1 - x0) * i / steps, y0);
          fire('touchend', x1, y0);
        }""", [x0, y0, x1, steps])
        pg.wait_for_timeout(250)

    swipe(5, 400, 180)   # open from left edge
    assert pg.locator("#sidebar").is_visible()
    assert pg.eval_on_selector("#sidebar-veil",
                               "el => el.classList.contains('show')")
    swipe(350, 400, 200)   # swipe left to close
    assert pg.eval_on_selector("#sidebar", "el => !el.classList.contains('open')")
    # vertical drag from the edge must NOT open the drawer
    pg.evaluate("""() => {
      const T = (x, y) => new Touch({identifier: 1, target: document.body,
                                     clientX: x, clientY: y});
      const fire = (type, x, y) => window.dispatchEvent(new TouchEvent(type, {
        touches: type === 'touchend' ? [] : [T(x, y)],
        changedTouches: [T(x, y)], bubbles: true, cancelable: true }));
      fire('touchstart', 5, 300); fire('touchmove', 8, 450); fire('touchend', 8, 450);
    }""")
    pg.wait_for_timeout(250)
    assert pg.eval_on_selector("#sidebar", "el => !el.classList.contains('open')")
    assert pg.errors == []
    pg.close()


def test_edge_gesture_ignores_scroll_jitter(ui_server, browser):
    """The complaint: vertical scrolls starting near the left edge carried
    a few px of horizontal jitter and the drawer peeked mid-scroll. A
    near-vertical drag (even with wobble) must not move the drawer."""
    pg = _new_page(browser, viewport={"width": 390, "height": 844},
                   has_touch=True)
    pg.goto(ui_server, wait_until="networkidle")
    x_before = pg.locator("#sidebar").bounding_box()["x"]
    pg.evaluate("""() => {
      const T = (x, y) => new Touch({identifier: 1, target: document.body,
                                     clientX: x, clientY: y});
      const fire = (type, x, y) => window.dispatchEvent(new TouchEvent(type, {
        touches: type === 'touchend' ? [] : [T(x, y)],
        changedTouches: [T(x, y)], bubbles: true, cancelable: true }));
      fire('touchstart', 8, 300);
      const wobble = [[11, 330], [6, 370], [14, 410], [9, 460], [16, 520]];
      for (const [x, y] of wobble) fire('touchmove', x, y);
      fire('touchend', 16, 520);
    }""")
    pg.wait_for_timeout(250)
    assert abs(pg.locator("#sidebar").bounding_box()["x"] - x_before) < 1
    assert pg.eval_on_selector("#sidebar",
                               "el => !el.classList.contains('open') && "
                               "!el.style.transform")
    assert pg.errors == []
    pg.close()


def test_ads_checkbox_moves_behind_cog(ui_server, browser):
    """v0.47: Ads/LLM live behind the cog, autosaved per field; the row
    keeps only On + a compact img tag. No phantom boxes (v0.37 lineage:
    controls exist exactly where the code reaches for them)."""
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.on("dialog", lambda d: d.accept())
    pg.goto(ui_server + "/admin", wait_until="networkidle")
    pg.fill("#new-url", "https://ads2.test/feed"); pg.select_option("#new-type","feed")
    pg.click("#add-feed-btn"); pg.wait_for_timeout(1200)
    fid = pg.eval_on_selector("tr[data-id]", "el => el.dataset.id")
    row = pg.locator(f"tr[data-id='{fid}']")
    assert row.locator("[data-role=enabled]").count() == 1
    assert row.locator("[data-role=spons]").count() == 0      # behind the cog
    puts = []
    pg.on("request", lambda r: puts.append(r.post_data)
          if r.method == "PUT" and r.url.endswith(f"/api/feeds/{fid}") else None)
    row.locator("button[data-act=cfg]").click()
    pg.wait_for_selector(".cfg-panel [data-role=spons]", timeout=5000)
    pg.check(".cfg-panel [data-role=spons]")          # autosaves on change
    pg.wait_for_selector(".cfg-panel label:has([data-role=spons]).cfg-ok",
                         timeout=5000)
    assert puts and puts[-1] == '{"hide_sponsored":true}', puts
    assert pg.errors == []
    api = pg.evaluate("() => fetch('/api/feeds').then(r=>r.json()).then("
                      f"f => f.find(x => x.id === {fid}).hide_sponsored)")
    assert api in (True, 1)
    pg.close()


def test_admin_close_pops_history_not_pushes(ui_server, browser):
    """The mobile complaint: reader -> admin -> close left /admin in the
    back-stack, so the browser's own edge-back cycled admin. Closing must
    POP, keeping history shallow."""
    pg = _new_page(browser, viewport={"width": 390, "height": 844},
                   has_touch=True)
    pg.goto(ui_server, wait_until="networkidle")            # history: [/]
    n0 = pg.evaluate("history.length")
    pg.click("a[title=Admin]")
    pg.wait_for_url("**/admin", wait_until="networkidle")   # history: [/, /admin]
    assert pg.evaluate("history.length") == n0 + 1
    pg.click("#admin-close")
    pg.wait_for_url(ui_server + "/", wait_until="networkidle")
    assert pg.evaluate("history.length") == n0 + 1, \
        "close pushed a new entry instead of popping"
    assert "/admin" not in pg.url
    assert pg.errors == []
    pg.close()


def test_diagonal_swipe_does_not_trigger_pull_refresh(ui_server, browser):
    """v0.37.1 quirk: an arcing edge-swipe (rightward with downward
    drift) opened the drawer AND fired our pull-to-refresh reload.
    A horizontally-led gesture must never arm PTR."""
    pg = _new_page(browser, viewport={"width": 390, "height": 844},
                   has_touch=True)
    polls = []
    pg.on("request", lambda r: polls.append(r.url)
          if r.url.endswith("/api/poll") else None)
    pg.goto(ui_server, wait_until="networkidle")
    pg.evaluate("""() => {
      const T = (x, y) => new Touch({identifier: 1, target: document.body,
                                     clientX: x, clientY: y});
      const fire = (type, x, y) => window.dispatchEvent(new TouchEvent(type, {
        touches: type === 'touchend' ? [] : [T(x, y)],
        changedTouches: [T(x, y)], bubbles: true, cancelable: true }));
      fire('touchstart', 6, 120);
      for (let i = 1; i <= 8; i++) fire('touchmove', 6 + i * 22, 120 + i * 18);
      fire('touchend', 182, 264);
    }""")
    pg.wait_for_timeout(600)
    assert polls == [], "diagonal swipe triggered a poll/reload"
    assert pg.eval_on_selector("#ptr",
                               "el => !el.classList.contains('spin')")
    assert pg.errors == []
    pg.close()


def test_copy_snapshot_puts_png_on_clipboard(ui_server, browser):
    """The share-to-group-chat flow: click the card's snapshot button and
    a card image lands on the clipboard (localhost = secure context)."""
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.context.grant_permissions(["clipboard-read", "clipboard-write"],
                                 origin=ui_server)
    pg.goto(ui_server, wait_until="networkidle")
    pg.click(".card .snap-btn[data-fmt=wide]")   # wide = snapshot_width
    pg.wait_for_function("() => !!document.querySelector('.snap-btn')")
    pg.wait_for_function(
        """async () => (await navigator.clipboard.read()).some(i =>
                        i.types.includes('image/png'))""", timeout=8000)
    got = pg.evaluate("""async () => {
      for (let try_ = 0; try_ < 30; try_++) {
        try {
          for (const it of await navigator.clipboard.read())
            for (const t of it.types) if (t === 'image/png') {
              const b = await it.getType(t);
              const bmp = await createImageBitmap(b);
              return {size: b.size, w: bmp.width, h: bmp.height};
            }
        } catch {}
        await new Promise(r => setTimeout(r, 200));
      }
      return null; }""")
    if got is None:
        diag = pg.evaluate("""async () => { try {
            const items = await navigator.clipboard.read();
            return {n: items.length, types: items.map(i=>i.types.join()),
              perm: (await navigator.permissions.query(
                       {name:'clipboard-write'})).state};
          } catch (e) { return {err: String(e)}; } }""")
        raise AssertionError(f"clipboard empty; diag={diag} errs={pg.errors}")
    assert got and got["size"] > 4000, "no substantive PNG on clipboard"
    assert got["w"] == 1600 and got["h"] >= 300     # DPR2 @ 800 default
    assert pg.errors == []
    pg.close()


def test_snapshot_falls_back_to_download(ui_server, browser):
    """In plain-http LAN contexts the Clipboard API is blocked; the button
    must still deliver the PNG via download."""
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server, wait_until="networkidle")
    pg.evaluate("""Object.defineProperty(navigator.clipboard, 'write',
                   { value: () => Promise.reject(new Error('insecure')) })""")
    with pg.expect_download() as dl:
        pg.click(".card .snap-btn")
    path = dl.value.path()
    assert dl.value.suggested_filename.endswith(".png")
    import os
    assert os.path.getsize(path) > 4000             # a real image, not a stub
    assert pg.errors == []
    pg.close()


def test_main_feed_requests_priority_mode(ui_server, browser):
    """Newest-first views ask the server for unread-first ordering - globally
    AND inside a single feed. A feed's pip links to that feed, and with plain
    chronological order a late arrival (published earlier, fetched later) sat
    under already-read cards while the pip said 1 unread: the reader looked
    empty. Scoped views are where a pip leads the reader, so they use prio
    too. Since-mode and oldest order stay chronological (they are explicit
    requests for a time window / chronology)."""
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    urls = []
    pg.on("request", lambda r: urls.append(r.url)
          if "/api/articles" in r.url else None)
    pg.goto(ui_server, wait_until="networkidle")
    assert any("prio=1" in u for u in urls), urls
    fid = _mkfeed("priofeed")
    _seed_article(fid, "p1", title="prio in a feed")
    pg.goto(ui_server, wait_until="networkidle")
    pg.click(f"#feed-filter li[data-feed='{fid}']")
    pg.wait_for_function(
        "() => [...document.querySelectorAll('#stream .card')].some"
        "(c => c.textContent.includes('prio in a feed'))", timeout=6000)
    assert any(f"prio=1" in u and f"feed_id={fid}" in u for u in urls), urls[-3:]
    assert pg.errors == []
    pg.close()


def test_display_panel_widths_autosave(ui_server, browser):
    """The width levers live in their own panel and persist on change -
    no distant save button involved."""
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server + "/admin", wait_until="networkidle")
    where = pg.evaluate("""() => document.getElementById('cfg-sharewidth')
        .closest('section').querySelector('h2').textContent""")
    assert "Display" in where
    img_in = pg.evaluate("""() => document.getElementById('cfg-imgperpost')
        .closest('section').querySelector('h2').textContent""")
    assert "Display" in img_in
    puts = []
    pg.on("request", lambda r: puts.append(r.post_data)
          if r.method == "PUT" and r.url.endswith("/api/config") else None)
    pg.fill("#cfg-streamwidth", "1000")
    pg.press("#cfg-streamwidth", "Tab")           # commit -> change fires
    pg.wait_for_selector("label:has(#cfg-streamwidth).cfg-ok", timeout=5000)
    assert puts and "stream_width" in puts[-1] and "1000" in puts[-1]
    saved = pg.evaluate("() => fetch('/api/config').then(r => r.json())"
                        ".then(c => c.ui.stream_width)")
    assert saved == 1000  # explicit set; defaults now 800/1280
    assert pg.locator(".sec-nav a").count() == 9  # nav incl. Status
    # every nav link must FEED BACK active on click (perceived success),
    # including bottom sections that can't scroll to the spy band
    for href in ("#sec-queue", "#sec-usage", "#sec-feeds"):
        pg.click(f'.sec-nav a[href="{href}"]')
        pg.wait_for_selector(f'.sec-nav a[href="{href}"].active', timeout=2000)
        assert pg.locator(".sec-nav a.active").count() == 1, href
    # THE layout assertion (v0.42.1 regression): every settings section
    # must sit in the RIGHT column - never wrapped into the nav gutter.
    nav_box = pg.locator(".sec-nav").bounding_box()
    xs = pg.evaluate("""() => [...document.querySelectorAll('section.panel[id]')]
        .map(s => Math.round(s.getBoundingClientRect().x))""")
    assert len(xs) == 9, xs   # all sections incl. Status
    assert all(x >= nav_box["x"] + nav_box["width"] for x in xs), \
        f"sections leaked into the nav column: x={xs}"
    assert max(xs) - min(xs) <= 1, f"sections not aligned in one column: {xs}"
    assert pg.errors == []
    pg.close()


def test_stream_progress_rail(ui_server, browser):
    """The rail must be PERCEIVABLE, not merely present: with no route
    throttling at all (LAN-fast fetches), it must light for >=300ms of
    wall-clock time on the boot load - measured by a MutationObserver
    installed before the app scripts run."""
    pg = _new_page(browser)
    pg.add_init_script("""
      window.__rail = [];
      new MutationObserver(rs => {
        for (const r of rs)
          if (r.type === 'attributes' && r.target.id === 'stream-progress')
            window.__rail.push([r.target.className, performance.now()]);
      }).observe(document, { childList: true, subtree: true,
                             attributes: true, attributeFilter: ['class'] });
    """)
    pg.goto(ui_server + "/", wait_until="networkidle")
    pg.wait_for_timeout(600)               # let the first spin settle
    pg.click("#refresh-btn")              # guaranteed in-flight window
    spans = pg.evaluate("""() => {
      const spans = []; let on = null;
      for (const [c, t] of (window.__rail || [])) {
        if (c.includes('on') && on === null) on = t;
        if (!c.includes('on') && on !== null) { spans.push(t - on); on = null; }
      }
      return spans; }""")
    assert spans, "rail never animated during boot load"
    assert max(spans) >= 300, f"rail too brief to see: {spans}"
    assert pg.errors == []
    pg.close()



def test_refresh_button_drives_the_rail(ui_server, browser):
    """Clicking the header refresh button animates the rail for the whole
    poll window (the miss: v0.44 rail only watched article fetches)."""
    pg = _new_page(browser)
    def slow_poll(route):
        time.sleep(0.8)
        route.continue_()
    pg.route("**/api/poll", slow_poll)
    pg.add_init_script(RAIL_RECORDER + "window.__SETTLE_MS = 300;")
    pg.goto(ui_server + "/", wait_until="networkidle")
    pg.wait_for_function("""() => !document.getElementById('stream-progress')
        .classList.contains('on')""", timeout=3000)   # boot rail settled
    base = len(pg.evaluate("window.__rail"))
    pg.click("#refresh-btn")
    pg.wait_for_selector("#refresh-btn.spinning", timeout=2000)
    pg.wait_for_selector("#refresh-btn:not(.spinning)", timeout=5000)
    pg.wait_for_function("""() => !document.getElementById('stream-progress')
        .classList.contains('on')""", timeout=3000)
    spans = pg.evaluate("""(base) => {
      const spans = []; let on = null;
      for (const [c, t] of (window.__rail || []).slice(base)) {
        if (c.includes('on') && on === null) on = t;
        if (!c.includes('on') && on !== null) { spans.push(t - on); on = null; }
      }
      return spans; }""", base)
    assert max(spans) >= 800, f"rail did not cover the 0.8s poll: {spans}"
    assert pg.errors == []
    pg.close()


def test_snapshot_prefers_native_share_sheet(ui_server, browser):
    """When the platform offers a file-capable share sheet (iOS home
    screen app, https browsers), the snapshot button must use it first;
    clipboard remains the fallback, not the primary."""
    pg = _new_page(browser)
    pg.add_init_script("""
      window.__shared = null;
      navigator.canShare = d => d && d.files && d.files.length > 0;
      navigator.share = async d => {
        window.__shared = { n: d.files.length, type: d.files[0].type,
                            name: d.files[0].name };
      };
    """)
    pg.goto(ui_server, wait_until="networkidle")
    pg.wait_for_selector(".snap-btn")
    pg.click(".card .snap-btn")
    pg.wait_for_function("() => window.__shared", timeout=6000)
    sh = pg.evaluate("window.__shared")
    assert sh["type"] == "image/png" and sh["n"] == 1
    assert sh["name"].endswith(".png")
    assert pg.errors == []
    pg.close()


# ------------------------------------------------------------ share cards
# Every share test derives coordinates from window.__lastShareGeo (layout
# truth) and verifies PIXELS inside those rects. Never hard-code pixel
# positions: when a layout moves, these tests move with it. Each style
# must satisfy the same invariants (parametrized), plus style-specific ones.

STYLES = ["banner", "float"]
LONG = " ".join(["Digest prose runs on with enough words to wrap across"
                 " many lines so every layout zone is exercised fully"] * 5)


def _crop_png(name_seed: str) -> str:
    """Portrait 200x600: orange field, blue band rows 270-330. Returns the
    cached filename (hash-named, as imgstore would)."""
    import hashlib
    from io import BytesIO
    from PIL import Image
    import rssgate.imgstore as ig
    img = Image.new("RGB", (200, 600), (255, 136, 0))
    for y in range(270, 331):
        for x in range(200):
            img.putpixel((x, y), (0, 0, 255))
    buf = BytesIO(); img.save(buf, "PNG")
    ig.init(str(UI_IMG_DIR))
    fname = hashlib.sha256(name_seed.encode()).hexdigest()[:24] + ".png"
    UI_IMG_DIR.joinpath(fname).write_bytes(buf.getvalue())
    return fname


def _share_art(pg, summary=LONG, title="Share probe", image=True, link=True):
    fid = _mkfeed("share")
    _seed_article(fid, "s1", title=title, summary=summary,
                  image=_crop_png(f"crop-{fid}") if image else None,
                  link=f"https://share.test/{fid}" if link else None)
    art = _feed_article(pg, fid)
    if not link:
        art["link"] = None
    return art


def _overlap(a, b, pad=0):
    ax, ay, aw, ah = a; bx, by, bw, bh = b
    return not (ax + aw <= bx - pad or bx + bw <= ax - pad or
                ay + ah <= by - pad or by + bh <= ay - pad)


@pytest.fixture
def share_page(ui_server, browser):
    pg = _new_page(browser, viewport={"width": 1280, "height": 900},
                   color_scheme="light")
    pg.goto(ui_server, wait_until="networkidle")
    yield pg
    assert pg.errors == []
    pg.close()


@pytest.mark.parametrize("style", STYLES)
def test_share_layout_never_overlaps(share_page, style):
    """No digest line, title or meta box intersects hero, QR, caption or
    via; the card contains every rect."""
    pg = share_page
    art = _share_art(pg)
    r = pg.evaluate("([a, s]) => window.__renderCardPng(a, {style: s})"
                    ".then(() => window.__lastShareGeo)", [art, style])
    R = r["rects"]
    for k in ("hero", "qr", "via", "title", "meta"):
        assert k in R, f"{style}: missing {k}: {list(R)}"
    solid = [R[k] for k in ("hero", "qr", "caption", "via") if k in R]
    for box in [R["title"], R["meta"], *r["lines"]]:
        for s in solid:
            assert not _overlap(box, s), f"{style}: {box} hits {s}"
    for k, (x, y, w, h) in R.items():
        assert x >= 0 and y >= 0 and x + w <= r["W"] + 1 and \
            y + h <= r["H"] + 1, f"{style}: {k} outside card {R[k]}"


@pytest.mark.parametrize("style", STYLES)
def test_share_pixels_match_layout(share_page, style):
    """Pixels agree with geometry: hero rect shows the photo (orange field
    plus the centre blue band = cover/contain, never stretched off-band),
    QR rect holds dark modules, via rect is accent-colored."""
    pg = share_page
    art = _share_art(pg)
    pg.evaluate("([a, s]) => window.__renderCardPng(a, {style: s})", [art, style])
    R = pg.evaluate("window.__lastShareGeo.rects")
    hx, hy, hw, hh = R["hero"]
    z = {"hero_top": [hx + 2, hy + 1, hw - 4, max(2, hh * 0.04)],
         "hero_mid": [hx + 2, hy + hh * 0.48, hw - 4, max(2, hh * 0.04)],
         "qr": R["qr"], "via": R["via"]}
    # _share re-renders with the configured default style; force the style
    out = pg.evaluate(SHARE_PROBE.replace(
        "window.__renderCardPng(a)", "window.__renderCardPng(a, {style: '%s'})"
        % style), [art, z])
    assert out["hero_top"]["orange"] > out["hero_top"]["n"] * 0.6, out["hero_top"]
    assert out["hero_mid"]["blue"] > out["hero_mid"]["n"] * 0.6, out["hero_mid"]
    assert out["qr"]["dark"] > out["qr"]["n"] * 0.2, out["qr"]
    assert out["via"]["accent"] > 10, out["via"]


@pytest.mark.parametrize("style", STYLES)
def test_share_paragraph_gap_is_visible(share_page, style):
    """Two paragraphs: exactly one inter-line gap exceeds normal spacing
    (geometry), and that gap is blank (pixels)."""
    pg = share_page
    para = ("First paragraph of the digest is long enough to wrap onto a"
            " second line. ")
    art = _share_art(pg, summary=para * 2 + "\n\n" + "Second paragraph. " * 8,
                     image=False, link=False)
    pg.evaluate("([a, s]) => window.__renderCardPng(a, {style: s})", [art, style])
    lines = pg.evaluate("window.__lastShareGeo.lines")
    steps = [b[1] - a[1] for a, b in zip(lines, lines[1:])]
    base = min(steps)
    big = [i for i, s in enumerate(steps) if s > base + 4]
    assert len(big) == 1, f"{style}: steps {steps}"
    i = big[0]
    gap = [lines[i][0], lines[i][1] + lines[i][3] + 1,
           600, max(1, lines[i + 1][1] - lines[i][1] - lines[i][3] - 2)]
    out = pg.evaluate(SHARE_PROBE.replace(
        "window.__renderCardPng(a)", "window.__renderCardPng(a, {style: '%s'})"
        % style), [art, {"gap": gap}])
    assert out["gap"]["dark"] == 0, f"{style}: gap not blank {out['gap']}"


def test_share_banner_title_and_digest_span(share_page):
    """Banner: a long title uses the full measure; digest lines too."""
    pg = share_page
    art = _share_art(pg, title="Photos must never steal the headline territory"
                                " on share cards because titles carry the story")
    g = pg.evaluate("(a) => window.__renderCardPng(a, {style: 'banner'})"
                    ".then(() => window.__lastShareGeo)", art)
    W = g["W"]
    assert g["rects"]["title"][2] > W * 0.6, g["rects"]["title"]
    assert max(l[2] for l in g["lines"]) > W * 0.75
    assert g["rects"]["hero"][2] == W              # full-bleed banner


def test_share_float_qr_is_the_last_line(share_page):
    """Float: fixed point holds - no digest text below the QR caption."""
    pg = share_page
    art = _share_art(pg)
    g = pg.evaluate("(a) => window.__renderCardPng(a, {style: 'float'})"
                    ".then(() => window.__lastShareGeo)", art)
    assert g["qzT"] is not None and g["qs"] > 0
    assert g["dY"] <= g["qzT"] + g["qs"] + 22 + 2, g


def test_share_without_image_or_link_still_renders(share_page):
    pg = share_page
    art = _share_art(pg, image=False, link=False)
    for style in STYLES:
        g = pg.evaluate("([a, s]) => window.__renderCardPng(a, {style: s})"
                        ".then(() => window.__lastShareGeo)", [art, style])
        assert "hero" not in g["rects"] and "qr" not in g["rects"]
        assert g["H"] > 100 and g["lines"], g


def test_status_pips_visibility_and_links(ui_server, browser):
    """Zero counts -> both pips display:none. Seed pending+error -> counts
    render, the queue pip links to the admin queue and the failures pip to the
    failures panel. Queued work reads NEUTRAL, not success green: a pending
    post has no card, so it must not borrow the reading palette."""
    from rssgate import db
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server, wait_until="networkidle")
    assert pg.eval_on_selector("#pip-queue",
                               "e => getComputedStyle(e).display") == "none"
    assert pg.eval_on_selector("#pip-fail",
                               "e => getComputedStyle(e).display") == "none"
    conn = db.connect(UI_DB)
    fid = db.add_feed(conn, "https://pips.test/feed", type_="feed")["id"]
    for i, st in (("q1", "pending"), ("q2", "pending"), ("f1", "error")):
        aid = db.upsert_article(conn, fid, i, f"https://pips.test/{i}",
                                "pip probe", "2027-03-01T00:00:00Z")
        conn.execute("UPDATE articles SET status=?, summary='x'"
                     " WHERE id=?", (st, aid))
    conn.commit(); conn.close()
    pg.reload(wait_until="networkidle")
    assert pg.inner_text("#pip-queue-n") == "2"
    assert pg.inner_text("#pip-fail-n") == "1"
    assert pg.eval_on_selector("#pip-queue",
                               "e => getComputedStyle(e).display") != "none"
    pg.click("#pip-fail")
    pg.wait_for_url("**/admin#sec-failures", wait_until="networkidle")
    top, vh = pg.evaluate("""() => [document.getElementById('sec-failures')
        .getBoundingClientRect().top, innerHeight]""")
    assert 0 <= top < 200, f"failures panel not anchored into view: {top}"
    pg.go_back(wait_until="networkidle")
    pg.click("#pip-queue")
    pg.wait_for_url("**/admin#sec-queue", wait_until="networkidle")
    top = pg.evaluate("""() => document.getElementById('sec-queue')
        .getBoundingClientRect().top""")
    assert 0 <= top < 200, f"queue panel not anchored into view: {top}"
    assert pg.errors == []
    pg.close()


def test_admin_clear_failed_button_flow(ui_server, browser):
    """Clear-all button empties the error pile, reports it, and the
    viewer's red pip vanishes on next refresh."""
    from rssgate import db
    conn = db.connect(UI_DB)
    fid = db.add_feed(conn, "https://clearbtn.test/feed",
                      type_="feed")["id"]
    aid = db.upsert_article(conn, fid, "cb1", "https://clearbtn.test/1",
                            "doomed post", "2027-03-02T00:00:00Z")
    conn.execute("UPDATE articles SET status='error', summary='x'"
                 " WHERE id=?", (aid,))
    conn.commit(); conn.close()
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server + "/admin#sec-failures", wait_until="networkidle")
    pg.wait_for_selector("#fail-clear-btn")
    pg.on("dialog", lambda d: d.accept())
    pg.click("#fail-clear-btn")
    pg.wait_for_function(
        "document.getElementById('fail-clear-result')"
        ".textContent.includes('cleared')", timeout=5000)
    txt = pg.inner_text("#fail-clear-result")
    import re
    assert re.search(r"cleared \d+ posts?", txt), txt
    c2 = db.connect(UI_DB)
    n = c2.execute("SELECT COUNT(*) c FROM articles WHERE"
                   " status='error' AND feed_id=?", (fid,)).fetchone()["c"]
    assert n == 0
    c2.close()
    pg.goto(ui_server + "/", wait_until="networkidle")
    assert pg.eval_on_selector("#pip-fail",
                               "e => getComputedStyle(e).display") == "none"
    assert pg.errors == []
    pg.close()


def test_admin_nav_groups_and_feed_filter(ui_server, browser):
    """Grouped nav: 4 group labels visible on desktop, hidden in the
    mobile chip row. Feed filter appears past 8 feeds and hides
    non-matching rows (computed display:none, not just a class)."""
    for i in range(9):
        _mkfeed("filt")
    tag = _mkfeed("needle")
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server + "/admin", wait_until="networkidle")
    groups = pg.eval_on_selector_all(
        ".sec-nav .nav-group", "els => els.map(e => [e.textContent,"
        " getComputedStyle(e).display])")
    assert [g[0] for g in groups] == ["Overview", "Reading", "Sources",
                                      "Processing"], groups
    assert all(g[1] != "none" for g in groups)
    assert pg.eval_on_selector("#feed-q", "e => getComputedStyle(e)"
                               ".display") != "none"
    pg.fill("#feed-q", "needle")
    shown = pg.eval_on_selector_all(
        "#feed-table tbody tr[data-id]",
        "rs => rs.filter(r => getComputedStyle(r).display !== 'none')"
        ".map(r => +r.dataset.id)")
    assert shown == [tag], shown
    pg.fill("#feed-q", "")
    assert pg.locator("#feed-table tbody tr[data-id]:visible").count() >= 10
    m = _new_page(browser, viewport={"width": 390, "height": 844})
    m.goto(ui_server + "/admin", wait_until="networkidle")
    assert all(d == "none" for d in m.eval_on_selector_all(
        ".sec-nav .nav-group", "els => els.map(e => getComputedStyle(e).display)"))
    assert pg.errors == [] and m.errors == []
    pg.close(); m.close()


def test_danger_zone_holds_destructive_action(ui_server, browser):
    """Clear-all lives inside the bordered danger zone at the END of the
    failures section, away from routine buttons."""
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server + "/admin#sec-failures", wait_until="networkidle")
    inside = pg.evaluate("""() => !!document.querySelector(
        '#sec-failures .danger-zone #fail-clear-btn')""")
    assert inside
    zone = pg.eval_on_selector("#sec-failures .danger-zone",
                               "e => e.getBoundingClientRect().top")
    maint = pg.eval_on_selector("#maint-run-btn",
                                "e => e.getBoundingClientRect().top")
    assert zone > maint, "danger zone must sit below routine maintenance"
    assert pg.errors == []
    pg.close()


def test_fetch_politeness_controls(ui_server, browser):
    """'Use this browser's' copies navigator.userAgent into fetch config
    (autosave); a site-paused feed shows a badge; Resume clears it."""
    from rssgate import db
    fid = _mkfeed("paused")
    conn = db.connect(UI_DB); db.feed_block(conn, fid, 60); conn.close()
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server + "/admin#sec-polling", wait_until="networkidle")
    pg.click("#ua-mine-btn")
    pg.wait_for_selector("label:has(#cfg-ua).cfg-ok", timeout=5000)
    ua = pg.evaluate("navigator.userAgent")
    saved = pg.evaluate("fetch('/api/config').then(r => r.json())"
                        ".then(c => c.fetch.user_agent)")
    assert saved == ua and pg.input_value("#cfg-ua") == ua
    row = pg.locator(f"tr[data-id='{fid}']")
    assert row.locator(".paused-pill").count() == 1
    assert "paused by site" in row.locator(".paused-pill").inner_text()
    row.locator("button[data-act=cfg]").click()
    pg.click(".cfg-panel button[data-act=cfg-unpause]")
    pg.wait_for_selector(f"tr[data-id='{fid}']:not(:has(.paused-pill))",
                         timeout=5000)
    conn = db.connect(UI_DB)
    assert db.get_feed(conn, fid)["backoff_level"] == 0
    conn.close()
    pg.click(".cfg-panel button[data-act=cfg-done]")
    pg.click("#ua-default-btn")                    # leave config default
    pg.wait_for_function("""() => fetch('/api/config').then(r => r.json())
        .then(c => c.fetch.user_agent === '')""", timeout=5000)
    assert pg.errors == []
    pg.close()


def test_excerpt_cards_are_labelled(ui_server, browser):
    """Posts published from the feed's own excerpt carry a visible
    'feed excerpt' chip (so a teaser never passes for a digest)."""
    from rssgate import db
    fid = _mkfeed("excerpt")
    aid = _seed_article(fid, "e1", title="Excerpt probe",
                        ts="2027-05-01T00:00:00Z",
                        summary="Teaser text the feed itself provided.")
    conn = db.connect(UI_DB)
    db.set_article(conn, aid, digest_source="excerpt"); conn.close()
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server, wait_until="networkidle")
    card = pg.locator(f".card[data-id='{aid}']")
    if card.count() == 0:                       # select by title fallback
        card = pg.locator(".card", has_text="Excerpt probe")
    chip = card.locator(".chip.excerpt")
    assert chip.count() == 1 and "feed excerpt" in chip.inner_text()
    assert chip.evaluate("e => getComputedStyle(e).display") != "none"
    assert pg.errors == []
    pg.close()


def test_content_source_select_and_fallback_badges(ui_server, browser):
    """Cog 'Content source' autosaves; the feed row shows the non-default
    source and how many posts fell back to the feed's excerpt."""
    from rssgate import db
    fid = _mkfeed("csrc")
    aid = _seed_article(fid, "x1", title="fallback probe")
    conn = db.connect(UI_DB)
    db.set_article(conn, aid, digest_source="excerpt"); conn.close()
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server + "/admin", wait_until="networkidle")
    row = pg.locator(f"tr[data-id='{fid}']")
    assert "1 from feed excerpt" in row.locator(".excerpt-pill").inner_text()
    row.locator("button[data-act=cfg]").click()
    pg.wait_for_selector(".cfg-panel [data-role=csrc]")
    assert "fell back" in pg.inner_text(".cfg-panel .hint")
    pg.select_option(".cfg-panel [data-role=csrc]", "feed")
    pg.wait_for_selector(".cfg-panel label:has([data-role=csrc]).cfg-ok",
                         timeout=5000)
    pg.click(".cfg-panel button[data-act=cfg-done]")
    pg.wait_for_selector(f"tr[data-id='{fid}'] .src-pill", timeout=5000)
    assert row.locator(".src-pill").inner_text() == "feed text only"
    conn = db.connect(UI_DB)
    assert db.get_feed(conn, fid)["content_source"] == "feed"
    conn.close()
    assert pg.errors == []
    pg.close()


def test_challenge_flag_warns_in_admin(ui_server, browser):
    """A site that serves a browser check gets a row badge and a warning
    right at the Content source control."""
    from rssgate import db
    fid = _mkfeed("chal")
    conn = db.connect(UI_DB)
    db.feed_challenge(conn, fid); conn.close()
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server + "/admin", wait_until="networkidle")
    row = pg.locator(f"tr[data-id='{fid}']")
    badge = row.locator(".challenge-pill")
    assert badge.count() == 1
    txt0 = badge.inner_text()          # Auto feeds now spell out the streak
    assert "bot check" in txt0 or "browser-check site" in txt0, txt0
    row.locator("button[data-act=cfg]").click()
    pg.wait_for_selector(".cfg-panel [data-role=csrc]")
    txt = pg.inner_text(".cfg-panel label:has([data-role=csrc])")
    assert "browser check" in txt and "Feed text only" in txt
    assert pg.errors == []
    pg.close()


def test_llm_outage_shows_held_not_failed(ui_server, browser):
    """AI backend down: the queue pip turns amber 'waiting · AI offline'
    and links to Status, which explains the hold."""
    from rssgate import db, refresh
    fid = _mkfeed("held")
    _seed_article(fid, "h1", status="pending")
    conn = db.connect(UI_DB)
    refresh.mark_llm_down(conn, "ConnectionError: refused")
    db.set_state(conn, "llm_next_try", "2999-01-01T00:00:00Z")  # stay down
    conn.close()
    try:
        pg = _new_page(browser, viewport={"width": 1280, "height": 900})
        pg.goto(ui_server, wait_until="networkidle")
        pip = pg.locator("#pip-queue")
        pg.wait_for_function("document.getElementById('pip-queue')"
                             ".classList.contains('pip-warn')", timeout=5000)
        assert "AI offline" in pip.inner_text()
        assert pip.get_attribute("href").endswith("#sec-status")
        pip.click()
        pg.wait_for_url("**/admin#sec-status", wait_until="networkidle")
        pg.wait_for_function("document.getElementById('status-note')"
                             ".textContent.includes('held')", timeout=5000)
        assert "offline since" in pg.inner_text("#status-grid")
        assert pg.errors == []
        pg.close()
    finally:
        conn = db.connect(UI_DB); refresh.mark_llm_up(conn); conn.close()


def test_retry_all_failed_button(ui_server, browser):
    from rssgate import db
    fid = _mkfeed("retry")
    aid = _seed_article(fid, "r1", status="error")
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server + "/admin#sec-failures", wait_until="networkidle")
    assert pg.locator("#cfg-logfail").count() == 0       # checkbox removed
    pg.click("#fail-retry-btn")
    pg.wait_for_function("document.getElementById('fail-retry-result')"
                         ".textContent.includes('back in the queue')", timeout=5000)
    conn = db.connect(UI_DB)
    assert conn.execute("SELECT status FROM articles WHERE id=?",
                        (aid,)).fetchone()[0] in ("pending", "processing", "ready")
    conn.close()
    assert pg.errors == []
    pg.close()


def test_read_marking_is_honest(ui_server, browser):
    """A card that has only partly scrolled into view stays unread; a card
    scrolled ENTIRELY past is read; clicking a card reads it instantly."""
    from rssgate import db
    fid = _mkfeed("readpass")
    long = " ".join(f"Sentence {i} of a long digest body." for i in range(60))
    ids = [_seed_article(fid, f"r{i}", title=f"read probe {i}",
                         ts=f"2027-06-0{9 - i}T00:00:00Z", summary=long)
           for i in range(5)]
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server, wait_until="networkidle")
    pg.click(f"#feed-filter li[data-feed='{fid}']")
    pg.wait_for_selector(f".card[data-id='{ids[0]}']", timeout=5000)
    card = lambda i: pg.locator(f".card[data-id='{ids[i]}']")
    # put card 1's top at 75% of the screen: mostly below the fold
    pg.evaluate("""(id) => { const c = document.querySelector(
        `.card[data-id='${id}']`);
        scrollTo(0, c.getBoundingClientRect().top + scrollY - innerHeight * 0.75);
    }""", ids[1])
    pg.wait_for_timeout(2000)                     # past the 1.2s save debounce
    assert "unread" in card(1).get_attribute("class")
    # scroll card 0 completely off the top -> read
    pg.evaluate("""(id) => { const c = document.querySelector(
        `.card[data-id='${id}']`);
        scrollTo(0, c.offsetTop + c.offsetHeight + 5); }""", ids[0])
    pg.wait_for_function(f"""() => !document.querySelector(
        ".card[data-id='{ids[0]}']").classList.contains('unread')""",
        timeout=4000)
    # click card 3 -> read immediately (well before any dwell timer)
    card(3).locator(".digest").click()
    assert "unread" not in card(3).get_attribute("class")
    pg.wait_for_timeout(400)
    conn = db.connect(UI_DB)
    got = dict(conn.execute(
        "SELECT id, read_at IS NOT NULL FROM articles WHERE feed_id=?",
        (fid,)).fetchall())
    conn.close()
    assert got[ids[0]] == 1 and got[ids[3]] == 1
    assert got[ids[2]] == 0 and got[ids[4]] == 0   # never seen: still unread
    assert pg.errors == []
    pg.close()


def test_tall_and_wide_share_buttons(ui_server, browser):
    """Each card offers two formats: tall = banner at a fixed 640px card,
    wide = magazine layout at ui.snapshot_width. Verified on the PNG that
    actually reaches the share sheet."""
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.add_init_script("""
      window.__shots = [];
      navigator.canShare = d => d && d.files && d.files.length > 0;
      navigator.share = async d => {
        const bmp = await createImageBitmap(d.files[0]);
        window.__shots.push({ w: bmp.width, h: bmp.height,
                              style: (window.__lastShareGeo || {}).style });
      };
    """)
    pg.goto(ui_server, wait_until="networkidle")
    card = pg.locator(".card").first
    assert card.locator(".snap-btn[data-fmt=tall]").count() == 1
    assert card.locator(".snap-btn[data-fmt=wide]").count() == 1
    sw = pg.evaluate("fetch('/api/resume').then(r => r.json())"
                     ".then(s => s.snapshot_width)")
    card.locator(".snap-btn[data-fmt=tall]").click()
    pg.wait_for_function("() => window.__shots.length === 1", timeout=8000)
    card.locator(".snap-btn[data-fmt=wide]").click()
    pg.wait_for_function("() => window.__shots.length === 2", timeout=8000)
    tall, wide = pg.evaluate("window.__shots")
    assert tall["style"] == "banner" and tall["w"] == 640 * 2, tall
    assert wide["style"] == "float" and wide["w"] == sw * 2, wide
    assert pg.errors == []
    pg.close()


def test_former_save_button_fields_autosave(ui_server, browser):
    """Language model / Polling fields save themselves (the global Save
    button sent EVERY field, so a stale tab could revert autosaved values).
    Single-field patches only; invalid JSON is refused without a PUT."""
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    alerts = []
    pg.on("dialog", lambda d: (alerts.append(d.message), d.accept()))
    puts = []
    pg.on("request", lambda r: puts.append(r.post_data)
          if r.method == "PUT" and r.url.endswith("/api/config") else None)
    pg.goto(ui_server + "/admin#sec-polling", wait_until="networkidle")
    assert pg.locator("#save-btn").count() == 0
    old = pg.input_value("#cfg-feed-min")
    pg.fill("#cfg-feed-min", "45"); pg.press("#cfg-feed-min", "Tab")
    pg.wait_for_selector("label:has(#cfg-feed-min).cfg-ok", timeout=5000)
    assert puts[-1] == '{"polling":{"feed_interval_minutes":45}}', puts[-1]
    got = pg.evaluate("fetch('/api/config').then(r => r.json())"
                      ".then(c => c.polling.feed_interval_minutes)")
    assert got == 45
    n = len(puts)
    pg.fill("#cfg-extra-body", "{not json"); pg.press("#cfg-extra-body", "Tab")
    pg.wait_for_timeout(300)
    assert len(puts) == n and any("valid JSON" in a for a in alerts)
    pg.fill("#cfg-feed-min", old or "30"); pg.press("#cfg-feed-min", "Tab")
    pg.wait_for_selector("label:has(#cfg-feed-min).cfg-ok", timeout=5000)
    assert pg.errors == []
    pg.close()


def test_share_icons_are_tappable_and_proportioned(ui_server, browser):
    """Both share buttons are real tap targets (>=28px), the tall icon is
    upright and the wide one only a little wide (not a letterbox)."""
    pg = _new_page(browser, viewport={"width": 390, "height": 844},
                   has_touch=True)
    pg.goto(ui_server, wait_until="networkidle")
    card = pg.locator(".card").first
    m = {}
    for fmt in ("tall", "wide"):
        b = card.locator(f".snap-btn[data-fmt={fmt}]")
        box = b.bounding_box()
        rect = b.locator("svg rect").bounding_box()
        m[fmt] = (box, rect)
        assert box["width"] >= 28 and box["height"] >= 28, (fmt, box)
    (_, t), (_, w) = m["tall"], m["wide"]
    assert t["height"] > t["width"] * 1.3, t
    assert 1.2 <= w["width"] / w["height"] <= 1.7, w
    assert pg.errors == []
    pg.close()


def test_auto_state_is_spelled_out(ui_server, browser):
    """A streaked Auto feed says what it's doing and when it next probes;
    the excerpt badge counts posts awaiting a page retry."""
    from rssgate import db
    fid = _mkfeed("autostate")
    aid = _seed_article(fid, "a1", title="auto state probe")
    conn = db.connect(UI_DB)
    for _ in range(3):
        db.feed_challenge(conn, fid)
    db.set_article(conn, aid, digest_source="excerpt",
                   upgrade_at="2999-01-01T00:00:00Z")
    conn.close()
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server + "/admin", wait_until="networkidle")
    row = pg.locator(f"tr[data-id='{fid}']")
    pill = row.locator(".challenge-pill").inner_text()
    assert "using feed text" in pill and "next page probe" in pill, pill
    assert "1 awaiting page retry" in row.locator(".excerpt-pill").inner_text()
    row.locator("button[data-act=cfg]").click()
    pg.wait_for_selector(".cfg-panel [data-role=csrc]")
    assert "using feed text" in pg.inner_text(".cfg-panel label:has([data-role=csrc])")
    assert pg.errors == []
    pg.close()


def test_status_rows_balanced(ui_server, browser):
    """Status cells split into two balanced rows (4+4 today) - by on-screen
    position, not markup alone."""
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server + "/admin", wait_until="networkidle")
    pg.wait_for_selector("#status-grid .status-cell")
    tops = pg.eval_on_selector_all("#status-grid .status-cell",
        "cs => cs.map(c => Math.round(c.getBoundingClientRect().top))")
    rows = sorted(set(tops))
    assert len(rows) == 2, f"expected two visual rows: {tops}"
    a, b = tops.count(rows[0]), tops.count(rows[1])
    assert a - b in (0, 1), f"unbalanced rows {a}+{b}"
    assert (a, b) == (4, 4), (a, b)
    assert pg.errors == []
    pg.close()


def test_admin_manual_refresh_spins(ui_server, browser):
    """Status Refresh spins its glyph (ptrspin) and dims the grid long
    enough to see (>=300ms wall clock), then settles."""
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server + "/admin", wait_until="networkidle")
    pg.add_script_tag(content="""
      window.__spin = [];
      new MutationObserver(() => window.__spin.push([
        document.getElementById('status-refresh').classList.contains('spinning'),
        performance.now()])).observe(document.getElementById('status-refresh'),
        { attributes: true, attributeFilter: ['class'] });""")
    pg.click("#status-refresh")
    anim = pg.eval_on_selector("#status-refresh .rf-glyph",
                               "e => getComputedStyle(e).animationName")
    assert anim == "ptrspin", anim
    assert "loading" in pg.get_attribute("#status-grid", "class")
    pg.wait_for_selector("#status-refresh:not(.spinning)", timeout=3000)
    marks = pg.evaluate("window.__spin")
    on = next(t for s, t in marks if s)
    off = next(t for s, t in marks if not s and t > on)
    assert off - on >= 300, f"spin too brief to see: {off - on:.0f}ms"
    assert "loading" not in (pg.get_attribute("#status-grid", "class") or "")
    assert pg.errors == []
    pg.close()


def test_admin_links_to_project(ui_server, browser):
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server + "/admin", wait_until="networkidle")
    links = pg.eval_on_selector_all("#sec-status .about-links a",
        "as => as.map(a => [a.href, a.target, a.rel, a.offsetWidth > 0])")
    hrefs = [l[0] for l in links]
    assert "https://rssgate.org/" in hrefs
    assert "https://github.com/nkavassalis/RSSgate" in hrefs
    assert all(t == "_blank" and "noopener" in r and vis for _, t, r, vis in links)
    assert pg.errors == []
    pg.close()


def test_manual_source_hides_auto_badges(ui_server, browser):
    """Taking a feed out of Auto hides the excerpt count and bot-check
    badges in admin (the posts themselves keep their excerpt label)."""
    from rssgate import db
    fid = _mkfeed("manualsrc")
    aid = _seed_article(fid, "m1", title="manual source probe")
    conn = db.connect(UI_DB)
    db.set_article(conn, aid, digest_source="excerpt")
    db.feed_challenge(conn, fid)
    conn.close()
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server + "/admin", wait_until="networkidle")
    row = pg.locator(f"tr[data-id='{fid}']")
    assert row.locator(".excerpt-pill").count() == 1        # auto: shown
    assert row.locator(".challenge-pill").count() == 1
    row.locator("button[data-act=cfg]").click()
    pg.select_option(".cfg-panel [data-role=csrc]", "feed")
    pg.wait_for_selector(".cfg-panel label:has([data-role=csrc]).cfg-ok", timeout=5000)
    pg.click(".cfg-panel button[data-act=cfg-done]")
    pg.wait_for_selector(f"tr[data-id='{fid}'] .src-pill", timeout=5000)
    assert row.locator(".excerpt-pill").count() == 0
    assert row.locator(".challenge-pill").count() == 0
    # the post keeps its label in the reader
    items = pg.evaluate(f"fetch('/api/articles?feed_id={fid}&fresh=1')"
                        ".then(r => r.json()).then(d => d.items)")
    assert items[0]["digest_source"] == "excerpt"
    assert pg.errors == []
    pg.close()


def test_work_queue_names_feeds_and_reasons(ui_server, browser):
    from rssgate import db
    fid = _mkfeed("wqreason")
    for i in range(2):
        _seed_article(fid, f"w{i}", title=f"queued probe {i}", status="pending",
                      ts=f"2027-07-0{i + 1}T00:00:00Z")
    conn = db.connect(UI_DB); db.feed_block(conn, fid, 60)
    name = db.get_feed(conn, fid)["url"]; conn.close()
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server + "/admin#sec-queue", wait_until="networkidle")
    pg.wait_for_function("document.getElementById('wq-waiting').textContent"
                         ".includes('paused by the site')", timeout=8000)
    li = pg.locator("#wq-waiting li", has_text=name)
    assert li.count() == 1 and "2 posts" in li.inner_text()
    assert pg.locator("#wq-next li", has_text="queued probe").count() == 0
    assert pg.errors == []
    pg.close()


def _seed_loaded_article(fid: int, guid: str, categories: str | None = None,
                         **fields) -> int:
    """ready + marked read (no unread dot shifting the row) + post tags."""
    from rssgate import db
    aid = _seed_article(fid, guid, **fields)
    conn = db.connect(UI_DB)
    db.mark_articles_read(conn, [aid])
    if categories:
        db.set_article(conn, aid, categories=categories)
    conn.commit(); conn.close()
    return aid


@pytest.mark.parametrize("width", [320, 390])
def test_share_buttons_stay_on_one_line_on_mobile(ui_server, browser, width):
    """The two share buttons must not be shunted onto their own line on a
    phone: time + both buttons are ONE right-hand group, inside the card,
    with no page-level horizontal overflow - even with a long feed name and
    chips competing for the row."""
    fid = _mkfeed("metashare")
    _seed_loaded_article(fid, "m1", title="wide group probe",
                         summary=("The council voted late on Tuesday to fund four "
                                  "kilometres of protected bike lanes, replacing the "
                                  "painted lanes installed in 2019, with construction "
                                  "expected to span two seasons. ") * 7,
                         categories="ai,hardware,long-category")
    pg = _new_page(browser, viewport={"width": width, "height": 800})
    pg.goto(ui_server, wait_until="networkidle")
    pg.wait_for_selector(".snap-btn")
    geo = pg.evaluate("""() => {
      const card = document.querySelector('.card');
      const g = e => e.getBoundingClientRect();
      const round = r => ({x: Math.round(r.x), y: Math.round(r.y),
                           width: Math.round(r.width), height: Math.round(r.height),
                           right: Math.round(r.right)});
      const btns = [...card.querySelectorAll('.snap-btn')].map(g).map(round);
      const time = round(g(card.querySelector('time'))), cr = g(card);
      return {btns, time, crRight: Math.round(cr.right),
              innerWidth, scrollOverflow: document.documentElement.scrollWidth - innerWidth,
              metaLeft: Math.round(g(card.querySelector('.meta-actions')).left),
              cardLeft: Math.round(cr.left), timeRight: Math.round(time.right)};
    }""")
    btns, t = geo["btns"], geo["time"]
    assert len(btns) == 2, geo
    def same_line(a, b):
        # vertically OVERLAPPING and side by side = one line (the 30px buttons
        # sit 5px taller than the time text when centred inside the group)
        overlap = min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"])
        return overlap >= min(a["height"], b["height"]) * .6 and abs(a["x"] - b["x"]) > 1
    assert all(same_line(b, t) for b in btns), f"buttons split from the timestamp: {btns} {t}"
    assert same_line(btns[0], btns[1]), "the two share buttons split from each other"
    assert btns[0]["x"] < btns[1]["x"], "tall card should come first"
    for b in btns:
        assert b["x"] + b["width"] <= geo["crRight"] + .5, f"button past the card edge: {b}"
        assert b["width"] >= 30, f"tap target shrunk: {b['width']}"
    assert t["width"] > 0, "timestamp hidden"
    assert (geo["metaLeft"] - geo["cardLeft"]) / (geo["crRight"] - geo["cardLeft"]) > .4, \
        "share group no longer right-aligned"
    assert geo["scrollOverflow"] <= 0, "page overflows horizontally"
    assert pg.errors == []
    pg.close()


def test_queue_panel_explains_idle_and_paused(ui_server, browser):
    """The Work queue summary is a sentence a human can act on: it names
    paused-by-site posts, and says when an enabled feed has produced nothing
    digested yet (a bare '0 summarizing · 0 queued' hides both)."""
    from rssgate import db
    _mkfeed("wqwords")                            # enabled, zero posts
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server + "/admin#sec-queue", wait_until="networkidle")
    # `never_summarized` is a GLOBAL count and the tier shares one session DB,
    # so assert the panel mirrors the API rather than a fixed number
    want = pg.evaluate(
        "() => fetch('/api/workqueue').then(r => r.json())"
        ".then(d => d.never_summarized)")
    if want:
        pg.wait_for_function(
            "document.getElementById('wq-summary').textContent"
            ".includes('no digested posts yet')", timeout=8000)
        assert f"{want} enabled feed" in pg.locator("#wq-summary").inner_text()
        assert "no digested posts" in pg.locator("#wq-current").inner_text()
    else:
        assert "no digested posts" not in pg.locator("#wq-summary").inner_text()

    # now a feed whose posts a site paused -> the sentence must say so
    b = _mkfeed("wqpause")
    _seed_article(b, "q1", title="paused probe", status="pending")
    conn = db.connect(UI_DB); db.feed_block(conn, b, 60); conn.close()
    pg.click("#status-refresh")                  # re-render the panel
    pg.wait_for_function("document.getElementById('wq-summary').textContent"
                         ".includes('paused by its site')", timeout=8000)
    txt = pg.locator("#wq-summary").inner_text()
    assert "1 is paused by its site" in txt and "resume on their own" in txt
    # total queue depth is shared-tier state; only assert a number this test
    # owns (the pause) and that the queue itself is reported
    assert "queued ahead" in txt
    # per-feed rows are asserted hermetically (shared-tier queue depth makes
    # row-level counts order-dependent here); check the list still renders
    assert pg.locator("#wq-waiting li").count() >= 1
    assert pg.errors == []
    pg.close()


def test_admin_theme_control_repaints_and_persists(ui_server, browser):
    """Pinned themes must actually recolour (installed/PWA windows sometimes
    report their own theme, so 'auto' cannot be relied on): attribute, body
    colours and a status colour all follow, and survive a reload."""
    def bg(pg):
        return pg.evaluate("getComputedStyle(document.body).backgroundColor")
    def scheme(pg):
        # forced themes must also set `color-scheme`: it is the only thing that
        # tells native scrollbars/form widgets, and a media query cannot fake it
        return pg.evaluate("getComputedStyle(document.documentElement)"
                           ".colorScheme")
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server + "/admin#sec-display", wait_until="networkidle")
    pg.select_option("#cfg-theme", "dark")
    pg.wait_for_selector("#cfg-theme:disabled, label.cfg-ok", timeout=5000)
    assert pg.get_attribute("html", "data-theme") == "dark"
    dark_bg, dark_cs = bg(pg), scheme(pg)
    pg.select_option("#cfg-theme", "light")
    pg.wait_for_selector("label.cfg-ok", timeout=5000)
    light_bg, light_cs = bg(pg), scheme(pg)
    assert dark_bg != light_bg, "body background did not follow the scheme"
    assert (dark_cs, light_cs) == ("dark", "light"), (dark_cs, light_cs)
    pg.reload(wait_until="networkidle")            # server-rendered, not just JS
    assert pg.get_attribute("html", "data-theme") == "light"
    assert bg(pg) == light_bg and scheme(pg) == "light"
    # the reader repaints too (it is the page the user actually reads)
    pg2 = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg2.goto(ui_server, wait_until="networkidle")
    assert pg2.get_attribute("html", "data-theme") == "light"
    assert pg2.evaluate("getComputedStyle(document.body).backgroundColor") == light_bg
    pg2.close()
    pg.select_option("#cfg-theme", "auto")
    pg.wait_for_selector("label.cfg-ok", timeout=5000)
    assert pg.get_attribute("html", "data-theme") == ""
    assert pg.errors == []
    pg.close()


def test_pulse_pill_informs_without_disturbing(ui_server, browser):
    """The pill says unread posts are waiting and does NOTHING else: no scroll
    move, no feed fetching, no inserted content. A click loads them and counts
    as an acknowledgement, so it stops repeating itself until the number grows
    again. The trigger is the server's unread-since-last-read number, so the
    pill may (honestly) speak at boot; /api/pulse is stubbed because this tier
    shares one DB and the pill's number is global."""
    fid, link = _mkfeed("pulse", url=True)
    _seed_article(fid, "old", title="already here", ts="2027-01-01T00:00:00Z")
    token = link.split("//")[1].split(".")[0]        # unique feed hostname
    stub = {"newest_ts": "2027-01-01T00:00:00Z", "ready_total": 1,
            "unread_total": 1, "unread_since_total": 1,
            "since_ts": "2026-12-31T00:00:00Z", "every_minutes": 1,
            "feeds": [{"feed_id": fid, "title": token, "unread": 1,
                       "unread_since": 1, "ts": "2027-01-01T00:00:00Z"}]}
    pg = _new_page(browser, viewport={"width": 1280, "height": 700})
    pg.route("**/api/pulse", lambda r: r.fulfill(json=stub))
    pg.goto(ui_server, wait_until="networkidle")
    pg.wait_for_selector(".card")
    pg.wait_for_selector("#pulse:not([hidden])", timeout=4000)
    txt = pg.locator("#pulse-btn").inner_text()
    assert "newer since your last read" in txt and "tap to jump" in txt, txt
    assert token in txt, f"pill does not name the feed with most to see: {txt}"
    pg.evaluate("window.scrollTo(0, 400)")
    pg.wait_for_timeout(150)
    y_before = pg.evaluate("scrollY")
    assert y_before > 0, "test needs the reader to be scrolled somewhere"
    mine = pg.locator(".card", has_text="already here")
    assert mine.count() == 1

    # a second readable post becomes waiting while the page sits open
    _seed_article(fid, "fresh", title="the new one", ts="2027-06-01T00:00:00Z")
    stub["unread_total"] = stub["feeds"][0]["unread"] = 2
    stub["unread_since_total"] = stub["feeds"][0]["unread_since"] = 2
    pg.evaluate("window.__pulseTick()")
    txt = pg.locator("#pulse-btn").inner_text()
    assert "2 newer since your last read" in txt, txt
    assert pg.evaluate("scrollY") == y_before, "pill moved the reader's place"
    assert mine.count() == 1, "pill inserted content by itself"
    assert all("/api/poll" not in u for u in pg.evaluate(
        "performance.getEntriesByType('resource').map(e => e.name)")), \
        "the pill asked the server to fetch feeds"

    pg.click("#pulse-btn")                      # the load the user asked for
    pg.wait_for_selector(".card:has-text('the new one')", timeout=5000)
    assert pg.is_hidden("#pulse"), "click did not acknowledge the pill"
    pg.evaluate("window.__pulseTick()")
    pg.wait_for_timeout(300)
    assert pg.is_hidden("#pulse"), "the pill repeated itself after a tap"
    stub["unread_since_total"] = stub["feeds"][0]["unread_since"] = 3
    pg.evaluate("window.__pulseTick()")
    pg.wait_for_selector("#pulse:not([hidden])", timeout=4000)
    assert "3 newer" in pg.locator("#pulse-btn").inner_text()
    assert pg.errors == []
    pg.close()


def test_pulse_stays_quiet_for_unreadable_posts(ui_server, browser):
    """A queued post has no card, so it must never be advertised: the SERVER
    leaves it out of unread_since (asserted against the real endpoint for my own
    feed), and the pill says nothing when the number it is handed is 0 (asserted
    with a stub, since this tier shares one DB and other tests' unread posts
    would honestly raise the pill)."""
    fid = _mkfeed("pulseq")
    aid = _seed_article(fid, "base", title="base post", ts="2027-01-01T00:00:00Z")
    from rssgate import db as _db
    _c = _db.connect(UI_DB); _db.mark_articles_read(_c, [aid]); _c.close()
    _seed_article(fid, "queued", title="queued later", status="pending",
                  ts="2027-09-01T00:00:00Z")
    stub = {"newest_ts": "2027-01-01T00:00:00Z", "ready_total": 1,
            "unread_total": 0, "unread_since_total": 0,
            "since_ts": "2027-01-01T00:00:00Z", "every_minutes": 1, "feeds": []}
    pg = _new_page(browser, viewport={"width": 1280, "height": 700})
    pg.route("**/api/pulse", lambda r: r.fulfill(json=stub))
    pg.goto(ui_server, wait_until="networkidle")
    pg.wait_for_selector(".card")
    mine = pg.evaluate("""(f) => fetch('/api/pulse').then(r => r.json()).then(p =>
        (p.feeds.find(x => x.feed_id === f) || {unread_since: 0}).unread_since)""", fid)
    assert mine == 0, "the server counted a post with no card as waiting"
    pg.evaluate("window.__pulseTick()")
    pg.wait_for_timeout(400)
    assert pg.is_hidden("#pulse"), "pill advertised a post with no card"
    assert pg.errors == []
    pg.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_offline_boot_shows_a_plain_error(ui_server, browser, scheme):
    """A refresh while the backend is restarting used to leave a logo, an
    empty sidebar and nothing else. It must say what happened, in the
    current theme, and offer a retry."""
    pg = _new_page(browser, viewport={"width": 1280, "height": 900},
                   color_scheme=scheme)
    pg.route("**/api/**", lambda r: r.abort())     # server unreachable
    pg.goto(ui_server, wait_until="domcontentloaded")
    pg.wait_for_selector("#boot-error:not([hidden])", timeout=6000)
    txt = pg.locator("#boot-error").inner_text().lower()
    assert "not answering" in txt and "try again" in txt
    assert pg.locator("#boot-retry").is_visible()
    assert pg.locator("#stream").inner_text() == ""   # no half-rendered UI
    assert pg.is_hidden("#empty-hint")               # not "no articles yet"
    # theming: the panel uses the variable set, so it is readable in both
    ink = pg.evaluate("getComputedStyle("
                      "document.querySelector('#boot-error h2')).color")
    body = pg.evaluate("getComputedStyle(document.body).color")
    assert ink == body, "offline heading does not follow the theme"
    assert pg.errors == []
    pg.close()


def test_pulse_counts_equal_the_sidebar_pills_in_ui(ui_server, browser):
    """Guard the whole tier's data: the reader's unread figure and the feed
    pills are computed the same way, whatever other tests left behind."""
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server, wait_until="networkidle")
    pill, pulse = pg.evaluate("""() => Promise.all([
        fetch('/api/feeds').then(r => r.json()),
        fetch('/api/pulse').then(r => r.json())])
        .then(([f, p]) => [f.reduce((s, x) => s + (x.unread || 0), 0), p])""")
    assert pulse["unread_total"] == pill, (pill, pulse["unread_total"])
    assert pulse["unread_since_total"] <= pulse["unread_total"], (
        "posts newer than the read marker cannot outnumber all unread posts")
    assert pulse["newest_ts"], "no ready post to compare against"
    assert pg.errors == []
    pg.close()


def test_pulse_pill_never_announces_zero(ui_server, browser):
    """Two bugs in one shape: the pill once printed "0 newer posts", and it
    announced posts that were merely unread rather than waiting. Stub the
    endpoint to reproduce both: 44 unread with nothing newer than the marker
    must be silent, and when it does speak it names the feed with most to see,
    not the biggest backlog."""
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    big = {"feed_id": 1, "title": "Big Old Feed", "unread": 40, "unread_since": 0,
           "ts": "2020-01-01T00:00:00Z"}
    gained = {"feed_id": 2, "title": "Feed That Gained", "unread": 4,
              "unread_since": 0, "ts": "2020-01-01T00:00:00Z"}
    fake = {"newest_ts": "2020-01-01T00:00:00Z", "ready_total": 1,
            "unread_total": 44, "unread_since_total": 0,
            "since_ts": "2020-01-01T00:00:00Z", "every_minutes": 0,
            "feeds": [big, gained]}
    pg.route("**/api/pulse", lambda r: r.fulfill(json=fake))   # before boot
    pg.goto(ui_server, wait_until="networkidle")
    pg.wait_for_selector(".card")
    pg.evaluate("window.__pulseTick()")
    pg.wait_for_timeout(300)
    assert pg.is_hidden("#pulse"), "pill announced 0 waiting posts"

    # posts newer than the marker: speak, name the biggest waiting feed, and
    # count the others only among feeds that ARE waiting
    gained["unread_since"] = big["unread_since"] = 0
    gained["unread"] = 10
    other = {"feed_id": 3, "title": "Other Waiter", "unread": 1,
             "unread_since": 1, "ts": "2020-01-03T00:00:00Z"}
    gained["unread_since"] = 6
    fake.update(feeds=[gained, other, big], unread_since_total=7)
    pg.evaluate("window.__pulseTick()")
    pg.wait_for_selector("#pulse:not([hidden])", timeout=4000)
    txt = pg.locator("#pulse-btn").inner_text()
    assert "7 newer since your last read" in txt, txt
    assert "Feed That Gained" in txt, txt
    assert "+1 more" in txt, f"waiting feeds other than the first not counted: {txt}"
    assert "Big Old Feed" not in txt, f"named a backlog with nothing waiting: {txt}"
    assert pg.errors == []
    pg.close()


def test_pulse_pill_does_not_widen_the_stream(ui_server, browser):
    """The pill must POP over nothing and shift nothing: it lives INSIDE the
    reading column, and showing it may not change the stream's width, the
    cards' width or their left edge (once it was a flex sibling of the stream
    in .layout and pushed every article's text to the right)."""
    fid = _mkfeed("pulgeo")
    _seed_article(fid, "g1", title="geometry probe",
                  summary="body text for width comparison " * 12)
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    stub = {"newest_ts": "2020-01-01T00:00:00Z", "ready_total": 9,
            "unread_total": 3, "unread_since_total": 0,
            "since_ts": "2020-01-01T00:00:00Z", "every_minutes": 0,
            "feeds": [{"feed_id": 99, "title": "Stub", "unread": 3,
                       "unread_since": 0, "ts": "2020-01-01T00:00:00Z"}]}
    pg.route("**/api/pulse", lambda r: r.fulfill(json=stub))
    pg.goto(ui_server, wait_until="networkidle")
    pg.wait_for_selector(".card")
    card = pg.locator(".card", has_text="geometry probe")
    assert card.count() == 1
    pg.wait_for_function("document.getElementById('pulse').hidden === true")
    b0 = card.bounding_box()
    s0 = pg.locator("#stream").bounding_box()
    stub["feeds"][0]["unread_since"] = 3       # now 3 posts are waiting
    stub["unread_since_total"] = 3
    pg.evaluate("window.__pulseTick()")
    pg.wait_for_selector("#pulse:not([hidden])", timeout=4000)
    b1 = card.bounding_box()
    s1 = pg.locator("#stream").bounding_box()
    assert abs(b1["x"] - b0["x"]) < 0.5, f"card moved: {b0} -> {b1}"
    assert abs(b1["width"] - b0["width"]) < 0.5, f"card resized: {b0} -> {b1}"
    assert abs(s1["width"] - s0["width"]) < 0.5, f"stream resized: {s0} -> {s1}"
    # the geometry above is the real assertion (a flex ITEM of .layout
    # squeezed the column by ~291px); these check the structure that prevents
    # it: the pill is inside #stream and positioned, so it is out of the row
    assert pg.evaluate("document.getElementById('pulse').parentElement.id") == "stream"
    assert pg.evaluate("() => getComputedStyle(document.getElementById('pulse'))"
                       ".position !== 'static'")
    # width honoured: flex sizing must not shrink a positioned child
    assert pg.evaluate("() => Math.round(document.getElementById('pulse')"
                       ".getBoundingClientRect().width)") == 520
    assert pg.errors == []
    pg.close()


def test_pulse_pill_overlays_without_pushing_text(ui_server, browser):
    """Second half of "don't disturb": an in-flow pill (inside the column)
    pushed the reader DOWN when it appeared (observed 456 -> 380 scrollY).
    It must be an overlay: showing it may not move the cards vertically
    either, in either direction."""
    fid = _mkfeed("pulov")
    _seed_article(fid, "o1", title="overlay probe",
                  summary="filler body text for vertical geometry " * 20)
    stub = {"newest_ts": "2020-01-01T00:00:00Z", "ready_total": 9,
            "unread_total": 3, "unread_since_total": 0,
            "since_ts": "2020-01-01T00:00:00Z", "every_minutes": 0,
            "feeds": [{"feed_id": 99, "title": "Stub", "unread": 3,
                       "unread_since": 0, "ts": "2020-01-01T00:00:00Z"}]}
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.route("**/api/pulse", lambda r: r.fulfill(json=stub))
    pg.goto(ui_server, wait_until="networkidle")
    pg.wait_for_selector(".card")
    pg.wait_for_function("document.getElementById('pulse').hidden === true")
    card = pg.locator(".card", has_text="overlay probe")
    pg.evaluate("window.scrollTo(0, 300)")
    pg.wait_for_timeout(150)
    y0 = pg.evaluate("scrollY")
    c0 = card.bounding_box()
    pg.evaluate("window.__pulseTick()")                 # nothing waiting yet
    stub["feeds"][0]["unread_since"] = 4
    stub["unread_since_total"] = 4
    pg.evaluate("window.__pulseTick()")
    pg.wait_for_selector("#pulse:not([hidden])", timeout=4000)
    c1 = card.bounding_box()
    assert abs(c1["y"] - c0["y"]) < 0.5, f"cards moved vertically: {c0} -> {c1}"
    assert abs(c1["x"] - c0["x"]) < 0.5, f"cards moved horizontally: {c0} -> {c1}"
    assert pg.evaluate("scrollY") == y0, "showing the pill scrolled the page"
    assert pg.evaluate("""() => {
        const cs = getComputedStyle(document.getElementById('pulse'));
        return cs.position === 'absolute'; }"""), "pill must overlay, not flow"
    assert pg.errors == []
    pg.close()


def _pulse_route(stub, counter):
    """Stub /api/pulse from a mutable dict and count how often it is asked."""
    def handler(req):
        counter.append(1)
        req.fulfill(json=stub)
    return handler


def _shorten_pulse_timer(pg, ms=300):
    """Shorten ONLY the pulse timer, so cadence behaviour is observable in
    seconds. Matches the armed callback by name (/pulse/i), which works both
    for a tick-first loop and an arm-first one."""
    pg.add_init_script("""
      const os = window.setTimeout.bind(window);
      window.setTimeout = (f, d, ...a) =>
        os(f, (typeof f === 'function' && /pulse/i.test(f.name || ''))
                ? %d : d, ...a);
    """ % ms)


def test_pulse_pill_arms_itself_without_any_manual_tick(ui_server, browser):
    """THE regression this file used to miss: every pill test drove
    window.__pulseTick() directly, so the timer chain that is supposed to call
    it went untested - and a normally-loaded page never armed it at all (no
    pill, ever, until you hid the tab). Here nothing is ticked by hand."""
    stub = {"newest_ts": "2020-01-01T00:00:00Z", "ready_total": 0,
            "unread_total": 0, "unread_since_total": 0, "since_ts": "",
            "every_minutes": 1, "feeds": []}
    hits = []
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    _shorten_pulse_timer(pg, 300)
    pg.route("**/api/pulse", _pulse_route(stub, hits))
    # domcontentloaded: a deliberately 300ms pulse timer keeps the network
    # busy forever, so networkidle would never fire (that itself is proof the
    # loop is running)
    pg.goto(ui_server, wait_until="domcontentloaded")
    pg.wait_for_selector(".card")
    assert len(hits) >= 1, "the page never asked /api/pulse at boot"
    n0 = len(hits)
    stub.update(unread_total=3, unread_since_total=3,
                newest_ts="2020-01-02T00:00:00Z", since_ts="2020-01-01T00:00:00Z",
                feeds=[{"feed_id": 77, "title": "Gained Feed", "unread": 3,
                        "unread_since": 3, "ts": "2020-01-02T00:00:00Z"}])
    pg.wait_for_selector("#pulse:not([hidden])", timeout=6000)
    assert len(hits) > n0, "the pill appeared without asking again?!"
    assert "Gained Feed" in pg.locator("#pulse-btn").inner_text()
    assert pg.errors == []
    pg.close()


def test_pulse_poll_rate_is_the_cadence_not_one_per_round_trip(ui_server, browser):
    """Second half of the same bug: pulseTick called schedulePulse, which
    called pulseTick, each clearTimeout-ing the timer it just set - so once a
    tab-return armed it, /api/pulse was asked once per network round trip
    forever (self-DoS, battery burn). At every_minutes=1, a few seconds of a
    loaded page must cost ONE request."""
    stub = {"newest_ts": "2020-01-01T00:00:00Z", "ready_total": 0,
            "unread_total": 0, "unread_since_total": 0, "since_ts": "",
            "every_minutes": 1, "feeds": []}
    hits = []
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.route("**/api/pulse", _pulse_route(stub, hits))
    pg.goto(ui_server, wait_until="domcontentloaded")
    pg.wait_for_selector(".card")
    pg.wait_for_timeout(5000)
    assert len(hits) <= 2, (f"polled {len(hits)}x in ~5s at a 60s cadence: the "
                            "tick and the scheduler are calling each other")
    # and it really does keep going (the chain survives a tick), just slowly
    pg.evaluate("window.__pulseTick()")
    pg.wait_for_timeout(200)
    assert len(hits) >= 2
    pg.close()


def test_backfilled_unread_surfaces_above_read_posts(ui_server, browser):
    """The live symptom: a feed's pip said 1 unread while the top of that
    feed's stream showed nothing but read cards, because the post arrived late
    with an older publish time (published 16:08, fetched 17:03, i.e. newer to
    us, older in the world). Unread-first in a scoped view must lift that card
    above the already-read newer ones."""
    fid = _mkfeed("backfill")
    _seed_loaded_article(fid, "b-new", title="newer already read post",
                         ts="2027-08-05T00:00:00Z")
    _seed_article(fid, "b-old", title="backfilled unread post",
                  ts="2027-08-01T00:00:00Z")
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.goto(ui_server, wait_until="networkidle")
    pg.click(f"#feed-filter li[data-feed='{fid}']")
    pg.wait_for_function(
        "() => [...document.querySelectorAll('#stream .card')].length >= 2",
        timeout=6000)
    unread = pg.locator(".card", has_text="backfilled unread post")
    read = pg.locator(".card", has_text="newer already read post")
    assert unread.count() == 1 and read.count() == 1
    uy, ry = unread.bounding_box()["y"], read.bounding_box()["y"]
    assert uy < ry, (f"backfilled unread card at y={uy} sits below the read "
                     f"post at y={ry}: unread-first is not applied to feeds")
    assert abs(unread.bounding_box()["x"] - read.bounding_box()["x"]) < 1.0
    assert pg.errors == []
    pg.close()


def test_queued_pip_does_not_wear_the_unread_green(ui_server, browser):
    """Regression: the queue pip carried .pip-ok (success green), so a backlog
    of posts waiting to be digested read as unread posts. It must render in
    --muted, and still turn --warn when the AI backend is unreachable."""
    from rssgate import db

    def to_rgb(v):
        v = v.strip().lstrip("#")
        return "rgb(%d, %d, %d)" % tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))

    conn = db.connect(UI_DB)
    fid = db.add_feed(conn, "https://piptone.test/feed", type_="feed")["id"]
    aid = db.upsert_article(conn, fid, "t1", "https://piptone.test/t1",
                           "tone probe", "2027-05-01T00:00:00Z")
    conn.execute("UPDATE articles SET status='pending' WHERE id=?", (aid,))
    conn.commit(); conn.close()
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    down = {"st": False}

    def handler(req):
        req.fulfill(json={"pending": 1, "processing": 0, "errors": 0,
                          "ready": 9, "every_minutes": 1,
                          "llm_down_since": "2026-10-08T00:00:00Z"
                          if down["st"] else ""})
    pg.route("**/api/status", handler)
    pg.goto(ui_server, wait_until="networkidle")
    pg.wait_for_function("document.getElementById('pip-queue')"
                         ".hidden === false", timeout=6000)
    cs = pg.evaluate("""() => {
        const r = getComputedStyle(document.documentElement);
        const q = getComputedStyle(document.getElementById('pip-queue'));
        return {muted: r.getPropertyValue('--muted'), ok: r.getPropertyValue('--ok'),
                col: q.color}; }""")
    assert cs["col"] == to_rgb(cs["muted"]), (
        f"queued pip is {cs['col']}, not muted {cs['muted']}: queued work must "
        "not wear the success/reading colour")
    assert cs["col"] != to_rgb(cs["ok"]), "queued pip is still success green"
    down["st"] = True                        # outage: the SAME pip goes amber
    pg.reload(wait_until="networkidle")
    pg.wait_for_function("document.getElementById('pip-queue')"
                         ".classList.contains('pip-warn')", timeout=6000)
    assert pg.evaluate("getComputedStyle(document.getElementById('pip-queue'))"
                       ".color") != to_rgb(cs["muted"]), "held pip still neutral"
    assert "waiting" in pg.inner_text("#pip-queue")
    assert pg.errors == []
    pg.close()


def test_pulse_pill_survives_a_stream_restart(ui_server, browser):
    """REGRESSION: the pill node lives inside #stream, and restart() does
    stream.innerHTML='' - which detached it, so after the first Refresh, feed
    switch or pill click the pill could never appear again for the rest of the
    session (a null box made every later tick a silent no-op). The node is now
    captured at parse time and re-attached when next needed."""
    fid, link = _mkfeed("pulsesurv", url=True)
    _seed_article(fid, "s1", title="survivor post", ts="2027-02-01T00:00:00Z")
    token = link.split("//")[1].split(".")[0]
    stub = {"newest_ts": "2027-02-01T00:00:00Z", "ready_total": 1,
            "unread_total": 1, "unread_since_total": 1,
            "since_ts": "2027-01-01T00:00:00Z", "every_minutes": 1,
            "feeds": [{"feed_id": fid, "title": token, "unread": 1,
                       "unread_since": 1, "ts": "2027-02-01T00:00:00Z"}]}
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.route("**/api/pulse", lambda r: r.fulfill(json=stub))
    pg.goto(ui_server, wait_until="networkidle")
    pg.wait_for_selector(".card")
    pg.wait_for_selector("#pulse:not([hidden])", timeout=4000)
    assert pg.locator("#pulse").count() == 1, "the pill node vanished"

    pg.click(f"#feed-filter li[data-feed='{fid}']")      # clears the stream
    pg.wait_for_function(
        "() => [...document.querySelectorAll('#stream .card')].some"
        "(c => c.textContent.includes('survivor post'))", timeout=6000)
    stub["unread_since_total"] = stub["feeds"][0]["unread_since"] = 2
    pg.evaluate("window.__pulseTick()")
    pg.wait_for_selector("#pulse:not([hidden])", timeout=4000)
    txt = pg.locator("#pulse-btn").inner_text()
    assert "2 newer since your last read" in txt, txt
    assert pg.errors == []
    pg.close()
