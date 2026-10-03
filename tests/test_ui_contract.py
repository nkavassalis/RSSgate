"""UI contract tests: the bug class pytest otherwise cannot see.

These guard the JS/CSS layer statically, so a change can't ship with:
  - a dialog that beats its own `hidden` attribute (CSS display trap)
  - buttons with ids nobody ever wires a handler to (zombie controls)
  - data-act actions rendered in markup with no branch handling them
  - $('id') references to elements that exist nowhere (typo'd hooks)

They parse source text; they need no browser and stay hermetic."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "rssgate"
ADMIN_JS = (ROOT / "static/admin.js").read_text()
VIEWER_JS = (ROOT / "static/viewer.js").read_text()
ADMIN_HTML = (ROOT / "templates/admin.html").read_text()
VIEWER_HTML = (ROOT / "templates/viewer.html").read_text()
CSS = (ROOT / "static/style.css").read_text()


def test_hidden_attribute_always_wins():
    """The v0.25.1 incident: a class with display:flex silently overrode
    [hidden], making a modal permanently visible with dead buttons."""
    m = re.search(r"\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important", CSS)
    assert m, "global [hidden]{display:none !important} rule is required"


def test_static_dialogs_ship_hidden():
    """Overlay containers in the server-rendered templates must start
    hidden; JS reveals them deliberately."""
    for html, name in ((ADMIN_HTML, "admin"), (VIEWER_HTML, "viewer")):
        for tag in re.findall(r"<(?:div|section)[^>]*class=\"[^\"]*"
                             r"(?:modal-veil|veil)[^\"]*\"[^>]*>", html):
            assert "hidden" in tag, f"{name}: overlay {tag[:60]} lacks hidden"


def test_button_ids_are_referenced_by_js():
    """Every interactive element with a stable id in a template must be
    mentioned by its script (wired, read, or filled)."""
    for html, js in ((ADMIN_HTML, ADMIN_JS), (VIEWER_HTML, VIEWER_JS)):
        for eid in re.findall(r"<(?:button|input|select|textarea)[^>]*"
                              r"id=\"([a-z0-9-]+)\"", html):
            assert eid in js, f"template control #{eid} never referenced by JS"


def test_data_act_actions_all_handled():
    """Markup emits data-act=\"x\"; every emitted action has a handler."""
    for js, html in ((ADMIN_JS, ADMIN_HTML), (VIEWER_JS, VIEWER_HTML)):
        emitted = set(re.findall(r"data-act=\"([a-z-]+)\"", js + html))
        emitted |= set(re.findall(r"data-act='([a-z-]+)'", js + html))
        handled = set(re.findall(r"dataset\.act === '([a-z-]+)'", js))
        handled |= set(re.findall(r"\[data-act=([a-z-]+)\]", js))  # querySelector wiring
        missing = emitted - handled
        assert not missing, f"unhandled data-act values: {missing}"


def test_js_id_hooks_exist_somewhere():
    """$('name') must resolve: the id appears in a template or in JS-built
    markup (createElement/innerHTML), never in neither."""
    for js, html in ((ADMIN_JS, ADMIN_HTML), (VIEWER_JS, VIEWER_HTML)):
        used = set(re.findall(r"\$\('([a-z0-9-]+)'\)", js))
        corpus = js + html
        for name in used:
            ok = (f'id="{name}"' in corpus or f"id='{name}'" in corpus
                  or f".id = '{name}'" in js or f'.id = "{name}"' in js)
            assert ok, f"$('{name}') has no source element"


def test_css_classes_used_in_markup_exist_and_vice_versa():
    """Catch removed-markup-but-styled and styled-forever-orphan classes
    only for the dialog machinery (the incident's blast radius)."""
    for cls in ("modal-veil", "modal"):
        assert f".{cls}" in CSS
    assert "rd-modal" in ADMIN_HTML and "rd-yes" in ADMIN_JS


def test_app_icon_links_present():
    for html, name in ((ADMIN_HTML, "admin"), (VIEWER_HTML, "viewer")):
        assert 'rel="icon"' in html and "favicon.svg" in html, f"{name} lacks icon link"
        assert "apple-touch-icon" in html and "manifest" in html
    for f in ("favicon.svg", "favicon-32.png", "apple-touch-icon.png",
              "icon-192.png", "icon-512.png", "manifest.webmanifest"):
        assert (ROOT / "static" / f).exists(), f


def test_icon_links_are_outside_the_title():
    """v0.30.0 incident: links inserted INSIDE <title> became the literal
    bookmark title (title content is raw text, never markup)."""
    for html, name in ((ADMIN_HTML, "admin"), (VIEWER_HTML, "viewer")):
        t0, t1 = html.index("<title>"), html.index("</title>")
        assert t1 > t0
        chunk = html[t0:t1]
        assert "link" not in chunk and "meta" not in chunk, \
            f"{name}: markup inside <title>"
        assert 'rel="icon"' in html[t1:], "icon links must follow </title>"


def test_rendered_classes_have_css_rules():
    """The v0.30.3 lesson: viewer.js emitted class="readmore" for six
    versions while style.css had NEVER contained a .readmore rule - the
    link wore browser-default blue and every 'verification' grepped the
    wrong file. Classes central to the reading experience must be styled."""
    for cls in ("readmore", "gallery", "card-thumb", "digest", "unsummarized",
                "unread-pill", "chip", "new-above", "end-banner", "lightbox"):
        assert f".{cls}" in CSS or f"#{cls}" in CSS, \
            f".{cls} used in markup but never styled"
    assert "color:var(--link)" in CSS
    assert 'id="ptr"' in VIEWER_HTML and "#ptr" in CSS  # pull-to-refresh wired
