"""Phase 6: demo video and main image picking. All network replies are simulated."""
import json
import struct

import pytest

import app.runner as runner
from app import media
from app.extract import ExtractResult
from app.fetch import media_parts
from tests.test_runner import NOW, FakeSheet, FakeWP, make_row
from tests.test_write import FakeClient, copy_from_sample, facts_from_sample


# ------------------------------------------------------------------ fake images and network

def png(w, h):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00" + b"\x00" * 50


def jpg(w, h):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHH", 17, 8, h, w) + b"\x03" + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + app0 + sof + b"\x00" * 50


def webp_vp8x(w, h):
    body = b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00" + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


class Resp:
    def __init__(self, status=200, body=b"", ctype="text/html", json_data=None, length=None):
        self.status_code, self._body, self._json = status, body, json_data
        self.headers = {"Content-Type": "application/json" if json_data is not None else ctype}
        if length is not None:
            self.headers["Content-Length"] = str(length)
        self.text = body.decode("latin-1") if isinstance(body, bytes) else body

    def json(self):
        return self._json

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    def __init__(self, routes):
        self.routes, self.seen = routes, []

    def get(self, url, **kw):
        self.seen.append(url)
        for key, resp in self.routes.items():
            if key in url:
                return resp
        return Resp(404)


# ------------------------------------------------------------------ image size reading

def test_image_size_reads_png_jpeg_webp():
    assert media.image_size(png(1200, 630)) == ("image/png", 1200, 630)
    assert media.image_size(jpg(1600, 900)) == ("image/jpeg", 1600, 900)
    assert media.image_size(webp_vp8x(1280, 720)) == ("image/webp", 1280, 720)
    assert media.image_size(b"GIF89a....") is None and media.image_size(b"<svg") is None


# ------------------------------------------------------------------ video

def test_normalize_known_formats_without_network():
    s = FakeSession({})
    assert media.normalize_video("https://x.wistia.com/medias/abcde12345", s)[0] == "https://fast.wistia.net/embed/iframe/abcde12345"
    assert media.normalize_video("https://jono.wistia.com/s/6sym77ugnbd51d3?wvideo=mmi98bf00z", s)[0] == \
        "https://fast.wistia.net/embed/iframe/mmi98bf00z"
    assert media.normalize_video("https://youtu.be/dQw4w9WgXcQ", s)[0] == "https://youtu.be/dQw4w9WgXcQ"
    assert media.normalize_video("https://example.com/video.mp4", s)[0] == ""
    assert s.seen == []


def test_wistia_share_link_resolved_through_oembed():
    s = FakeSession({"fast.wistia.com/oembed": Resp(json_data={
        "title": "buyerengine-demo-1790529801093",
        "html": '<iframe src="https://fast.wistia.net/embed/iframe/mmi98bf00z"></iframe>'})})
    url, title = media.normalize_video("https://jonoarmstrong1980.wistia.com/s/6sym77ugnbd51d3", s)
    assert url == "https://fast.wistia.net/embed/iframe/mmi98bf00z" and "demo" in title


def test_wistia_share_link_resolved_from_page_when_oembed_fails():
    s = FakeSession({"oembed": Resp(404), "wistia.com/s/": Resp(body=b'..."hashedId":"mmi98bf00z"...')})
    assert media.normalize_video("https://j.wistia.com/s/tok", s)[0] == "https://fast.wistia.net/embed/iframe/mmi98bf00z"


def test_never_picks_jv_video_and_picks_labelled_demo():
    cands = [{"url": "https://www.youtube.com/watch?v=AAAAAAAAAAA", "context": "JV Video: watch our partner invite", "source": "the JV doc"},
             {"url": "https://www.youtube.com/watch?v=BBBBBBBBBBB", "context": "Demo Video: see it in action", "source": "the JV doc"}]
    vp = media.pick_video(cands, FakeSession({}))
    assert vp.url.endswith("BBBBBBBBBBB") and "CHECK" not in vp.check and "the JV doc" in vp.check


def test_demo_label_with_jv_word_is_rejected():
    vp = media.pick_video([{"url": "https://youtu.be/AAAAAAAAAAA", "context": "JV demo webinar replay", "source": "x"}],
                          FakeSession({}))
    assert vp.url == "" and "none clearly labelled" in vp.note


