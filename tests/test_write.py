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


def _scrub(node):
    """The sample text claims testing ('in our tests'); a real writer may not, so remove it."""
    if isinstance(node, dict):
        return {k: _scrub(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_scrub(v) for v in node]
    if isinstance(node, str):
        return (node.replace(" (about 2× faster in our tests)", "").replace(" in our tests", "")
                .replace("we tested", "we found"))
    return node


def copy_from_sample():
    s = _scrub(SAMPLE)
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

    def complete_json(self, system, user, max_tokens=0, **kw):
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
    assert not res.ok and client.calls == 0 and any("needs info" in p for p in res.problems)


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


def test_derived_amounts_allowed_but_others_flagged():
    from app.write import allowed_amounts, price_mentions
    f = {"frontEnd": {"price": 36.95, "priceAfterLaunch": 47},
         "otos": [{"price": 67, "downsell": {"price": 47}}, {"price": 97}, {"price": 147}, {"price": 77}],
         "bundles": [{"name": "Bundle", "price": 367}, {"name": "DFY", "price": 127}],
         "coupons": [{"code": "CVB100", "discount": "$100 off", "appliesTo": "Bundle"},
                     {"code": "DFY30", "discount": "$30 off", "appliesTo": "DFY"}]}
    allowed = allowed_amounts(f, [{"value": 97}])
    ok_text = ("Front end $36.95, rising to $47 (+$10.05). Pro $67 or the $47 downsell (save $20). "
               "Full funnel $424.95; the $367 bundle saves $57.95, or $267 with CVB100. DFY is $97 with the $30 coupon.")
    review = {"faq": [{"q": "Price?", "a": ok_text}]}
    assert price_mentions(review, allowed) == []
    bad = {"faq": [{"q": "Earnings?", "a": "One user made $10,060 in a month and only pays $5."}]}
    flagged = " | ".join(price_mentions(bad, allowed))
    assert "$10,060" in flagged and "$5" in flagged


def test_testing_statements_are_flagged_but_badge_is_fine():
    c = copy_from_sample()
    c["cons"] = c["cons"][:2] + ["Not hands-on tested by us"]
    c["pros"] = [p.replace(" in our tests", "") for p in c["pros"]]
    fixed = copy.deepcopy(c)
    fixed["cons"] = c["cons"][:2] + ["Template library is thin"]
    client = FakeClient([c, fixed])
    res = run(client, inputs(), today="2026-10-06")
    assert res.ok and client.calls == 2 and res.review["review"]["trustBadge"] == "Bought & tested"


def test_coupon_codes_combined_from_all_sources():
    from app.extract import merge
    main = {"coupons": [{"code": "", "discount": "$10 off"}]}
    other = {"coupons": [{"code": "CVAI6OFF", "discount": "$6 off", "appliesTo": "Front end"}]}
    assert [c["code"] for c in merge(main, [other])["coupons"] if c["code"]] == ["CVAI6OFF"]


def test_testing_check_has_no_false_positives():
    import re as _re
    from app import write as w
    src = open(w.__file__).read()
    pat = _re.search(r'tested_re = re.compile\((.*?), re.I\)', src, _re.S).group(1)
    rx = _re.compile(eval(pat.replace("\n", " ")), _re.I)
    flagged = ["Not hands-on tested by us", "In our tests it was fast", "We tested it for a week",
               "It hasn't been tested yet", "We've tested every OTO", "Personally tested by our team"]
    clean = ["A/B testing is not included", "The vendor did not share testimonials",
             "Split testing isn't available", "It doesn't include a test mode", "Did not offer A/B testing"]
    assert all(rx.search(t) for t in flagged)
    assert not any(rx.search(t) for t in clean)


def test_needs_info_stops_before_any_ai_call():
    f = facts_from_sample()
    f["bundles"] = [{"name": "Bundle", "price": 367}, {"name": "Mega Bundle", "price": None}]
    client = FakeClient([])
    res = run(client, inputs(facts=f, bundle_links=["https://jvz1.com/c/1/b1", "https://jvz1.com/c/1/b2"]),
              today="2026-10-06")
    assert not res.ok and client.calls == 0 and any("Mega Bundle" in p for p in res.problems)


def test_oto_link_count_mismatch_stops_before_ai():
    client = FakeClient([])
    inp = inputs()
    inp.oto_links = inp.oto_links[:4]
    res = run(client, inp, today="2026-10-06")
    assert client.calls == 0 and any("OTO links" in p for p in res.problems)


def test_schema_messages_are_short():
    from app.validate import schema_errors
    d = json.loads(json.dumps(SAMPLE))
    d["features"] = d["features"] * 3
    msgs = [m for m in schema_errors(d) if m.startswith("features")]
    assert msgs == [f"features: has {len(d['features'])} items (max 8)"]


def test_unknown_price_type_is_not_claimed_as_one_time():
    f = facts_from_sample()
    f["frontEnd"]["priceType"] = None
    review, _, _ = assemble(copy_from_sample(), inputs(facts=f), "2026-10-06")
    assert review["pricing"]["frontEnd"]["priceType"] == ""
    f["frontEnd"]["priceType"] = "one-time"
    review, _, _ = assemble(copy_from_sample(), inputs(facts=f), "2026-10-06")
    assert review["pricing"]["frontEnd"]["priceType"] == "one-time"


def test_claiming_vendor_has_no_bonuses_is_flagged():
    from app.write import _check
    good, _, _ = assemble(copy_from_sample(), inputs(), "2026-10-06")
    assert not any("no bonuses" in p for p in _check(good, inputs()))
    bad = copy.deepcopy(good)
    bad["faq"][3]["a"] = "Buy through our link. The vendor lists no buyer bonuses of its own."
    assert any("claims there are no bonuses" in p for p in _check(bad, inputs()))
