"""Phase 6: demo video and main image picking. All network replies are simulated."""
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
    def __init__(self, featured=0, fail_upload=False):
        super().__init__()
        self.uploads, self.featured, self.fail_upload = [], featured, fail_upload

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
        return {"id": post_id, "featured_media": self.featured, "status": "draft"}

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


def fake_pick_image(cands, product, s=None):
    if not cands:
        return None, "No images found in the sources."
    return media.ImagePick(png(1200, 630), "image/png", "clipforge-ai-review.png", 1200, 630, cands[0]["url"], "x"), \
        "Main image picked automatically."


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


def test_runner_uploads_image_and_sets_featured_media(mctx, monkeypatch):
    make, state = mctx
    monkeypatch.setattr(media, "pick_image", fake_pick_image)
    state["images"] = [{"url": "https://v.test/hero.png", "alt": "", "source": "the sales page", "sales": True}]
    c = make([make_row()])
    runner.poll(c, now_utc=NOW)
    assert c.wp.uploads == [("clipforge-ai-review.png", "image/png", "x")] and c.wp.calls_fm == [777]
    assert c.wp.last_review["media"]["type"] == "image"
    assert c.wp.last_review["media"]["imageUrl"] == "https://s.test/wp-content/uploads/clipforge-ai-review.png"
    assert c.sheet.final(2)["Status"] == "Draft ready"


def test_runner_rewrite_reuses_existing_image(mctx, monkeypatch):
    make, state = mctx
    monkeypatch.setattr(media, "pick_image", fake_pick_image)
    state["images"] = [{"url": "https://v.test/hero.png", "alt": "", "source": "s", "sales": True}]
    wp = MediaWP(featured=55)
    c = make([make_row(**{"WP post ID": 9, "Rewrite": True})], wp=wp)
    runner.poll(c, now_utc=NOW)
    assert wp.uploads == [] and wp.calls_fm == [55]
    assert wp.last_review["media"]["imageUrl"] == "https://s.test/wp-content/uploads/old.png"


def test_runner_upload_failure_never_stops_the_row(mctx, monkeypatch):
    make, state = mctx
    monkeypatch.setattr(media, "pick_image", fake_pick_image)
    state["images"] = [{"url": "https://v.test/hero.png", "alt": "", "source": "s", "sales": True}]
    c = make([make_row()], wp=MediaWP(fail_upload=True))
    runner.poll(c, now_utc=NOW)
    out = c.sheet.final(2)
    assert out["Status"] == "Draft ready" and "Image step skipped" in out["Messages"] and c.wp.calls_fm == [None]


def test_runner_sheet_wistia_share_link_with_wvideo_is_converted(mctx):
    make, state = mctx
    state["videos"] = [{"url": "https://www.youtube.com/watch?v=BBBBBBBBBBB", "context": "Demo", "source": "x"}]
    c = make([make_row(**{"Video URL": "https://jono.wistia.com/s/6sym77ugnbd51d3?wvideo=mmi98bf00z"})])
    runner.poll(c, now_utc=NOW)
    assert c.wp.last_review["media"]["videoUrl"] == "https://fast.wistia.net/embed/iframe/mmi98bf00z"
    assert "CHECK: Video" not in c.sheet.final(2)["Messages"]  # the sheet's video wins, no auto pick
