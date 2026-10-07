"""Phase 7: patch mode and daily refresh. Neither may call the AI."""
import copy
import json
from datetime import datetime, timezone

import pytest

import app.runner as runner
from app import media, patch
from app.sheets import Row, norm
from app.wordpress import WPError
from tests.test_media import png
from tests.test_runner import FakeSheet
from tests.test_write import SAMPLE

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


class NoAI:
    def complete_json(self, *a, **k):
        raise AssertionError("patch/refresh must never call the AI")


class StoreWP:
    """Keeps posts like WordPress does (review JSON in meta)."""

    def __init__(self, review, status="draft", featured=0, media_desc=""):
        self.posts = {9: {"id": 9, "status": status, "featured_media": featured, "slug": "clipforge-ai",
                          "title": {"raw": "ClipForge AI Review"}, "link": "https://s.test/reviews/clipforge-ai/",
                          "meta": {"sp_review_json": json.dumps(review)}}}
        self.saves, self.uploads, self.media_desc = [], [], media_desc

    def get_review(self, post_id):
        if post_id not in self.posts:
            raise WPError("HTTP 404 (rest_post_invalid_id): Invalid post ID.")
        return copy.deepcopy(self.posts[post_id])

    def save_review(self, *, review, title, post_id, want_status, date_gmt=None, featured_media=None, **kw):
        p = self.posts[post_id]
        if p["status"] in ("publish", "future") and want_status == "draft":
            want_status = p["status"]
        p.update(status=want_status, meta={"sp_review_json": json.dumps(review)})
        if featured_media:
            p["featured_media"] = featured_media
        self.saves.append(dict(status=want_status, featured=featured_media, date_gmt=date_gmt))
        return dict(copy.deepcopy(p), sp_warnings=[])

    def review(self):
        return json.loads(self.posts[9]["meta"]["sp_review_json"])

    @staticmethod
    def warnings(post):
        return post.get("sp_warnings") or []

    def get_media(self, mid):
        return {"id": mid, "source_url": "https://s.test/u/old.png", "description": {"raw": self.media_desc}}

    def upload_media(self, data, filename, mime, alt="", source=""):
        self.uploads.append((filename, source))
        return {"id": 500, "source_url": "https://s.test/u/" + filename}


def row(n=2, **over):
    s = SAMPLE
    vals = {"Status": "Run", "Product name": "ClipForge AI", "JV page URL": "https://v.test/jv/",
            "FE affiliate link": s["links"]["frontEnd"], "Mode": "Draft", "WP post ID": 9, "Slug": "clipforge-ai",
            "OTO links": "\n".join(o["link"] for o in s["pricing"]["otos"])}
    vals.update(over)
    return Row(n, {norm(k): v for k, v in vals.items()})


def ctx_for(wp, rows):
    return runner.Context(client=NoAI(), wp=wp, sheet=FakeSheet(rows), base_url="https://s.test")


def sample_review():
    r = copy.deepcopy(SAMPLE)
    r["review"]["verdict"] = "MY MANUAL EDIT in wp-admin."
    return r


# ------------------------------------------------------------------ patch

def test_patch_updates_links_and_dates_keeps_manual_edits_no_ai():
    wp = StoreWP(sample_review())
    links = [o["link"] for o in SAMPLE["pricing"]["otos"]]
    links[1] = "https://jvz1.com/c/123456/new-oto2"
    c = ctx_for(wp, [row(**{"FE affiliate link": "https://jvz1.com/c/123456/new-fe", "OTO links": "\n".join(links),
                            "Cart close": "2026-10-20 23:59", "Timezone": "America/New_York"})])
    runner.poll(c, now_utc=NOW)
    out, r = c.sheet.final(2), wp.review()
    assert out["Status"] == "Draft ready" and "Patched (no AI, $0): FE link, OTO 2 link, cart close" in out["Messages"]
    assert r["links"]["frontEnd"] == "https://jvz1.com/c/123456/new-fe"
    assert r["pricing"]["otos"][1]["link"] == "https://jvz1.com/c/123456/new-oto2"
    assert r["launch"]["cartClose"] == "2026-10-21T03:59:00Z"
    assert r["review"]["verdict"] == "MY MANUAL EDIT in wp-admin."  # manual edit kept
    assert r["review"]["updatedAt"] == "2026-10-07" and out["WP post ID"] == 9


