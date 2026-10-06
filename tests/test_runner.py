from datetime import datetime, timezone

import pytest

import app.runner as runner
from app.extract import ExtractResult
from app.sheets import Row, norm, sheet_datetime
from tests.test_write import SAMPLE, FakeClient, copy_from_sample, facts_from_sample

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


class FakeSheet:
    def __init__(self, rows):
        self.rows = rows
        self.updates, self.logs = [], []

    def launches(self):
        return self.rows

    def bonuses(self):
        return [{"type": "Access", "title": "15-Min 1-on-1 Founder Call", "description": "A call.", "value": 97}]

    def method_notes(self):
        return ["Note A", "Note B"]

    def update(self, row, values):
        self.updates.append((row, values))

    def log(self, row):
        self.logs.append(row)

    def final(self, row):
        out = {}
        for r, v in self.updates:
            if r == row:
                out.update(v)
        return out


class FakeWP:
    def __init__(self, warnings=()):
        self.calls, self._warnings, self.next_id = [], list(warnings), 100

    def save_review(self, *, review, title, post_id, want_status, date_gmt=None, **kw):
        self.calls.append(dict(post_id=post_id, status=want_status, date_gmt=date_gmt, title=title, slug=review["slug"]))
        pid = post_id or self.next_id
        return {"id": pid, "slug": review["slug"], "status": want_status, "link": f"https://s.test/reviews/{review['slug']}/",
                "sp_warnings": self._warnings}

    @staticmethod
    def warnings(post):
        return post.get("sp_warnings") or []


def make_row(n=2, **over):
    s = SAMPLE
    vals = {"Status": "Run", "Product name": "ClipForge AI", "JV doc / JV page URL": "https://v.test/jv/",
            "FE affiliate link": s["links"]["frontEnd"],
            "OTO links": "\n".join(o["link"] for o in s["pricing"]["otos"]), "Mode": "Draft"}
    vals.update(over)
    return Row(n, {norm(k): v for k, v in vals.items()})


@pytest.fixture
def ctx(monkeypatch):
    def fake_extract(client, url, sales=None, cart_open_override_utc=None, cart_close_override_utc=None, owner_notes=None):
        f = facts_from_sample()
        f["productName"] = "Something Else"
        if cart_open_override_utc:
            f["cartOpenUtc"] = cart_open_override_utc
        return ExtractResult(facts=f, notes=["source note"])
    monkeypatch.setattr(runner.extract, "run", fake_extract)

    def make(rows, warnings=(), replies=None):
        return runner.Context(client=FakeClient(replies if replies is not None else [copy_from_sample()] * len(rows)),
                              wp=FakeWP(warnings), sheet=FakeSheet(rows), base_url="https://s.test")
    return make


def test_run_row_creates_draft_and_fills_sheet(ctx):
    c = ctx([make_row()])
    counts = runner.poll(c, now_utc=NOW)
    out = c.sheet.final(2)
    assert counts == {"Draft ready": 1} and out["Status"] == "Draft ready"
    assert out["WP post ID"] == 100 and out["Slug"] == "clipforge-ai"
    assert out["Preview link"] == "https://s.test/?post_type=review&p=100&preview=true"
    assert out["Method note used"] == "Note A" and c.wp.calls[0]["title"] == "ClipForge AI Review"
    assert c.sheet.updates[0][1]["Status"] == "Processing" and c.sheet.logs


def test_method_notes_rotate_across_rows(ctx):
    c = ctx([make_row(2), make_row(3, **{"Product name": "Other"})])
    runner.poll(c, now_utc=NOW)
    assert c.sheet.final(2)["Method note used"] == "Note A" and c.sheet.final(3)["Method note used"] == "Note B"


def test_publish_mode_publishes_when_no_warnings(ctx):
    c = ctx([make_row(Mode="Publish")])
    runner.poll(c, now_utc=NOW)
    assert c.sheet.final(2)["Status"] == "Published" and [x["status"] for x in c.wp.calls] == ["draft", "publish"]
    assert c.sheet.final(2)["Preview link"] == "https://s.test/reviews/clipforge-ai/"


def test_publish_mode_with_warnings_stays_draft(ctx):
    c = ctx([make_row(Mode="Publish")], warnings=["seo.title is 63 characters (max 60)."])
    runner.poll(c, now_utc=NOW)
    out = c.sheet.final(2)
    assert out["Status"] == "Draft ready" and "Kept as draft" in out["Messages"] and len(c.wp.calls) == 1


def test_publish_at_in_future_schedules(ctx):
    c = ctx([make_row(Mode="Publish", **{"Publish at": "2026-10-09 09:00", "Timezone": "America/New_York"})])
    runner.poll(c, now_utc=NOW)
    assert c.sheet.final(2)["Status"] == "Scheduled"
    assert c.wp.calls[-1]["status"] == "future" and c.wp.calls[-1]["date_gmt"] == "2026-10-09T13:00:00"


