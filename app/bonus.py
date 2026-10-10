"""Buyer bonus: AI-written CONTENT (never code) for the theme's bonus page tools.

One extra AI call per new review (or per Rewrite with "Rebuild bonus" ticked). The result is checked with the
same rules as the theme (inc/bonus.php, sp_validate_bonus). A bad bonus never stops the review: the tool is
dropped if only the tool is wrong, otherwise the bonus is skipped with a message.
"""
from __future__ import annotations

import copy
import json
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .llm import LLMError, Truncated

PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "bonus.md").read_text(encoding="utf-8")
META_KEY = "sp_bonus_json"
CODE_RE = re.compile(r"[a-z0-9]{12}")  # always used with fullmatch
_ABC = "abcdefghijkmnpqrstuvwxyz23456789"

FIXER = """You fix the JSON content of a StackPiston buyer bonus. You get the current JSON and a list of problems.
Return the complete corrected JSON object with the same keys ("tool" and "plan"), changing only what is needed.
Output only the JSON object. Plain text only, no money amounts, no URLs, no income claims."""


def new_code() -> str:
    return "".join(secrets.choice(_ABC) for _ in range(12))


# ------------------------------------------------------------------ validation (mirror of the theme's PHP)

_CTRL = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
_MONEY = re.compile(r"[$€£₹¥]\s?\d|\b(?:usd|inr|rs\.?)\s?\d|\d\s?(?:usd|inr|dollars?|bucks|rupees?|euros?|pounds?)\b",
                    re.I)
_PHRASE_EXTRA = set(" '’-.,!?%&/:#@+")
_PHP_TRIM = " \t\n\r\0\x0b"


def _text(e: list, label: str, val, lo: int, hi: int, required: bool = True) -> None:
    if val is None or val == "":
        if required:
            e.append(f"{label} is missing.")
        return
    if not isinstance(val, str):
        e.append(f"{label} should be text.")
        return
    n = len(val)
    if n > hi:
        e.append(f"{label} is {n} characters (max {hi}).")
    elif n < lo:
        e.append(f"{label} is {n} characters (min {lo}).")
    if _CTRL.search(val):
        e.append(f"{label} has hidden control characters.")
    if _MONEY.search(val):
        e.append(f"{label} mentions a money amount (not allowed in bonuses).")


def _list(e: list, label: str, val, lo: int, hi: int) -> bool:
    if not isinstance(val, list):
        e.append(f"{label} should be a list.")
        return False
    n = len(val)
    if n > hi:
        e.append(f"{label} has {n} items (max {hi}).")
        return False
    if n < lo:
        e.append(f"{label} has {n} items (min {lo}).")
        return False
    return True


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _int(e: list, label: str, val, lo: int, hi: int) -> None:
    if not _is_int(val):
        e.append(f"{label} should be a whole number.")
        return
    if val < lo or val > hi:
        e.append(f"{label} is {val} (allowed {lo} to {hi}).")


def _phrase_ok(p: str) -> bool:
    return bool(p) and all(unicodedata.category(ch)[0] in "LN" or ch in _PHRASE_EXTRA for ch in p)


