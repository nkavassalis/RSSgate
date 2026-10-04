"""HTML -> clean text extraction (ad/boilerplate removal, link harvesting)."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

JUNK_RE = re.compile(
    r"(?<![a-z0-9])(ad|ads|advert|advertisement|sponsor|sponsored|promo|promotion|banner|"
    r"sidebar|nav|navbar|menu|footer|header|masthead|comment|comments|disclaimer|"
    r"cookie|newsletter|subscribe|signup|sign-up|social|share|sharing|related|"
    r"recommend|paywall|modal|popup|overlay|player|embed|cta)(?![a-z0-9])", re.I)

STRIP_TAGS = ("script", "style", "noscript", "svg", "iframe", "form", "button",
              "nav", "footer", "header", "aside", "figcaption")

BODY_TAGS = ("p", "li", "blockquote", "h1", "h2", "h3", "h4", "pre", "td")


HARD_JUNK_RE = re.compile(
    r"(?<![a-z0-9])(ad|ads|advert|advertisement|sponsor|sponsored|promo|promotion|banner|"
    r"cookie|newsletter|subscribe|signup|sign-up|paywall|modal|popup|disclaimer|"
    r"comments?)(?![a-z0-9])", re.I)

# Chrome words that frequently appear in classes of elements that WRAP real
# content (WordPress entry-header, layout__document--sidebar, ...). These may
# only be removed when the subtree holds little real text; otherwise unwrap.
SOFT_WORDS = re.compile(
    r"(?<![a-z0-9])(sidebar|nav|navbar|menu|footer|header|masthead|share|sharing|related|"
    r"recommend|social|embed|player|cta)(?![a-z0-9])", re.I)

CONTENTISH_RE = re.compile(
    r"(?<![a-z0-9])(article|post|entry|story|content|body|text)(?![a-z0-9])",
    re.I)   # sites label content slots with junk-ish words (advert__autofill
            # wraps the article on Gematsu); such wrappers are never hard-killed

STRIP_ALWAYS = ("script", "style", "noscript", "svg", "iframe", "form",
                "button", "figcaption")
CHROME_TAGS = ("nav", "footer", "header", "aside")   # weight-checked, not blind


def _ident(el) -> str:
    return " ".join(filter(None, [el.get("id", ""), " ".join(el.get("class", []))]))


def _is_junk(el) -> bool:
    if el.name in ("body", "html"):
        return False   # never nuke the whole page over a silly class name
    return bool(JUNK_RE.search(_ident(el)))


def _ptext_len(el) -> int:
    return sum(len(p.get_text(strip=True)) for p in el.find_all("p", limit=40))


def _wraps_real_article(el) -> bool:
    """Junk-classed wrappers that CONTAIN the true article node (WP pages
    love ad-named divs around <article>/<main>) must be unwrapped, not
    decomposed - the candidate picker discards the shell anyway."""
    for node in el.find_all(("article", "main"), limit=6):
        if _ptext_len(node) > 150:
            return True
    return False


def _prune(soup) -> None:
    """Two-tier junk removal. Hard junk (ads, comments, paywalls) is always
    destroyed. Soft junk (header/sidebar/nav - words that hide inside
    hyphenated CSS like entry-header or document--sidebar) is only removed
    when it contains little paragraph text; content-rich wrappers are
    UNWRAPPED so the article survives. Fixes the Gematsu/Automaton class of
    total-extraction failures."""
    for tag in soup(list(STRIP_ALWAYS)):
        if tag.parent:
            tag.decompose()
    for el in soup.find_all(_is_junk):
        if el.parent is None or el.name in ("body", "html"):
            continue
        ident = _ident(el)
        hard = HARD_JUNK_RE.search(ident) and not CONTENTISH_RE.search(ident)
        if (hard or _ptext_len(el) < 120) and not _wraps_real_article(el):
            el.decompose()
        else:
            el.unwrap()
    for name in CHROME_TAGS:
        for el in soup.find_all(name):
            if el.parent is None:
                continue
            if _ptext_len(el) < 600:
                el.decompose()
            else:
                el.unwrap()


def extract_article_text(html: str, max_chars: int = 24000) -> str:
    """Pull the meaningful article text out of a page, dropping ads & chrome."""
    soup = BeautifulSoup(html, "lxml")
    _prune(soup)

    def weight(el):
        return sum(len(p.get_text(strip=True)) for p in el.find_all("p", limit=30))

    candidates = soup.find_all("article") + soup.find_all("main") \
        + soup.find_all(attrs={"role": "main"}) + [soup.body or soup]
    root = max((c for c in candidates if c), key=weight, default=soup)

    blocks: list[str] = []
    seen: set[str] = set()
    for el in root.find_all(BODY_TAGS):
        text = el.get_text(" ", strip=True)
        if not text or len(text) < 3 or text in seen:
            continue
        if _is_junk(el):
            continue
        seen.add(text)
        if el.name in ("h1", "h2", "h3", "h4"):
            blocks.append(f"## {text}")
        else:
            blocks.append(text)
        if sum(len(b) for b in blocks) > max_chars:
            break
    return "\n\n".join(blocks).strip()[:max_chars]


def page_title(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    return (soup.title.get_text(strip=True) if soup.title else "") or ""


def extract_candidate_links(html: str, base_url: str) -> list[dict]:
    """Harvest plausible article links from a bare page (same host, real text)."""
    soup = BeautifulSoup(html, "lxml")
    host = urlparse(base_url).netloc.lower().removeprefix("www.")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"]).split("#")[0]
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https"):
            continue
        ahost = parsed.netloc.lower().removeprefix("www.")
        if ahost != host and not ahost.endswith("." + host):
            continue
        title = a.get_text(" ", strip=True)
        if not title:
            img = a.find("img")
            title = (img.get("alt", "").strip() if img else "")
        if len(title) < 15 or href in seen:
            continue
        seen.add(href)
        out.append({"title": title[:300], "link": href})
    return out[:60]


AVATAR_RE = re.compile(
    r"(avatar|gravatar|byline|author|profile|userpic|user[-_/]|member|crew|"
    r"mention|staff|comment(\b|er\b|s\b|[-_/])|respondent|persona|"
    r"emoji|emoticon|icon[s]?[-/]|sprite|logo|favicon|button|badge|"
    r"signature|reaction|face[-_/]|\?s=\d{1,3}\b|&s=\d{1,3}\b)", re.I)


CHROME_TAGS = ("aside", "nav", "header", "footer")
ROOT_CLASS_RE = re.compile(
    r"(entry|post|article)[-_ ]?(content|body|text)|article[-_]?main|"
    r"^content$|^article-body$", re.I)


def _in_chrome(img) -> bool:
    el = img
    for _ in range(12):
        el = el.parent
        if el is None:
            break
        if el.name in CHROME_TAGS:
            return True
    return False


def _content_root(soup):
    """The container holding the real article body, when identifiable:
    the <article>/<main>/entry-content ancestor of the <h1> first, then
    the text-heaviest candidate, else None (whole-page scan)."""
    h1 = soup.find("h1")
    cands = [el for el in soup.find_all(["article", "main"])
             if el.find("img") is not None]
    if h1 is not None:
        for el in cands:
            if h1 in el.descendants:
                return el
        for el in soup.find_all(attrs={"class": ROOT_CLASS_RE}):
            if h1 in el.descendants and el.find("img") is not None:
                return el
    for el in cands:
        if h1 is None and len(el.get_text()) > 800:
            return el
    return None


def extract_images(html: str, base_url: str) -> list[str]:
    """Candidate hero/gallery images from an article page: og:image /
    twitter:image first, then content <img>s. Content images are scoped to
    the article root (<article>/<main>/entry-content when present), never
    inside chrome tags (aside/nav/header/footer), never before the <h1>,
    and must survive the avatar/icon/size filter. Returns absolute URLs,
    best first, max 8."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []

    def add(u):
        if not u:
            return
        u = urljoin(base_url, u.strip())
        if u.startswith(("http://", "https://")) and u not in out:
            out.append(u)

    for attrs in ({"property": "og:image"}, {"property": "og:image:url"},
                  {"name": "twitter:image"}):
        m = soup.find("meta", attrs=attrs)
        if m and m.get("content"):
            add(m["content"])

    def reject(img) -> bool:
        parts = [str(img.get("class", "")), str(img.get("id", "")),
                 str(img.get("src") or img.get("data-src") or ""),
                 str(img.get("alt", "")), str(img.get("title", ""))]
        el = img
        for _ in range(4):
            el = el.parent
            if el is None or el.name in (None, "body", "html"):
                break
            parts.append(" ".join([str(el.get("class", "")),
                                   str(el.get("id", ""))]))
        blob = " ".join(parts)
        if AVATAR_RE.search(blob):
            return True
        if JUNK_RE.search(blob):          # junk anywhere in the 4-ancestor chain
            return True
        w = "".join(c for c in str(img.get("width") or "") if c.isdigit())
        h = "".join(c for c in str(img.get("height") or "") if c.isdigit())
        if w and int(w) < 150:
            return True
        if w and h and w == h and int(w) <= 200:
            return True
        return False

    h1 = soup.find("h1")
    root = _content_root(soup)
    scope = root if root is not None else soup
    # positional floor: content images live after the headline
    order = {id(el): i for i, el in enumerate(soup.find_all(True))}
    floor = order.get(id(h1), -1) if h1 is not None else -1

    for img in scope.find_all("img", limit=60):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if not src or src.startswith("data:"):
            continue
        if _in_chrome(img):
            continue
        if root is None and h1 is not None and order.get(id(img), 0) <= floor:
            continue
        if reject(img):
            continue
        add(src)
    return out[:8]
