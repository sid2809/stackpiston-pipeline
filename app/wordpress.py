"""WordPress REST client for StackPiston reviews.

Rules built in (from the build plan):
- Every request is authenticated (so drafts are visible) and asks caches not to cache.
- A row without a post ID never overwrites an existing post with the same slug.
- The pipeline never unpublishes a live or scheduled post.
- Scheduled posts use date_gmt.
- The slug WordPress actually saved is returned to the caller.
"""
from __future__ import annotations

import json
import re

import requests

from .config import WPConfig

META_KEY = "sp_review_json"
LIVE_STATUSES = {"publish", "future"}
UA = "StackPiston-Pipeline/1.0 (+https://stackpiston.com)"


class WPError(RuntimeError):
    pass


def _explain(resp: requests.Response) -> str:
    ctype = resp.headers.get("Content-Type", "")
    if "json" not in ctype:
        return (f"HTTP {resp.status_code}: the site returned a web page instead of API data. "
                "Usually a firewall or security setting on the host is blocking the request.")
    try:
        body = resp.json()
    except ValueError:
        body = {}
    code = body.get("code", "")
    msg = body.get("message", resp.text[:300])
    if resp.status_code == 401 and code in ("rest_not_logged_in", "incorrect_password", "invalid_username"):
        return (f"HTTP 401 ({code}): login failed. Check WP_USER / WP_APP_PASSWORD. If they are right, "
                "the host is stripping the Authorization header (needs the .htaccess fix).")
    return f"HTTP {resp.status_code} ({code}): {msg}"


class WordPress:
    def __init__(self, cfg: WPConfig, session: requests.Session | None = None, timeout: int = 60):
        self.api = f"{cfg.base_url}/wp-json/wp/v2"
        self.timeout = timeout
        self.s = session or requests.Session()
        self.s.auth = (cfg.user, cfg.app_password.replace(" ", ""))
        self.s.headers.update({"User-Agent": UA, "Cache-Control": "no-cache", "Pragma": "no-cache"})

    # ---------------------------------------------------------- low level
    def _req(self, method: str, path: str, **kw):
        url = path if path.startswith("http") else f"{self.api}{path}"
        resp = self.s.request(method, url, timeout=self.timeout, **kw)
        if resp.status_code >= 400 or "json" not in resp.headers.get("Content-Type", ""):
            raise WPError(_explain(resp))
        return resp.json()

    # ---------------------------------------------------------- reads
    def me(self) -> dict:
        return self._req("GET", "/users/me", params={"context": "edit"})

    def get_review(self, post_id: int) -> dict:
        return self._req("GET", f"/reviews/{int(post_id)}", params={"context": "edit"})

    def find_by_slug(self, slug: str) -> dict | None:
        items = self._req("GET", "/reviews", params={"slug": slug, "status": "any", "context": "edit"})
        return items[0] if items else None

    @staticmethod
    def review_json(post: dict) -> dict | None:
        raw = (post.get("meta") or {}).get(META_KEY) or ""
        try:
            d = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return d if isinstance(d, dict) else None

    @staticmethod
    def warnings(post: dict) -> list[str]:
        return list(post.get("sp_warnings") or [])

    # ---------------------------------------------------------- writes
    def save_review(self, *, review: dict, title: str, post_id: int | None, want_status: str,
                    date_gmt: str | None = None, featured_media: int | None = None,
                    excerpt: str | None = None) -> dict:
        """Create or update one review. Returns the saved post (with sp_warnings).

        post_id given  -> update that post.
        post_id absent -> always create a new post. If the slug is taken, WordPress adds a suffix;
                          the caller compares post['slug'] with review['slug'] and flags it.
        """
        if want_status not in ("draft", "publish", "future"):
            raise ValueError(f"Unsupported status {want_status}")
        body: dict = {
            "title": title,
            "slug": review.get("slug", ""),
            "meta": {META_KEY: json.dumps(review, ensure_ascii=False)},
        }
        if excerpt is not None:
            body["excerpt"] = excerpt
        if featured_media:
            body["featured_media"] = int(featured_media)

        if post_id:
            current = self.get_review(post_id)
            body.pop("slug")  # slug never changes after first run
            status = want_status
            if current.get("status") in LIVE_STATUSES and want_status == "draft":
                status = current["status"]  # never unpublish
            body["status"] = status
            if status == "future" and date_gmt:
                body["date_gmt"] = date_gmt
            return self._req("POST", f"/reviews/{int(post_id)}", json=body)

        body["status"] = want_status
        if want_status == "future" and date_gmt:
            body["date_gmt"] = date_gmt
        return self._req("POST", "/reviews", json=body)

    def upload_media(self, data: bytes, filename: str, mime: str, alt: str = "", source: str = "") -> dict:
        """source: the link the image came from, kept in the image's description so a patch can tell
        whether the sheet's Main image URL changed."""
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-") or "image"
        media = self._req("POST", "/media", data=data, headers={
            "Content-Type": mime, "Content-Disposition": f'attachment; filename="{safe}"'})
        extra = {k: v for k, v in (("alt_text", alt), ("description", f"Source: {source}" if source else "")) if v}
        if extra:
            self._req("POST", f"/media/{media['id']}", json=extra)
        return media

    def get_media(self, media_id: int) -> dict:
        return self._req("GET", f"/media/{int(media_id)}", params={"context": "edit"})

    def delete_review(self, post_id: int) -> None:
        """Only used by the connection check to remove its own test post."""
        self._req("DELETE", f"/reviews/{int(post_id)}", params={"force": "true"})