def test_unlabelled_video_uses_its_title():
    s = FakeSession({"youtube.com/oembed": Resp(json_data={"title": "BuyerEngine AI Demo Walkthrough"})})
    vp = media.pick_video([{"url": "https://www.youtube.com/watch?v=CCCCCCCCCCC", "context": "", "source": "the sales page"}], s)
    assert vp.url.endswith("CCCCCCCCCCC") and vp.check


def test_unlabelled_video_with_neutral_title_is_not_used():
    s = FakeSession({"youtube.com/oembed": Resp(json_data={"title": "Welcome"})})
    vp = media.pick_video([{"url": "https://www.youtube.com/watch?v=CCCCCCCCCCC", "context": "", "source": "x"}], s)
    assert vp.url == "" and not vp.check


def test_demo_that_cannot_be_played_gives_instructions():
    s = FakeSession({})
    vp = media.pick_video([{"url": "https://j.wistia.com/s/tok", "context": "Product demo", "source": "x"}], s)
    assert vp.url == "" and "Copy link and thumbnail" in vp.note


def test_no_videos_note():
    assert "No video found" in media.pick_video([], FakeSession({})).note


# ------------------------------------------------------------------ image

def test_image_pick_skips_logos_small_and_portrait_and_prefers_sales():
    routes = {
        "/logo.png": Resp(body=png(1600, 800)),            # skipped by name, never downloaded
        "/small.png": Resp(body=png(400, 200)),            # too small
        "/tall.jpg": Resp(body=jpg(800, 1600)),            # portrait
        "/jvpage-hero.jpg": Resp(body=jpg(1600, 900)),     # good but on the JV page
        "/mockup.png": Resp(body=png(1400, 800)),          # good, on the sales page
    }
    s = FakeSession(routes)
    cands = [{"url": "https://v.test/jvpage-hero.jpg", "alt": "", "source": "JV", "sales": False},
             {"url": "https://v.test/logo.png", "alt": "", "source": "sales", "sales": True},
             {"url": "https://v.test/small.png", "alt": "", "source": "sales", "sales": True},
             {"url": "https://v.test/tall.jpg", "alt": "", "source": "sales", "sales": True},
             {"url": "https://v.test/mockup.png", "alt": "BuyerEngine dashboard", "source": "sales", "sales": True}]
    pick, note = media.pick_image(cands, "BuyerEngine AI", s)
    assert pick.source.endswith("/mockup.png") and pick.mime == "image/png" and pick.filename == "buyerengine-ai-review.png"
    assert "https://v.test/logo.png" not in s.seen and "picked automatically" in note


def test_image_too_big_or_not_an_image_is_skipped():
    s = FakeSession({"/huge.jpg": Resp(body=b"x", length=media.MAX_IMAGE_BYTES + 1),
                     "/page.jpg": Resp(body=b"<html>not an image</html>")})
    pick, note = media.pick_image([{"url": "https://v.test/huge.jpg", "alt": "", "sales": True},
                                   {"url": "https://v.test/page.jpg", "alt": "", "sales": True}], "X", s)
    assert pick is None and "none looked like" in note


def test_no_images_note():
    assert "No images found" in media.pick_image([], "X", FakeSession({}))[1]


# ------------------------------------------------------------------ page reading

def test_media_parts_reads_labels_and_unwraps_google_links():
    html = """
    <p>JV Video: <a href="https://www.youtube.com/watch?v=AAAAAAAAAAA">https://www.youtube.com/watch?v=AAAAAAAAAAA</a></p>
    <p>Demo Video: <a href="https://www.google.com/url?q=https://jono.wistia.com/s/6sym77ugnbd51d3&amp;sa=D">
       https://jono.wistia.com/s/6sym77ugnbd51d3</a></p>
    <h2>Watch the walkthrough</h2>
    <div><iframe src="https://player.vimeo.com/video/123456"></iframe></div>
    <img src="/img/hero.png" alt="Product dashboard"><img src="data:image/png;base64,xx">
    """
    videos, images = media_parts(html, "https://v.test/page/")
    by_url = {v["url"]: v["context"] for v in videos}
    assert by_url["https://www.youtube.com/watch?v=AAAAAAAAAAA"].startswith("JV Video")
    assert by_url["https://jono.wistia.com/s/6sym77ugnbd51d3"].startswith("Demo Video")
    assert "walkthrough" in by_url["https://player.vimeo.com/video/123456"]
    assert images == [{"url": "https://v.test/img/hero.png", "alt": "Product dashboard"}]