def test_patch_cannot_change_number_of_otos():
    wp = StoreWP(sample_review())
    c = ctx_for(wp, [row(**{"OTO links": "https://jvz1.com/c/1/a\nhttps://jvz1.com/c/1/b"})])
    runner.poll(c, now_utc=NOW)
    out = c.sheet.final(2)
    assert out["Status"] == "Needs info" and "tick Rewrite" in out["Messages"] and not wp.saves


def test_patch_with_nothing_changed_saves_nothing():
    wp = StoreWP(sample_review())
    c = ctx_for(wp, [row()])
    runner.poll(c, now_utc=NOW)
    assert "Nothing to update" in c.sheet.final(2)["Messages"] and not wp.saves


def test_patch_video_link_is_converted():
    wp = StoreWP(sample_review())
    c = ctx_for(wp, [row(**{"Video URL": "https://j.wistia.com/s/6sym77ugnbd51d3?wvideo=mmi98bf00z"})])
    runner.poll(c, now_utc=NOW)
    m = wp.review()["media"]
    assert m["type"] == "video" and m["videoUrl"] == "https://fast.wistia.net/embed/iframe/mmi98bf00z"
    assert m["videoTitle"] == SAMPLE["media"]["videoTitle"]


def test_patch_main_image_only_when_link_is_new(monkeypatch):
    monkeypatch.setattr(media, "fetch_image", lambda u, p, s=None: (
        media.ImagePick(png(1200, 630), "image/png", "clipforge-ai-review.png", 1200, 630, u, "alt"), "Main image taken."))
    wp = StoreWP(sample_review(), featured=40, media_desc="Source: https://v.test/old.png")
    c = ctx_for(wp, [row(**{"Main image URL": "https://v.test/new.png"})])
    runner.poll(c, now_utc=NOW)
    assert wp.uploads == [("clipforge-ai-review.png", "https://v.test/new.png")] and wp.saves[0]["featured"] == 500

    wp2 = StoreWP(sample_review(), featured=40, media_desc="Source: https://v.test/same.png")
    c2 = ctx_for(wp2, [row(**{"Main image URL": "https://v.test/same.png"})])
    runner.poll(c2, now_utc=NOW)
    assert wp2.uploads == [] and "Nothing to update" in c2.sheet.final(2)["Messages"]


def test_patch_with_mode_publish_publishes_without_ai():
    wp = StoreWP(sample_review())
    c = ctx_for(wp, [row(Mode="Publish")])
    runner.poll(c, now_utc=NOW)
    assert c.sheet.final(2)["Status"] == "Published" and [x["status"] for x in wp.saves] == ["draft", "publish"]


def test_patch_never_unpublishes_a_live_post():
    wp = StoreWP(sample_review(), status="publish")
    c = ctx_for(wp, [row(**{"FE affiliate link": "https://jvz1.com/c/123456/new-fe"})])
    runner.poll(c, now_utc=NOW)
    assert wp.posts[9]["status"] == "publish" and c.sheet.final(2)["Status"] == "Published"


def test_patch_missing_post_needs_info():
    wp = StoreWP(sample_review())
    c = ctx_for(wp, [row(**{"WP post ID": 77})])
    runner.poll(c, now_utc=NOW)
    out = c.sheet.final(2)
    assert out["Status"] == "Needs info" and "clear WP post ID" in out["Messages"]


# ------------------------------------------------------------------ refresh

def ended_review(**fe_over):
    r = sample_review()
    r["launch"]["cartClose"] = "2026-10-06T03:59:00Z"
    r["pricing"]["frontEnd"].update(fe_over)
    r["review"]["verdict"] = "Good value at $17 during launch."
    return r


