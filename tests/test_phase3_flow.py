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


def test_full_flow_flags_conflicts_and_summer_est(monkeypatch):
    main = Source("https://v.com/jv/", "web", "x" * 900, [("https://v.com/jvdoc/", "JV Doc")], ["https://v.com/a.webp"])
    doc = Source("https://v.com/jvdoc/", "web", "y" * 900, [], [], ["https://player.vimeo.com/video/1"])
    monkeypatch.setattr(ex, "fetch", _fake_fetch({main.url: main, doc.url: doc}))
    page = dict(JV_PAGE, productName="Comic Videos AI")
    res = ex.run(FakeClient([page, JV_DOC]), main.url)
    joined = " | ".join(res.blocking)
    assert not res.ok and "OTO count" in joined and "summer time" in joined
    assert res.facts["cartOpenUtc"] == "2026-10-05T15:00:00Z"
    assert res.images == ["https://v.com/a.webp"] and res.videos == ["https://player.vimeo.com/video/1"]
    assert res.input_tokens == 200


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
