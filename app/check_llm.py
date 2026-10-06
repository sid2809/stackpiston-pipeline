"""Phase 0 checkpoint: `python -m app.check_llm` — does the AI key + model work and return JSON?"""
from __future__ import annotations

import sys
import time

from .llm import LLMError, get_client


def main() -> int:
    try:
        client = get_client()
        t = time.time()
        r = client.complete_json(
            "You reply with one JSON object only. No prose, no code fences.",
            'Return exactly: {"ok": true, "greeting": "StackPiston pipeline connected"}',
            max_tokens=200)
    except LLMError as e:
        print(f"FAIL  {e}")
        return 1
    print(f"OK  model answered: {r.model} in {time.time() - t:.1f}s "
          f"({r.input_tokens} in / {r.output_tokens} out tokens)")
    if r.data.get("ok") is not True:
        print(f"FAIL  unexpected reply: {r.text[:200]}")
        return 1
    print(f"OK  JSON reply parsed: {r.data}")
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
