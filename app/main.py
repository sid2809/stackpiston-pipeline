"""Pipeline entry point (Railway start command).

  python -m app.main poll           process every row with Status = Run (the scheduled job)
  python -m app.main run --row 5    process sheet row 5 now, whatever its status
"""
from __future__ import annotations

import argparse
import sys

from .config import ConfigError
from .llm import LLMError


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.main")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("poll")
    r = sub.add_parser("run")
    r.add_argument("--row", type=int, required=True)
    args = ap.parse_args(argv)
    try:
        from .runner import build_context, poll
        ctx = build_context()
        counts = poll(ctx, only_row=args.row if args.cmd == "run" else None)
    except (ConfigError, LLMError) as e:
        print(f"FAIL  {e}")
        return 1
    if not counts:
        print("OK  nothing to do (no rows with Status = Run)")
    else:
        print("OK  " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
