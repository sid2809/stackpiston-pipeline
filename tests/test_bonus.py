"""Buyer bonus: rules (same answers as the theme's PHP), AI step, and how a run uses it."""
import copy
import json
from pathlib import Path

import app.runner as runner
from app import bonus
from app.llm import LLMError, LLMResult
from tests.test_runner import NOW, FakeWP, make_row
from tests.test_write import SAMPLE, FakeClient, copy_from_sample

FIX = Path(__file__).parent / "fixtures"
REPLY = json.loads((FIX / "sample_bonus_reply.json").read_text(encoding="utf-8"))


def reply(**over):
    r = copy.deepcopy(REPLY)
    r.update(over)
    return r


# ------------------------------------------------------------------ rules: parity with the theme

def test_validator_matches_theme_php_on_every_case():
    cases = json.loads((FIX / "bonus_cases.json").read_text(encoding="utf-8"))
    php = json.loads((FIX / "bonus_cases_php.json").read_text(encoding="utf-8"))
    assert len(cases) >= 60
    for c in cases:
        py, ph = bonus.validate(c["bonus"]), php[c["name"]]
        assert bool(py) == bool(ph), (c["name"], py, ph)  # same pass / fail
        if c["name"] != "not object":  # a JSON list at the top: both reject it, with different wording
            assert py == ph, (c["name"], py, ph)
        if c["ok"] is not None:
            assert (not py) == c["ok"], c["name"]


def test_new_code_shape_and_randomness():
    codes = {bonus.new_code() for _ in range(200)}
    assert len(codes) == 200 and all(bonus.CODE_RE.match(c) for c in codes)


def test_brief_sends_no_prices():
    r = copy.deepcopy(SAMPLE)
    r["cons"] = ["OTOs add $200 to the cost", "50 renders a month"]
    r["pricing"]["frontEnd"]["items"] = ["Commercial license ($47 value)"]
    text = json.dumps(bonus.brief(r))
    assert "$" not in text and "50 renders a month" in text and '"price"' not in text
    assert "NOT available to front-end buyers" in text and "OTO 1:" in text


def test_existing_bonus_reading():
    good = dict(REPLY, v=1, code="abcdefgh2345")
    assert bonus.existing({"meta": {"sp_bonus_json": json.dumps(good)}}) == (good, True)
    bad = dict(good, plan=None)
    assert bonus.existing({"meta": {"sp_bonus_json": json.dumps(bad)}})[1] is False
    assert bonus.existing({"meta": {}}) == (None, False)
    assert bonus.existing({"meta": {"sp_bonus_json": "{not json"}}) == (None, False)


# ------------------------------------------------------------------ the AI step

def test_good_reply_first_try():
    c = FakeClient([reply()])
    res = bonus.generate(c, SAMPLE)
    assert res.bonus and not bonus.validate(res.bonus) and res.attempts == 1 and c.calls == 1
    assert res.bonus["v"] == 1 and bonus.CODE_RE.match(res.bonus["code"])
    assert (res.input_tokens, res.output_tokens) == (1000, 500)


def test_keeps_given_code():
    assert bonus.generate(FakeClient([reply()]), SAMPLE, code="keepme234567").bonus["code"] == "keepme234567"
    assert bonus.generate(FakeClient([reply()]), SAMPLE, code="BAD").bonus["code"] != "BAD"


def test_reply_with_money_is_fixed():
    bad = reply()
    bad["plan"]["intro"] = "Spend $5 a day on ads and you'll see results this week."
    c = FakeClient([bad, reply()])
    res = bonus.generate(c, SAMPLE)
    assert res.bonus and res.attempts == 2 and c.calls == 2 and "$" not in json.dumps(res.bonus)


def test_bad_tool_is_dropped_but_plan_kept():
    bad = reply()
    bad["tool"]["bands"][2]["min"] = 10  # no band starting at 0
    res = bonus.generate(FakeClient([bad, copy.deepcopy(bad)]), SAMPLE)
    assert res.bonus and res.bonus["tool"] is None and res.bonus["plan"]["days"]
    assert any("gap-fixer tool was dropped" in n for n in res.notes)


def test_bad_plan_skips_bonus_without_raising():
    bad = reply()
    bad["plan"]["days"] = bad["plan"]["days"][:5]
    res = bonus.generate(FakeClient([bad, copy.deepcopy(bad)]), SAMPLE)
    assert res.bonus is None and any(n.startswith("BONUS skipped") for n in res.notes)


