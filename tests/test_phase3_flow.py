import app.extract as ex
from app.fetch import Source
from app.llm import LLMResult
from tests.test_phase3 import JV_DOC, JV_PAGE


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)

    def complete_json(self, system, user, max_tokens=0, **kw):
        return LLMResult(self.replies.pop(0), "", 100, 50, "fake")


def _fake_fetch(pages):
    def f(url, session=None):
        return pages[url]
    return f


def test_full_flow_conflicts_are_settled_by_ai_with_warnings(monkeypatch):
    main = Source("https://v.com/jv/", "web", "x" * 900, [("https://v.com/jvdoc/", "JV Doc")], ["https://v.com/a.webp"])
    doc = Source("https://v.com/jvdoc/", "web", "y" * 900, [], [], ["https://player.vimeo.com/video/1"])
    monkeypatch.setattr(ex, "fetch", _fake_fetch({main.url: main, doc.url: doc}))
    page = dict(JV_PAGE, productName="Comic Videos AI")
    settled = {"facts": dict(page), "decisions": ["OTO 3: chose Agency (JV page) over Growth (JV doc)."]}
    res = ex.run(FakeClient([page, JV_DOC, settled]), main.url)
    assert res.ok  # disagreements no longer stop the row...
    joined = " | ".join(res.warnings)
    assert "chose Agency" in joined and "summer time" in joined  # ...they become warnings to check
    assert res.facts["cartOpenUtc"] == "2026-10-05T15:00:00Z"
    assert res.images == ["https://v.com/a.webp"] and res.videos == ["https://player.vimeo.com/video/1"]

def test_sheet_override_clears_time_problem(monkeypatch):
    main = Source("https://v.com/jv/", "web", "x" * 900)
    monkeypatch.setattr(ex, "fetch", _fake_fetch({main.url: main}))
    page = dict(JV_PAGE, productName="Comic Videos AI")
    res = ex.run(FakeClient([page]), main.url, cart_open_override_utc="2026-10-05T15:00:00Z")
    assert not any("summer" in b for b in res.blocking)
    assert res.ok  # no supporting page => no conflicts; all OTOs priced


def test_thin_main_source_stops(monkeypatch):
    main = Source("https://v.com/jv/", "web", "tiny")
    monkeypatch.setattr(ex, "fetch", _fake_fetch({main.url: main}))
    res = ex.run(FakeClient([]), main.url)
    assert not res.ok and "almost no text" in res.blocking[0]


def test_sloppy_ai_output_does_not_crash(monkeypatch):
    main = Source("https://v.com/jv/", "web", "x" * 900, [("https://v.com/jvdoc/", "JV Doc")])
    doc = Source("https://v.com/jvdoc/", "web", "y" * 900)
    monkeypatch.setattr(ex, "fetch", _fake_fetch({main.url: main, doc.url: doc}))
    sloppy = {"productName": "X", "launch": None, "frontEnd": {"price": "$1,017"},
              "otos": [{"name": "Pro", "price": "$67"}, "bad"], "bundles": None, "vendorBonuses": ["oops"]}
    res = ex.run(FakeClient([sloppy, sloppy]), main.url)
    assert res.facts["frontEnd"]["price"] == 1017.0
    assert res.facts["otos"] == [res.facts["otos"][0]] and res.facts["otos"][0]["price"] == 67.0
    assert any("Cart open not found" in b for b in res.blocking)


def test_supporting_page_ai_failure_is_only_a_note(monkeypatch):
    from app.llm import LLMError
    main = Source("https://v.com/jv/", "web", "x" * 900, [("https://v.com/jvdoc/", "JV Doc")])
    doc = Source("https://v.com/jvdoc/", "web", "y" * 900)
    monkeypatch.setattr(ex, "fetch", _fake_fetch({main.url: main, doc.url: doc}))

    class Flaky:
        n = 0

        def complete_json(self, *a, **k):
            self.n += 1
            if self.n == 2:
                raise LLMError("Reply was cut off")
            return LLMResult(dict(JV_PAGE, productName="X"), "", 1, 1, "f")
    res = ex.run(Flaky(), main.url, cart_open_override_utc="2026-10-05T15:00:00Z")
    assert res.facts["productName"] == "X"
    assert any("AI could not read it" in n for n in res.notes)


