"""Fetch JV sources: vendor web pages and public Google Docs.

- Google Docs (/document/d/ID/...) are read through the export URL (the edit page has no text).
- Published docs (/document/d/e/.../pub) are plain web pages.
- A login page back means the doc isn't public.
- Web pages: scripts/styles removed, repeated lines removed, HTML entities decoded.
- Links, images and videos are collected from the HTML for later steps.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
MAX_TEXT = 60_000
MIN_USEFUL_TEXT = 500
GDOC_RE = re.compile(r"docs\.google\.com/document/d/(?!e/)([A-Za-z0-9_-]{20,})")
GDOC_PUB_RE = re.compile(r"docs\.google\.com/document/d/e/[A-Za-z0-9_-]+/pub")
VIDEO_RE = re.compile(r"(youtube\.com/(embed/|watch\?v=)|youtu\.be/|player\.vimeo\.com/video/|vimeo\.com/\d+|"
                      r"fast\.wistia\.net|wistia\.com/medias)", re.I)
FOLLOW_RE = re.compile(r"jv.?doc|bonus|preview|sales.?page", re.I)
SKIP_RE = re.compile(r"swipe|affiliate|request|contest|jvzoo\.com|warriorplus|facebook|twitter|"
                     r"skype|youtube|vimeo|mailto:", re.I)


class FetchError(RuntimeError):
    pass


@dataclass
class Source:
    url: str
    kind: str  # "gdoc" | "web"
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)  # (url, anchor text)
    images: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def thin(self) -> bool:
        return len(self.text) < MIN_USEFUL_TEXT


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


def _is_login(resp: requests.Response) -> bool:
    return "accounts.google.com" in resp.url or "ServiceLogin" in resp.url


def _get(s: requests.Session, url: str) -> requests.Response:
    try:
        r = s.get(url, timeout=45, allow_redirects=True)
    except requests.RequestException as e:
        raise FetchError(f"Could not download {url}: {e.__class__.__name__}")
    if _is_login(r):
        raise FetchError(f"{url} needs a Google login, so it isn't public. "
                         "Ask the vendor for a public link or set sharing to 'Anyone with the link'.")
    if r.status_code >= 400:
        raise FetchError(f"{url} returned HTTP {r.status_code}.")
    r.encoding = r.encoding or r.apparent_encoding
    return r


def html_to_parts(html: str, base_url: str) -> tuple[str, list, list, list]:
    soup = BeautifulSoup(html, "html.parser")
    videos: list[str] = []
    for tag in soup.find_all(["iframe", "a", "source", "video"]):
        u = tag.get("src") or tag.get("href") or tag.get("data-src") or ""
        if u and VIDEO_RE.search(u):
            videos.append(urljoin(base_url, u))
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "javascript:")):
            continue
        links.append((_unwrap(urljoin(base_url, href)), " ".join(a.get_text(" ").split())[:80]))
    images = []
    for img in soup.find_all("img"):
        u = img.get("data-src") or img.get("src") or ""
        if not u and img.get("srcset"):
            u = img["srcset"].split(",")[0].split()[0]
        if u and not u.startswith("data:"):
            images.append(urljoin(base_url, u))
    for t in soup(["script", "style", "noscript", "svg", "template", "head"]):
        t.decompose()
    raw = soup.get_text("\n")
    seen, lines = set(), []
    for line in raw.splitlines():
        line = " ".join(line.split())
        if not line or line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return "\n".join(lines), _dedupe(links), _dedupe(images), _dedupe(videos)


def _unwrap(u: str) -> str:
    """Google Docs wraps links as https://www.google.com/url?q=REAL&... ; return REAL."""
    p = urlparse(u)
    if p.netloc.endswith("google.com") and p.path == "/url":
        q = parse_qs(p.query).get("q")
        if q:
            return q[0]
    return u


def _dedupe(items):
    seen, out = set(), []
    for x in items:
        k = x[0] if isinstance(x, tuple) else x
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def fetch(url: str, session: requests.Session | None = None) -> Source:
    s = session or _session()
    url = url.strip()
    m = GDOC_RE.search(url)
    if m:
        doc_id = m.group(1)
        base = f"https://docs.google.com/document/d/{doc_id}/export?format="
        text = _get(s, base + "txt").content.decode("utf-8", errors="replace").lstrip("\ufeff")
        html = _get(s, base + "html").text
        _, links, images, videos = html_to_parts(html, url)
        text = "\n".join(" ".join(x.split()) for x in text.splitlines() if x.strip())
        kind = "gdoc"
    else:
        r = _get(s, url)
        if "html" not in r.headers.get("Content-Type", "html"):
            raise FetchError(f"{url} is not a web page ({r.headers.get('Content-Type')}).")
        text, links, images, videos = html_to_parts(r.text, r.url)
        kind = "web"
    truncated = len(text) > MAX_TEXT
    return Source(url, kind, text[:MAX_TEXT], links, images, videos, truncated)


def pick_follow_links(main: Source, limit: int = 3) -> list[str]:
    """Linked pages worth reading: Google Docs (often the bonus doc) and JV-doc/preview pages."""
    main_host = urlparse(main.url).netloc
    picked: list[str] = []
    for u, text in main.links:
        if u.rstrip("/") == main.url.rstrip("/") or u in picked:
            continue
        if GDOC_RE.search(u) or GDOC_PUB_RE.search(u):
            picked.append(u)
        elif (urlparse(u).netloc == main_host and FOLLOW_RE.search(u + " " + text)
              and not SKIP_RE.search(u + " " + text)):
            picked.append(u)
        if len(picked) >= limit:
            break
    return picked
