"""Phase 4 checkpoint (dry run): `python -m app.check_write <JV URL> [sales URL]`

Extracts facts, then writes and validates a full review with placeholder affiliate links.
Uses your real Bonuses and Method notes tabs if the sheet settings exist. Writes nothing to
WordPress or the sheet. Continues past extraction problems so the writing can be judged,
but prints them: a real run would stop there.

Optional: CART_OPEN_UTC=2026-10-05T15:00:00Z (pretend Cart open is filled in the sheet).
"""
from __future__ import annotations

import json
import os
import sys

from . import extract, write
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


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("FAIL  give a JV URL: python -m app.check_write <JV URL> [sales page URL]")
        return 1
    try:
        client = get_client()
        ex = extract.run(client, args[0], args[1] if len(args) > 1 else None,
                         cart_open_override_utc=os.environ.get("CART_OPEN_UTC") or None)
    except LLMError as e:
        print(f"FAIL  {e}")
        return 1
    print("EXTRACTION:\n" + extract.summary(ex) if ex.facts else "EXTRACTION: no facts")
    for b in ex.blocking:
        print(f"  ✗ (real run would stop here) {b}")
    if not ex.facts.get("productName"):
        print("FAIL  no facts to write from.")
        return 1
    bonuses, method_note, src = sheet_extras()
    f = ex.facts
    inp = write.Inputs(
        facts=f, fe_link=PLACEHOLDER.format("fe"),
        oto_links=[PLACEHOLDER.format(f"oto{i + 1}") for i in range(len(f.get("otos") or []))],
        bundle_links=[PLACEHOLDER.format(f"bundle{i + 1}") for i in range(len(f.get("bundles") or []))],
        own_bonuses=bonuses, method_note=method_note, authors=None)
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
        print("\nREVIEW JSON:\n" + json.dumps(res.review, ensure_ascii=False, indent=2))
    print("\nRESULT: " + ("WRITING OK (would create the draft)" if res.ok else "WRITING HAS PROBLEMS"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
