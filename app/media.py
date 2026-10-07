"""Phase 6: pick the review's demo video and main image from the JV sources.

Video (only used when the sheet's Video URL is empty):
- Only a clearly labelled demo is used (demo / walkthrough / training / tutorial...).
- Anything that looks like the JV / affiliate / webinar video is never used.
- If the label is missing, the video's own title (from YouTube/Vimeo/Wistia) is checked.
- Wistia share links (x.wistia.com/s/...) are converted to the real player link.

Main image (used inside the review when there is no video, and always as the share image):
- Candidates come from the sales page first, then the other pages.
- Logos, icons, badges, payment cards, avatars, bonus covers... are skipped by name/alt text.
- Each candidate is downloaded (max 8 MB) and must be a real JPG/PNG/WebP, landscape,
  at least 600x300. The best-scoring one wins.
Nothing here ever stops a row: problems become notes.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlparse

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_TRIES = 12
MAX_TITLE_LOOKUPS = 6
GALLERY_MAX = 4

VIDEO_GOOD = re.compile(r"(?<![a-z])(demo|walk-?through|training|tutorial|sneak[\s-]*peek|inside[\s-]look|"
                        r"see it in action|how it works|product video|overview video)(?![a-z])", re.I)
VIDEO_BAD = re.compile(r"(?<![a-z])(jv|affiliates?|partners?|commissions?|webinars?|invite|contests?|prizes?|"
                       r"testimonials?|interviews?|launch video|promo video|swipes?|recruit)(?![a-z])", re.I)
IMAGE_SKIP = re.compile(r"logo|icon|favicon|badge|guarantee|seal|payment|paypal|visa|mastercard|amex|"
                        r"credit-?card|avatar|author|profile|headshot|selfie|testimonial|stars?\b|rating|"
                        r"arrow|button|btn|cta|bonus|background|\bbg\b|pattern|divider|spacer|pixel|tracking|"
                        r"facebook|instagram|youtube-thumb|jvzoo|warriorplus|clickbank|checkmark|tick|bullet|"
                        r"emoji|spinner|loader|qr-?code|signature|countdown|timer|banner-ad|ad-banner", re.I)
IMAGE_GOOD = re.compile(r"dashboard|screenshot|screen|demo|app|software|mockup|hero|product|interface|ui\b|"
                        r"preview|platform|header", re.I)
WISTIA_ID = r"([a-z0-9]{10})"


@dataclass
class VideoPick:
    url: str = ""
    note: str = ""
    check: str = ""   # set when the pipeline chose a video by itself (you should look at it)


@dataclass
class ImagePick:
    data: bytes
    mime: str
    filename: str
    width: int
    height: int
    source: str
    alt: str


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


# ------------------------------------------------------------------ video

def _wistia_id_from_url(u: str) -> str:
    m = re.search(r"(?:wistia\.(?:com|net)|wi\.st)/(?:medias|embed/iframe|embed/medias)/" + WISTIA_ID, u, re.I)
    if m:
        return m.group(1).lower()
    q = parse_qs(urlparse(u).query).get("wvideo")
    if q and re.fullmatch(WISTIA_ID, q[0], re.I):
        return q[0].lower()
    return ""


def _wistia_id_from_text(text: str) -> str:
    for pat in (r'"hashedId"\s*:\s*"' + WISTIA_ID + '"', r"wvideo=" + WISTIA_ID,
                r"embed/iframe/" + WISTIA_ID, r"embed/medias/" + WISTIA_ID, r"/medias/" + WISTIA_ID):
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1).lower()
    return ""


def _oembed(u: str, s: requests.Session) -> dict:
    host = urlparse(u).netloc.lower()
    if "youtu" in host:
        api = "https://www.youtube.com/oembed?format=json&url="
    elif "vimeo" in host:
        api = "https://vimeo.com/api/oembed.json?url="
    elif "wistia" in host or host == "wi.st":
        api = "https://fast.wistia.com/oembed?url="
    else:
        return {}
    try:
        r = s.get(api + quote(u, safe=""), timeout=20)
        if r.status_code == 200 and "json" in r.headers.get("Content-Type", ""):
            d = r.json()
            return d if isinstance(d, dict) else {}
    except (requests.RequestException, ValueError):
        pass
    return {}


def normalize_video(u: str, s: requests.Session | None = None) -> tuple[str, str]:
    """Return (link the theme can play, title). Link is "" if it can't be made playable."""
    u = (u or "").strip()
    if not u:
        return "", ""
    low = u.lower()
    if "youtube.com" in low or "youtu.be" in low or "vimeo.com" in low:
        return u, ""
    if "wistia" not in low and "wi.st" not in low:
        return "", ""
    vid = _wistia_id_from_url(u)
    if vid:
        return f"https://fast.wistia.net/embed/iframe/{vid}", ""
    s = s or _session()
    meta = _oembed(u, s)
    vid = _wistia_id_from_text(str(meta.get("html", "")) + " " + str(meta.get("thumbnail_url", "")))
    title = str(meta.get("title") or "")
    if not vid:
        try:
            r = s.get(u, timeout=30)
            if r.status_code == 200:
                vid = _wistia_id_from_text(r.text)
        except requests.RequestException:
            pass
    return (f"https://fast.wistia.net/embed/iframe/{vid}" if vid else ""), title


