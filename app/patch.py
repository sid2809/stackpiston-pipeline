"""Phase 7: changes without AI ($0).

Patch (a row that already has a post, Status = Run, Rewrite NOT ticked):
  Updates only what comes from the sheet: FE / OTO / bundle links, cart open / close, video, main image,
  and Publish at. Everything else in the review, including your manual edits in wp-admin, stays as it is.
  Adding or removing OTOs or bundles needs Rewrite (that changes the review's text).

Refresh (daily job: python -m app.main refresh):
  - Sheet Status follows WordPress: a post you published shows Published, a scheduled one Scheduled.
  - After the launch ends, the main price switches to the after-launch price (if the vendor stated one).
    Coupons stay. "Post-launch done" is filled in so it happens only once.
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import media
from .safety import scrub
from .validate import link_errors, schema_errors
from .wordpress import WordPress, WPError
from .write import _money

NY = ZoneInfo("America/New_York")
EDITABLE = ("draft", "pending", "future", "publish")  # never touch trashed or private posts
IST = timezone(timedelta(hours=5, minutes=30))


def _tz_label(cart_open_utc: str) -> str:
    try:
        return datetime.fromisoformat(cart_open_utc.replace("Z", "+00:00")).astimezone(NY).tzname() or ""
    except ValueError:
        return ""


def _urls_in(node) -> list[str]:
    out = []
    if isinstance(node, dict):
        for v in node.values():
            out += _urls_in(v)
    elif isinstance(node, list):
        for v in node:
            out += _urls_in(v)
    elif isinstance(node, str) and node.startswith(("http://", "https://")):
        out.append(node)
    return out


# ------------------------------------------------------------------ patch

def patch_row(row, ctx, *, post_id: int, name: str, fe: str, oto_links: list[str], bundle_links: list[str],
              open_utc, close_utc, publish_utc, now_utc: datetime):
    from .runner import Result, save_and_finish
    try:
        current = ctx.wp.get_review(post_id)
    except WPError as e:
        return Result("Needs info", [f"Post #{post_id} couldn't be read from WordPress ({e}). If it was deleted, "
                                     "clear WP post ID (and Slug) to create a new review."])
    if current.get("status") not in EDITABLE:
        return Result("Needs info", [f"Post #{post_id} is '{current.get('status')}' in WordPress (for example in the "
                                     "trash), so it wasn't changed. Restore it in wp-admin, or clear WP post ID and "
                                     "Slug to create a new review."])
    review = WordPress.review_json(current)
    if not review:
        return Result("Needs info", [f"Post #{post_id} has no review data to patch. Tick Rewrite to write it again."])
    before = copy.deepcopy(review)
    errors_before = set(schema_errors(before))
    changed, messages = [], []

    # Links
    links = review.setdefault("links", {})
    if fe and links.get("frontEnd") != fe:
        links["frontEnd"] = fe
        changed.append("FE link")
    pricing = review.setdefault("pricing", {})
    for key, label, sheet_links in (("otos", "OTO", oto_links), ("bundles", "Bundle", bundle_links)):
        items = pricing.get(key) or []
        if not sheet_links:
            continue
        if len(sheet_links) != len(items):
            return Result("Needs info", [f"The sheet has {len(sheet_links)} {label} link(s) but the review has "
                                         f"{len(items)} {label}(s). Patch can't add or remove offers; tick Rewrite."])
        for i, (item, link) in enumerate(zip(items, sheet_links), 1):
            if isinstance(item, dict) and item.get("link") != link:
                item["link"] = link
                changed.append(f"{label} {i} link")

    # Launch dates (blank cells keep what the review has)
    launch = review.setdefault("launch", {})
    if open_utc and launch.get("cartOpen") != open_utc:
        launch["cartOpen"] = open_utc
        launch["timezoneLabel"] = _tz_label(open_utc)
        changed.append("cart open")
    if close_utc and launch.get("cartClose") != close_utc:
        launch["cartClose"] = close_utc
        changed.append("cart close")
        if row.text("Post-launch done") and datetime.fromisoformat(close_utc.replace("Z", "+00:00")) > now_utc:
            messages.append("Cart close moved to a later date, but the after-launch price switch already happened, so "
                            "the page keeps the after-launch price. Fix the price in wp-admin or tick Rewrite.")

    # Video (blank keeps the current one)
    video = row.text("Video URL")
    if video:
        playable, _ = media.normalize_video(video)
        if not playable:
            if "wistia" in video.lower():
                messages.append("Video URL is a Wistia share link I couldn't convert. Use right-click on the video → "
                                "Copy link and thumbnail.")
            playable = video
        m = review.get("media") or {}
        if m.get("type") != "video" or m.get("videoUrl") != playable:
            review["media"] = {"type": "video", "videoUrl": playable, "videoTitle": m.get("videoTitle", ""),
                               "caption": m.get("caption", "")}
            changed.append("video")

    # Main image (only when the link is new; the source link is remembered on the uploaded image)
    featured_id = int(current.get("featured_media") or 0) or None
    main_url = row.text("Main image URL")
    if main_url:
        known = ""
        if featured_id:
            try:
                known = ((ctx.wp.get_media(featured_id) or {}).get("description") or {}).get("raw", "") or ""
            except Exception:
                known = ""
        if known.strip() != f"Source: {main_url}":
            try:
                pick, note = media.fetch_image(main_url, name)
                messages.append(note)
                if pick:
                    up = ctx.wp.upload_media(pick.data, pick.filename, pick.mime, alt=pick.alt, source=main_url)
                    featured_id = int(up["id"])
                    if (review.get("media") or {}).get("type") == "image" and up.get("source_url"):
                        review["media"]["imageUrl"] = up["source_url"]
                    changed.append("main image")
            except Exception as e:
                messages.append(f"Main image skipped ({e.__class__.__name__}: {str(e)[:150]}).")

    # Check only what the patch touched: new schema errors and affiliate-link mismatches.
    new_errors = [e for e in schema_errors(review) if e not in errors_before]
    def _links(key, sheet):
        return sheet or [x.get("link", "") for x in pricing.get(key) or [] if isinstance(x, dict)]
    link_probs = [e for e in link_errors(review, fe, _links("otos", oto_links), bundle_links=_links("bundles", bundle_links),
                                         extra_allowed=_urls_in(review)) if not e.startswith("Unexpected URL")]
    if new_errors or link_probs:
        return Result("Needs info", ["Patch not saved: " + p for p in new_errors + link_probs])

    if not changed and row.text("Mode").lower() != "publish":
        status = {"publish": "Published", "future": "Scheduled"}.get(current.get("status"), "Draft ready")
        return Result(status, messages + ["Nothing to update: the sheet already matches the post."])

    if changed:
        review.setdefault("review", {})["updatedAt"] = now_utc.astimezone(NY).strftime("%Y-%m-%d")
    messages.insert(0, "Patched (no AI, $0): " + (", ".join(changed) if changed else "publishing settings only") +
                    ". Your other edits were kept.")
    return save_and_finish(row, ctx, review=review, name=name, post_id=post_id, slug=row.text("Slug"),
                           messages=messages, checks=[], publish_utc=publish_utc, featured_id=featured_id,
                           now_utc=now_utc, extra_updates={})


# ------------------------------------------------------------------ refresh

def _all_amounts(review: dict) -> list[float]:
    def d(x):
        return x if isinstance(x, dict) else {}
    p = d(review.get("pricing"))
    out = [d(p.get("frontEnd")).get("price")]
    for o in p.get("otos") or []:
        out += [d(o).get("price"), d(d(o).get("downsell")).get("price")]
    out += [d(b).get("price") for b in p.get("bundles") or []]
    out += [d(b).get("value") for b in (review.get("bonuses") or []) + (review.get("vendorBonuses") or [])]
    return [float(x) for x in out if isinstance(x, (int, float)) and not isinstance(x, bool)]


def _replace_price(node, old: str, new: str, counter: list):
    pat = re.compile(r"\$" + re.escape(old) + r"(?![\d.,]\d|\d)")
    if isinstance(node, dict):
        return {k: _replace_price(v, old, new, counter) for k, v in node.items()}
    if isinstance(node, list):
        return [_replace_price(v, old, new, counter) for v in node]
    if isinstance(node, str):
        res, n = pat.subn("$" + new, node)
        counter[0] += n
        return res
    return node


def switch_to_after_launch(review: dict) -> tuple[dict, str]:
    """Returns (updated review, message). The review is unchanged when there's no after-launch price."""
    fe = (review.get("pricing") or {}).get("frontEnd") or {}
    price, after = fe.get("price"), fe.get("priceAfterLaunch")
    if not isinstance(after, (int, float)) or not isinstance(price, (int, float)) or after <= price:
        return review, (f"Launch ended. No after-launch price was stated, so the page keeps ${_money(price or 0)}. "
                        "Change it in wp-admin if the vendor raised the price.")
    old, new = _money(price), _money(after)
    r = copy.deepcopy(review)
    if _all_amounts(review).count(float(price)) == 1:
        counter = [0]
        for key in ("seo", "product", "review", "pricing", "features", "pros", "cons", "goodFor", "notFor", "faq"):
            if key in r:
                r[key] = _replace_price(r[key], old, new, counter)
        text_note = f" ${old} was also replaced in {counter[0]} place(s) in the text." if counter[0] else ""
    else:
        text_note = (f" The text still mentions ${old} in places (another offer costs the same, so I didn't replace "
                     "it automatically). Check the wording in wp-admin.")
    r["pricing"]["frontEnd"]["price"] = float(after)
    r["pricing"]["frontEnd"].pop("priceAfterLaunch", None)
    return r, f"Launch ended: front-end price switched from ${old} to ${new}. Coupons kept.{text_note}"


