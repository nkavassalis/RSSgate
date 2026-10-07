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


def _mkfeed(prefix: str = "t") -> int:
    """Fresh, uniquely-addressed feed in the session DB; returns its id."""
    from rssgate import db
    _feed_seq[0] += 1
    conn = db.connect(UI_DB)
    fid = db.add_feed(conn, f"https://{prefix}-{_feed_seq[0]}-"
                      f"{time.time_ns()}.test/feed", type_="feed")["id"]
    conn.commit(); conn.close()
    return fid


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
    pg.click(".card .snap-btn")
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
    """All-feeds New view must ask the server for unread-first ordering."""
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    urls = []
    pg.on("request", lambda r: urls.append(r.url)
          if "/api/articles" in r.url else None)
    pg.goto(ui_server, wait_until="networkidle")
    assert any("prio=1" in u for u in urls), urls
    # scoped views (single feed) must NOT use priority mode
    pg.click("#feed-filter li[data-feed='1']") if pg.locator("#feed-filter li[data-feed='1']").count() else None
    pg.wait_for_timeout(800)
    assert all("prio=1" not in u for u in urls[-1:]), urls[-1:]
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
    """Zero counts -> both pips display:none. Seed pending+error ->
    counts render, green links to admin queue, red to failures."""
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