def test_ai_failure_skips_bonus_without_raising():
    class Boom:
        def complete_json(self, *a, **k):
            raise LLMError("Anthropic rate limit or spend limit reached.")
    res = bonus.generate(Boom(), SAMPLE)
    assert res.bonus is None and "rate limit" in res.notes[0]


def test_fixer_failure_keeps_going():
    calls = []

    class Half:
        def complete_json(self, system, user, max_tokens=0, **kw):
            calls.append(system)
            if len(calls) == 1:
                bad = reply()
                bad["tool"]["engine"] = "html"
                return LLMResult(bad, "", 10, 5, "fake")
            raise LLMError("network")
    res = bonus.generate(Half(), SAMPLE)
    assert res.bonus and res.bonus["tool"] is None and len(calls) == 2


# ------------------------------------------------------------------ in a run

class BonusWP(FakeWP):
    """A site with theme 1.5.0: posts report sp_bonus_status, and the bonus can be saved."""

    def __init__(self, existing=None, fail_save=False, **kw):
        super().__init__(**kw)
        self.existing, self.fail_save, self.saved = existing, fail_save, []

    def _status(self, b):
        if not b:
            return {"saved": False, "ok": False, "errors": [], "url": ""}
        errs = bonus.validate(b)
        return {"saved": True, "ok": not errs, "errors": errs,
                "url": "" if errs else f"https://s.test/bonus/clipforge-ai-{b['code']}/"}

    def save_review(self, **kw):
        post = super().save_review(**kw)
        post["sp_bonus_status"] = self._status(self.saved[-1] if self.saved else self.existing)
        return post

    def save_bonus(self, post_id, b):
        if self.fail_save:
            raise runner.WPError("HTTP 500 (oops): boom")
        self.calls.append(dict(post_id=post_id, status="bonus"))
        self.saved.append(b)
        return {"id": post_id, "sp_bonus_status": self._status(b)}

    def get_review(self, post_id):
        meta = {"sp_bonus_json": json.dumps(self.existing)} if self.existing else {}
        return {"id": post_id, "status": "draft", "featured_media": 0, "meta": meta,
                "sp_bonus_status": self._status(self.existing)}


def run_rows(ctx, rows, replies, wp):
    c = ctx(rows, replies=replies)
    c.wp = wp
    runner.poll(c, now_utc=NOW)
    return c


def test_new_review_gets_bonus_link_and_usage_log(ctx):
    c = run_rows(ctx, [make_row(**{"Bonus link": ""})], [copy_from_sample(), reply()], BonusWP())
    out = c.sheet.final(2)
    assert out["Status"] == "Draft ready" and c.client.calls == 2
    assert [x["status"] for x in c.wp.calls] == ["draft", "bonus"]  # bonus only after the review is saved
    assert out["Bonus link"].startswith("https://s.test/bonus/clipforge-ai-") and "BONUS" not in out["Messages"]
    usage = [l for l in c.sheet.logs if l[3] == "ai-usage"]
    assert usage and "bonus 1000/500" in usage[0][5]


def test_old_theme_means_no_bonus_ai(ctx):
    c = run_rows(ctx, [make_row()], [copy_from_sample()], FakeWP())
    out = c.sheet.final(2)
    assert out["Status"] == "Draft ready" and c.client.calls == 1 and "theme is older than 1.5.1" in out["Messages"]
    assert "Bonus link" not in out


def test_bonus_failure_never_blocks_the_review(ctx):
    bad = reply()
    bad["plan"] = None
    c = run_rows(ctx, [make_row(**{"Bonus link": ""})], [copy_from_sample(), bad, copy.deepcopy(bad)], BonusWP())
    out = c.sheet.final(2)
    assert out["Status"] == "Draft ready" and "BONUS skipped" in out["Messages"] and not c.wp.saved
    assert "Bonus link" not in out


def test_bonus_message_does_not_hold_publishing(ctx):
    bad = reply()
    bad["plan"] = None
    c = run_rows(ctx, [make_row(Mode="Publish")], [copy_from_sample(), bad, copy.deepcopy(bad)], BonusWP())
    assert c.sheet.final(2)["Status"] == "Published"


def test_save_error_is_reported_not_fatal(ctx):
    c = run_rows(ctx, [make_row()], [copy_from_sample(), reply()], BonusWP(fail_save=True))
    out = c.sheet.final(2)
    assert out["Status"] == "Draft ready" and "BONUS not saved" in out["Messages"]


