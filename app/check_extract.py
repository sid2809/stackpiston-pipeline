"""Phase 3 checkpoint: `python -m app.check_extract <JV URL> [sales page URL]`

Reads the JV source (and linked pages), extracts facts with the AI, compares sources,
and prints what it found plus anything that would stop a run. Writes nothing anywhere.
"""
from __future__ import annotations

import os
import sys

from .extract import run, summary, to_json
from .llm import LLMError, get_client


def main() -> int:
    args = sys.argv[1:] or [x for x in (os.environ.get("TEST_JV_URL"), os.environ.get("TEST_SALES_URL")) if x]
    if not args:
        print("FAIL  give a JV URL: python -m app.check_extract <JV URL> [sales page URL]")
        return 1
    try:
        client = get_client()
        res = run(client, args[0], args[1] if len(args) > 1 else None)
    except LLMError as e:
        print(f"FAIL  {e}")
        return 1
    print("SOURCES READ:")
    for s in res.sources:
        print(f"  - {s['kind']:4} {s['chars']:>6} chars  {s['url']}{'  (cut)' if s['truncated'] else ''}")
    if res.facts:
        print("\nFACTS:\n" + summary(res))
    print(f"\nBLOCKING ({len(res.blocking)}):" + ("" if res.blocking else " none"))
    for b in res.blocking:
        print(f"  ✗ {b}")
    print(f"\nNOTES ({len(res.notes)}):" + ("" if res.notes else " none"))
    for n in res.notes:
        print(f"  • {n}")
    print(f"\nAI usage: {res.input_tokens} input / {res.output_tokens} output tokens")
    if res.facts:
        print("\nFULL FACTS JSON:\n" + to_json(res))
    print("\nRESULT: " + ("READY (would continue to writing)" if res.ok else "NEEDS INFO (row would stop here)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