def test_notes_for_ai_override_ai_choices(monkeypatch):
    main = Source("https://v.com/jv/", "web", "x" * 900, [("https://v.com/jvdoc/", "JV Doc")])
    doc = Source("https://v.com/jvdoc/", "web", "y" * 900)
    monkeypatch.setattr(ex, "fetch", _fake_fetch({main.url: main, doc.url: doc}))
    page = dict(JV_PAGE, productName="Comic Videos AI",
                otos=JV_PAGE["otos"] + [{"position": 5, "name": "DFY", "price": 127}],
                bundles=[{"name": "Bundle", "price": 367}, {"name": "Mega Bundle", "price": None}])
    settled = {"facts": dict(page), "decisions": ["kept JV page funnel"]}
    corrected = dict(page, otos=JV_PAGE["otos"],
                     bundles=[{"name": "Bundle", "price": 367}, {"name": "Mega Bundle", "price": 147},
                              {"name": "DFY", "price": 127}],
                     launch={"cartOpen": {"local": "2030-01-01 00:00", "zone": "UTC", "raw": "notes tried this"}})
    res = ex.run(FakeClient([page, JV_DOC, settled, corrected]), main.url,
                 cart_open_override_utc="2026-10-05T15:00:00Z",
                 owner_notes="OTOs are only Pro, Automation, Agency, Growth. Mega Bundle $147. DFY is a bundle.")
    assert res.ok, res.blocking
    assert [o["name"] for o in res.facts["otos"]] == ["Unlimited", "Pro", "Agency", "Growth"]
    assert [b["price"] for b in res.facts["bundles"]] == [367, 147, 127]
    assert res.facts["launch"]["cartOpen"]["raw"] == "10 AM EST"  # notes can't change launch times
    assert any("Notes for AI applied" in n for n in res.notes)

def test_missing_price_still_blocks_and_summer_est_warns(monkeypatch):
    main = Source("https://v.com/jv/", "web", "x" * 900)
    monkeypatch.setattr(ex, "fetch", _fake_fetch({main.url: main}))
    page = dict(JV_PAGE, productName="X")
    still_missing = dict(page, otos=[{"position": 1, "name": "Pro", "price": None}])
    res = ex.run(FakeClient([page, still_missing]), main.url, owner_notes="Pro is the only OTO.")
    assert any("has no price" in b for b in res.blocking)
    assert any("summer time" in w for w in res.warnings)

def test_no_notes_means_no_extra_ai_call(monkeypatch):
    main = Source("https://v.com/jv/", "web", "x" * 900)
    monkeypatch.setattr(ex, "fetch", _fake_fetch({main.url: main}))
    client = FakeClient([dict(JV_PAGE, productName="X")])
    ex.run(client, main.url, owner_notes="   ")
    assert client.replies == []


def test_partial_notes_reply_keeps_untouched_facts(monkeypatch):
    main = Source("https://v.com/jv/", "web", "x" * 900)
    monkeypatch.setattr(ex, "fetch", _fake_fetch({main.url: main}))
    page = dict(JV_PAGE, productName="X", vendor="Yogesh",
                affiliateBonuses=[{"title": "Pack 1"}], coupons=[{"code": "CVAI6OFF", "discount": "$6 off"}])
    partial = {"bundles": [{"name": "Mega Bundle", "price": 147}], "made_up_key": 1}
    res = ex.run(FakeClient([page, partial]), main.url, cart_open_override_utc="2026-10-05T15:00:00Z",
                 owner_notes="Mega Bundle is $147.")
    f = res.facts
    assert f["vendor"] == "Yogesh" and [o["name"] for o in f["otos"]] == [o["name"] for o in JV_PAGE["otos"]]
    assert f["affiliateBonuses"][0]["title"] == "Pack 1" and f["coupons"][0]["code"] == "CVAI6OFF"
    assert f["bundles"] == [{"name": "Mega Bundle", "price": 147.0, "includes": []}] and "made_up_key" not in f