def test_rewrite_keeps_existing_bonus_without_ai(ctx):
    old = dict(copy.deepcopy(REPLY), v=1, code="oldcode23456")
    wp = BonusWP(existing=old)
    c = run_rows(ctx, [make_row(**{"WP post ID": 55, "Rewrite": True, "Slug": "clipforge-ai", "Bonus link": ""})],
                 [copy_from_sample()], wp)
    out = c.sheet.final(2)
    assert c.client.calls == 1 and not wp.saved
    assert out["Bonus link"] == "https://s.test/bonus/clipforge-ai-oldcode23456/"


def test_rewrite_with_rebuild_makes_new_bonus_same_link(ctx):
    old = dict(copy.deepcopy(REPLY), v=1, code="oldcode23456")
    wp = BonusWP(existing=old)
    new = reply()
    new["plan"]["title"] = "A fresh 7-day plan for ClipForge AI"
    c = run_rows(ctx, [make_row(**{"WP post ID": 55, "Rewrite": True, "Rebuild bonus": True, "Slug": "clipforge-ai",
                                   "Bonus link": ""})], [copy_from_sample(), new], wp)
    out = c.sheet.final(2)
    assert c.client.calls == 2 and wp.saved[0]["code"] == "oldcode23456"
    assert wp.saved[0]["plan"]["title"] == "A fresh 7-day plan for ClipForge AI"
    assert out["Rebuild bonus"] is False and out["Bonus link"].endswith("-oldcode23456/")


def test_rewrite_replaces_a_broken_saved_bonus(ctx):
    old = dict(copy.deepcopy(REPLY), v=1, code="oldcode23456")
    old["plan"]["days"] = old["plan"]["days"][:3]
    wp = BonusWP(existing=old)
    c = run_rows(ctx, [make_row(**{"WP post ID": 55, "Rewrite": True, "Slug": "clipforge-ai"})],
                 [copy_from_sample(), reply()], wp)
    assert wp.saved and wp.saved[0]["code"] == "oldcode23456"
    assert "saved bonus broke the rules" in c.sheet.final(2)["Messages"]


def test_rebuild_without_rewrite_makes_only_the_bonus(ctx, monkeypatch):
    from app.runner import Result
    monkeypatch.setattr(runner.patch, "patch_row", lambda *a, **k: Result("Published", ["Nothing to update."]))
    old = dict(copy.deepcopy(REPLY), v=1, code="oldcode23456")
    wp = BonusWP(existing=old)
    wp.get_review = lambda pid: {"id": pid, "status": "publish", "meta": {"sp_bonus_json": json.dumps(old),
                                 "sp_review_json": json.dumps(SAMPLE)}, "sp_bonus_status": wp._status(old)}
    c = run_rows(ctx, [make_row(**{"WP post ID": 55, "Rebuild bonus": True, "Slug": "clipforge-ai", "Bonus link": ""})],
                 [reply()], wp)
    out = c.sheet.final(2)
    assert c.client.calls == 1 and wp.saved[0]["code"] == "oldcode23456"  # only the bonus AI call, same link
    assert not [x for x in wp.calls if x["status"] != "bonus"]  # the review itself wasn't saved again
    assert out["Rebuild bonus"] is False and out["Bonus link"].endswith("-oldcode23456/") and out["Status"] == "Published"
    assert any(l[3] == "ai-usage" and "bonus 1000/500" in l[5] for l in c.sheet.logs)


def test_rebuild_not_tried_when_patch_needs_info(ctx, monkeypatch):
    from app.runner import Result
    monkeypatch.setattr(runner.patch, "patch_row", lambda *a, **k: Result("Needs info", ["Post is in the trash."]))
    c = run_rows(ctx, [make_row(**{"WP post ID": 55, "Rebuild bonus": True})], [], BonusWP())
    assert c.client.calls == 0 and "Rebuild bonus" not in c.sheet.final(2)


def test_bonus_switched_off_means_no_ai_and_no_link(ctx):
    wp = BonusWP()
    real = wp._status
    wp._status = lambda b: dict(real(b), enabled=False)
    c = run_rows(ctx, [make_row(**{"Bonus link": ""})], [copy_from_sample()], wp)
    out = c.sheet.final(2)
    assert c.client.calls == 1 and "switched off" in out["Messages"] and "Bonus link" not in out


def test_unreadable_status_means_no_ai(ctx):
    wp = BonusWP()
    wp._status = lambda b: None
    c = run_rows(ctx, [make_row()], [copy_from_sample()], wp)
    assert c.client.calls == 1 and "didn't report the bonus status" in c.sheet.final(2)["Messages"]


