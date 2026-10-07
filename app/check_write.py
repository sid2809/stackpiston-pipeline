"""Phase 4 checkpoint (dry run): `python -m app.check_write <JV URL> [sales URL]`

Extracts facts, then writes and validates a full review with placeholder affiliate links.
Uses your real Bonuses and Method notes tabs if the sheet settings exist. Writes nothing to
WordPress or the sheet. Continues past extraction problems so the writing can be judged,
but prints them: a real run would stop there.

Optional: CART_OPEN_UTC=2026-10-05T15:00:00Z (pretend Cart open is filled in the sheet).
Optional: NOTES_FOR_AI="..." (pretend the Notes for AI column is filled in).
Optional: add --draft to also create a "[DRY RUN]" draft on WP_BASE_URL (use staging) so you can
see the real page. Re-running replaces nothing: delete old dry-run drafts in wp-admin.
"""
from __future__ import annotations

import json
import os
import sys

from . import extract, media, write
from .llm import LLMError, get_client

PLACEHOLDER = "https://www.jvzoo.com/c/DRYRUN/{}"
DEFAULT_BONUS = [{"type": "Access", "title": "15-Min 1-on-1 Founder Call",
                  "description": "A private 15-minute call with the StackPiston founder to plan your setup.", "value": 97}]


def sheet_extras():
    try:
        from .config import SheetConfig
        from .sheets import open_sheet
        sh = open_sheet(SheetConfig.from_env())
        bonuses = []
        for row in sh.worksheet("Bonuses").get_all_records():
            if str(row.get("Active", "")).strip().upper() == "Y" and row.get("Title"):
                bonuses.append({"type": str(row.get("Type", ""))[:14], "title": str(row["Title"])[:40],
                                "description": str(row.get("Description", ""))[:140],
                                "value": float(row.get("Value ($)") or 0)})
        notes = [r[0] for r in sh.worksheet("Method notes").get_all_values()[1:] if r and r[0].strip()]
        return bonuses or DEFAULT_BONUS, (notes[0] if notes else ""), "sheet"
    except Exception as e:  # dry run only: fall back quietly
        return DEFAULT_BONUS, "", f"defaults ({e.__class__.__name__})"


def draft_preview(review: dict) -> None:
    from .config import ConfigError, WPConfig
    from .wordpress import WordPress, WPError
    try:
        cfg = WPConfig.from_env()
        if "staging" not in cfg.base_url and os.environ.get("DRYRUN_ALLOW_LIVE") != "1":
            print(f"\nDRAFT NOT CREATED: WP_BASE_URL is {cfg.base_url}, not staging. Dry-run drafts with "
                  "placeholder links only go to staging (set DRYRUN_ALLOW_LIVE=1 to override).")
            return
        wp = WordPress(cfg)
        r = dict(review, slug="dryrun-" + review.get("slug", "review"))
        post = wp.save_review(review=r, title=f"[DRY RUN] {review['product']['name']} Review",
                              post_id=None, want_status="draft")
        print(f"\nDRAFT CREATED on {cfg.base_url}: post #{post['id']}")
        print(f"  Preview (log into wp-admin first): {cfg.base_url}/?post_type=review&p={post['id']}&preview=true")
        w = wp.warnings(post)
        print(f"  Theme warnings: {len(w)}" + ("" if not w else " -> " + " | ".join(w)))
    except (ConfigError, WPError) as e:
        print(f"\nDRAFT NOT CREATED: {e}")


def main() -> int:
    make_draft = "--draft" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--draft"]
    if not args:
        print("FAIL  give a JV URL: python -m app.check_write <JV URL> [sales page URL]")
        return 1
    try:
        client = get_client()
        ex = extract.run(client, args[0], args[1] if len(args) > 1 else None,
                         cart_open_override_utc=os.environ.get("CART_OPEN_UTC") or None,
                         owner_notes=os.environ.get("NOTES_FOR_AI") or None)
    except LLMError as e:
        print(f"FAIL  {e}")
        return 1
    print("SOURCES READ:")
    for src in ex.sources:
        print(f"  - {src['kind']:4} {src['chars']:>6} chars  {src['url']}")
    print("EXTRACTION:\n" + extract.summary(ex) if ex.facts else "EXTRACTION: no facts")
    for w in ex.warnings:
        print(f"  ⚠ CHECK: {w}")
    for n in ex.notes:
        print(f"  • {n}")
    for b in ex.blocking:
        print(f"  ✗ (real run would stop here) {b}")
    if not ex.facts.get("productName"):
        print("FAIL  no facts to write from.")
        return 1
    print(f"\nMEDIA (dry run: nothing is uploaded): {len(ex.video_candidates)} video(s), "
          f"{len(ex.image_candidates)} image(s) found")
    for v in ex.video_candidates[:8]:
        print(f"  - video {v['url']}  label: \"{v['context'][:80]}\"  ({v['source']})")
    vp = media.pick_video(ex.video_candidates)
    print(f"  VIDEO PICK: {vp.url or 'none'}" + (f"  ⚠ CHECK: {vp.check}" if vp.check else "") +
          (f"  • {vp.note}" if vp.note else ""))
    pick, note = media.pick_image(ex.image_candidates, ex.facts.get("productName", ""))
    print(f"  IMAGE PICK: {note}")
    bonuses, method_note, src = sheet_extras()
    f = dict(ex.facts)
    unpriced = [b.get("name") for b in f.get("bundles") or [] if b.get("price") is None]
    if unpriced:
        print(f"  ✗ (real run would stop here) bundle(s) with no price: {', '.join(map(str, unpriced))}. "
              "Dry run leaves them out.")
        f["bundles"] = [b for b in f.get("bundles") or [] if b.get("price") is not None]
    inp = write.Inputs(
        facts=f, fe_link=PLACEHOLDER.format("fe"),
        oto_links=[PLACEHOLDER.format(f"oto{i + 1}") for i in range(len(f.get("otos") or []))],
        bundle_links=[PLACEHOLDER.format(f"bundle{i + 1}") for i in range(len(f.get("bundles") or []))],
        own_bonuses=bonuses, method_note=method_note, authors=None,
        author=os.environ.get("DEFAULT_AUTHOR", "marcus").strip() or "marcus", video_url=vp.url)
    res = write.run(client, inp)
    print(f"\nWRITING: bonuses/method note from {src}; attempts used: {res.attempts} of 3")
    for n in res.notes:
        print(f"  • {n}")
    print(f"PROBLEMS LEFT ({len(res.problems)}):" + ("" if res.problems else " none"))
    for p in res.problems:
        print(f"  ✗ {p}")
    tin, tout = ex.input_tokens + res.input_tokens, ex.output_tokens + res.output_tokens
    print(f"\nAI usage (extract + write): {tin} input / {tout} output tokens")
    if res.review:
        # One line keeps the JSON in order in Railway's log viewer.
        print("\nREVIEW JSON (one line):\n" + json.dumps(res.review, ensure_ascii=False))
    if make_draft and res.review:
        draft_preview(res.review)
    print("\nRESULT: " + ("WRITING OK (would create the draft)" if res.ok else "WRITING HAS PROBLEMS"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
