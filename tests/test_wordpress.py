import json

import pytest

from app.config import ConfigError, WPConfig
from app.wordpress import WordPress, WPError


class FakeResp:
    def __init__(self, status=200, body=None, ctype="application/json"):
        self.status_code, self._body = status, body if body is not None else {}
        self.headers = {"Content-Type": ctype}
        self.text = json.dumps(self._body) if ctype.endswith("json") else "<html>blocked</html>"

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls, self.headers, self.auth = list(responses), [], {}, None

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)


CFG = WPConfig("https://staging.stackpiston.com", "pipeline", "abcd efgh ijkl")
REVIEW = {"slug": "comicvideos-ai", "product": {"name": "X"}}


def test_app_password_spaces_removed_and_no_cache_headers():
    s = FakeSession([])
    WordPress(CFG, session=s)
    assert s.auth == ("pipeline", "abcdefghijkl")
    assert s.headers["Cache-Control"] == "no-cache"


def test_create_sends_slug_status_and_meta_string():
    s = FakeSession([FakeResp(201, {"id": 5, "slug": "comicvideos-ai"})])
    WordPress(CFG, session=s).save_review(review=REVIEW, title="T", post_id=None, want_status="draft")
    method, url, kw = s.calls[0]
    assert method == "POST" and url.endswith("/reviews")
    assert kw["json"]["slug"] == "comicvideos-ai" and kw["json"]["status"] == "draft"
    assert json.loads(kw["json"]["meta"]["sp_review_json"]) == REVIEW


def test_update_never_unpublishes_and_keeps_slug():
    s = FakeSession([FakeResp(200, {"id": 5, "status": "publish"}), FakeResp(200, {"id": 5})])
    WordPress(CFG, session=s).save_review(review=REVIEW, title="T", post_id=5, want_status="draft")
    body = s.calls[1][2]["json"]
    assert body["status"] == "publish" and "slug" not in body


def test_scheduled_uses_date_gmt():
    s = FakeSession([FakeResp(201, {"id": 6})])
    WordPress(CFG, session=s).save_review(review=REVIEW, title="T", post_id=None, want_status="future",
                                          date_gmt="2026-10-10T15:00:00")
    assert s.calls[0][2]["json"]["date_gmt"] == "2026-10-10T15:00:00"


def test_lookup_is_authenticated_and_includes_drafts():
    s = FakeSession([FakeResp(200, [])])
    assert WordPress(CFG, session=s).find_by_slug("x") is None
    params = s.calls[0][2]["params"]
    assert params["status"] == "any" and params["context"] == "edit"


def test_firewall_html_gives_clear_error():
    s = FakeSession([FakeResp(403, ctype="text/html")])
    with pytest.raises(WPError, match="firewall"):
        WordPress(CFG, session=s).me()


def test_401_mentions_header_fix():
    s = FakeSession([FakeResp(401, {"code": "rest_not_logged_in", "message": "x"})])
    with pytest.raises(WPError, match="htaccess"):
        WordPress(CFG, session=s).me()


def test_https_required(monkeypatch):
    monkeypatch.setenv("WP_BASE_URL", "http://staging.stackpiston.com")
    monkeypatch.setenv("WP_USER", "pipeline")
    monkeypatch.setenv("WP_APP_PASSWORD", "x")
    with pytest.raises(ConfigError, match="https"):
        WPConfig.from_env()
