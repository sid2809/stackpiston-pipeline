import copy
import json
from pathlib import Path

from app.llm import LLMResult
from app.write import Inputs, assemble, run

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "sample-review.json").read_text(encoding="utf-8"))
PAD = " It is aimed at buyers who want this specific upgrade and nothing else."


def facts_from_sample():
    s = SAMPLE
    return {
        "productName": s["product"]["name"], "vendor": s["product"]["vendor"], "platform": "JVZoo",
        "refundDays": s["product"]["refundDays"],
        "frontEnd": {"name": s["pricing"]["frontEnd"]["name"], "price": s["pricing"]["frontEnd"]["price"],
                     "priceAfterLaunch": s["pricing"]["frontEnd"]["priceAfterLaunch"], "priceType": "one-time"},
        "otos": [{"position": i + 1, "name": o["name"], "price": o["price"],
                  "downsell": ({"name": o["downsell"]["name"], "price": o["downsell"]["price"]} if o.get("downsell") else None)}
                 for i, o in enumerate(s["pricing"]["otos"])],
        "bundles": [], "coupons": [], "vendorBonuses": [],
        "cartOpenUtc": s["launch"]["cartOpen"], "cartCloseUtc": s["launch"]["cartClose"],
    }


def copy_from_sample():
    s = SAMPLE
    otos = []
    for i, o in enumerate(s["pricing"]["otos"]):
        otos.append({"position": i + 1, "name": o["name"], "items": o["items"], "verdict": o["verdict"],
                     "note": o["note"], "summary": (o["summary"] + PAD)[:300] if len(o["summary"]) < 120 else o["summary"],
                     "included": o["included"], "pros": o["pros"], "cons": o["cons"], "score": o["score"],
                     "takeaway": (o["takeaway"] + PAD + PAD)[:400] if len(o["takeaway"]) < 120 else o["takeaway"],
                     "downsellNote": (o.get("downsell") or {}).get("note", "")})
    return {"seoTitle": "ClipForge AI Review: Score, OTOs & Bonuses", "seoDescription": s["seo"]["description"],
            "productName": s["product"]["name"], "niche": s["product"]["niche"], "summary": s["product"]["summary"],
            "verdict": s["review"]["verdict"], "finalVerdict": s["review"]["finalVerdict"],
            "scoreBreakdown": s["review"]["scoreBreakdown"], "imageAlt": "ClipForge AI dashboard",
            "caption": "The ClipForge AI editor", "videoTitle": "ClipForge AI demo",
            "frontEnd": {"name": s["pricing"]["frontEnd"]["name"], "items": s["pricing"]["frontEnd"]["items"],
                         "note": s["pricing"]["frontEnd"]["note"]},
            "otos": otos, "bundles": [], "features": s["features"], "pros": s["pros"], "cons": s["cons"],
            "goodFor": s["goodFor"], "notFor": s["notFor"], "vendorBonuses": [], "faq": s["faq"]}


def inputs(**kw):
    base = dict(facts=facts_from_sample(), fe_link=SAMPLE["links"]["frontEnd"],
                oto_links=[o["link"] for o in SAMPLE["pricing"]["otos"]],
                own_bonuses=[{"type": "Access", "title": "15-Min 1-on-1 Founder Call",
                              "description": "A private 15-minute call with the StackPiston founder.", "value": 97}],
                method_note="We got early access before launch.", authors={"marcus"})
    base.update(kw)
    return Inputs(**base)


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def complete_json(self, system, user, max_tokens=0):
        self.calls += 1
        return LLMResult(self.replies.pop(0), "", 1000, 500, "fake")


def test_good_copy_gives_clean_review_first_try():
    res = run(FakeClient([copy_from_sample()]), inputs(), today="2026-10-06")
    assert res.ok, res.problems
    r = res.review
    assert r["pricing"]["frontEnd"]["price"] == 17 and r["links"]["frontEnd"] == SAMPLE["links"]["frontEnd"]
    assert r["review"]["label"] == "Recommended" and r["review"]["methodNote"].startswith("We got early")
    assert r["launch"]["timezoneLabel"] == "EDT" and r["bonuses"][0]["value"] == 97
    assert res.attempts == 1