# ------------------------------------------------------------------ runner

class MediaWP(FakeWP):
    def __init__(self, featured=0, fail_upload=False, gallery=None):
        super().__init__()
        self.uploads, self.featured, self.fail_upload, self.gallery = [], featured, fail_upload, gallery or []

    def save_review(self, **kw):
        self.calls_fm = getattr(self, "calls_fm", []) + [kw.get("featured_media")]
        self.last_review = kw["review"]
        return super().save_review(**kw)

    def upload_media(self, data, filename, mime, alt=""):
        if self.fail_upload:
            raise RuntimeError("HTTP 413 too large")
        self.uploads.append((filename, mime, alt))
        return {"id": 777, "source_url": "https://s.test/wp-content/uploads/" + filename}

    def get_review(self, post_id):
        meta = {"sp_review_json": json.dumps({"gallery": self.gallery})} if self.gallery else {}
        return {"id": post_id, "featured_media": self.featured, "status": "draft", "meta": meta}

    def get_media(self, mid):
        return {"id": mid, "source_url": "https://s.test/wp-content/uploads/old.png"}


@pytest.fixture
def mctx(monkeypatch):
    state = {"videos": [], "images": []}

    def fake_extract(client, urls, sales=None, **kw):
        f = facts_from_sample()
        return ExtractResult(facts=f, video_candidates=state["videos"], image_candidates=state["images"])
    monkeypatch.setattr(runner.extract, "run", fake_extract)

    def make(rows, wp=None):
        return runner.Context(client=FakeClient([copy_from_sample()] * len(rows)), wp=wp or MediaWP(),
                              sheet=FakeSheet(rows), base_url="https://s.test")
    return make, state


def fake_pick_all(cands, product, s=None, want_main=True, want_gallery=True):
    main = media.ImagePick(png(1200, 630), "image/png", "clipforge-ai-review.png", 1200, 630, "https://v.test/hero.png", "x") \
        if (cands and want_main) else None
    shots = [media.ImagePick(png(1200, 700), "image/png", f"clipforge-ai-screenshot-{i}.png", 1200, 700,
                             f"https://v.test/s{i}.png", f"shot {i}") for i in (1, 2)] if (cands and want_gallery) else []
    return main, shots, ["fake note"]


def test_runner_auto_demo_video_is_a_check_and_blocks_auto_publish(mctx, monkeypatch):
    make, state = mctx
    monkeypatch.setattr(media, "video_title", lambda u, s: "")
    state["videos"] = [{"url": "https://www.youtube.com/watch?v=BBBBBBBBBBB", "context": "Demo video", "source": "the JV doc"}]
    c = make([make_row(Mode="Publish")])
    runner.poll(c, now_utc=NOW)
    out = c.sheet.final(2)
    assert out["Status"] == "Draft ready" and "CHECK: Video:" in out["Messages"] and "Kept as draft" in out["Messages"]
    assert c.wp.last_review["media"] == {**c.wp.last_review["media"], "type": "video",
                                         "videoUrl": "https://www.youtube.com/watch?v=BBBBBBBBBBB"}


def fake_fetch_image(url, product, s=None):
    return media.ImagePick(png(1200, 630), "image/png", "clipforge-ai-review.png", 1200, 630, url, "x"), "Main image taken from the sheet."