def test_publishing_happens_before_the_bonus(ctx):
    c = run_rows(ctx, [make_row(Mode="Publish")], [copy_from_sample(), reply()], BonusWP())
    assert [x["status"] for x in c.wp.calls] == ["draft", "publish", "bonus"]


def test_rejected_rebuild_puts_old_bonus_back(ctx):
    old = dict(copy.deepcopy(REPLY), v=1, code="oldcode23456")
    wp = BonusWP(existing=old)
    first = []
    real_status = wp._status

    def picky(b):  # the site rejects the first (new) bonus, accepts the old one
        if b and b != old and not first:
            first.append(1)
            return dict(real_status(b), ok=False, errors=["plan.title mentions a money amount."], url="")
        return real_status(b)
    wp._status = picky
    fresh = reply()
    fresh["plan"]["title"] = "A different 7-day plan"
    c = run_rows(ctx, [make_row(**{"WP post ID": 55, "Rewrite": True, "Rebuild bonus": True, "Slug": "clipforge-ai",
                                   "Bonus link": ""})], [copy_from_sample(), fresh], wp)
    out = c.sheet.final(2)
    assert len(wp.saved) == 2 and wp.saved[-1] == old and "previous bonus was put back" in out["Messages"]
    assert out["Bonus link"].endswith("-oldcode23456/")


def test_usage_logged_when_write_fails(ctx):
    bad = copy_from_sample()
    bad["seoTitle"] = "X" * 70
    c = run_rows(ctx, [make_row()], [bad, bad, bad], BonusWP())
    assert c.sheet.final(2)["Status"] == "Error"
    assert any(l[3] == "ai-usage" and "write 3000/1500" in l[5] for l in c.sheet.logs)


def test_unknown_keys_dropped_and_code_must_match_exactly():
    r = reply()
    r["tool"]["onclick"] = "alert(1)"
    r["plan"]["days"][0]["html"] = "<b>x</b>"
    r["extra"] = 1
    b = bonus.generate(FakeClient([r]), SAMPLE).bonus
    text = json.dumps(b)
    assert "onclick" not in text and '"html"' not in text and '"extra"' not in text and not bonus.validate(b)
    assert bonus.validate(dict(b, code=b["code"] + "\n"))  # a trailing newline is not a valid code


def test_first_reply_not_json_is_retried_once():
    calls = []

    class Flaky:
        def complete_json(self, system, user, max_tokens=0, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise LLMError("Reply was not valid JSON: Expecting value")
            return LLMResult(reply(), "", 10, 5, "fake")
    assert bonus.generate(Flaky(), SAMPLE).bonus and len(calls) == 2


def test_nan_is_never_written_to_wordpress():
    import math
    import pytest
    from types import SimpleNamespace
    from app.wordpress import WordPress
    wp = WordPress(SimpleNamespace(base_url="https://s.test", user="u", app_password="p"))
    with pytest.raises(ValueError):
        wp.save_bonus(1, {"v": 1, "x": math.nan})


def test_brief_strips_other_price_forms():
    r = copy.deepcopy(SAMPLE)
    r["pricing"]["otos"][0]["name"] = "Unlimited ($47)"
    r["features"][0]["title"] = "Worth 47 dollars alone"
    r["goodFor"] = ["have USD 20 a day for ads"]
    text = json.dumps(bonus.brief(r))
    assert "47" not in text and "USD 20" not in text and "dollars" not in text


def test_missing_bonus_link_column_is_explained(ctx):
    c = run_rows(ctx, [make_row()], [copy_from_sample(), reply()], BonusWP())
    out = c.sheet.final(2)
    assert "Add a column named 'Bonus link'" in out["Messages"] and "Bonus link" not in out


def test_review_data_sent_to_wordpress_never_contains_the_bonus(ctx):
    wp = BonusWP()
    sent = []
    real = wp.save_review

    def spy(**kw):
        sent.append(json.dumps(kw["review"]))
        return real(**kw)
    wp.save_review = spy
    run_rows(ctx, [make_row()], [copy_from_sample(), reply()], wp)
    assert sent and all("oldcode" not in s and '"plan"' not in s and wp.saved[0]["code"] not in s for s in sent)
def test_wrapped_tool_is_unwrapped():
    r = reply()
    r["tool"] = {"tool": r["tool"]}
    b = bonus.generate(FakeClient([r]), SAMPLE).bonus
    assert b["tool"]["engine"] == "scorer" and not bonus.validate(b)