def test_missing_required_fields_need_info_without_ai(ctx):
    c = ctx([make_row(**{"FE affiliate link": ""})], replies=[])
    runner.poll(c, now_utc=NOW)
    out = c.sheet.final(2)
    assert out["Status"] == "Needs info" and "FE affiliate link" in out["Messages"] and not c.wp.calls


def test_existing_post_needs_rewrite_tick(ctx):
    c = ctx([make_row(**{"WP post ID": 55})], replies=[])
    runner.poll(c, now_utc=NOW)
    assert c.sheet.final(2)["Status"] == "Needs info" and "Tick Rewrite" in c.sheet.final(2)["Messages"]


def test_rewrite_updates_same_post_and_unticks(ctx):
    c = ctx([make_row(**{"WP post ID": 55, "Rewrite": True, "Slug": "clipforge-ai"})])
    runner.poll(c, now_utc=NOW)
    out = c.sheet.final(2)
    assert c.wp.calls[0]["post_id"] == 55 and out["Rewrite"] is False and out["WP post ID"] == 55


def test_sheet_product_name_wins_and_cart_open_from_sheet(ctx, monkeypatch):
    seen = {}
    real = runner.write.run

    def spy(client, inp, **kw):
        seen["name"], seen["open"] = inp.facts["productName"], inp.facts.get("cartOpenUtc")
        return real(client, inp, **kw)
    monkeypatch.setattr(runner.write, "run", spy)
    c = ctx([make_row(**{"Cart open": "2026-10-05 11:00"})])
    runner.poll(c, now_utc=NOW)
    assert seen == {"name": "ClipForge AI", "open": "2026-10-05T15:00:00Z"}


def test_extraction_problems_go_to_needs_info(ctx, monkeypatch):
    monkeypatch.setattr(runner.extract, "run", lambda *a, **k: ExtractResult(facts={}, blocking=["OTO count differs."]))
    c = ctx([make_row()], replies=[])
    runner.poll(c, now_utc=NOW)
    assert c.sheet.final(2)["Status"] == "Needs info" and "OTO count differs." in c.sheet.final(2)["Messages"]


def test_crash_in_one_row_does_not_stop_the_next(ctx, monkeypatch):
    real = runner.process_row

    def flaky(row, ctx_, now=None):
        if row.number == 2:
            raise KeyError("boom")
        return real(row, ctx_, now)
    monkeypatch.setattr(runner, "process_row", flaky)
    c = ctx([make_row(2), make_row(3)], replies=[copy_from_sample()])
    runner.poll(c, now_utc=NOW)
    assert c.sheet.final(2)["Status"] == "Error" and "KeyError" in c.sheet.final(2)["Messages"]
    assert c.sheet.final(3)["Status"] == "Draft ready"


def test_stuck_processing_row_marked_error(ctx):
    c = ctx([make_row(Status="Processing", **{"Last run": "2026-10-06 16:00"})], replies=[])
    runner.poll(c, now_utc=NOW)  # 16:00 IST = 10:30 UTC, 90 minutes ago
    assert c.sheet.final(2)["Status"] == "Error"


def test_sheet_datetime_reading():
    assert sheet_datetime(46301.5) == datetime(2026, 10, 6, 12, 0)
    eleven = 46300 + 11 / 24  # Oct 5, 11:00 as Google Sheets stores it (inexact float)
    assert sheet_datetime(eleven) == datetime(2026, 10, 5, 11, 0)
    assert sheet_datetime("2026-10-05 11:00") == datetime(2026, 10, 5, 11, 0)
    assert sheet_datetime("") is None
    with pytest.raises(ValueError):
        sheet_datetime("05/10/2026 11:00")


def test_bad_date_in_sheet_needs_info(ctx):
    c = ctx([make_row(**{"Cart open": "05/10/2026"})], replies=[])
    runner.poll(c, now_utc=NOW)
    assert c.sheet.final(2)["Status"] == "Needs info" and "date picker" in c.sheet.final(2)["Messages"]


def test_post_id_saved_even_if_publishing_step_fails(ctx):
    c = ctx([make_row(Mode="Publish")])
    real = c.wp.save_review

    def failing(**kw):
        if kw["want_status"] == "publish":
            raise runner.WPError("HTTP 500")
        return real(**kw)
    c.wp.save_review = failing
    runner.poll(c, now_utc=NOW)
    out = c.sheet.final(2)
    assert out["Status"] == "Error" and out["WP post ID"] == 100  # next run updates post 100, no duplicate


def test_rewrite_of_live_post_with_warnings_says_live(ctx):
    c = ctx([make_row(Mode="Publish", **{"WP post ID": 55, "Rewrite": True})], warnings=["x"])
    real = c.wp.save_review
    c.wp.save_review = lambda **kw: dict(real(**kw), status="publish")
    runner.poll(c, now_utc=NOW)
    out = c.sheet.final(2)
    assert out["Status"] == "Published" and "already live" in out["Messages"]
