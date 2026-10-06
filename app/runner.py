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

from . import extract, notify, write
from .llm import LLMError
from .sheets import ist_now, sheet_datetime
from .timeparse import to_utc
from .wordpress import WPError

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
    name, jv, fe = row.text("Product name"), row.text("JV doc / JV page URL"), row.text("FE affiliate link")
    missing = [h for h, v in (("Product name", name), ("JV doc / JV page URL", jv), ("FE affiliate link", fe)) if not v]
    if missing:
        return Result("Needs info", [f"Fill in: {', '.join(missing)}."])

    post_id = None
    if row.text("WP post ID"):
        try:
            post_id = int(float(row.text("WP post ID")))
        except ValueError:
            return Result("Needs info", ["WP post ID isn't a number. Clear it or fix it."])
    rewrite = row.checked("Rewrite")
    if post_id and not rewrite:
        return Result("Needs info", [f"This launch already has a post (#{post_id}). Tick Rewrite to regenerate it "
                                     "(updates that keep your manual edits come in Phase 7)."])

    tz = row.text("Timezone") or "America/New_York"
    open_utc, p1 = _local_to_utc(row.get("Cart open"), tz, "Cart open")
    close_utc, p2 = _local_to_utc(row.get("Cart close"), tz, "Cart close")
    publish_utc, p3 = _local_to_utc(row.get("Publish at"), tz, "Publish at")
    if p1 + p2 + p3:
        return Result("Needs info", p1 + p2 + p3)

    ex = extract.run(ctx.client, jv, row.text("Sales page URL") or None,
                     cart_open_override_utc=open_utc, cart_close_override_utc=close_utc,
                     owner_notes=row.text("Notes for AI") or None)
    messages = list(ex.notes)
    if not ex.ok:
        return Result("Needs info", ex.blocking + messages)

    facts = dict(ex.facts, productName=name)  # the sheet's product name is the official one
    slug = row.text("Slug") or write.slugify(name)
    method_note = row.text("Method note used") or ctx.take_method_note()
    inp = write.Inputs(
        facts=facts, fe_link=fe, oto_links=row.lines("OTO links"), bundle_links=row.lines("Bundle links"),
        own_bonuses=ctx.bonuses, method_note=method_note, testing_notes=row.text("Testing notes"),
        days_tested=row.text("Days tested"), video_url=row.text("Video URL"), author=ctx.author,
        authors=None, slug=slug)
    res = write.run(ctx.client, inp)
    messages += res.notes
    if not res.ok:
        status = "Needs info" if any("needs info" in p for p in res.problems) else "Error"
        return Result(status, res.problems + messages)

    review = res.review
    post = ctx.wp.save_review(review=review, title=f"{name} Review", post_id=post_id, want_status="draft")
    post_id, saved_slug = post["id"], post.get("slug") or slug
    # Record the post right away: if anything later fails, the next run updates this post instead of making a duplicate.
    ctx.sheet.update(row.number, {"WP post ID": post_id, "Slug": saved_slug})
    if saved_slug != slug:
        messages.append(f"WordPress saved the address as '{saved_slug}' because '{slug}' was taken.")
    warnings = ctx.wp.warnings(post)

    mode = row.text("Mode").lower()
    already_live = post.get("status") in ("publish", "future")
    if mode == "publish":
        if warnings and already_live:
            messages.append("Post is already live and was updated, but the theme reported warnings: " + " | ".join(warnings))
        elif warnings:
            messages.append("Kept as draft because the theme reported warnings: " + " | ".join(warnings))
        elif publish_utc and datetime.fromisoformat(publish_utc.replace("Z", "+00:00")) > now_utc:
            post = ctx.wp.save_review(review=review, title=f"{name} Review", post_id=post_id,
                                      want_status="future", date_gmt=publish_utc.replace("Z", ""))
        else:
            if publish_utc:
                messages.append("Publish at was already in the past, so it was published now.")
            post = ctx.wp.save_review(review=review, title=f"{name} Review", post_id=post_id, want_status="publish")
    elif warnings:
        messages.append("Theme warnings: " + " | ".join(warnings))

    wp_status = post.get("status", "draft")
    status = STATUS_FROM_WP.get(wp_status, "Draft ready")
    preview = (f"{ctx.base_url}/?post_type=review&p={post_id}&preview=true" if status == "Draft ready"
               else post.get("link", ""))
    updates = {"WP post ID": post_id, "Slug": saved_slug, "Preview link": preview,
               "Method note used": method_note}
    if rewrite:
        updates["Rewrite"] = False
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
        msg_text = " | ".join(result.messages)
        ctx.sheet.update(r.number, dict(result.updates, Status=result.status, Messages=msg_text, **{"Last run": ist_now()}))
        ctx.sheet.log([ist_now(), r.number, result.updates.get("Slug", r.text("Slug")), "run", result.status, msg_text])
        err = notify.send(f"[StackPiston] {r.text('Product name') or 'Row ' + str(r.number)}: {result.status}",
                          f"Row {r.number}: {result.status}\n\n{msg_text}\n\n{result.updates.get('Preview link', '')}")
        if err:
            ctx.sheet.log([ist_now(), r.number, "", "email", "Error", err])
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


def build_context() -> Context:
    from .config import SheetConfig, WPConfig
    from .llm import get_client
    from .sheets import SheetIO, open_sheet
    from .wordpress import WordPress
    wp_cfg = WPConfig.from_env()
    return Context(client=get_client(), wp=WordPress(wp_cfg), sheet=SheetIO(open_sheet(SheetConfig.from_env())),
                   base_url=wp_cfg.base_url, author=os.environ.get("DEFAULT_AUTHOR", "marcus").strip() or "marcus")
