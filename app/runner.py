"""Phase 5: run launches from the Google Sheet.

For every row with Status = Run (oldest first):
  Processing -> read JV sources -> apply Notes for AI -> write -> validate -> save to WordPress
  -> Draft ready / Scheduled / Published, or Needs info / Error with the reason in Messages.
Rows stuck in Processing for over 30 minutes are marked Error.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import bonus, extract, media, notify, patch, write
from .llm import LLMError
from .safety import scrub
from .sheets import ist_now, sheet_datetime
from .timeparse import to_utc
from .wordpress import WordPress, WPError

STUCK_AFTER = timedelta(minutes=30)
IST = timezone(timedelta(hours=5, minutes=30))
STATUS_FROM_WP = {"publish": "Published", "future": "Scheduled", "draft": "Draft ready", "pending": "Draft ready"}


@dataclass
class Context:
    client: object
    wp: object
    sheet: object
    base_url: str
    author: str = "marcus"
    bonuses: list = field(default_factory=list)
    method_notes: list = field(default_factory=list)
    next_note: int = 0

    def take_method_note(self) -> str:
        if not self.method_notes:
            return ""
        note = self.method_notes[self.next_note % len(self.method_notes)]
        self.next_note += 1
        return note


@dataclass
class Result:
    status: str
    messages: list = field(default_factory=list)
    updates: dict = field(default_factory=dict)
    usage: str = ""  # AI tokens used for this row, written to the Logs tab


def _local_to_utc(value, tz: str, label: str) -> tuple[str | None, list[str]]:
    try:
        dt = sheet_datetime(value)
    except ValueError as e:
        return None, [f"{label}: {e}"]
    if dt is None:
        return None, []
    utc, problems = to_utc(dt.strftime("%Y-%m-%d %H:%M"), tz)
    return utc, [f"{label}: {p}" for p in problems]


def process_row(row, ctx: Context, now_utc: datetime | None = None) -> Result:
    now_utc = now_utc or datetime.now(timezone.utc)
    name = row.text("Product name")
    jv_urls = [u for u in (row.text("JV page URL") or row.text("JV doc / JV page URL"), row.text("JV doc URL")) if u]
    fe_links = row.links("FE affiliate link")
    fe = fe_links[0] if fe_links else ""
    if row.text("FE affiliate link") and not fe:
        return Result("Needs info", ["FE affiliate link: no link starting with https:// found in the cell."])
    missing = [h for h, v in (("Product name", name), ("JV page URL or JV doc URL", jv_urls), ("FE affiliate link", fe)) if not v]
    if missing:
        return Result("Needs info", [f"Fill in: {', '.join(missing)}."])

    post_id = None
    if row.text("WP post ID"):
        try:
            post_id = int(float(row.text("WP post ID")))
        except ValueError:
            return Result("Needs info", ["WP post ID isn't a number. Clear it or fix it."])
    rewrite = row.checked("Rewrite")

    tz = row.text("Timezone") or "America/New_York"
    open_utc, p1 = _local_to_utc(row.get("Cart open"), tz, "Cart open")
    close_utc, p2 = _local_to_utc(row.get("Cart close"), tz, "Cart close")
    publish_utc, p3 = _local_to_utc(row.get("Publish at"), tz, "Publish at")
    if p1 + p2 + p3:
        return Result("Needs info", p1 + p2 + p3)

    oto_links, bundle_links = row.links("OTO links"), row.links("Bundle links")
    bad = []
    for col, found in (("OTO links", oto_links), ("Bundle links", bundle_links)):
        lines = row.lines(col)
        if len(lines) != len(found):
            bad.append(f"{col}: {len(lines)} line(s) but {len(found)} link(s) starting with https://. "
                       "Put one full link per line (labels like 'OTO 1:' are fine).")
    if bad:
        return Result("Needs info", bad)
    if post_id and not rewrite:
        # Existing post without Rewrite: patch from the sheet only (no AI, $0), keeping manual edits.
        result = patch.patch_row(row, ctx, post_id=post_id, name=name, fe=fe, oto_links=oto_links,
                                 bundle_links=bundle_links, open_utc=open_utc, close_utc=close_utc,
                                 publish_utc=publish_utc, now_utc=now_utc)
        if row.checked("Rebuild bonus") and result.status in STATUS_FROM_WP.values():
            rebuild_bonus_only(row, ctx, post_id, result)
        return result
    current = {}
    if post_id:  # Rewrite: make sure the post exists and isn't trashed BEFORE spending anything on AI
        try:
            current = ctx.wp.get_review(post_id) or {}
        except WPError as e:
            return Result("Needs info", [f"Post #{post_id} couldn't be read from WordPress ({e}). If it was deleted, "
                                         "clear WP post ID (and Slug) to create a new review. No AI was used."])
        if current.get("status") not in patch.EDITABLE:
            return Result("Needs info", [f"Post #{post_id} is '{current.get('status')}' in WordPress (for example in "
                                         "the trash), so it wasn't rewritten. Restore it in wp-admin, or clear WP post "
                                         "ID and Slug to create a new review. No AI was used."])
    ex = extract.run(ctx.client, jv_urls, row.text("Sales page URL") or None,
                     cart_open_override_utc=open_utc, cart_close_override_utc=close_utc,
                     owner_notes=row.text("Notes for AI") or None,
                     # Only counts the owner actually entered: an empty OTO links cell means "no links", not "no OTOs".
                     hints={k: n for k, n in (("oto_links", len(oto_links)), ("bundle_links", len(bundle_links))) if n} or None)
    checks = [f"CHECK: {w}" for w in ex.warnings]
    messages = checks + list(ex.notes)
    if not ex.ok:
        return Result("Needs info", ex.blocking + messages, usage=_usage(ctx, ex))

    # Phase 6: demo video. The sheet's Video URL always wins; otherwise only a clearly labelled demo is used.
    video_url = row.text("Video URL")
    try:
        if video_url:
            playable, _ = media.normalize_video(video_url)
            if playable:
                video_url = playable
            elif "wistia" in video_url.lower():
                messages.append("Video URL is a Wistia share link I couldn't convert, so the player may not show. "
                                "Use the link from right-click on the video → Copy link and thumbnail.")
        else:
            vp = media.pick_video(ex.video_candidates)
            video_url = vp.url
            if vp.check:
                checks.append(f"CHECK: {vp.check}")
                messages.insert(0, f"CHECK: {vp.check}")
            if vp.note:
                messages.append(vp.note)
    except Exception as e:  # media is optional: never stop a row for it
        messages.append(f"Video step skipped ({e.__class__.__name__}).")

    # Main image and screenshots: a Rewrite keeps the ones already on the post instead of uploading copies.
    featured_id, image_url, gallery, image_source = None, "", [], ""
    if post_id:
        try:
            fm = int(current.get("featured_media") or 0)
            if fm:
                m = ctx.wp.get_media(fm) or {}
                featured_id, image_url = fm, m.get("source_url", "")
                image_source = ((m.get("description") or {}).get("raw") or "").strip()
            gallery = list((WordPress.review_json(current) or {}).get("gallery") or [])
        except Exception:
            featured_id, image_url, gallery, image_source = None, "", [], ""

    facts = dict(ex.facts, productName=name)  # the sheet's product name is the official one
    slug = row.text("Slug") or write.slugify(name)
    method_note = row.text("Method note used") or ctx.take_method_note()
    inp = write.Inputs(
        facts=facts, fe_link=fe, oto_links=oto_links, bundle_links=bundle_links,
        own_bonuses=ctx.bonuses, method_note=method_note, testing_notes=row.text("Testing notes"),
        days_tested=row.text("Days tested"), video_url=video_url, image_url=image_url, author=ctx.author,
        authors=None, slug=slug)
    res = write.run(ctx.client, inp)
    messages += res.notes
    if not res.ok:
        status = "Needs info" if any("needs info" in p for p in res.problems) else "Error"
        return Result(status, res.problems + messages, usage=_usage(ctx, ex, res))

    review = res.review
    # Main image: only from the sheet's Main image URL (a Rewrite without a link keeps the current one).
    main_url = row.text("Main image URL")
    if main_url and featured_id and image_source == f"Source: {main_url}":
        pass  # same link as the image already on the post: reuse it, no duplicate upload
    elif main_url:
        try:
            main, note = media.fetch_image(main_url, name)
            messages.append(note)
            if main:
                up = ctx.wp.upload_media(main.data, main.filename, main.mime, alt=main.alt, source=main_url)
                featured_id, image_url = int(up["id"]), up.get("source_url", "")
        except Exception as e:  # media is optional: never stop a row for it
            messages.append(f"Main image skipped ({e.__class__.__name__}: {str(e)[:150]}).")
    elif not featured_id:
        messages.append("Main image URL is empty, so the review has no main image (or share image).")
    if image_url and (review.get("media") or {}).get("type") == "image":
        review["media"]["imageUrl"] = image_url
    # Screenshots for "Inside the product": picked automatically from the sources.
    if not gallery:
        try:
            cands = [c for c in ex.image_candidates if c.get("url") != main_url]
            _, shots, notes = media.pick_all(cands, name, want_main=False)
            messages += notes
            for shot in shots:
                try:
                    up = ctx.wp.upload_media(shot.data, shot.filename, shot.mime, alt=shot.alt)
                    if up.get("source_url"):
                        gallery.append({"url": up["source_url"], "alt": shot.alt})
                except Exception as e:
                    messages.append(f"A screenshot upload was skipped ({e.__class__.__name__}).")
        except Exception as e:  # media is optional: never stop a row for it
            messages.append(f"Image step skipped ({e.__class__.__name__}: {str(e)[:150]}).")
    if gallery:
        review["gallery"] = gallery[:media.GALLERY_MAX]
    updates = {"Method note used": method_note}
    if rewrite:
        updates["Rewrite"] = False

    # Buyer bonus (one extra AI call, after the review is saved and published). A Rewrite keeps the saved bonus,
    # and its link, unless Rebuild bonus is ticked.
    rebuild = row.checked("Rebuild bonus")
    if rebuild:
        updates["Rebuild bonus"] = False
    old_bonus, old_ok = bonus.existing(current) if post_id else (None, False)
    used = {"in": 0, "out": 0}
    job = {"review": review, "old": old_bonus, "old_ok": old_ok, "want_new": old_bonus is None or not old_ok or rebuild,
           "used": used}
    result = save_and_finish(row, ctx, review=review, name=name, post_id=post_id, slug=slug, messages=messages,
                             checks=checks, publish_utc=publish_utc, featured_id=featured_id, now_utc=now_utc,
                             extra_updates=updates, bonus_job=job)
    result.usage = _usage(ctx, ex, res, used)
    return result


def _usage(ctx: Context, ex=None, res=None, used: dict | None = None) -> str:
    """AI tokens for the Logs tab, so the real cost per review can be worked out."""
    parts = []
    if ex is not None:
        parts.append(f"extract {ex.input_tokens}/{ex.output_tokens}")
    if res is not None:
        parts.append(f"write {res.input_tokens}/{res.output_tokens}")
    if used is not None:
        parts.append(f"bonus {used.get('in', 0)}/{used.get('out', 0)}")
    model = getattr(ctx.client, "model", "")
    return "tokens in/out: " + ", ".join(parts) + (f" ({model})" if model else "")


def _bonus_support(post: dict) -> tuple[dict | None, str]:
    """(bonus status from the theme, reason the bonus step can't run). An empty reason means it can run."""
    if "sp_bonus_status" not in post:
        return None, ("BONUS not made: the site's theme is older than 1.5.1. Upload the new theme, then tick "
                      "Rebuild bonus. No AI was used for the bonus.")
    st = post.get("sp_bonus_status")
    if not isinstance(st, dict):
        return None, ("BONUS not made: WordPress didn't report the bonus status (the pipeline user must be able to "
                      "edit reviews). No AI was used for the bonus.")
    if st.get("enabled") is False:
        return st, "BONUS not made: bonus pages are switched off in Appearance → StackPiston. No AI was used for the bonus."
    return st, ""


def bonus_step(row, ctx: Context, *, post: dict, post_id: int, review: dict, old_bonus, old_ok: bool,
               want_new: bool, messages: list, used: dict) -> dict:
    """Make (if wanted) and save the buyer bonus, then return the sheet updates (the Bonus link).
    Never raises: the bonus is optional and must never affect the review."""
    st, problem = _bonus_support(post)
    made = False
    if want_new and problem:
        messages.append(problem)
    elif want_new:
        try:
            br = bonus.generate(ctx.client, review, code=old_bonus.get("code") if old_bonus else None)
        except Exception as e:
            br = bonus.BonusResult(notes=[f"BONUS skipped ({e.__class__.__name__}: {str(e)[:150]})."])
        used["in"], used["out"] = br.input_tokens, br.output_tokens
        messages.extend(br.notes)
        if br.bonus:
            try:
                saved = ctx.wp.save_bonus(post_id, br.bonus)
                st = saved.get("sp_bonus_status") if isinstance(saved.get("sp_bonus_status"), dict) else None
                made = True
                if st and not st.get("ok"):
                    messages.append("BONUS saved but the site hid it: " + "; ".join(st.get("errors") or [])[:300])
                    if old_bonus is not None and old_ok:  # put the working bonus back so the JVZoo link keeps working
                        back = ctx.wp.save_bonus(post_id, old_bonus)
                        st = back.get("sp_bonus_status") if isinstance(back.get("sp_bonus_status"), dict) else None
                        messages.append("The previous bonus was put back, so its link still works.")
                elif st and old_bonus is not None and not old_ok:
                    messages.append("BONUS: the saved bonus broke the rules, so a new one was made (same link).")
            except Exception as e:
                messages.append(f"BONUS not saved ({e.__class__.__name__}: {str(e)[:150]}).")
    if not want_new and st and st.get("enabled") is not False and not st.get("saved"):
        messages.append("No buyer bonus on this review yet: tick Rebuild bonus to make one (one AI call).")
    updates = {}
    if st and st.get("enabled") is not False and st.get("ok") and st.get("url"):
        if "bonus link" in getattr(row, "values", {}):
            updates["Bonus link"] = st["url"]
        elif made:
            messages.append("Add a column named 'Bonus link' to the sheet to see the bonus page link there. "
                            "It's also in the review's edit screen in wp-admin.")
    return updates


def rebuild_bonus_only(row, ctx: Context, post_id: int, result: "Result") -> None:
    """Rebuild bonus ticked without Rewrite: only the bonus AI call, from the review already on the post.
    The review itself (and any manual edits) stays as it is."""
    try:
        current = ctx.wp.get_review(post_id)
    except Exception as e:
        result.messages.append(f"Rebuild bonus: the post couldn't be read ({e.__class__.__name__}). Try again later.")
        return
    review = WordPress.review_json(current) or {}
    old, ok = bonus.existing(current)
    used = {"in": 0, "out": 0}
    result.updates.update(bonus_step(row, ctx, post=current, post_id=post_id, review=review, old_bonus=old,
                                     old_ok=ok, want_new=True, messages=result.messages, used=used))
    result.updates["Rebuild bonus"] = False
    result.usage = _usage(ctx, used=used)


def save_and_finish(row, ctx: Context, *, review: dict, name: str, post_id, slug: str, messages: list,
                    checks: list, publish_utc, featured_id, now_utc: datetime, extra_updates: dict,
                    bonus_job: dict | None = None) -> Result:
    """Save as draft, record the post in the sheet, then publish/schedule if Mode = Publish allows it.
    Shared by new reviews, rewrites and patches, so all follow the same publishing rules.
    bonus_job: what to do about the buyer bonus; it runs last, after the review is saved and published."""
    post = ctx.wp.save_review(review=review, title=f"{name} Review", post_id=post_id, want_status="draft",
                              featured_media=featured_id)
    post_id, saved_slug = post["id"], post.get("slug") or slug
    # Record the post right away: if anything later fails, the next run updates this post instead of making a duplicate.
    ctx.sheet.update(row.number, {"WP post ID": post_id, "Slug": saved_slug})
    if slug and saved_slug != slug:
        messages.append(f"WordPress saved the address as '{saved_slug}' because '{slug}' was taken.")
    warnings = ctx.wp.warnings(post)

    mode = row.text("Mode").lower()
    already_live = post.get("status") in ("publish", "future")
    if mode == "publish":
        if checks and not already_live:
            messages.insert(0, "Kept as draft: the AI made choices you should check (see CHECK lines).")
            if warnings:
                messages.append("Theme warnings: " + " | ".join(warnings))
        elif warnings and already_live:
            messages.append("Post is already live and was updated, but the theme reported warnings: " + " | ".join(warnings))
        elif warnings:
            messages.append("Kept as draft because the theme reported warnings: " + " | ".join(warnings))
        elif publish_utc and datetime.fromisoformat(publish_utc.replace("Z", "+00:00")) > now_utc:
            post = ctx.wp.save_review(review=review, title=f"{name} Review", post_id=post_id,
                                      want_status="future", date_gmt=publish_utc.replace("Z", ""),
                                      featured_media=featured_id)
        else:
            if publish_utc:
                messages.append("Publish at was already in the past, so it was published now.")
            post = ctx.wp.save_review(review=review, title=f"{name} Review", post_id=post_id, want_status="publish",
                                      featured_media=featured_id)
    elif warnings:
        messages.append("Theme warnings: " + " | ".join(warnings))

    # Buyer bonus last, so a slow or failed AI call can never hold back saving or publishing the review.
    job = bonus_job or {}
    extra_updates = dict(extra_updates, **bonus_step(
        row, ctx, post=post, post_id=post_id, review=job.get("review") or review, old_bonus=job.get("old"),
        old_ok=job.get("old_ok", False), want_new=job.get("want_new", False), messages=messages,
        used=job.get("used", {})))

    wp_status = post.get("status", "draft")
    status = STATUS_FROM_WP.get(wp_status, "Draft ready")
    preview = (f"{ctx.base_url}/?post_type=review&p={post_id}&preview=true" if status == "Draft ready"
               else post.get("link", ""))
    updates = dict(extra_updates, **{"WP post ID": post_id, "Slug": saved_slug, "Preview link": preview})
    return Result(status, messages, updates)


def _stuck(row, now_utc: datetime) -> bool:
    if row.text("Status").lower() != "processing":
        return False
    try:
        last = datetime.strptime(row.text("Last run"), "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    except ValueError:
        return True
    return now_utc - last > STUCK_AFTER


def poll(ctx: Context, only_row: int | None = None, now_utc: datetime | None = None) -> dict:
    now_utc = now_utc or datetime.now(timezone.utc)
    rows = ctx.sheet.launches()
    ctx.bonuses, ctx.method_notes = ctx.sheet.bonuses(), ctx.sheet.method_notes()
    ctx.next_note = sum(1 for r in rows if r.text("Method note used"))
    counts: dict[str, int] = {}

    for r in rows:
        if only_row is None and _stuck(r, now_utc):
            ctx.sheet.update(r.number, {"Status": "Error", "Messages": "The run stopped unexpectedly. Set Status to Run to try again."})
            counts["Error"] = counts.get("Error", 0) + 1

    todo = [r for r in rows if (r.number == only_row if only_row else r.text("Status").lower() == "run")]
    for r in todo:
        ctx.sheet.update(r.number, {"Status": "Processing", "Last run": ist_now(), "Messages": ""})
        try:
            result = process_row(r, ctx, now_utc)
        except (LLMError, WPError) as e:
            result = Result("Error", [str(e)])
        except Exception as e:  # keep going with the next row; the message never includes secrets
            result = Result("Error", [f"Unexpected problem: {e.__class__.__name__}: {str(e)[:300]}"])
        msg_text = scrub(" | ".join(result.messages))
        ctx.sheet.update(r.number, dict(result.updates, Status=result.status, Messages=msg_text, **{"Last run": ist_now()}))
        ctx.sheet.log([ist_now(), r.number, result.updates.get("Slug", r.text("Slug")), "run", result.status, msg_text])
        if result.usage:
            ctx.sheet.log([ist_now(), r.number, result.updates.get("Slug", r.text("Slug")), "ai-usage", "", result.usage])
        err = notify.send(f"[StackPiston] {r.text('Product name') or 'Row ' + str(r.number)}: {result.status}",
                          f"Row {r.number}: {result.status}\n\n{msg_text}\n\n{result.updates.get('Preview link', '')}")
        if err:
            ctx.sheet.log([ist_now(), r.number, "", "email", "Error", err])
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


def build_context(need_ai: bool = True) -> Context:
    from .config import SheetConfig, WPConfig
    from .llm import get_client
    from .sheets import SheetIO, open_sheet
    wp_cfg = WPConfig.from_env()
    return Context(client=get_client() if need_ai else None, wp=WordPress(wp_cfg),
                   sheet=SheetIO(open_sheet(SheetConfig.from_env())),
                   base_url=wp_cfg.base_url, author=os.environ.get("DEFAULT_AUTHOR", "marcus").strip() or "marcus")