def video_title(u: str, s: requests.Session) -> str:
    return str(_oembed(u, s).get("title") or "")


def _label(text: str) -> str:
    if VIDEO_BAD.search(text):
        return "bad"
    if VIDEO_GOOD.search(text):
        return "good"
    return ""


def pick_video(candidates: list[dict], s: requests.Session | None = None) -> VideoPick:
    """candidates: [{"url", "context", "source"}] in source order."""
    if not candidates:
        return VideoPick(note="No video found in the sources; the review shows the main image instead.")
    s = s or _session()
    lookups, rejected = 0, 0
    for c in candidates:
        url, ctx = c.get("url", ""), c.get("context", "")
        verdict = _label(ctx + " " + urlparse(url).path.replace("-", " ").replace("_", " "))
        title = ""
        if verdict == "" and lookups < MAX_TITLE_LOOKUPS:
            lookups += 1
            title = video_title(url, s)
            verdict = _label(title)
        if verdict != "good":
            rejected += 1
            continue
        playable, t2 = normalize_video(url, s)
        title = title or t2
        if not playable:
            return VideoPick(note=f"Found a demo video ({url}) but couldn't turn it into a player link. Copy its "
                                  "share link with the video ID (for Wistia: right-click the video → Copy link and "
                                  "thumbnail) into Video URL and tick Rewrite.")
        why = f"labelled \"{_short(ctx or title)}\"" if (ctx or title) else "labelled as a demo"
        return VideoPick(url=playable, check=f"Video: no Video URL in the sheet, so I used the demo video from "
                                             f"{c.get('source', 'the sources')} ({why}). Watch it once before publishing.")
    return VideoPick(note=f"Found {len(candidates)} video(s) but none clearly labelled as a demo (JV/affiliate videos "
                          "are never used). Put the demo link in Video URL if you want one; the review shows the main "
                          "image instead.")


def _short(t: str, n: int = 60) -> str:
    t = " ".join(t.split())
    return t if len(t) <= n else t[: n - 1] + "…"


# ------------------------------------------------------------------ image