def refresh_row(n=2, **over):
    vals = {"Status": "Draft ready", "Product name": "ClipForge AI", "WP post ID": 9, "Slug": "clipforge-ai"}
    vals.update(over)
    return Row(n, {norm(k): v for k, v in vals.items()})


def test_refresh_switches_price_after_launch_and_keeps_coupons():
    wp = StoreWP(ended_review(coupon={"code": "SAVE5", "discount": "$5 off"}), status="publish")
    c = ctx_for(wp, [refresh_row(Status="Published")])
    counts = patch.refresh(c, now_utc=NOW)
    r, out = wp.review(), c.sheet.final(2)
    assert r["pricing"]["frontEnd"]["price"] == 47 and "priceAfterLaunch" not in r["pricing"]["frontEnd"]
    assert r["pricing"]["frontEnd"]["coupon"] == {"code": "SAVE5", "discount": "$5 off"}
    assert r["review"]["verdict"] == "Good value at $47 during launch."
    assert out["Post-launch done"].startswith("Yes (") and "switched from $17 to $47" in out["Messages"]
    assert wp.posts[9]["status"] == "publish" and counts == {"Post-launch switched": 1}


def test_refresh_does_nothing_before_launch_ends_or_twice():
    r = ended_review()
    r["launch"]["cartClose"] = "2026-10-20T03:59:00Z"
    wp = StoreWP(r)
    c = ctx_for(wp, [refresh_row()])
    assert patch.refresh(c, now_utc=NOW) == {} and not wp.saves
    wp2 = StoreWP(ended_review())
    c2 = ctx_for(wp2, [refresh_row(**{"Post-launch done": "Yes (2026-10-06)"})])
    assert patch.refresh(c2, now_utc=NOW) == {} and not wp2.saves


def test_refresh_without_after_launch_price_only_notes_it():
    r = ended_review()
    r["pricing"]["frontEnd"].pop("priceAfterLaunch")
    wp = StoreWP(r)
    c = ctx_for(wp, [refresh_row()])
    patch.refresh(c, now_utc=NOW)
    out = c.sheet.final(2)
    assert not wp.saves and "No after-launch price was stated" in out["Messages"] and out["Post-launch done"]


def test_refresh_leaves_text_when_old_price_is_shared():
    r = ended_review(price=37, priceAfterLaunch=67)  # an OTO also costs $37
    r["review"]["verdict"] = "Worth $37."
    new, msg = patch.switch_to_after_launch(r)
    assert new["pricing"]["frontEnd"]["price"] == 67 and new["review"]["verdict"] == "Worth $37."
    assert "another offer costs the same" in msg


def test_price_replace_is_exact():
    counter = [0]
    out = patch._replace_price(["$17", "$17.", "$17.50", "$170", "$17,000", "save $17 now"], "17", "47", counter)
    assert out == ["$47", "$47.", "$17.50", "$170", "$17,000", "save $47 now"] and counter[0] == 3


def test_refresh_syncs_status_from_wordpress():
    r = sample_review()
    r["launch"]["cartClose"] = "2026-10-20T03:59:00Z"
    wp = StoreWP(r, status="publish")
    c = ctx_for(wp, [refresh_row(), refresh_row(3, Status="Run"), refresh_row(4, Status="Needs info")])
    counts = patch.refresh(c, now_utc=NOW)
    assert c.sheet.final(2)["Status"] == "Published" and c.sheet.final(2)["Preview link"].endswith("/clipforge-ai/")
    assert c.sheet.final(3) == {} and c.sheet.final(4) == {} and counts == {"Published": 1}


def test_refresh_command_needs_no_ai_settings(monkeypatch):
    import app.main as main
    seen = {}

    def fake_build(need_ai=True):
        seen["need_ai"] = need_ai
        return ctx_for(StoreWP(sample_review()), [])
    monkeypatch.setattr(runner, "build_context", fake_build)
    assert main.main(["refresh"]) == 0 and seen == {"need_ai": False}


