"""Convert launch times as written ("2026-10-05 10:00" + "EST") to UTC, and flag ambiguity.

Rules (build plan):
- An explicit abbreviation is used as written: EST = UTC-5, EDT = UTC-4, etc.
- "EST" (or PST/CST/MST) on a date when the US is on summer time is flagged: vendors often
  write EST but mean Eastern local time. The row goes to Needs info unless Cart open is in the sheet.
- ET / Eastern / Eastern Time = America/New_York local time (DST-aware).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

FIXED = {
    "EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6, "PST": -8, "PDT": -7,
    "UTC": 0, "GMT": 0, "BST": 1, "CET": 1, "CEST": 2, "IST": 5.5, "AEST": 10, "AEDT": 11,
}
REGIONAL = {
    "ET": "America/New_York", "EASTERN": "America/New_York", "EASTERN TIME": "America/New_York",
    "NEW YORK": "America/New_York", "AMERICA/NEW_YORK": "America/New_York",
    "CT": "America/Chicago", "CENTRAL": "America/Chicago",
    "MT": "America/Denver", "MOUNTAIN": "America/Denver",
    "PT": "America/Los_Angeles", "PACIFIC": "America/Los_Angeles",
}
STANDARD_ONLY = {"EST": "America/New_York", "CST": "America/Chicago",
                 "MST": "America/Denver", "PST": "America/Los_Angeles"}


def _parse_local(s: str) -> datetime | None:
    s = (s or "").strip().replace("T", " ").upper().replace("A.M.", "AM").replace("P.M.", "PM")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %I:%M %p", "%Y-%m-%d %I %p", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def to_utc(local: str | None, zone: str | None) -> tuple[str | None, list[str]]:
    """Returns (ISO UTC string like 2026-10-05T15:00:00Z or None, list of problems)."""
    if not local:
        return None, ["no time given"]
    dt = _parse_local(local)
    if dt is None:
        return None, [f"could not read the time '{local}'"]
    z = " ".join((zone or "").upper().replace(".", "").split())
    if not z:
        return None, [f"time '{local}' has no timezone"]
    problems = []
    if z in FIXED:
        aware = dt.replace(tzinfo=timezone(timedelta(hours=FIXED[z])))
        if z in STANDARD_ONLY:
            local_zone = ZoneInfo(STANDARD_ONLY[z])
            if dt.replace(tzinfo=local_zone).dst():
                problems.append(f"'{z}' used on {dt:%Y-%m-%d}, when the US is on summer time "
                                f"({z[0]}DT). It may mean {z[0]}DT, one hour different. "
                                "Fill Cart open in the sheet to confirm.")
    elif z in REGIONAL:
        aware = dt.replace(tzinfo=ZoneInfo(REGIONAL[z]))
    else:
        try:
            aware = dt.replace(tzinfo=ZoneInfo(zone.strip()))
        except Exception:
            return None, [f"unknown timezone '{zone}'"]
    utc = aware.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ"), problems
