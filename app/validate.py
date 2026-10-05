"""Review validation.

Three layers:
1. JSON Schema (schema/review-v1.json) with format checking ON.
2. Theme rules: an exact port of stackpiston/inc/validate.php (sp_validate_review),
   producing the same warning text the theme returns as sp_warnings.
3. Link rules: affiliate links must equal the sheet values exactly, and no other
   URL may appear except an explicitly allowed image/video URL.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "review-v1.json"
_SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
_VALIDATOR = Draft202012Validator(_SCHEMA, format_checker=FormatChecker())


# ---------------------------------------------------------------- schema

def schema_errors(review: dict) -> list[str]:
    out = []
    for e in sorted(_VALIDATOR.iter_errors(review), key=lambda e: list(e.absolute_path)):
        path = ".".join(str(p) for p in e.absolute_path) or "(root)"
        out.append(f"{path}: {e.message}")
    return out


# ---------------------------------------------------------------- theme rules (port of validate.php)

def _get(d, path):
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def _is_numeric(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        try:
            float(v.strip())
            return v.strip() != ""
        except ValueError:
            return False
    return False


def _num_str(v) -> str:
    """Render a number the way PHP string interpolation does (17 not 17.0)."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _valid_url(u) -> bool:
    if not isinstance(u, str) or not u:
        return False
    try:
        u.encode("ascii")
    except UnicodeEncodeError:
        return False  # PHP FILTER_VALIDATE_URL rejects non-ASCII
    p = urlparse(u)
    return bool(p.scheme) and bool(p.netloc)


def _valid_date(v) -> bool:
    if not isinstance(v, str):
        return False
    s = v.strip()
    if not s:
        return False
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _ts(v):
    if not isinstance(v, str) or not _valid_date(v):
        return None
    return datetime.fromisoformat(v.strip().replace("Z", "+00:00")).timestamp()


def _v_str(w, label, val, mn, mx, required=False):
    if val is None or val == "":
        if required:
            w.append(f"{label} is missing.")
        return
    if not isinstance(val, str):
        w.append(f"{label} should be text.")
        return
    n = len(val)  # same as PHP mb_strlen for normal text
    if mx and n > mx:
        w.append(f"{label} is {n} characters (max {mx}).")
    elif mn and n < mn:
        w.append(f"{label} is {n} characters (min {mn}).")


def _v_list(w, label, val, mn, mx, required=False) -> bool:
    if val is None:
        if required:
            w.append(f"{label} is missing.")
        return False
    if not isinstance(val, list):
        w.append(f"{label} should be a list.")
        return False
    n = len(val)
    if mx is not None and n > mx:
        w.append(f"{label} has {n} items (max {mx}).")
    elif mn and n < mn:
        w.append(f"{label} has {n} items (min {mn}).")
    return True


def _v_strlist(w, label, val, mn, mx, each, required=False):
    if not _v_list(w, label, val, mn, mx, required):
        return
    for i, s in enumerate(val):
        _v_str(w, f"{label} #{i + 1}", s, 0, each, True)


def _v_num(w, label, val, mn=None, mx=None, required=False):
    if val is None or val == "":
        if required:
            w.append(f"{label} is missing.")
        return
    if not _is_numeric(val):
        w.append(f"{label} should be a number.")
        return
    f = float(val)
    if mn is not None and f < mn:
        w.append(f"{label} is {_num_str(val)} (min {mn}).")
    if mx is not None and f > mx:
        w.append(f"{label} is {_num_str(val)} (max {mx}).")


def _v_date(w, label, val, required=False):
    if val is None or val == "":
        if required:
            w.append(f"{label} is missing.")
        return
    if not _valid_date(val):
        w.append(f"{label} is not a valid date.")