@pytest.mark.parametrize("label", ["EDT", "EST"])
def test_tz_label(label):
    utc = "2026-10-06T15:00:00Z" if label == "EDT" else "2026-12-06T16:00:00Z"
    assert patch._tz_label(utc) == label


# ------------------------------------------------------------------ review fixes

def test_patch_and_refresh_never_touch_a_trashed_post():
    wp = StoreWP(sample_review(), status="trash")
    c = ctx_for(wp, [row(**{"FE affiliate link": "https://jvz1.com/c/123456/new-fe"})])
    runner.poll(c, now_utc=NOW)
    assert c.sheet.final(2)["Status"] == "Needs info" and "trash" in c.sheet.final(2)["Messages"] and not wp.saves
    wp2 = StoreWP(ended_review(), status="trash")
    c2 = ctx_for(wp2, [refresh_row()])
    assert patch.refresh(c2, now_utc=NOW) == {} and not wp2.saves and wp2.posts[9]["status"] == "trash"
    assert c2.sheet.logs and c2.sheet.logs[0][4] == "Skipped"


def test_patch_image_link_compare_is_exact(monkeypatch):
    monkeypatch.setattr(media, "fetch_image", lambda u, p, s=None: (
        media.ImagePick(png(1200, 630), "image/png", "clipforge-ai-review.png", 1200, 630, u, "alt"), "ok"))
    wp = StoreWP(sample_review(), featured=40, media_desc="Source: https://v.test/a.png?v=2")
    c = ctx_for(wp, [row(**{"Main image URL": "https://v.test/a.png"})])
    runner.poll(c, now_utc=NOW)
    assert wp.uploads == [("clipforge-ai-review.png", "https://v.test/a.png")]


def test_patch_publish_at_in_draft_mode_saves_nothing():
    wp = StoreWP(sample_review())
    c = ctx_for(wp, [row(**{"Publish at": "2026-10-09 09:00"})])
    runner.poll(c, now_utc=NOW)
    assert not wp.saves and "Nothing to update" in c.sheet.final(2)["Messages"]


def test_patch_with_blank_bundle_links_keeps_bundles():
    bund = json.load(open("tests/fixtures/review-with-bundles.json"))
    wp = StoreWP(bund)
    r = Row(2, {norm(k): v for k, v in {
        "Status": "Run", "Product name": "ClipForge AI", "JV page URL": "https://v.test/jv/", "Mode": "Draft",
        "FE affiliate link": "https://jvz1.com/c/123456/changed-fe", "WP post ID": 9, "Slug": "clipforge-ai",
        "OTO links": "\n".join(o["link"] for o in bund["pricing"]["otos"])}.items()})
    c = ctx_for(wp, [r])
    runner.poll(c, now_utc=NOW)
    out = c.sheet.final(2)
    assert out["Status"] == "Draft ready" and "FE link" in out["Messages"]
    assert [b["link"] for b in wp.review()["pricing"]["bundles"]] == [b["link"] for b in bund["pricing"]["bundles"]]


def test_refresh_survives_odd_shapes():
    r = ended_review()
    r["pricing"]["otos"].append("not an offer")
    wp = StoreWP(r)
    wp.posts[9]["title"] = "Plain string title"
    c = ctx_for(wp, [refresh_row()])
    patch.refresh(c, now_utc=NOW)
    assert wp.review()["pricing"]["frontEnd"]["price"] == 47


def test_rewrite_checks_the_post_before_any_ai():
    wp = StoreWP(sample_review(), status="trash")
    c = ctx_for(wp, [row(Rewrite=True)])  # NoAI client: any AI call would fail the test
    runner.poll(c, now_utc=NOW)
    out = c.sheet.final(2)
    assert out["Status"] == "Needs info" and "No AI was used" in out["Messages"] and not wp.saves
    wp2 = StoreWP(sample_review())
    c2 = ctx_for(wp2, [row(Rewrite=True, **{"WP post ID": 77})])
    runner.poll(c2, now_utc=NOW)
    assert c2.sheet.final(2)["Status"] == "Needs info" and "No AI was used" in c2.sheet.final(2)["Messages"]