def test_runner_main_image_from_sheet_and_auto_screenshots(mctx, monkeypatch):
    make, state = mctx
    monkeypatch.setattr(media, "fetch_image", fake_fetch_image)
    seen = {}

    def spy_pick_all(cands, product, s=None, want_main=True, want_gallery=True):
        seen["want_main"], seen["urls"] = want_main, [c["url"] for c in cands]
        return fake_pick_all(cands, product, s, want_main, want_gallery)
    monkeypatch.setattr(media, "pick_all", spy_pick_all)
    state["images"] = [{"url": "https://v.test/mine.png", "alt": "", "source": "s", "sales": True},
                       {"url": "https://v.test/other.png", "alt": "", "source": "s", "sales": True}]
    c = make([make_row(**{"Main image URL": "https://v.test/mine.png"})])
    runner.poll(c, now_utc=NOW)
    assert c.wp.uploads[0] == ("clipforge-ai-review.png", "image/png", "x") and c.wp.calls_fm == [777]
    assert seen == {"want_main": False, "urls": ["https://v.test/other.png"]}  # never auto-picks the main image
    assert [u[0] for u in c.wp.uploads[1:]] == ["clipforge-ai-screenshot-1.png", "clipforge-ai-screenshot-2.png"]
    assert len(c.wp.last_review["gallery"]) == 2 and c.sheet.final(2)["Status"] == "Draft ready"


def test_runner_blank_main_image_means_none_and_a_note(mctx, monkeypatch):
    make, state = mctx
    monkeypatch.setattr(media, "pick_all", fake_pick_all)
    state["images"] = [{"url": "https://v.test/x.png", "alt": "", "source": "s", "sales": True}]
    c = make([make_row()])
    runner.poll(c, now_utc=NOW)
    assert c.wp.calls_fm == [None] and "Main image URL is empty" in c.sheet.final(2)["Messages"]
    assert all("review." not in u[0] for u in c.wp.uploads)  # only screenshots uploaded


def test_runner_rewrite_reuses_existing_image(mctx, monkeypatch):
    make, state = mctx
    monkeypatch.setattr(media, "pick_all", fake_pick_all)
    state["images"] = [{"url": "https://v.test/hero.png", "alt": "", "source": "s", "sales": True}]
    old_gallery = [{"url": "https://s.test/wp-content/uploads/old-shot.png", "alt": "old"}]
    wp = MediaWP(featured=55, gallery=old_gallery)
    c = make([make_row(**{"WP post ID": 9, "Rewrite": True})], wp=wp)
    runner.poll(c, now_utc=NOW)
    assert wp.uploads == [] and wp.calls_fm == [55] and wp.last_review["gallery"] == old_gallery
    assert "Main image URL is empty" not in c.sheet.final(2)["Messages"]


def test_runner_rewrite_with_new_main_image_link_replaces_it(mctx, monkeypatch):
    make, state = mctx
    monkeypatch.setattr(media, "fetch_image", fake_fetch_image)
    wp = MediaWP(featured=55, gallery=[{"url": "https://s.test/u/g.png", "alt": "g"}])
    c = make([make_row(**{"WP post ID": 9, "Rewrite": True, "Main image URL": "https://v.test/new.png"})], wp=wp)
    runner.poll(c, now_utc=NOW)
    assert wp.calls_fm == [777] and len(wp.uploads) == 1


def test_runner_upload_failure_never_stops_the_row(mctx, monkeypatch):
    make, state = mctx
    monkeypatch.setattr(media, "fetch_image", fake_fetch_image)
    c = make([make_row(**{"Main image URL": "https://v.test/mine.png"})], wp=MediaWP(fail_upload=True))
    runner.poll(c, now_utc=NOW)
    out = c.sheet.final(2)
    assert out["Status"] == "Draft ready" and "Main image skipped" in out["Messages"] and c.wp.calls_fm == [None]


def test_fetch_image_checks_the_file_and_converts_drive_links():
    s = FakeSession({"uc?export=download&id=1AbCdEfGhIjKlMnOpQrStUv": Resp(body=jpg(1600, 900)),
                     "/page": Resp(body=b"<html>login</html>"), "/small.png": Resp(body=png(400, 300))})
    pick, note = media.fetch_image("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUv/view?usp=sharing", "BuyerEngine AI", s)
    assert pick.mime == "image/jpeg" and pick.filename == "buyerengine-ai-review.jpg" and "1600×900" in note
    assert media.fetch_image("https://v.test/page", "X", s)[0] is None
    small, note = media.fetch_image("https://v.test/small.png", "X", s)
    assert small and "quite small" in note
    assert media.fetch_image("ftp://x/y.png", "X", s)[0] is None


