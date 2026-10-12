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
                  "items": o["items"], "description": o["summary"],
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
                method_note="We got early access before launch.", authors={"marcus"},
                days_tested="7")  # the sample copy describes hands-on use, so by default it's a tested review
    base.update(kw)
    return Inputs(**base)


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def complete_json(self, system, user, max_tokens=0, **kw):
        self.calls += 1
        return LLMResult(self.replies.pop(0), "", 1000, 500, "fake")


def untested_copy():
    """The sample copy without first-hand wording, as the writer must produce when nothing was tested."""
    from app.write import HANDS_ON_RE, _strings
    c = copy_from_sample()
    c["verdict"] = c["verdict"].replace("The fastest way we found to", "A fast way to")
    c["seoDescription"] = c["seoDescription"].replace("The fastest way we found to", "A fast way to")

    def clean(node):
        if isinstance(node, dict):
            return {k: clean(v) for k, v in node.items()}
        if isinstance(node, list):
            return [clean(x) for x in node if not (isinstance(x, str) and HANDS_ON_RE.search(x))]
        if isinstance(node, str) and HANDS_ON_RE.search(node):
            kept = [x for x in node.split(". ") if not HANDS_ON_RE.search(x)]
            return ". ".join(kept) or "See the vendor's demo for details."
        return node
    c = clean(c)
    assert not [p for p, t in _strings(c) if HANDS_ON_RE.search(t)]
    return c


def test_good_copy_gives_clean_review_first_try():
    res = run(FakeClient([untested_copy()]), inputs(days_tested=""), today="2026-10-06")
    assert res.ok, res.problems
    r = res.review
    assert r["pricing"]["frontEnd"]["price"] == 17 and r["links"]["frontEnd"] == SAMPLE["links"]["frontEnd"]
    assert r["review"]["label"] == "Recommended"
    # Not tested (no Days tested / Testing notes): honest research note, no hands-on badge or "tested N days".
    assert r["review"]["tested"] is False and r["review"]["methodNote"].startswith("How we reviewed it")
    assert r["review"]["trustBadge"] == "Researched review" and r["review"]["testedNote"] == ""
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
    res = run(client, inputs(days_tested="5"), today="2026-10-06")
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


def _pack_facts(n=2, values=(47, None)):
    f = facts_from_sample()
    f["affiliateBonuses"] = [{"title": f"Pack bonus {i + 1}", "description": "d", "value": values[i % len(values)]}
                             for i in range(n)]
    return f


def test_affiliate_pack_bonuses_listed_as_our_bonuses_after_sheet_bonus():
    c = copy_from_sample()
    c["packBonuses"] = [{"title": "Viral Video WP Theme", "description": "WordPress theme for video blogs."},
                        {"title": "Buyer Triggers", "description": "Ten triggers that turn leads into customers."}]
    res = run(FakeClient([c]), inputs(facts=_pack_facts()), today="2026-10-06")
    assert res.ok, res.problems
    b = res.review["bonuses"]
    assert [x["title"] for x in b] == ["15-Min 1-on-1 Founder Call", "Viral Video WP Theme", "Buyer Triggers"]
    assert b[1]["value"] == 47 and b[2]["value"] == 0 and b[1]["type"] == "Bonus"


def test_income_claim_in_pack_bonus_is_flagged_and_fixed():
    bad = copy_from_sample()
    bad["packBonuses"] = [{"title": "$10,060 & 6,424 Leads Case Study", "description": "A case study."},
                          {"title": "Buyer Triggers", "description": "Ten triggers."}]
    good = copy.deepcopy(bad)
    good["packBonuses"][0]["title"] = "Lead Generation Case Study"
    client = FakeClient([bad, good])
    res = run(client, inputs(facts=_pack_facts()), today="2026-10-06")
    assert res.ok and client.calls == 2 and res.review["bonuses"][1]["title"] == "Lead Generation Case Study"


def test_pack_bonuses_capped_at_twelve_total():
    f = _pack_facts(n=15, values=(10,))
    c = copy_from_sample()
    c["packBonuses"] = [{"title": f"B{i}", "description": "d"} for i in range(11)]
    res = run(FakeClient([c]), inputs(facts=f), today="2026-10-06")
    assert res.ok and len(res.review["bonuses"]) == 12
    assert any("only 11 fit" in n for n in res.notes)


