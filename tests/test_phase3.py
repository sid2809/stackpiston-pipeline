from app.extract import compare, merge
from app.fetch import Source, html_to_parts, pick_follow_links
from app.timeparse import to_utc


# ---- timezone trap (real case: ComicVideos AI, 5 Oct 2026)
def test_est_literal_in_summer_is_flagged():
    utc, problems = to_utc("2026-10-05 10:00", "EST")
    assert utc == "2026-10-05T15:00:00Z"
    assert problems and "summer time" in problems[0]


def test_edt_matches_est_literal():
    assert to_utc("2026-10-05 11:00", "EDT") == ("2026-10-05T15:00:00Z", [])


def test_est_in_winter_is_fine():
    assert to_utc("2026-12-01 10:00", "EST") == ("2026-12-01T15:00:00Z", [])


def test_regional_et_is_dst_aware():
    assert to_utc("2026-10-05 11:00", "ET")[0] == "2026-10-05T15:00:00Z"
    assert to_utc("2026-12-01 11:00", "ET")[0] == "2026-12-01T16:00:00Z"


def test_missing_or_unknown_zone():
    assert to_utc("2026-10-05 11:00", None)[0] is None
    assert to_utc("2026-10-05 11:00", "XYZ")[1] == ["unknown timezone 'XYZ'"]


# ---- fetching
HTML = """<html><head><style>.x{}</style><script>var a=1</script></head><body>
<h1>Comic Videos AI</h1><p>Launch: Oct 5 &amp; 6</p><p>Launch: Oct 5 &amp; 6</p>
<img src="/img/cover.webp"><img src="data:image/png;base64,xx">
<iframe src="https://player.vimeo.com/video/123?h=a&amp;b=1"></iframe>
<a href="https://docs.google.com/document/d/1MQAFkjHBdhZ8zK5rIDlGK5VJL4O3sS949DbLdyWpklM/edit">Bonus doc</a>
<a href="https://www.google.com/url?q=https://docs.google.com/document/d/1AAAAAAAAAAAAAAAAAAAAAAAA/edit&sa=D">wrapped</a>
<a href="/jvdoc/">JV Doc</a><a href="/swipes/">Swipes</a><a href="https://www.jvzoo.com/affiliate/x">Request</a>
</body></html>"""


def test_html_cleaning_dedupe_entities_media():
    text, links, images, videos = html_to_parts(HTML, "https://sales.comicvideosai.com/jv/")
    assert "var a" not in text and text.count("Launch: Oct 5 & 6") == 1
    assert images == ["https://sales.comicvideosai.com/img/cover.webp"]
    assert videos == ["https://player.vimeo.com/video/123?h=a&b=1"]
    assert ("https://docs.google.com/document/d/1AAAAAAAAAAAAAAAAAAAAAAAA/edit", "wrapped") in links


def test_follow_links_picks_docs_and_jvdoc_not_swipes():
    _, links, _, _ = html_to_parts(HTML, "https://sales.comicvideosai.com/jv/")
    src = Source("https://sales.comicvideosai.com/jv/", "web", "x" * 600, links)
    picked = pick_follow_links(src)
    assert "https://sales.comicvideosai.com/jvdoc/" in picked
    assert any("docs.google.com" in u for u in picked)
    assert not any("swipes" in u or "jvzoo" in u for u in picked)


# ---- conflicts (real case: ComicVideos AI JV page vs JV doc)
JV_PAGE = {"frontEnd": {"price": 17}, "otos": [
    {"position": 1, "name": "Unlimited", "price": 47}, {"position": 2, "name": "Pro", "price": 67},
    {"position": 3, "name": "Agency", "price": 147}, {"position": 4, "name": "Growth", "price": 77}],
    "launch": {"cartOpen": {"local": "2026-10-05 10:00", "zone": "EST", "raw": "10 AM EST"}}}
JV_DOC = {"frontEnd": {"price": 17}, "otos": [
    {"position": 1, "name": "Unlimited", "price": 47}, {"position": 2, "name": "Pro", "price": 67},
    {"position": 3, "name": "Growth", "price": 77}, {"position": 4, "name": "Agency", "price": 147},
    {"position": 5, "name": "Cinematic Sites AI", "price": 37}, {"position": 6, "name": "Claw Agents AI", "price": 47}],
    "launch": {"cartOpen": {"local": "2026-10-05 11:00", "zone": "EDT", "raw": "11:00 AM EDT"}}}


def test_real_conflicts_are_caught():
    c = compare(JV_PAGE, JV_DOC, "linked page 1")
    joined = " | ".join(c)
    assert "OTO count" in joined and "OTO 3" in joined and "OTO 4" in joined
    assert not any("Cart open" in x for x in c)  # 10 AM EST == 11 AM EDT: same moment, no conflict


def test_merge_fills_gaps_only():
    main = {"productName": "X", "refundDays": None, "frontEnd": {"price": 17, "items": []}, "otos": [],
            "vendorBonuses": [{"title": "A"}]}
    other = {"productName": "Y", "refundDays": 30, "frontEnd": {"price": 99, "items": ["App"]},
             "otos": [{"position": 1, "name": "Pro", "price": 67}], "vendorBonuses": [{"title": "A"}, {"title": "B"}]}
    m = merge(main, [other])
    assert m["productName"] == "X" and m["refundDays"] == 30
    assert m["frontEnd"]["price"] == 17 and m["frontEnd"]["items"] == ["App"]
    assert len(m["otos"]) == 1 and [b["title"] for b in m["vendorBonuses"]] == ["A", "B"]


def test_am_pm_times_are_read():
    assert to_utc("2026-10-05 11:00 AM", "EDT")[0] == "2026-10-05T15:00:00Z"
    assert to_utc("2026-10-05 2:30 pm", "EDT")[0] == "2026-10-05T18:30:00Z"