def refresh(ctx, now_utc: datetime | None = None) -> dict:
    from .runner import STATUS_FROM_WP
    from .sheets import ist_now
    now_utc = now_utc or datetime.now(timezone.utc)
    counts: dict[str, int] = {}
    for row in ctx.sheet.launches():
        if not row.text("WP post ID") or row.text("Status").lower() in ("run", "processing"):
            continue
        try:
            post_id = int(float(row.text("WP post ID")))
            post = ctx.wp.get_review(post_id)
        except (ValueError, WPError) as e:
            ctx.sheet.log([ist_now(), row.number, row.text("Slug"), "refresh", "Error", scrub(f"Couldn't read the post: {e}")])
            counts["Error"] = counts.get("Error", 0) + 1
            continue
        updates, notes = {}, []
        wp_status = post.get("status", "draft")
        if wp_status not in EDITABLE:  # trashed/private: never save (saving would restore it from the trash)
            ctx.sheet.log([ist_now(), row.number, row.text("Slug"), "refresh", "Skipped",
                           f"Post #{post_id} is '{wp_status}' in WordPress, so it was left alone."])
            continue

        review = WordPress.review_json(post)
        close = ((review or {}).get("launch") or {}).get("cartClose") or ""
        ended = False
        try:
            ended = bool(close) and datetime.fromisoformat(close.replace("Z", "+00:00")) <= now_utc
        except ValueError:
            pass
        if review and ended and not row.text("Post-launch done"):
            new_review, msg = switch_to_after_launch(review)
            if new_review is not review:
                new_review.setdefault("review", {})["updatedAt"] = now_utc.astimezone(NY).strftime("%Y-%m-%d")
                title = post.get("title")
                title = (title.get("raw") if isinstance(title, dict) else title) or f"{row.text('Product name')} Review"
                saved = ctx.wp.save_review(review=new_review, title=title, post_id=post_id,
                                           want_status=wp_status if wp_status in ("draft", "publish", "future") else "draft")
                wp_status = saved.get("status", wp_status)
            notes.append(msg)
            updates["Post-launch done"] = f"Yes ({now_utc.astimezone(IST).strftime('%Y-%m-%d')})"
            counts["Post-launch switched"] = counts.get("Post-launch switched", 0) + 1

        sheet_status = row.text("Status")
        wp_label = STATUS_FROM_WP.get(wp_status, "Draft ready")
        if sheet_status in ("", "Draft ready", "Scheduled", "Published") and sheet_status != wp_label:
            updates["Status"] = wp_label
            if wp_label != "Draft ready":
                updates["Preview link"] = post.get("link", "")
            notes.append(f"Status updated to {wp_label} (from WordPress).")
            counts[wp_label] = counts.get(wp_label, 0) + 1

        if updates:
            if notes:
                old = row.text("Messages")
                updates["Messages"] = scrub(" | ".join(notes) + (" | " + old if old else ""))[:2000]
            ctx.sheet.update(row.number, updates)
            ctx.sheet.log([ist_now(), row.number, row.text("Slug"), "refresh", "OK", " | ".join(notes)])
    return counts
