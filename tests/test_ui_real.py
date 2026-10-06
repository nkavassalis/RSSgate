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
import os
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
        db.set_article(conn, aid, image=None)
        # second feed left disabled on purpose (layout + poll semantics)
        db.update_feed(conn, fid, summarize=1)
        conn.close()
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
    """Synthesized touch sequence: pill becomes visible, armed state at
    threshold, release fires POST /api/poll, spinner shows."""
    pg = _new_page(browser, viewport={"width": 390, "height": 844},
                   has_touch=True)
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
    pg.wait_for_function("() => document.getElementById('ptr')"
                         ".classList.contains('spin')", timeout=3000)
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


def test_ads_checkbox_autosaves_and_survives_reload(ui_server, browser):
    """The Techdirt report: ticking Ads must persist WITHOUT a Save click
    and must not throw (the missing-enabled-box TypeError ate every save
    for a month)."""
    pg = _new_page(browser, viewport={"width": 1280, "height": 900})
    pg.on("dialog", lambda d: d.accept())
    pg.goto(ui_server + "/admin", wait_until="networkidle")
    pg.fill("#new-url", "https://ads.test/feed"); pg.select_option("#new-type","feed")
    pg.click("#add-feed-btn"); pg.wait_for_timeout(1200)
    fid = pg.eval_on_selector("tr[data-id]", "el => el.dataset.id")
    puts = []
    pg.on("request", lambda r: puts.append(r.post_data)
          if r.method == "PUT" and r.url.endswith(f"/api/feeds/{fid}") else None)
    row = pg.locator(f"tr[data-id='{fid}']")
    assert row.locator("[data-role=enabled]").count() == 1   # box is back
    row.locator("[data-role=spons]").check()
    pg.wait_for_selector("td.saved", timeout=5000)
    assert puts and '"hide_sponsored":true' in puts[0]
    assert pg.errors == [], "page threw during checkbox save"
    pg.reload(wait_until="networkidle")
    row = pg.locator(f"tr[data-id='{fid}']")
    assert row.locator("[data-role=spons]").is_checked()
    # Save button must work too (the old null-deref path)
    row.locator("button[data-act=save]").click()
    pg.wait_for_timeout(500)
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
    pg.click(f"#feed-filter li[data-feed='1']") if pg.locator("#feed-filter li[data-feed='1']").count() else None
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
    pg.wait_for_selector("label.cfg-ok", timeout=5000)
    assert puts and "stream_width" in puts[-1] and "1000" in puts[-1]
    saved = pg.evaluate("() => fetch('/api/config').then(r => r.json())"
                        ".then(c => c.ui.stream_width)")
    assert saved == 1000  # explicit set; defaults now 800/1280
    assert pg.locator(".sec-nav a").count() == 9  # nav incl. Status
    # every nav link must FEED BACK active on click (perceived success),
    # including bottom sections that can't scroll to the spy band
    for href in ("#sec-queue", "#sec-usage", "#sec-feeds"):
        pg.click(f'.sec-nav a[href="{href}"]')
        pg.wait_for_timeout(350)
        assert pg.locator(f'.sec-nav a[href="{href}"].active').count() == 1, href
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
