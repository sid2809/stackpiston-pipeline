import copy
import json
from pathlib import Path

from app.validate import link_errors, schema_errors, theme_warnings

FULL = json.loads((Path(__file__).parent / "fixtures" / "review-with-bundles.json").read_text(encoding="utf-8"))
BASE = json.loads((Path(__file__).parent / "fixtures" / "sample-review.json").read_text(encoding="utf-8"))


def _links(d):
    return (d["links"]["frontEnd"], [o["link"] for o in d["pricing"]["otos"]],
            [b["link"] for b in d["pricing"].get("bundles", [])])


def test_bundles_coupons_vendor_bonuses_add_no_warnings():
    # The PHP theme returned 12 warnings for both files (render test); Python must agree.
    assert theme_warnings(FULL, {"marcus"}) == theme_warnings(BASE, {"marcus"})
    assert schema_errors(FULL) == schema_errors(BASE)


def test_bad_bundle_data_matches_theme_messages():
    d = copy.deepcopy(FULL)
    d["pricing"]["bundles"][0]["link"] = "not a url"
    d["pricing"]["bundles"][1]["name"] = "X" * 40
    d["vendorBonuses"][0]["title"] = ""
    d["pricing"]["frontEnd"]["coupon"] = {"code": "", "discount": "x"}
    w = theme_warnings(d, {"marcus"})
    for msg in ("pricing.frontEnd.coupon code is missing.", "Bundle 1 link is not a valid URL.",
                "Bundle 2 name is 40 characters (max 32).", "vendorBonuses #1 title is missing."):
        assert msg in w


def test_bundle_links_checked_against_sheet():
    fe, otos, bundles = _links(FULL)
    assert link_errors(FULL, fe, otos, bundle_links=bundles) == []
    errs = link_errors(FULL, fe, otos, bundle_links=bundles[:1])
    assert any("2 bundles but the sheet has 1" in e for e in errs)
    d = copy.deepcopy(FULL)
    d["pricing"]["bundles"][0]["link"] += "x"
    assert any("Bundle 1 link does not match" in e for e in link_errors(d, fe, otos, bundle_links=bundles))