def test_auto_runs_refresh_then_poll_and_survives_refresh_failure(monkeypatch, capsys):
    import app.main as main
    order = []
    import app.llm as llm
    monkeypatch.setattr(runner, "build_context", lambda need_ai=True: ctx_for(StoreWP(sample_review()), []))
    monkeypatch.setattr(llm, "get_client", lambda: NoAI())
    monkeypatch.setattr(patch, "refresh", lambda ctx: order.append("refresh") or {"Published": 1})
    monkeypatch.setattr(runner, "poll", lambda ctx: order.append("poll") or {})
    assert main.main(["auto"]) == 0 and order == ["refresh", "poll"]
    assert "OK  refresh: 1 Published" in capsys.readouterr().out

    def boom(ctx):
        raise RuntimeError("sheet hiccup")
    order.clear()
    monkeypatch.setattr(patch, "refresh", boom)
    assert main.main(["auto"]) == 0 and order == ["poll"]
    assert "continuing with poll" in capsys.readouterr().out


def test_auto_still_refreshes_when_ai_settings_are_broken(monkeypatch, capsys):
    import app.main as main
    order = []
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setattr(runner, "build_context", lambda need_ai=True: ctx_for(StoreWP(sample_review()), []))
    monkeypatch.setattr(patch, "refresh", lambda ctx: order.append("refresh") or {})
    monkeypatch.setattr(runner, "poll", lambda ctx: order.append("poll") or {})
    assert main.main(["auto"]) == 1 and order == ["refresh"]
    out = capsys.readouterr().out
    assert "OK  refresh" in out and "poll skipped: Missing setting LLM_MODEL" in out


def test_auto_reports_unexpected_poll_error_cleanly_and_scrubbed(monkeypatch, capsys):
    import app.llm as llm
    import app.main as main
    monkeypatch.setenv("WP_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setattr(runner, "build_context", lambda need_ai=True: ctx_for(StoreWP(sample_review()), []))
    monkeypatch.setattr(llm, "get_client", lambda: NoAI())
    monkeypatch.setattr(patch, "refresh", lambda ctx: {})

    def boom(ctx):
        raise RuntimeError("sheet down, auth abcdefghijklmnop failed")
    monkeypatch.setattr(runner, "poll", boom)
    assert main.main(["auto"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  poll stopped (RuntimeError" in out and "abcdefghijklmnop" not in out and "***" in out


def test_scrub_hides_secrets(monkeypatch):
    from app.safety import scrub
    monkeypatch.setenv("WP_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-SECRETSECRETSECRET")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "eyJ0eXBlIjoic2VydmljZV9hY2NvdW50In0=")
    text = ("pw abcd efgh ijkl mnop / abcdefghijklmnop key sk-ant-api03-SECRETSECRETSECRET other sk-proj-ABCDEFGHIJKLMNOPQRST "
            "b64 eyJ0eXBlIjoic2VydmljZV9hY2NvdW50In0= pk -----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY----- "
            '"private_key": "xyz" ok')
    out = scrub(text)
    for secret in ("abcd efgh", "abcdefghijklmnop", "SECRETSECRET", "ABCDEFGHIJKLMNOPQRST", "eyJ0eXBl", "MIIE", "xyz"):
        assert secret not in out
    assert out.endswith("ok") and scrub(None) == "" and scrub("plain text") == "plain text"


def test_sheet_messages_are_scrubbed(monkeypatch):
    monkeypatch.setenv("WP_APP_PASSWORD", "abcd efgh ijkl mnop")

    class LeakyWP(StoreWP):
        def get_review(self, post_id):
            raise RuntimeError("weird error mentioning abcd efgh ijkl mnop")
    c = ctx_for(LeakyWP(sample_review()), [row()])
    runner.poll(c, now_utc=NOW)
    out = c.sheet.final(2)
    assert "abcd efgh" not in out["Messages"] and "***" in out["Messages"] and out["Status"] == "Error"
    assert all("abcd efgh" not in str(x) for x in c.sheet.logs)