def validate(b) -> list[str]:
    """Problems; an empty list means the theme will show this bonus."""
    e: list[str] = []
    if not isinstance(b, dict) or not b:
        return ["Bonus data is empty or not valid JSON."]
    if not (_is_int(b.get("v")) and b.get("v") == 1):
        e.append("v should be 1.")
    if not (isinstance(b.get("code"), str) and CODE_RE.fullmatch(b["code"])):
        e.append("code should be 12 lowercase letters or digits.")

    p = b.get("plan")
    if not isinstance(p, dict):
        e.append("plan is missing.")
    else:
        _text(e, "plan.title", p.get("title"), 5, 70)
        _text(e, "plan.summary", p.get("summary"), 20, 200)
        _text(e, "plan.intro", p.get("intro"), 20, 400)
        if _list(e, "plan.days", p.get("days"), 7, 7):
            for i, d in enumerate(p["days"]):
                lab = f"plan day {i + 1}"
                if not isinstance(d, dict):
                    e.append(f"{lab} should be an object.")
                    continue
                _text(e, f"{lab} title", d.get("title"), 3, 70)
                if _list(e, f"{lab} tasks", d.get("tasks"), 1, 5):
                    for j, t in enumerate(d["tasks"]):
                        _text(e, f"{lab} task {j + 1}", t, 3, 200)
                _text(e, f"{lab} result", d.get("result"), 5, 200)

    t = b.get("tool")
    if t is not None:
        if not isinstance(t, dict):
            return e + ["tool should be an object or null."]
        engine = t.get("engine", "")
        if engine not in ("scorer", "checker", "checklist"):
            e.append("tool.engine should be scorer, checker or checklist.")
            return e
        _text(e, "tool.title", t.get("title"), 5, 70)
        _text(e, "tool.summary", t.get("summary"), 20, 200)
        _text(e, "tool.intro", t.get("intro"), 20, 400)
        if engine == "scorer":
            max_total = 0
            if _list(e, "tool.questions", t.get("questions"), 3, 10):
                for i, q in enumerate(t["questions"]):
                    lab = f"question {i + 1}"
                    if not isinstance(q, dict):
                        e.append(f"{lab} should be an object.")
                        continue
                    _text(e, f"{lab} text", q.get("q"), 5, 160)
                    if _list(e, f"{lab} options", q.get("options"), 2, 5):
                        best = 0
                        for j, o in enumerate(q["options"]):
                            ol = f"{lab} option {j + 1}"
                            if not isinstance(o, dict):
                                e.append(f"{ol} should be an object.")
                                continue
                            _text(e, f"{ol} label", o.get("label"), 1, 80)
                            _int(e, f"{ol} points", o.get("points"), 0, 10)
                            if _is_int(o.get("points")):
                                best = max(best, o["points"])
                        max_total += best
                if max_total < 1:
                    e.append("tool.questions: at least one answer must score above 0.")
            if _list(e, "tool.bands", t.get("bands"), 2, 4):
                mins = []
                for i, band in enumerate(t["bands"]):
                    lab = f"band {i + 1}"
                    if not isinstance(band, dict):
                        e.append(f"{lab} should be an object.")
                        continue
                    _int(e, f"{lab} min", band.get("min"), 0, 100)
                    if _is_int(band.get("min")):
                        mins.append(band["min"])
                    _text(e, f"{lab} title", band.get("title"), 2, 60)
                    _text(e, f"{lab} advice", band.get("advice"), 10, 400)
                if len(mins) != len(set(mins)):
                    e.append("tool.bands: each band needs a different min.")
                if 0 not in mins:
                    e.append("tool.bands: one band must start at 0.")
        elif engine == "checker":
            _text(e, "tool.placeholder", t.get("placeholder"), 0, 200, required=False)
            _text(e, "tool.cleanMessage", t.get("cleanMessage"), 5, 200)
            if _list(e, "tool.rules", t.get("rules"), 3, 40):
                seen = set()
                for i, r in enumerate(t["rules"]):
                    lab = f"rule {i + 1}"
                    if not isinstance(r, dict):
                        e.append(f"{lab} should be an object.")
                        continue
                    ph = r.get("phrase")
                    _text(e, f"{lab} phrase", ph, 2, 60)
                    if isinstance(ph, str):
                        if not _phrase_ok(ph):
                            e.append(f"{lab} phrase has characters that aren't allowed.")
                        if ph.strip(_PHP_TRIM) != ph or len(ph.strip(_PHP_TRIM)) < 2:
                            e.append(f"{lab} phrase has extra spaces at the start or end.")
                        k = ph.lower()
                        if k in seen:
                            e.append(f"{lab} phrase repeats an earlier rule.")
                        seen.add(k)
                    _text(e, f"{lab} reason", r.get("reason"), 5, 200)
                    _text(e, f"{lab} fix", r.get("fix"), 5, 200)
        else:
            if _list(e, "tool.groups", t.get("groups"), 1, 6):
                total = 0
                for i, g in enumerate(t["groups"]):
                    lab = f"group {i + 1}"
                    if not isinstance(g, dict):
                        e.append(f"{lab} should be an object.")
                        continue
                    _text(e, f"{lab} title", g.get("title"), 2, 60)
                    if _list(e, f"{lab} items", g.get("items"), 2, 10):
                        for j, it in enumerate(g["items"]):
                            il = f"{lab} item {j + 1}"
                            if not isinstance(it, dict):
                                e.append(f"{il} should be an object.")
                                continue
                            _text(e, f"{il} text", it.get("text"), 3, 160)
                            _text(e, f"{il} why", it.get("why"), 0, 240, required=False)
                            total += 1
                if total > 40:
                    e.append(f"tool.groups have {total} items in total (max 40).")
    return e


