"""Pipeline entry point (Railway start command).

  python -m app.main poll           process every row with Status = Run (the scheduled job)
  python -m app.main run --row 5    process sheet row 5 now, whatever its status
  python -m app.main refresh        sync Status from WordPress, switch prices after launch (no AI)
  python -m app.main auto           refresh, then poll: the ONE start command for the scheduled service
"""
from __future__ import annotations

import argparse
import sys

from .config import ConfigError
from .llm import LLMError
from .safety import scrub


def _say(line: str) -> None:
    print(scrub(line))


def _counts(c: dict, empty: str) -> str:
    return ", ".join(f"{n} {s}" for s, n in sorted(c.items())) if c else empty


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.main")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("poll")
    r = sub.add_parser("run")
    r.add_argument("--row", type=int, required=True)
    sub.add_parser("refresh")
    sub.add_parser("auto")
    args = ap.parse_args(argv)
    if args.cmd == "auto":
        return auto()
    if args.cmd == "refresh":
        try:
            from .patch import refresh
            from .runner import build_context
            counts = refresh(build_context(need_ai=False))
        except Exception as e:
            _say(f"FAIL  {e if isinstance(e, ConfigError) else e.__class__.__name__ + ': ' + str(e)[:300]}")
            return 1
        _say("OK  refresh: " + _counts(counts, "nothing to change"))
        return 0
    try:
        from .runner import build_context, poll
        ctx = build_context()
        counts = poll(ctx, only_row=args.row if args.cmd == "run" else None)
    except (ConfigError, LLMError) as e:
        _say(f"FAIL  {e}")
        return 1
    except Exception as e:
        _say(f"FAIL  {e.__class__.__name__}: {str(e)[:300]}")
        return 1
    _say("OK  " + _counts(counts, "nothing to do (no rows with Status = Run)"))
    return 0


def auto() -> int:
    """Scheduled run: refresh (no AI, $0) then poll.
    - A refresh problem never stops the poll.
    - If the AI settings are broken, the refresh still runs; only the poll reports the problem."""
    try:
        from .llm import get_client
        from .patch import refresh
        from .runner import build_context, poll
        ctx = build_context(need_ai=False)
    except Exception as e:  # WordPress or Sheet settings missing/broken: nothing can run
        _say(f"FAIL  {e if isinstance(e, ConfigError) else e.__class__.__name__ + ': ' + str(e)[:300]}")
        return 1
    ai_problem = ""
    try:
        ctx.client = get_client()
    except (ConfigError, LLMError) as e:
        ai_problem = str(e)
    try:
        _say("OK  refresh: " + _counts(refresh(ctx), "nothing to change"))
    except Exception as e:  # keep going: new launches matter more
        _say(f"FAIL  refresh stopped ({e.__class__.__name__}: {str(e)[:200]}); continuing with poll")
    if ai_problem:
        _say(f"FAIL  poll skipped: {ai_problem}")
        return 1
    try:
        counts = poll(ctx)
    except Exception as e:
        _say(f"FAIL  poll stopped ({e if isinstance(e, (ConfigError, LLMError)) else e.__class__.__name__ + ': ' + str(e)[:300]})")
        return 1
    _say("OK  poll: " + _counts(counts, "nothing to do (no rows with Status = Run)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