def image_size(data: bytes) -> tuple[str, int, int] | None:
    """(mime, width, height) for JPG/PNG/WebP read from the file itself, or None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return "image/png", w, h
    if data[:3] == b"\xff\xd8\xff":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return "image/jpeg", w, h
            i += 2 + seg
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        kind = data[12:16]
        if kind == b"VP8 ":
            w, h = struct.unpack("<HH", data[26:30])
            return "image/webp", w & 0x3FFF, h & 0x3FFF
        if kind == b"VP8L":
            b = data[21:25]
            w = 1 + (((b[1] & 0x3F) << 8) | b[0])
            h = 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
            return "image/webp", w, h
        if kind == b"VP8X":
            w = 1 + int.from_bytes(data[24:27], "little")
            h = 1 + int.from_bytes(data[27:30], "little")
            return "image/webp", w, h
    return None


def _download(u: str, s: requests.Session) -> bytes | None:
    try:
        with s.get(u, timeout=30, stream=True) as r:
            if r.status_code != 200:
                return None
            if int(r.headers.get("Content-Length") or 0) > MAX_IMAGE_BYTES:
                return None
            buf = bytearray()
            for chunk in r.iter_content(64 * 1024):
                buf += chunk
                if len(buf) > MAX_IMAGE_BYTES:
                    return None
            return bytes(buf)
    except requests.RequestException:
        return None


def _name_tokens(product: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (product or "").lower()) if len(t) >= 3 and t not in ("the", "and", "pro")]


def rank_images(candidates: list[dict], product: str, s: requests.Session | None = None,
                max_tries: int = MAX_IMAGE_TRIES) -> tuple[list[ImagePick], int]:
    """All usable product images, best first, plus how many were downloaded to check."""
    s = s or _session()
    ordered = sorted(candidates, key=lambda c: 0 if c.get("sales") else 1)
    tokens = _name_tokens(product)
    slug = re.sub(r"[^a-z0-9]+", "-", (product or "review").lower()).strip("-") or "review"
    tried, scored, seen_data = 0, [], set()
    for c in ordered:
        u, alt = c.get("url", ""), c.get("alt", "")
        path = urlparse(u).path.lower()
        label = f"{path} {alt}".lower()
        if not u.startswith(("http://", "https://")) or IMAGE_SKIP.search(label) or path.endswith((".svg", ".gif", ".ico")):
            continue
        if tried >= max_tries:
            break
        tried += 1
        data = _download(u, s)
        info = image_size(data) if data else None
        if not info or hash(data) in seen_data:
            continue
        seen_data.add(hash(data))
        mime, w, h = info
        if w < 600 or h < 300 or not (1.2 <= w / h <= 2.4):
            continue
        score = min(w * h, 2_000_000) / 2_000_000
        score += 1.0 if c.get("sales") else 0
        score += 0.6 if IMAGE_GOOD.search(label) else 0
        score += 0.6 if any(t in label.replace("-", "").replace("_", "") for t in tokens) else 0
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[mime]
        scored.append((score, len(scored), ImagePick(data=data, mime=mime, filename=f"{slug}-review.{ext}", width=w,
                                                      height=h, source=u, alt=f"{product} screenshot"[:120])))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [p for _, _, p in scored], tried


def pick_image(candidates: list[dict], product: str, s: requests.Session | None = None) -> tuple[ImagePick | None, str]:
    """candidates: [{"url", "alt", "source", "sales": bool}]. Returns (pick or None, note)."""
    ranked, tried = rank_images(candidates, product, s)
    if ranked:
        best = ranked[0]
        return best, f"Main image picked automatically from {best.source} ({best.width}×{best.height})."
    if not candidates:
        return None, "No images found in the sources, so the review has no main image. Add one in wp-admin if needed."
    return None, (f"Checked {tried} image(s) but none looked like a main product image (landscape, at least 600×300). "
                  "Add one in wp-admin (Featured image) if needed.")


def pick_all(candidates: list[dict], product: str, s: requests.Session | None = None,
             want_main: bool = True, want_gallery: bool = True) -> tuple[ImagePick | None, list[ImagePick], list[str]]:
    """One download pass for both the main image and the screenshot gallery."""
    if not candidates:
        notes = []
        if want_main:
            notes.append("No images found in the sources, so the review has no main image. Add one in wp-admin if needed.")
        return None, [], notes
    ranked, tried = rank_images(candidates, product, s, max_tries=MAX_IMAGE_TRIES + GALLERY_MAX * 2)
    notes, main = [], None
    if want_main:
        if ranked:
            main = ranked[0]
            notes.append(f"Main image picked automatically from {main.source} ({main.width}×{main.height}).")
        else:
            notes.append(f"Checked {tried} image(s) but none looked like a main product image (landscape, at least "
                         "600×300). Add one in wp-admin (Featured image) if needed.")
    gallery: list[ImagePick] = []
    if want_gallery:
        rest = ranked[1:] if want_main else ranked
        slug = re.sub(r"[^a-z0-9]+", "-", (product or "review").lower()).strip("-") or "review"
        for i, p in enumerate(rest[:GALLERY_MAX], 1):
            p.filename = f"{slug}-screenshot-{i}.{p.filename.rsplit('.', 1)[-1]}"
            p.alt = f"{product} screenshot {i}"[:120]
            gallery.append(p)
        notes.append(f"{len(gallery)} screenshot(s) added to the \"Inside the product\" section." if gallery else
                     "No extra screenshots found, so the \"Inside the product\" section is hidden.")
    return main, gallery, notes


def _direct_image_url(u: str) -> str:
    """Google Drive share links -> direct download link; anything else unchanged."""
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:export=\w+&)?id=)([A-Za-z0-9_-]{20,})", u)
    return f"https://drive.google.com/uc?export=download&id={m.group(1)}" if m else u


def fetch_image(url: str, product: str, s: requests.Session | None = None) -> tuple[ImagePick | None, str]:
    """The main image from the sheet's Main image URL column."""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None, "Main image URL must start with https://. The review has no main image this time."
    s = s or _session()
    data = _download(_direct_image_url(url), s)
    info = image_size(data) if data else None
    if not info:
        return None, ("Main image URL didn't return a JPG, PNG or WebP image (it may be a web page, need a login, or be "
                      "over 8 MB). Use a link that opens only the image. The review has no main image this time.")
    mime, w, h = info
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[mime]
    slug = re.sub(r"[^a-z0-9]+", "-", (product or "review").lower()).strip("-") or "review"
    note = f"Main image taken from the sheet ({w}×{h})."
    if w < 600:
        note += " It's quite small (under 600 px wide), so it may look blurry in link previews."
    return ImagePick(data=data, mime=mime, filename=f"{slug}-review.{ext}", width=w, height=h, source=url,
                     alt=f"{product} screenshot"[:120]), note