# ------------------------------------------------------------------ generation

@dataclass
class BonusResult:
    bonus: dict | None = None
    notes: list = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    attempts: int = 0


def _as_list(x) -> list:
    return x if isinstance(x, list) else []


_PRICE = re.compile(r"\s*\(?(?:[$€£₹¥]\s?|\b(?:usd|inr|rs\.?)\s?)[\d,.]+(?:\s?(?:usd|inr))?(?:\s?/\s?\w+)?(?:\s+value)?\)?"
                    r"|\s*\b[\d,.]+\s?(?:usd|inr|dollars?|bucks|rupees?|euros?)\b", re.I)


def _strip_money(s):
    """Prices are removed from what the AI reads, so it has nothing to copy (bonuses never mention money)."""
    return _PRICE.sub("", s).strip() if isinstance(s, str) else s


def brief(review: dict) -> dict:
    """What the AI sees: the finished review's product facts, without prices (bonuses never mention money)."""
    product = review.get("product") or {}
    pricing = review.get("pricing") or {}
    fe = pricing.get("frontEnd") or {}
    extras = []
    for kind, key in (("OTO", "otos"), ("Bundle", "bundles")):
        for i, o in enumerate(_as_list(pricing.get(key))):
            if isinstance(o, dict):
                extras.append({"offer": f"{kind} {i + 1}: {_strip_money(o.get('name', ''))}",
                               "contents": [_strip_money(x) for x in _as_list(o.get("items") or o.get("included"))]})
    return {
        "product": product.get("name", ""),
        "niche": product.get("niche", ""),
        "summary": _strip_money(product.get("summary", "")),
        "frontEndIncludes": [_strip_money(x) for x in _as_list(fe.get("items"))],
        "features": [{"title": _strip_money(f.get("title", "")), "benefit": _strip_money(f.get("benefit", ""))}
                     for f in _as_list(review.get("features")) if isinstance(f, dict)],
        "pros": [_strip_money(x) for x in _as_list(review.get("pros"))],
        "cons": [_strip_money(x) for x in _as_list(review.get("cons"))],
        "goodFor": [_strip_money(x) for x in _as_list(review.get("goodFor"))],
        "notFor": [_strip_money(x) for x in _as_list(review.get("notFor"))],
        "OTO and bundle contents (NOT available to front-end buyers)": extras,
    }


def _pick(d, keys, nested=None):
    """Only the keys the theme knows; anything else the AI adds is dropped (kept shapes are checked by validate)."""
    if not isinstance(d, dict):
        return d
    out = {k: d[k] for k in keys if k in d}
    for k, sub_keys in (nested or {}).items():
        if isinstance(out.get(k), list):
            out[k] = [sub_keys(x) for x in out[k]]
    return out