def test_extraction_keeps_affiliate_and_vendor_bonuses_apart():
    from app.extract import clean_facts, merge
    a = clean_facts({"vendorBonuses": [{"title": "V1"}], "affiliateBonuses": [{"title": "A1", "value": "$27"}]})
    b = clean_facts({"affiliateBonuses": [{"title": "A1"}, {"title": "A2"}]})
    m = merge(a, [b])
    assert [x["title"] for x in m["vendorBonuses"]] == ["V1"]
    assert [x["title"] for x in m["affiliateBonuses"]] == ["A1", "A2"] and m["affiliateBonuses"][0]["value"] == 27.0


def test_oto_without_contents_must_be_skip():
    f = facts_from_sample()
    for o in f["otos"]:
        o["items"], o["description"] = ["x"], "described"
    f["otos"][2]["items"], f["otos"][2]["description"] = [], None
    bad = copy_from_sample()
    bad["otos"][2]["verdict"] = "optional"
    good = copy.deepcopy(bad)
    good["otos"][2]["verdict"] = "skip"
    client = FakeClient([bad, good])
    res = run(client, inputs(facts=f), today="2026-10-06")
    assert res.ok and client.calls == 2 and res.review["pricing"]["otos"][2]["verdict"] == "skip"


def test_tested_review_keeps_method_note_and_days():
    r = run(FakeClient([copy_from_sample()]), inputs(days_tested="5"), today="2026-10-06").review["review"]
    assert r["tested"] is True and r["methodNote"].startswith("We got early") and r["testedNote"] == "tested 5 days"
    r = run(FakeClient([copy_from_sample()]), inputs(days_tested="", testing_notes="Rendered 12 videos."), today="2026-10-06").review["review"]
    assert r["tested"] is True and r["testedNote"] == ""  # tested, but no day count given: no invented "7 days"


def test_untested_review_cannot_claim_hands_on_use():
    bad = copy_from_sample()
    bad["pros"] = ["We had our first result within 20 minutes"] + bad["pros"][1:]
    client = FakeClient([bad, untested_copy()])
    res = run(client, inputs(days_tested=""), today="2026-10-06")
    assert res.ok and client.calls == 2  # flagged, then fixed
    tested = run(FakeClient([bad]), inputs(days_tested="5"), today="2026-10-06")
    assert tested.ok  # with real testing notes/days, first-hand wording is allowed


def test_hands_on_check_catches_claims_but_not_normal_copy():
    from app.write import hands_on
    for t in ["The fastest way we tested", "fast and stable in our tests", "about 2× faster in our tests", "We hit the cap on day four",
              "We had our first result within 20 minutes", "we ran it for a week", "We bought the front end on launch day"]:
        assert hands_on(t), t
    # normal review copy and FAQ wording must NOT trigger a (paid) fixer call
    for t in ["30-day refund via JVZoo", "Turns blog posts into videos", "The vendor says renders are 3x faster", "Your first 7 days",
              "Day 1: set up your account", "from day one", "If we had to pick one, it's OTO 1", "We found the refund terms clear",
              "Once you have used it, the templates make sense", "Anyone who has tried it", "What if I bought through another link?",
              "Do I get bonuses if I used a coupon?", "Hands-on video training", "10x Faster Rendering", "$5 off on launch day",
              "post 3 videos a day 7 days a week", "In the demo, we used to see renders finish in 90 seconds."]:
        assert not hands_on(t), t


def test_fact_fields_are_never_checked():
    from app.write import FACT_PATH_RE
    for p in ["product.name", "slug", "links.frontEnd", "launch.cartOpen", "pricing.otos[0].name", "pricing.otos[2].downsell.name",
              "pricing.frontEnd.coupon.code", "pricing.bundles[0].name"]:
        assert FACT_PATH_RE.search(p), p
    for p in ["verdict", "pros[0]", "pricing.otos[0].summary", "faq[1].a", "pricing.frontEnd.note"]:
        assert not FACT_PATH_RE.search(p), p


def test_zero_days_is_not_tested():
    from app.write import tested_from
    assert not tested_from("0", "") and not tested_from("0 days", "") and tested_from("5", "") and tested_from("", "used it")