def theme_warnings(d, authors: set[str] | None = None) -> list[str]:
    """Same checks, order and wording as sp_validate_review() in the theme.

    authors: the keys of Appearance -> StackPiston -> Authors. None skips that check.
    """
    w: list[str] = []
    if not isinstance(d, dict) or not d:
        return ["Review data is empty or not valid JSON."]

    _v_str(w, "seo.title", _get(d, "seo.title"), 0, 60, True)
    _v_str(w, "seo.description", _get(d, "seo.description"), 0, 155, True)

    _v_str(w, "product.name", _get(d, "product.name"), 0, 28, True)
    _v_str(w, "product.vendor", _get(d, "product.vendor"), 0, 32, True)
    _v_str(w, "product.niche", _get(d, "product.niche"), 0, 24, True)
    _v_str(w, "product.summary", _get(d, "product.summary"), 200, 400, True)
    _v_num(w, "product.refundDays", _get(d, "product.refundDays"), 0, 365)

    _v_date(w, "launch.cartOpen", _get(d, "launch.cartOpen"), True)
    _v_date(w, "launch.cartClose", _get(d, "launch.cartClose"))
    o, c = _ts(_get(d, "launch.cartOpen")), _ts(_get(d, "launch.cartClose"))
    if o and c and c <= o:
        w.append("launch.cartClose is before launch.cartOpen.")

    _v_str(w, "review.label", _get(d, "review.label"), 0, 20, True)
    _v_str(w, "review.verdict", _get(d, "review.verdict"), 80, 160, True)
    _v_str(w, "review.finalVerdict", _get(d, "review.finalVerdict"), 60, 120, True)
    _v_str(w, "review.methodNote", _get(d, "review.methodNote"), 0, 220)
    _v_str(w, "review.trustBadge", _get(d, "review.trustBadge"), 0, 20)
    _v_str(w, "review.testedNote", _get(d, "review.testedNote"), 0, 20)
    _v_date(w, "review.updatedAt", _get(d, "review.updatedAt"), True)
    _v_num(w, "review.score", _get(d, "review.score"), 0, 10)
    author = _get(d, "review.author")
    if not author:
        w.append("review.author is missing.")
    elif authors is not None and author not in authors:
        w.append(f'review.author "{author}" is not defined in Appearance → StackPiston → Authors '
                 f"(the WP post author will be shown).")
    sb = _get(d, "review.scoreBreakdown")
    if _v_list(w, "review.scoreBreakdown", sb, 4, 6, True):
        for i, b in enumerate(sb):
            b = b if isinstance(b, dict) else {}
            _v_str(w, f"review.scoreBreakdown #{i + 1} label", b.get("label"), 0, 18, True)
            _v_num(w, f"review.scoreBreakdown #{i + 1} score", b.get("score"), 0, 10, True)

    mt = _get(d, "media.type")
    if mt is not None and mt not in ("video", "image"):
        w.append('media.type should be "video" or "image".')
    if mt == "video" and not _get(d, "media.videoUrl"):
        w.append('media.type is "video" but media.videoUrl is empty (section will be hidden).')
    _v_str(w, "media.imageAlt", _get(d, "media.imageAlt"), 0, 120)
    _v_str(w, "media.caption", _get(d, "media.caption"), 0, 100)
    _v_str(w, "media.videoTitle", _get(d, "media.videoTitle"), 0, 40)

    fl = _get(d, "links.frontEnd")
    if not fl:
        w.append("links.frontEnd (affiliate link) is missing — CTAs will not work.")
    elif not _valid_url(fl):
        w.append("links.frontEnd is not a valid URL.")

    _v_str(w, "pricing.frontEnd.name", _get(d, "pricing.frontEnd.name"), 0, 32, True)
    _v_num(w, "pricing.frontEnd.price", _get(d, "pricing.frontEnd.price"), 0, None, True)
    _v_num(w, "pricing.frontEnd.priceAfterLaunch", _get(d, "pricing.frontEnd.priceAfterLaunch"), 0)
    _v_strlist(w, "pricing.frontEnd.items", _get(d, "pricing.frontEnd.items"), 1, 4, 48, True)
    _v_str(w, "pricing.frontEnd.note", _get(d, "pricing.frontEnd.note"), 0, 120, True)

    otos = _get(d, "pricing.otos")
    if _v_list(w, "pricing.otos", otos, 0, 10):
        for i, oto in enumerate(otos):
            oto = oto if isinstance(oto, dict) else {}
            L = f"OTO {i + 1}"
            _v_str(w, f"{L} name", oto.get("name"), 0, 28, True)
            _v_num(w, f"{L} price", oto.get("price"), 0, None, True)
            _v_strlist(w, f"{L} items", oto.get("items"), 2, 4, 48, True)
            if oto.get("verdict", "") not in ("worth_it", "optional", "skip"):
                w.append(f"{L} verdict should be worth_it, optional or skip.")
            _v_str(w, f"{L} note", oto.get("note"), 0, 120, True)
            if oto.get("link") and not _valid_url(oto.get("link")):
                w.append(f"{L} link is not a valid URL.")
            if not oto.get("summary"):
                w.append(f"{L} has no summary — it will not get a detailed review.")
                continue
            _v_str(w, f"{L} summary", oto["summary"], 120, 300, True)
            _v_strlist(w, f"{L} included", oto.get("included"), 3, 8, 70)
            _v_strlist(w, f"{L} pros", oto.get("pros"), 1, 4, 70)
            _v_strlist(w, f"{L} cons", oto.get("cons"), 1, 4, 70)
            _v_num(w, f"{L} score", oto.get("score"), 0, 10)
            _v_str(w, f"{L} takeaway", oto.get("takeaway"), 120, 400, True)
            ds = oto.get("downsell")
            if ds:
                ds = ds if isinstance(ds, dict) else {}
                _v_num(w, f"{L} downsell price", ds.get("price"), 0, None, True)
                _v_str(w, f"{L} downsell note", ds.get("note"), 0, 40)

    f = _get(d, "features")
    if _v_list(w, "features", f, 3, 8, True):
        for i, x in enumerate(f):
            x = x if isinstance(x, dict) else {}
            _v_str(w, f"features #{i + 1} title", x.get("title"), 0, 36, True)
            _v_str(w, f"features #{i + 1} benefit", x.get("benefit"), 0, 140, True)
    _v_strlist(w, "pros", _get(d, "pros"), 3, 6, 70, True)
    _v_strlist(w, "cons", _get(d, "cons"), 2, 5, 70, True)
    _v_strlist(w, "goodFor", _get(d, "goodFor"), 2, 4, 60)
    _v_strlist(w, "notFor", _get(d, "notFor"), 2, 4, 60)

    b = _get(d, "bonuses")
    if _v_list(w, "bonuses", b, 0, 12):
        for i, x in enumerate(b):
            x = x if isinstance(x, dict) else {}
            L = f"bonuses #{i + 1}"
            _v_str(w, f"{L} type", x.get("type"), 0, 14)
            _v_str(w, f"{L} title", x.get("title"), 0, 40, True)
            _v_str(w, f"{L} description", x.get("description"), 0, 140, True)
            _v_num(w, f"{L} value", x.get("value"), 0, None, True)

    q = _get(d, "faq")
    if _v_list(w, "faq", q, 4, 8):
        for i, x in enumerate(q):
            x = x if isinstance(x, dict) else {}
            _v_str(w, f"faq #{i + 1} question", x.get("q"), 0, 80, True)
            _v_str(w, f"faq #{i + 1} answer", x.get("a"), 0, 320, True)
    return w