_ENGINE_KEYS = {
    "scorer": (("questions", "bands"), {
        "questions": lambda q: _pick(q, ("q", "options"), {"options": lambda o: _pick(o, ("label", "points"))}),
        "bands": lambda b: _pick(b, ("min", "title", "advice"))}),
    "checker": (("placeholder", "cleanMessage", "rules"), {"rules": lambda r: _pick(r, ("phrase", "reason", "fix"))}),
    "checklist": (("groups",), {"groups": lambda g: _pick(g, ("title", "items"), {"items": lambda i: _pick(i, ("text", "why"))})}),
}


def _clean_tool(t):
    if isinstance(t, dict) and "engine" not in t and isinstance(t.get("tool"), dict):
        t = t["tool"]  # the AI wrapped the tool in another "tool": unwrap it
    if not isinstance(t, dict):
        return t
    keys, nested = _ENGINE_KEYS.get(t.get("engine"), ((), {}))
    return _pick(t, ("engine", "title", "summary", "intro") + keys, nested)


def _assemble(data: dict, code: str) -> dict:
    data = data if isinstance(data, dict) else {}
    plan = _pick(data.get("plan"), ("title", "summary", "intro", "days"),
                 {"days": lambda d: _pick(d, ("title", "tasks", "result"))})
    return {"v": 1, "code": code, "tool": _clean_tool(data.get("tool")), "plan": plan}


def generate(client, review: dict, code: str | None = None, max_fixes: int = 1) -> BonusResult:
    """Never raises for AI or content problems: returns bonus=None with a note instead."""
    res = BonusResult()
    code = code if isinstance(code, str) and CODE_RE.fullmatch(code) else new_code()
    user = "REVIEW (JSON):\n" + json.dumps(brief(review), ensure_ascii=False, indent=1)
    r = None
    for attempt in range(2):
        try:
            r = client.complete_json(PROMPT, user, max_tokens=8000)
            break
        except LLMError as e:
            unusable = isinstance(e, Truncated) or "JSON" in str(e)  # a bad reply, worth one more try
            if attempt == 0 and unusable:
                continue
            res.notes.append(f"BONUS skipped: the AI step failed ({e}). The review is fine; tick Rebuild bonus "
                             "(without Rewrite) to try only the bonus again.")
            return res
    res.input_tokens += r.input_tokens
    res.output_tokens += r.output_tokens
    data = r.data
    for attempt in range(max_fixes + 1):
        res.attempts = attempt + 1
        bonus = _assemble(data, code)
        problems = validate(bonus)
        if not problems:
            res.bonus = bonus
            return res
        if attempt == max_fixes:
            break
        fix = ("PROBLEMS:\n- " + "\n- ".join(problems[:40]) + "\n\nCURRENT JSON:\n" +
               json.dumps({"tool": bonus["tool"], "plan": bonus["plan"]}, ensure_ascii=False))
        try:
            r = client.complete_json(FIXER, fix, max_tokens=8000)
        except LLMError as e:
            res.notes.append(f"Bonus fixer failed ({e}).")
            break
        res.input_tokens += r.input_tokens
        res.output_tokens += r.output_tokens
        data = copy.deepcopy(r.data)
    # Still wrong: keep the 7-day plan if only the tool is the problem.
    bonus = _assemble(data, code)
    if bonus.get("tool") is not None:
        no_tool = dict(bonus, tool=None)
        if not validate(no_tool):
            res.bonus = no_tool
            res.notes.append("BONUS: the gap-fixer tool was dropped (it broke the rules: "
                             + "; ".join(validate(bonus)[:3]) + "). The 7-day plan and toolkit are kept.")
            return res
    res.notes.append(f"BONUS skipped: {len(problems)} problem(s), e.g. " + "; ".join(problems[:3])
                     + ". The review is fine; tick Rebuild bonus (without Rewrite) to try only the bonus again.")
    return res


def existing(post: dict) -> tuple[dict | None, bool]:
    """The bonus already saved on a post: (data, valid). Valid means it passes the rules."""
    raw = (post.get("meta") or {}).get(META_KEY) or ""
    try:
        d = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        d = None
    if not isinstance(d, dict):
        return None, False
    return d, not validate(d)
