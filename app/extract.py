"""Phase 3: read the JV sources and turn them into one checked set of facts.

Each source is extracted separately, then compared in code. That catches contradictions
(different OTO count, order or prices, different launch times) instead of trusting the AI
to notice them. The URL in the sheet is the main source; linked pages only fill gaps.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .fetch import FetchError, Source, fetch, pick_follow_links
from .timeparse import to_utc

PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "extraction.md").read_text(encoding="utf-8")


@dataclass
class ExtractResult:
    facts: dict
    blocking: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def ok(self) -> bool:
        return not self.blocking


# ------------------------------------------------------------------ AI step

def _obj(v) -> dict:
    return v if isinstance(v, dict) else {}


def _objs(v) -> list[dict]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _strs(v) -> list[str]:
    return [str(x) for x in v if isinstance(x, (str, int, float)) and str(x).strip()] if isinstance(v, list) else []


def clean_facts(d) -> dict:
    """Force the AI's reply into the expected shape so sloppy output can't crash later steps."""
    d = _obj(d)
    out = {k: d.get(k) for k in ("productName", "vendor", "niche", "whatItDoes", "platform",
                                 "refundDays", "salesPageUrl")}
    for k in ("productName", "vendor", "niche", "whatItDoes", "platform", "salesPageUrl"):
        if out[k] is not None and not isinstance(out[k], str):
            out[k] = str(out[k])
    out["refundDays"] = _num(out["refundDays"])
    launch = _obj(d.get("launch"))
    out["launch"] = {k: {x: (str(_obj(launch.get(k)).get(x)) if _obj(launch.get(k)).get(x) is not None else None)
                         for x in ("local", "zone", "raw")} for k in ("cartOpen", "cartClose")}
    fe = _obj(d.get("frontEnd"))
    out["frontEnd"] = {"name": fe.get("name"), "price": _num(fe.get("price")),
                       "priceAfterLaunch": _num(fe.get("priceAfterLaunch")),
                       "priceType": fe.get("priceType"), "items": _strs(fe.get("items"))}
    otos = []
    for i, o in enumerate(_objs(d.get("otos")), 1):
        ds = _obj(o.get("downsell"))
        otos.append({"position": i, "name": str(o.get("name") or f"OTO {i}"), "price": _num(o.get("price")),
                     "priceType": o.get("priceType"), "items": _strs(o.get("items")),
                     "description": o.get("description"),
                     "downsell": ({"name": ds.get("name"), "price": _num(ds.get("price")),
                                   "description": ds.get("description")} if ds else None)})
    out["otos"] = otos
    out["bundles"] = [{"name": str(b.get("name") or "Bundle"), "price": _num(b.get("price")),
                       "includes": _strs(b.get("includes"))} for b in _objs(d.get("bundles"))]
    out["coupons"] = [{"code": str(c.get("code") or ""), "discount": str(c.get("discount") or ""),
                       "appliesTo": c.get("appliesTo")} for c in _objs(d.get("coupons"))]
    out["vendorBonuses"] = [{"title": str(b.get("title") or ""), "description": b.get("description"),
                             "value": _num(b.get("value"))} for b in _objs(d.get("vendorBonuses")) if b.get("title")]
    out["features"] = [{"title": str(f.get("title") or ""), "detail": str(f.get("detail") or "")}
                       for f in _objs(d.get("features")) if f.get("title")]
    for k in ("goodFor", "limitations", "internalConflicts", "unknowns"):
        out[k] = _strs(d.get(k))
    return out


def extract_source(client, src: Source, role: str) -> tuple[dict, int, int]:
    user = (f"Source role: {role}\nSource URL: {src.url}\n"
            f"{'(Text was cut at 60,000 characters.)' if src.truncated else ''}\n\n"
            f"--- SOURCE TEXT START ---\n{src.text}\n--- SOURCE TEXT END ---")
    r = client.complete_json(PROMPT, user, max_tokens=8000)
    return clean_facts(r.data), r.input_tokens, r.output_tokens


# ------------------------------------------------------------------ comparing

def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def _same_name(a, b) -> bool:
    na, nb = _norm(a), _norm(b)
    return bool(na) and bool(nb) and (na in nb or nb in na)


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str):
        v = v.replace("$", "").replace(",", "").strip()
        if not v:
            return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _time_utc(facts: dict, key: str):
    t = (facts.get("launch") or {}).get(key) or {}
    if not t.get("local"):
        return None
    utc, _ = to_utc(t.get("local"), t.get("zone"))
    return utc


def compare(main: dict, other: dict, label: str) -> list[str]:
    out = []
    a, b = _num((main.get("frontEnd") or {}).get("price")), _num((other.get("frontEnd") or {}).get("price"))
    if a is not None and b is not None and a != b:
        out.append(f"Front-end price: main source says ${a:g}, {label} says ${b:g}.")

    mo, oo = main.get("otos") or [], other.get("otos") or []
    if mo and oo:
        if len(mo) != len(oo):
            out.append(f"OTO count: main source lists {len(mo)}, {label} lists {len(oo)}.")
        for i, (x, y) in enumerate(zip(mo, oo), 1):
            if not _same_name(x.get("name"), y.get("name")):
                out.append(f"OTO {i}: main source says '{x.get('name')}', {label} says '{y.get('name')}'.")
            elif _num(x.get("price")) is not None and _num(y.get("price")) is not None \
                    and _num(x.get("price")) != _num(y.get("price")):
                out.append(f"OTO {i} ({x.get('name')}) price: main ${_num(x['price']):g}, "
                           f"{label} ${_num(y['price']):g}.")

    for key, name in (("cartOpen", "Cart open"), ("cartClose", "Cart close")):
        ta, tb = _time_utc(main, key), _time_utc(other, key)
        if ta and tb and ta != tb:
            ra = ((main.get("launch") or {}).get(key) or {}).get("raw")
            rb = ((other.get("launch") or {}).get(key) or {}).get("raw")
            out.append(f"{name}: main source '{ra}' ({ta}), {label} '{rb}' ({tb}).")

    return out


def _bundle_key(name, product) -> str:
    n = _norm(name)
    p = _norm(product)
    return n.replace(p, "") if p and len(n) > len(p) else n


def bundle_conflicts(main: dict, other: dict, label: str) -> list[str]:
    """Bundles are matched by exact name (minus the product name) — 'Bundle' must not match 'DFY Bundle'."""
    out = []
    prod = main.get("productName")
    for bm in main.get("bundles") or []:
        for bo in other.get("bundles") or []:
            if _bundle_key(bm.get("name"), prod) == _bundle_key(bo.get("name"), prod) \
                    and _num(bm.get("price")) is not None and _num(bo.get("price")) is not None \
                    and _num(bm["price"]) != _num(bo["price"]):
                out.append(f"Bundle '{bm.get('name')}' price: main ${_num(bm['price']):g}, "
                           f"{label} ${_num(bo['price']):g}.")
    return out


def coupon_conflicts(main: dict, other: dict, label: str) -> list[str]:
    ca = {(_norm(c.get("code")), _norm(c.get("discount"))) for c in main.get("coupons") or []}
    cb = {(_norm(c.get("code")), _norm(c.get("discount"))) for c in other.get("coupons") or []}
    if ca and cb and ca != cb:
        return [f"Coupons differ between main source and {label}."]
    return []


# ------------------------------------------------------------------ merging

def merge(main: dict, others: list[dict]) -> dict:
    m = copy.deepcopy(main)
    for o in others:
        for k in ("productName", "vendor", "niche", "whatItDoes", "platform", "refundDays", "salesPageUrl"):
            if m.get(k) in (None, "") and o.get(k) not in (None, ""):
                m[k] = o[k]
        m.setdefault("launch", {})
        for key in ("cartOpen", "cartClose"):
            if not (m["launch"].get(key) or {}).get("local") and ((o.get("launch") or {}).get(key) or {}).get("local"):
                m["launch"][key] = o["launch"][key]
        fe, ofe = m.setdefault("frontEnd", {}), o.get("frontEnd") or {}
        for k, v in ofe.items():
            if fe.get(k) in (None, "", []) and v not in (None, "", []):
                fe[k] = v
        if not m.get("otos") and o.get("otos"):
            m["otos"] = copy.deepcopy(o["otos"])
        else:
            for x, y in zip(m.get("otos") or [], o.get("otos") or []):
                if _same_name(x.get("name"), y.get("name")):
                    for k in ("price", "priceType", "description", "downsell"):
                        if x.get(k) in (None, "") and y.get(k) not in (None, ""):
                            x[k] = y[k]
                    if not x.get("items") and y.get("items"):
                        x["items"] = y["items"]
        for k in ("bundles", "coupons", "features", "goodFor", "limitations"):
            if not m.get(k) and o.get(k):
                m[k] = copy.deepcopy(o[k])
        have = {_norm(b.get("title")) for b in m.get("vendorBonuses") or []}
        for b in o.get("vendorBonuses") or []:
            if _norm(b.get("title")) not in have:
                m.setdefault("vendorBonuses", []).append(b)
                have.add(_norm(b.get("title")))
    return m


# ------------------------------------------------------------------ orchestration

def run(client, main_url: str, sales_url: str | None = None,
        cart_open_override_utc: str | None = None, cart_close_override_utc: str | None = None) -> ExtractResult:
    res = ExtractResult(facts={})
    try:
        main_src = fetch(main_url)
    except FetchError as e:
        res.blocking.append(f"Main JV source could not be read: {e}")
        return res
    if main_src.thin:
        res.blocking.append(f"Main JV source has almost no text ({len(main_src.text)} characters). "
                            "The page may need JavaScript or the doc may be empty.")
        return res

    supporting: list[Source] = []
    urls = pick_follow_links(main_src)
    if sales_url and sales_url not in urls:
        urls.append(sales_url)
    for u in urls:
        try:
            s = fetch(u)
        except FetchError as e:
            res.notes.append(f"Skipped linked page: {e}")
            continue
        if s.thin:
            res.notes.append(f"Skipped {u}: almost no text (may need JavaScript).")
            continue
        supporting.append(s)

    all_src = [main_src] + supporting
    for s in all_src:
        res.sources.append({"url": s.url, "kind": s.kind, "chars": len(s.text), "truncated": s.truncated})
        res.images += [i for i in s.images if i not in res.images]
        res.videos += [v for v in s.videos if v not in res.videos]

    main_facts, ti, to = extract_source(client, main_src, "MAIN JV source (chosen by the site owner)")
    res.input_tokens, res.output_tokens = ti, to
    other_facts = []
    for i, s in enumerate(supporting, 1):
        f, ti, to = extract_source(client, s, "supporting page linked from the JV source")
        res.input_tokens += ti
        res.output_tokens += to
        label = f"linked page {i} ({s.url})"
        res.blocking += compare(main_facts, f, label)
        res.notes += coupon_conflicts(main_facts, f, label)
        res.notes += bundle_conflicts(main_facts, f, label)
        other_facts.append(f)

    for c in main_facts.get("internalConflicts") or []:
        res.blocking.append(f"Main source contradicts itself: {c}")

    facts = merge(main_facts, other_facts)

    # times -> UTC (sheet overrides win and clear time problems)
    launch = facts.get("launch") or {}
    for key, override, required in (("cartOpen", cart_open_override_utc, True),
                                    ("cartClose", cart_close_override_utc, False)):
        name = "Cart open" if key == "cartOpen" else "Cart close"
        if override:
            facts[f"{key}Utc"] = override
            continue
        t = launch.get(key) or {}
        if not t.get("local"):
            facts[f"{key}Utc"] = None
            (res.blocking if required else res.notes).append(f"{name} not found in the sources.")
            continue
        utc, problems = to_utc(t.get("local"), t.get("zone"))
        facts[f"{key}Utc"] = utc
        for p in problems:
            res.blocking.append(f"{name} ('{t.get('raw')}'): {p}")

    if not facts.get("productName"):
        res.blocking.append("Product name not found.")
    if _num((facts.get("frontEnd") or {}).get("price")) is None:
        res.blocking.append("Front-end price not found.")
    for o in facts.get("otos") or []:
        if _num(o.get("price")) is None:
            res.blocking.append(f"OTO {o.get('position')} ({o.get('name')}) has no price.")
    if facts.get("refundDays") is None:
        res.notes.append("Refund period not stated; that field will be left out.")
    if facts.get("bundles"):
        res.notes.append(f"Bundle offers found: {', '.join(b.get('name', '?') for b in facts['bundles'])}.")
    if facts.get("coupons"):
        res.notes.append(f"Coupons found: {', '.join(c.get('code', '?') for c in facts['coupons'])}.")
    nov = [b.get("title") for b in facts.get("vendorBonuses") or [] if b.get("value") is None]
    if nov:
        res.notes.append(f"{len(nov)} vendor bonus(es) have no stated value.")
    if main_src.truncated:
        res.notes.append("Main source was longer than 60,000 characters; the end was cut.")
    res.facts = facts
    return res


def summary(res: ExtractResult) -> str:
    f = res.facts or {}
    fe = f.get("frontEnd") or {}
    lines = [f"Product: {f.get('productName')}  |  Vendor: {f.get('vendor')}  |  Niche: {f.get('niche')}",
             f"Front end: {fe.get('name')} ${fe.get('price')} (after launch: {fe.get('priceAfterLaunch')})",
             f"Cart open (UTC): {f.get('cartOpenUtc')}  |  Cart close (UTC): {f.get('cartCloseUtc')}",
             f"Refund days: {f.get('refundDays')}"]
    for o in f.get("otos") or []:
        ds = o.get("downsell") or {}
        lines.append(f"  OTO {o.get('position')}: {o.get('name')} ${o.get('price')}"
                     + (f"  (downsell ${ds.get('price')})" if ds.get("price") is not None else ""))
    for b in f.get("bundles") or []:
        lines.append(f"  Bundle: {b.get('name')} ${b.get('price')}")
    lines.append(f"Vendor bonuses: {len(f.get('vendorBonuses') or [])}  |  Features: {len(f.get('features') or [])}"
                 f"  |  Images found: {len(res.images)}  |  Videos found: {len(res.videos)}")
    return "\n".join(lines)


def to_json(res: ExtractResult) -> str:
    return json.dumps(res.facts, ensure_ascii=False, indent=2)
