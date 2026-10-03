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
