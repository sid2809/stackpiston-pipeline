import copy
import json
from pathlib import Path

from app.validate import link_errors, schema_errors, theme_warnings

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "sample-review.json").read_text(encoding="utf-8"))

# Exactly what the live theme returned as sp_warnings for the sample (screenshot, 5 Oct 2026).
THEME_EXPECTED = [
    "seo.title is 63 characters (max 60).",
    "OTO 2 summary is 104 characters (min 120).",
    "OTO 2 takeaway is 119 characters (min 120).",
    "OTO 4 summary is 117 characters (min 120).",
    "OTO 4 takeaway is 106 characters (min 120).",
    "OTO 5 summary is 94 characters (min 120).",
    "OTO 5 takeaway is 58 characters (min 120).",
    "OTO 6 summary is 103 characters (min 120).",
    "OTO 6 takeaway is 79 characters (min 120).",
    "OTO 7 summary is 69 characters (min 120).",
    "OTO 7 takeaway is 73 characters (min 120).",
]


def test_checkpoint_matches_theme_exactly():
    # The theme's sp_create_sample_review() swaps media for an image before saving the live post.
    live = copy.deepcopy(SAMPLE)
    live["media"] = {"type": "image", "imageUrl": "", "imageAlt": "ClipForge AI dashboard (sample image)",
                     "caption": "Sample image. Replace it with your own screenshot."}
    assert theme_warnings(live, authors={"marcus"}) == THEME_EXPECTED


def test_unknown_author_is_flagged():
    w = theme_warnings(SAMPLE, authors={"someone"})
    assert any('review.author "marcus"' in x for x in w)


def test_empty_media_type_is_flagged():
    d = copy.deepcopy(SAMPLE)
    d["media"]["type"] = ""
    assert 'media.type should be "video" or "image".' in theme_warnings(d, {"marcus"})


def test_schema_catches_bad_url_and_slug():
    d = copy.deepcopy(SAMPLE)
    d["links"]["frontEnd"] = "not a url"
    d["slug"] = "Bad Slug"
    errs = " | ".join(schema_errors(d))
    assert "links.frontEnd" in errs and "slug" in errs


def _links(d):
    return d["links"]["frontEnd"], [o["link"] for o in d["pricing"]["otos"]]


def test_links_clean_on_sample():
    fe, otos = _links(SAMPLE)
    assert link_errors(SAMPLE, fe, otos) == []


def test_changed_affiliate_link_is_caught():
    d = copy.deepcopy(SAMPLE)
    fe, otos = _links(SAMPLE)
    d["pricing"]["otos"][0]["link"] = otos[0] + "x"
    errs = link_errors(d, fe, otos)
    assert any("OTO 1 link does not match" in e for e in errs)


def test_injected_url_is_caught():
    d = copy.deepcopy(SAMPLE)
    fe, otos = _links(SAMPLE)
    d["faq"][0]["a"] += " Visit https://evil.example.com now."
    assert any("evil.example.com" in e for e in link_errors(d, fe, otos))


def test_oto_count_mismatch_is_caught():
    fe, otos = _links(SAMPLE)
    assert any("OTO links" in e for e in link_errors(SAMPLE, fe, otos[:-1]))