def test_runner_sheet_wistia_share_link_with_wvideo_is_converted(mctx):
    make, state = mctx
    state["videos"] = [{"url": "https://www.youtube.com/watch?v=BBBBBBBBBBB", "context": "Demo", "source": "x"}]
    c = make([make_row(**{"Video URL": "https://jono.wistia.com/s/6sym77ugnbd51d3?wvideo=mmi98bf00z"})])
    runner.poll(c, now_utc=NOW)
    assert c.wp.last_review["media"]["videoUrl"] == "https://fast.wistia.net/embed/iframe/mmi98bf00z"
    assert "CHECK: Video" not in c.sheet.final(2)["Messages"]  # the sheet's video wins, no auto pick


# ------------------------------------------------------------------ reconcile decisions stick

def test_reconcile_removal_is_not_undone_by_merge(monkeypatch):
    from app import extract
    from app.fetch import Source
    text = "x" * 600
    pages = {"https://v.test/jv": Source("https://v.test/jv", "web", text),
             "https://docs.google.com/document/d/AAAAAAAAAAAAAAAAAAAAAAAA/edit": Source("gdoc-url", "gdoc", text)}
    monkeypatch.setattr(extract, "fetch", lambda u: pages[u])
    monkeypatch.setattr(extract, "pick_follow_links", lambda s: [])
    gift = {"title": "Hot Buyer Reply Vault", "description": "webinar gift"}
    base = {"productName": "P", "frontEnd": {"price": 37}, "otos": [], "affiliateBonuses": [],
            "launch": {"cartOpen": {"local": "2026-10-06 11:00", "zone": "ET"}}}
    other = dict(base, affiliateBonuses=[gift], vendor="V2")
    seq = iter([base, other])
    monkeypatch.setattr(extract, "extract_source", lambda c, s, role: (next(seq), 1, 1))
    monkeypatch.setattr(extract, "compare", lambda a, b, label: ["affiliate bonuses differ"])

    class C:
        def complete_json(self, system, user, max_tokens=0):
            class R:
                data = {"facts": dict(base, affiliateBonuses=[]),
                        "decisions": ["Affiliate bonuses: left empty, they are webinar gifts."]}
                input_tokens = output_tokens = 1
            return R()
    res = extract.run(C(), ["https://v.test/jv", "https://docs.google.com/document/d/AAAAAAAAAAAAAAAAAAAAAAAA/edit"],
                      today="2026-10-07")
    assert res.facts.get("affiliateBonuses") in ([], None)
    assert res.facts.get("vendor") == "V2"  # ordinary gaps are still filled from other sources


def test_no_screenshots_means_no_gallery_key(mctx, monkeypatch):
    make, state = mctx
    monkeypatch.setattr(media, "pick_all", lambda *a, **k: (None, [], ["none"]))
    state["images"] = [{"url": "https://v.test/x.png", "alt": "", "source": "s", "sales": True}]
    c = make([make_row()])
    runner.poll(c, now_utc=NOW)
    assert "gallery" not in c.wp.last_review and c.sheet.final(2)["Status"] == "Draft ready"


def test_pick_all_one_pass_main_then_gallery():
    routes = {f"/shot{i}.png": Resp(body=png(1200 + i * 100, 900)) for i in range(1, 7)}
    s = FakeSession(routes)
    cands = [{"url": f"https://v.test/shot{i}.png", "alt": "", "sales": True} for i in range(1, 7)]
    main, shots, notes = media.pick_all(cands, "BuyerEngine AI", s)
    assert main.source.endswith("shot6.png")  # biggest wins
    assert len(shots) == media.GALLERY_MAX and main.source not in [x.source for x in shots]
    assert shots[0].filename == "buyerengine-ai-screenshot-1.png"
    assert len(s.seen) == 6  # each image downloaded once


def test_unknown_info_wording_is_flagged():
    from app import write
    from tests.test_write import SAMPLE
    review = json.loads(json.dumps(SAMPLE))
    review["faq"][0]["a"] = "The refund period isn't stated by the vendor."
    inp = write.Inputs(facts=facts_from_sample(), fe_link=SAMPLE["links"]["frontEnd"],
                       oto_links=[o["link"] for o in SAMPLE["pricing"]["otos"]])
    assert any("points out missing information" in p for p in write._check(review, inp))