# ---------------------------------------------------------------- link rules

_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)


def _all_strings(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _all_strings(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _all_strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def link_errors(review: dict, fe_link: str, oto_links: list[str],
                extra_allowed: list[str] | None = None) -> list[str]:
    """Affiliate links must match the sheet exactly; no other URLs allowed."""
    errs = []
    fe_link = (fe_link or "").strip()
    oto_links = [x.strip() for x in (oto_links or []) if x and x.strip()]

    if _get(review, "links.frontEnd") != fe_link:
        errs.append("links.frontEnd does not match the FE affiliate link in the sheet.")
    otos = _get(review, "pricing.otos") or []
    if oto_links and len(otos) != len(oto_links):
        errs.append(f"Review has {len(otos)} OTOs but the sheet has {len(oto_links)} OTO links.")
    for i, oto in enumerate(otos):
        if i < len(oto_links) and isinstance(oto, dict) and oto.get("link") != oto_links[i]:
            errs.append(f"OTO {i + 1} link does not match OTO link #{i + 1} in the sheet.")

    allowed = {fe_link, *oto_links, *[u.strip() for u in (extra_allowed or []) if u]}
    allowed.discard("")
    for path, s in _all_strings(review):
        for url in _URL_RE.findall(s):
            if url.rstrip(".,;") not in allowed and url not in allowed:
                errs.append(f"Unexpected URL in {path}: {url}")
    return errs


# ---------------------------------------------------------------- all together

def validate(review: dict, *, fe_link: str, oto_links: list[str],
             authors: set[str] | None, extra_allowed: list[str] | None = None) -> dict:
    return {
        "schema_errors": schema_errors(review),
        "theme_warnings": theme_warnings(review, authors),
        "link_errors": link_errors(review, fe_link, oto_links, extra_allowed),
    }


def is_clean(result: dict) -> bool:
    return not any(result.values())
