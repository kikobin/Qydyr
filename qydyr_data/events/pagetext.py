"""Turn an HTML page into clean main text for the LLM, and find event links on listing pages."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup

DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "form", "nav", "aside", "template")
# header/footer are dropped only when they don't hold the title or a <time> (an <article><header>
# often contains exactly the event title and date).
SOFT_DROP_TAGS = ("header", "footer")
# Blocks with other events ("похожие", "популярное") would leak foreign dates into the main event.
NOISE_RE = re.compile(
    r"(related|similar|recommend|popular|other[-_]?(events|news)|more[-_]?(events|news)|also[-_]|"
    r"banner|advert|promo|cookie|subscribe|share|social|comment|breadcrumb|navbar|\bmenu\b|sidebar|"
    r"modal|popup|swiper|carousel|tags?-list)",
    re.I,
)
KEEP_TAGS = ("html", "body", "main", "article")
MAX_CHARS = 12000


@dataclass
class PageText:
    title: str | None
    text: str
    og_title: str | None
    og_description: str | None
    og_image: str | None
    canonical: str | None


def _meta(soup: BeautifulSoup, prop: str) -> str | None:
    tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    return tag.get("content").strip() if tag and tag.get("content") else None


def main_text(html: str) -> PageText:
    soup = BeautifulSoup(html, "lxml")
    og_title, og_desc, og_img = _meta(soup, "og:title"), _meta(soup, "og:description"), _meta(soup, "og:image")
    canon = soup.find("link", rel="canonical")
    canonical = canon.get("href") if canon else None

    for t in soup(DROP_TAGS):
        t.decompose()
    for t in soup(SOFT_DROP_TAGS):
        if not t.decomposed and t.find(["h1", "time"]) is None:
            t.decompose()
    for el in soup.find_all(True):
        if el.decomposed or el.name in KEEP_TAGS:
            continue
        marker = " ".join(el.get("class", [])) + " " + (el.get("id") or "")
        if marker.strip() and NOISE_RE.search(marker) and el.find("h1") is None:
            el.decompose()

    root = soup.find("main") or soup.find("article") or soup.find(attrs={"role": "main"}) or soup.body or soup
    h1 = root.find("h1") or soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else og_title

    text = root.get_text("\n")
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return PageText(title=title, text=text[:MAX_CHARS], og_title=og_title, og_description=og_desc,
                    og_image=og_img, canonical=canonical)


def find_links(html: str, base_url: str, pattern: str, text_pattern: str | None = None, limit: int = 200) -> list[str]:
    """Absolute links matching a regex (and optionally a regex on link text), in page order, unique."""
    soup = BeautifulSoup(html, "lxml")
    rx = re.compile(pattern)
    trx = re.compile(text_pattern, re.I) if text_pattern else None
    seen: set[str] = set()
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        url, _ = urldefrag(urljoin(base_url, a["href"]))
        if not rx.search(url):
            url = url.split("?")[0]  # tracking params (utm_...) must not hide an event link
            if not rx.search(url):
                continue
        if url in seen:
            continue
        if trx and not trx.search(a.get_text(" ", strip=True) + " " + (a.get("title") or "")):
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= limit:
            break
    return out
