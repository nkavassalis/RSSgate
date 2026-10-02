"""HTML -> clean text extraction (ad/boilerplate removal, link harvesting)."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

JUNK_RE = re.compile(
    r"\b(ad|ads|advert|advertisement|sponsor|sponsored|promo|promotion|banner|"
    r"sidebar|nav|navbar|menu|footer|header|masthead|comment|comments|disclaimer|"
    r"cookie|newsletter|subscribe|signup|sign-up|social|share|sharing|related|"
    r"recommend|paywall|modal|popup|overlay|player|embed|cta)\b", re.I)

STRIP_TAGS = ("script", "style", "noscript", "svg", "iframe", "form", "button",
              "nav", "footer", "header", "aside", "figcaption")

BODY_TAGS = ("p", "li", "blockquote", "h1", "h2", "h3", "h4", "pre", "td")


def _is_junk(el) -> bool:
    if el.name in ("body", "html"):
        return False   # never nuke the whole page over a silly class name
    ident = " ".join(filter(None, [el.get("id", ""), " ".join(el.get("class", []))]))
    return bool(JUNK_RE.search(ident))


def extract_article_text(html: str, max_chars: int = 24000) -> str:
    """Pull the meaningful article text out of a page, dropping ads & chrome."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(list(STRIP_TAGS)):
        tag.decompose()
    for tag in soup.find_all(_is_junk):
        tag.decompose()

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