def test_fixer_repairs_length_and_invented_price():
    bad = copy_from_sample()
    bad["seoTitle"] = "X" * 70
    bad["verdict"] = bad["verdict"][:60] + " Only $9 today."
    client = FakeClient([bad, copy_from_sample()])
    res = run(client, inputs(), today="2026-10-06")
    assert res.ok and res.attempts == 2 and client.calls == 2


def test_gives_up_after_two_fixes():
    bad = copy_from_sample()
    bad["seoTitle"] = "X" * 70
    client = FakeClient([bad, bad, bad])
    res = run(client, inputs(), today="2026-10-06")
    assert not res.ok and client.calls == 3 and any("seo.title" in p for p in res.problems)


def test_ai_cannot_change_prices_or_links():
    c = copy_from_sample()
    c["otos"][0]["price"] = 1
    c["otos"][0]["link"] = "https://evil.example.com"
    review, _, _ = assemble(c, inputs(), "2026-10-06")
    assert review["pricing"]["otos"][0]["price"] == SAMPLE["pricing"]["otos"][0]["price"]
    assert review["pricing"]["otos"][0]["link"] == SAMPLE["pricing"]["otos"][0]["link"]


def test_bundles_and_coupons_assembled():
    f = facts_from_sample()
    f["bundles"] = [{"name": "Bundle", "price": 367}, {"name": "Mega Bundle", "price": 147}]
    f["coupons"] = [{"code": "CVAI6OFF", "discount": "$6 off", "appliesTo": "Front end"},
                    {"code": "CVB100", "discount": "$100 off", "appliesTo": "Bundle"}]
    c = copy_from_sample()
    c["bundles"] = [{"position": 1, "name": "Bundle", "items": ["Front end + all OTOs"], "verdict": "worth_it",
                     "note": "Best value if you'd buy two upsells anyway."},
                    {"position": 2, "name": "Mega Bundle", "items": [], "verdict": "optional", "note": "Extras for bundle buyers."}]
    inp = inputs(facts=f, bundle_links=["https://jvz1.com/c/1/b1", "https://jvz1.com/c/1/b2"])
    res = run(FakeClient([c]), inp, today="2026-10-06")
    assert res.ok, res.problems
    b = res.review["pricing"]["bundles"]
    assert b[0]["coupon"]["code"] == "CVB100" and "coupon" not in b[1]
    assert res.review["pricing"]["frontEnd"]["coupon"]["code"] == "CVAI6OFF"


def test_bundle_link_count_mismatch_stops_without_fixer():
    f = facts_from_sample()
    f["bundles"] = [{"name": "Bundle", "price": 367}, {"name": "DFY", "price": 127}]
    c = copy_from_sample()
    c["bundles"] = [{"position": 1, "name": "Bundle", "items": [], "verdict": "optional", "note": "x" * 20},
                    {"position": 2, "name": "DFY", "items": [], "verdict": "optional", "note": "x" * 20}]
    client = FakeClient([c])
    res = run(client, inputs(facts=f, bundle_links=["https://jvz1.com/c/1/b1"]), today="2026-10-06")
    assert not res.ok and client.calls == 1 and any("needs info" in p for p in res.problems)


def test_no_bundle_links_means_bundles_left_out_with_note():
    f = facts_from_sample()
    f["bundles"] = [{"name": "Bundle", "price": 367}]
    res = run(FakeClient([copy_from_sample()]), inputs(facts=f), today="2026-10-06")
    assert res.ok and "bundles" not in res.review["pricing"]
    assert any("no Bundle links" in n for n in res.notes)


def test_income_claim_in_vendor_bonus_is_flagged():
    f = facts_from_sample()
    f["vendorBonuses"] = [{"title": "$10,060 & 6,424 Leads Case Study", "value": None}]
    c = copy_from_sample()
    c["vendorBonuses"] = [{"title": "$10,060 & 6,424 Leads Case Study", "description": "A case study."}]
    fixed = copy.deepcopy(c)
    fixed["vendorBonuses"] = [{"title": "Lead Generation Case Study", "description": "A 3-part case study."}]
    client = FakeClient([c, fixed])
    res = run(client, inputs(facts=f), today="2026-10-06")
    assert res.ok and client.calls == 2 and res.review["vendorBonuses"][0]["title"] == "Lead Generation Case Study"
