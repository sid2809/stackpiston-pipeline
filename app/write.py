"""Phase 4: turn checked facts into a complete, validated review JSON.

Split of work (so the AI can't change facts):
- The AI writes only text ("copy"): summaries, verdicts, items, FAQ...
- Code fills everything factual: names from FACTS, prices, dates, links, coupons, bonuses,
  author, testing fields, method note, score label.
- The validator (schema + exact theme rules + link rules + price-mention check) runs on the
  assembled review. Problems go back to the AI to fix, at most 2 times.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .llm import LLMError
from .validate import validate

ROOT = Path(__file__).resolve().parent.parent
WRITER = (ROOT / "prompts" / "writer.md").read_text(encoding="utf-8")
FIXER = """You fix the TEXT of a StackPiston review. You get the current text JSON and a list of problems.
Change only what the problems mention. Keep the meaning: shorten or expand wording, never change facts.
Problems use page field names; they map to text keys like this: seo.title -> seoTitle, seo.description ->
seoDescription, product.summary -> summary, review.verdict -> verdict, review.finalVerdict -> finalVerdict,
product.niche -> niche, "OTO 2 summary" -> otos[position 2].summary, "Bundle 1 note" -> bundles[position 1].note,
pricing.frontEnd.* -> frontEnd.*, "faq #3 answer" -> faq[3].a, vendorBonuses #1 -> vendorBonuses[1].
Return the complete corrected text JSON with the same keys, and nothing else."""
NICHES = ["AI Video", "Traffic Software", "Agency Kit", "AI Content", "Email Marketing", "Make Money Training"]
NY = ZoneInfo("America/New_York")


@dataclass
class Inputs:
    facts: dict
    fe_link: str
    oto_links: list[str]
    bundle_links: list[str] = field(default_factory=list)
    own_bonuses: list[dict] = field(default_factory=list)
    method_note: str = ""
    testing_notes: str = ""
    days_tested: str = ""
    video_url: str = ""
    author: str = "marcus"
    authors: set[str] | None = None
    slug: str | None = None
    published_at: str | None = None


@dataclass
class WriteResult:
    review: dict | None = None
    problems: list[str] = field(default_factory=list)  # still unresolved after fixing
    notes: list[str] = field(default_factory=list)
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def ok(self) -> bool:
        return self.review is not None and not self.problems


# ------------------------------------------------------------------ helpers

def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "review"


def _money(v) -> str:
    f = float(v)
    return f"{f:.2f}".rstrip("0").rstrip(".")


def _price_type(t) -> str:
    return {"monthly": "/month", "yearly": "/year"}.get((t or "").lower(), "one-time")


def _avg_label(breakdown: list) -> str:
    scores = [float(b.get("score", 0)) for b in breakdown if isinstance(b, dict)]
    avg = sum(scores) / len(scores) if scores else 0
    if avg >= 9:
        return "Exceptional"
    if avg >= 8:
        return "Recommended"
    if avg >= 7:
        return "Good, with caveats"
    return "Not recommended"


def _coupon_for(facts: dict, target: str, product: str):
    """Pick the coupon whose 'appliesTo' names the target (front end or a bundle name)."""
    t = re.sub(r"[^a-z0-9]", "", target.lower())
    p = re.sub(r"[^a-z0-9]", "", (product or "").lower())
    for c in facts.get("coupons") or []:
        a = re.sub(r"[^a-z0-9]", "", str(c.get("appliesTo") or "").lower())
        if not c.get("code") or len(str(c["code"])) > 24:
            continue
        if target == "front end" and ("frontend" in a or a in ("fe", p) or (p and a == p)):
            return {"code": str(c["code"]), "discount": str(c.get("discount") or "")[:40] or "Discount"}
        if target != "front end" and a and (a == t or a.replace(p, "") == t.replace(p, "")):
            return {"code": str(c["code"]), "discount": str(c.get("discount") or "")[:40] or "Discount"}
    return None


# ------------------------------------------------------------------ assembly

def assemble(copy_: dict, inp: Inputs, today: str | None = None) -> tuple[dict, list[str], list[str]]:
    """Copy + facts + sheet inputs -> review JSON. Returns (review, structural problems, notes)."""
    f, c = inp.facts, copy_ if isinstance(copy_, dict) else {}
    problems, notes = [], []
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    product_name = (f.get("productName") or "").strip()
    if len(product_name) > 28 and c.get("productName"):
        product_name = str(c["productName"])
    fe = f.get("frontEnd") or {}

    otos_f, otos_c = f.get("otos") or [], [o for o in (c.get("otos") or []) if isinstance(o, dict)]
    if len(otos_c) != len(otos_f):
        problems.append(f"Text has {len(otos_c)} OTOs but FACTS has {len(otos_f)}; write exactly {len(otos_f)} in order.")
    vb_f = (f.get("vendorBonuses") or [])[:12]
    vb_c = [x for x in (c.get("vendorBonuses") or []) if isinstance(x, dict)]
    if vb_f and len(vb_c) != len(vb_f):
        problems.append(f"Text has {len(vb_c)} vendor bonuses but FACTS has {len(vb_f)}; write exactly {len(vb_f)}.")

    breakdown = [b for b in (c.get("scoreBreakdown") or []) if isinstance(b, dict)]
    cart_open, cart_close = f.get("cartOpenUtc"), f.get("cartCloseUtc")
    tz_label = ""
    if cart_open:
        tz_label = datetime.fromisoformat(cart_open.replace("Z", "+00:00")).astimezone(NY).tzname() or ""

    review = {
        "schemaVersion": 1,
        "slug": inp.slug or slugify(product_name),
        "seo": {"title": c.get("seoTitle", ""), "description": c.get("seoDescription", "")},
        "product": {"name": product_name, "vendor": (f.get("vendor") or "")[:32], "niche": c.get("niche", ""),
                    "summary": c.get("summary", ""), "platform": f.get("platform") or "JVZoo"},
        "launch": {"cartOpen": cart_open or "", "cartClose": cart_close or "",
                   "timeZone": "America/New_York", "timezoneLabel": tz_label},
        "review": {"label": _avg_label(breakdown), "verdict": c.get("verdict", ""),
                   "finalVerdict": c.get("finalVerdict", ""), "methodNote": inp.method_note[:220],
                   "trustBadge": "Bought & tested",
                   "testedNote": f"tested {inp.days_tested} days" if str(inp.days_tested).strip() else "tested 7 days",
                   "publishedAt": inp.published_at or today, "updatedAt": today,
                   "author": inp.author, "scoreBreakdown": breakdown},
        "links": {"frontEnd": inp.fe_link},
        "pricing": {"frontEnd": {
            "name": (c.get("frontEnd") or {}).get("name") or (fe.get("name") or product_name)[:32],
            "price": float(fe.get("price") or 0),
            "priceType": _price_type(fe.get("priceType")),
            "items": (c.get("frontEnd") or {}).get("items") or [],
            "note": (c.get("frontEnd") or {}).get("note", "")}},
        "features": c.get("features") or [], "pros": c.get("pros") or [], "cons": c.get("cons") or [],
        "goodFor": c.get("goodFor") or [], "notFor": c.get("notFor") or [],
        "bonuses": inp.own_bonuses,
        "faq": c.get("faq") or [],
    }
    if f.get("refundDays") is not None:
        review["product"]["refundDays"] = int(f["refundDays"])
    pal = fe.get("priceAfterLaunch")
    if pal is not None and float(pal) > float(fe.get("price") or 0):
        review["pricing"]["frontEnd"]["priceAfterLaunch"] = float(pal)
    fc = _coupon_for(f, "front end", product_name)
    if fc:
        review["pricing"]["frontEnd"]["coupon"] = fc

    if inp.video_url:
        review["media"] = {"type": "video", "videoUrl": inp.video_url, "videoTitle": c.get("videoTitle", ""),
                           "caption": c.get("caption", "")}
    else:
        review["media"] = {"type": "image", "imageUrl": "", "imageAlt": c.get("imageAlt", ""),
                           "caption": c.get("caption", "")}

    otos = []
    for i, of in enumerate(otos_f):
        oc = otos_c[i] if i < len(otos_c) else {}
        o = {"name": oc.get("name") or (of.get("name") or f"OTO {i + 1}")[:28],
             "price": float(of.get("price") or 0), "items": oc.get("items") or [],
             "verdict": oc.get("verdict", "optional"), "note": oc.get("note", ""),
             "link": inp.oto_links[i] if i < len(inp.oto_links) else "",
             "summary": oc.get("summary", ""), "included": oc.get("included") or [],
             "pros": oc.get("pros") or [], "cons": oc.get("cons") or [], "takeaway": oc.get("takeaway", "")}
        if oc.get("score") not in (None, ""):
            o["score"] = float(oc["score"])
        ds = of.get("downsell") or {}
        if ds.get("price") is not None:
            o["downsell"] = {"name": (ds.get("name") or "Lite version")[:28], "price": float(ds["price"]),
                             "note": (oc.get("downsellNote") or "")[:40]}
        otos.append(o)
    review["pricing"]["otos"] = otos

    bundles_f = f.get("bundles") or []
    if bundles_f and not inp.bundle_links:
        notes.append(f"{len(bundles_f)} bundle(s) found but the sheet has no Bundle links; bundles left out.")
    elif bundles_f:
        if len(inp.bundle_links) != len(bundles_f):
            problems.append(f"FACTS has {len(bundles_f)} bundles but the sheet has {len(inp.bundle_links)} "
                            "Bundle links (needs info, not a text problem).")
        bc = [b for b in (c.get("bundles") or []) if isinstance(b, dict)]
        if len(bc) != len(bundles_f):
            problems.append(f"Text has {len(bc)} bundles but FACTS has {len(bundles_f)}; write exactly {len(bundles_f)}.")
        out = []
        for i, bf in enumerate(bundles_f):
            if bf.get("price") is None:
                problems.append(f"Bundle '{bf.get('name')}' has no price in FACTS (needs info, not a text problem).")
                continue
            b = bc[i] if i < len(bc) else {}
            item = {"name": b.get("name") or str(bf.get("name"))[:32], "price": float(bf["price"]),
                    "items": b.get("items") or [], "verdict": b.get("verdict", "optional"), "note": b.get("note", ""),
                    "link": inp.bundle_links[i] if i < len(inp.bundle_links) else ""}
            bcpn = _coupon_for(f, str(bf.get("name") or ""), product_name)
            if bcpn:
                item["coupon"] = bcpn
            out.append(item)
        review["pricing"]["bundles"] = out

    if vb_f:
        vbs = []
        for i, vf in enumerate(vb_f):
            vc = vb_c[i] if i < len(vb_c) else {}
            x = {"title": vc.get("title") or str(vf.get("title") or "")[:40]}
            if vc.get("description"):
                x["description"] = vc["description"]
            if vf.get("value") is not None:
                x["value"] = float(vf["value"])
            vbs.append(x)
        review["vendorBonuses"] = vbs
    return review, problems, notes


# ------------------------------------------------------------------ price-mention check

_PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")


def allowed_amounts(facts: dict, own_bonuses: list[dict]) -> set[str]:
    nums = set()
    fe = facts.get("frontEnd") or {}

    def add(v):
        if v is not None:
            try:
                nums.add(_money(v))
            except (TypeError, ValueError):
                pass
    add(fe.get("price"))
    add(fe.get("priceAfterLaunch"))
    total = float(fe.get("price") or 0)
    for o in facts.get("otos") or []:
        add(o.get("price"))
        total += float(o.get("price") or 0)
        add((o.get("downsell") or {}).get("price"))
    add(total)
    for b in facts.get("bundles") or []:
        add(b.get("price"))
    for x in (facts.get("vendorBonuses") or []) + (own_bonuses or []):
        add(x.get("value"))
    add(sum(float(b.get("value") or 0) for b in own_bonuses or []))
    for cpn in facts.get("coupons") or []:
        for m in _PRICE_RE.findall(str(cpn.get("discount") or "")):
            add(m.replace(",", ""))
    return nums


def price_mentions(review: dict, allowed: set[str]) -> list[str]:
    out = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str):
            for m in _PRICE_RE.findall(node):
                if _money(m.replace(",", "")) not in allowed:
                    out.append(f"{path} mentions ${m}, which is not a price in FACTS. Remove or correct it.")
    walk({k: v for k, v in review.items() if k not in ("pricing", "bonuses", "vendorBonuses")}, "")
    for i, o in enumerate((review.get("pricing") or {}).get("otos") or []):
        walk({k: o.get(k) for k in ("note", "summary", "takeaway", "items", "included", "pros", "cons")},
             f"OTO {i + 1}")
    for i, b in enumerate((review.get("pricing") or {}).get("bundles") or []):
        walk({k: b.get(k) for k in ("note", "items")}, f"Bundle {i + 1}")
    walk({"note": (review.get("pricing") or {}).get("frontEnd", {}).get("note")}, "pricing.frontEnd")
    for i, v in enumerate(review.get("vendorBonuses") or []):
        walk({k: v.get(k) for k in ("title", "description")}, f"vendorBonuses #{i + 1}")
    return out


# ------------------------------------------------------------------ run

def _check(review: dict, inp: Inputs) -> list[str]:
    extra = [inp.video_url] if inp.video_url else []
    res = validate(review, fe_link=inp.fe_link, oto_links=inp.oto_links, authors=inp.authors,
                   extra_allowed=extra, bundle_links=inp.bundle_links)
    probs = res["schema_errors"] + res["theme_warnings"] + res["link_errors"]
    probs += price_mentions(review, allowed_amounts(inp.facts, inp.own_bonuses))
    return probs


def _user_message(inp: Inputs) -> str:
    f = {k: v for k, v in inp.facts.items() if k not in ("internalConflicts", "launch")}
    counts = (f"COUNTS: otos={len(inp.facts.get('otos') or [])}, "
              f"bundles={len(inp.facts.get('bundles') or []) if inp.bundle_links else 0}, "
              f"vendorBonuses={len((inp.facts.get('vendorBonuses') or [])[:12])}")
    if not inp.bundle_links:
        f = dict(f, bundles=[])
    return (f"FACTS:\n{json.dumps(f, ensure_ascii=False, indent=1)}\n\n{counts}\n\n"
            f"SETTINGS: site StackPiston; our bonuses: {len(inp.own_bonuses)} "
            f"({', '.join(b.get('title', '') for b in inp.own_bonuses)}), delivered in the JVZoo purchase area.\n\n"
            f"NICHES: {json.dumps(NICHES)}\n\n"
            f"TESTING NOTES (optional, from a human tester):\n{inp.testing_notes or '(none)'}")


def run(client, inp: Inputs, max_fixes: int = 2, today: str | None = None) -> WriteResult:
    res = WriteResult()
    try:
        r = client.complete_json(WRITER, _user_message(inp), max_tokens=12000)
    except LLMError as e:
        res.problems = [f"Writer failed: {e}"]
        return res
    res.input_tokens += r.input_tokens
    res.output_tokens += r.output_tokens
    copy_ = r.data
    for attempt in range(max_fixes + 1):
        res.attempts = attempt + 1
        review, structural, notes = assemble(copy_, inp, today)
        res.notes = notes
        problems = structural + _check(review, inp)
        if not problems:
            res.review, res.problems = review, []
            return res
        needs_info = [p for p in problems if "needs info" in p]
        if needs_info or attempt == max_fixes:
            res.review, res.problems = review, problems
            return res
        fix_msg = ("PROBLEMS:\n- " + "\n- ".join(problems) +
                   f"\n\nCURRENT TEXT JSON:\n{json.dumps(copy_, ensure_ascii=False)}\n\n"
                   f"FACTS (for reference, do not change facts):\n"
                   f"{json.dumps({k: inp.facts.get(k) for k in ('productName', 'frontEnd', 'otos', 'bundles', 'coupons', 'vendorBonuses', 'refundDays')}, ensure_ascii=False)}")
        try:
            r = client.complete_json(FIXER, fix_msg, max_tokens=12000)
        except LLMError as e:
            res.review, res.problems = review, problems + [f"Fixer failed: {e}"]
            return res
        res.input_tokens += r.input_tokens
        res.output_tokens += r.output_tokens
        copy_ = copy.deepcopy(r.data)
    return res
