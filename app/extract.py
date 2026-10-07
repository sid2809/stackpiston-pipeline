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
from .llm import LLMError
from .timeparse import to_utc

PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "extraction.md").read_text(encoding="utf-8")
NOTES_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "notes.md").read_text(encoding="utf-8")
RECONCILE_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "reconcile.md").read_text(encoding="utf-8")


@dataclass
class ExtractResult:
    facts: dict
    blocking: list[str] = field(default_factory=list)   # the row must stop
    warnings: list[str] = field(default_factory=list)   # the AI decided something you should check (row stays a draft)
    notes: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    video_candidates: list[dict] = field(default_factory=list)  # {"url", "context", "source"}
    image_candidates: list[dict] = field(default_factory=list)  # {"url", "alt", "source", "sales"}
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
    for key in ("vendorBonuses", "affiliateBonuses"):
        out[key] = [{"title": str(b.get("title") or ""), "description": b.get("description"),
                     "value": _num(b.get("value"))} for b in _objs(d.get(key)) if b.get("title")]
    out["features"] = [{"title": str(f.get("title") or ""), "detail": str(f.get("detail") or "")}
                       for f in _objs(d.get("features")) if f.get("title")]
    for k in ("goodFor", "limitations", "offerNotes", "internalConflicts", "unknowns"):
        out[k] = _strs(d.get(k))
    return out


def extract_source(client, src: Source, role: str) -> tuple[dict, int, int]:
    user = (f"Source role: {role}\nSource URL: {src.url}\n"
            f"{'(Text was cut at 60,000 characters.)' if src.truncated else ''}\n\n"
            f"--- SOURCE TEXT START ---\n{src.text}\n--- SOURCE TEXT END ---")
    r = client.complete_json(PROMPT, user, max_tokens=16000, temperature=0)
    return clean_facts(r.data), r.input_tokens, r.output_tokens


def apply_notes(client, facts: dict, notes: str) -> tuple[dict, int, int]:
    """Owner's 'Notes for AI' win over the vendor pages. Launch times are never taken from notes."""
    user = (f"OWNER NOTES:\n{notes.strip()}\n\nFACTS:\n{json.dumps(facts, ensure_ascii=False, indent=1)}")
    r = client.complete_json(NOTES_PROMPT, user, max_tokens=16000)
    # Keys the AI left out keep their original values, so a partial reply can't wipe facts.
    reply = r.data if isinstance(r.data, dict) else {}
    out = clean_facts({**facts, **{k: v for k, v in reply.items() if k in facts}})
    out["launch"] = copy.deepcopy(facts.get("launch") or out.get("launch"))
    out["internalConflicts"] = []
    return out, r.input_tokens, r.output_tokens


# Lists where the AI may deliberately drop items another source has (leftovers, webinar gifts...).
STICKY_DECISIONS = {"affiliateBonuses", "vendorBonuses", "otos", "bundles", "coupons"}


def reconcile(client, labelled: list[tuple[str, dict]], disagreements: list[str], today: str,
              hints: dict | None = None) -> tuple[dict, list[str], int, int]:
    """The AI picks the best value for each disagreement and says why. Launch info is kept from the base source."""
    payload = {label: f for label, f in labelled}
    hint = f"SHEET HINTS (affiliate links the site owner entered): {json.dumps(hints)}\n\n" if hints else ""
    user = (hint + f"TODAY: {today}\n\nDISAGREEMENTS:\n- " + "\n- ".join(disagreements) +
            f"\n\nFACTS BY SOURCE:\n{json.dumps(payload, ensure_ascii=False, indent=1)}")
    r = client.complete_json(RECONCILE_PROMPT, user, max_tokens=16000)
    reply = r.data if isinstance(r.data, dict) else {}
    base = labelled[0][1]
    chosen = reply.get("facts") if isinstance(reply.get("facts"), dict) else {}
    picked = {k: v for k, v in chosen.items() if k in base}
    facts = clean_facts({**base, **picked})
    decisions = [str(d) for d in reply.get("decisions") or [] if str(d).strip()]
    return facts, decisions, r.input_tokens, r.output_tokens, set(picked)


# ------------------------------------------------------------------ comparing

def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


GENERIC = {"oto", "upgrade", "upsell", "edition", "version", "package", "pack", "plan", "the", "and", "of",
           "license", "access", "deal", "offer", "level", "tier"}


def _tokens(name, product: str = "") -> set[str]:
    def toks(x):
        return {t[:-1] if len(t) > 3 and t.endswith("s") else t for t in re.findall(r"[a-z0-9]+", str(x or "").lower())}
    drop = toks(product) | GENERIC
    return {t for t in toks(name) if t not in drop and not t.isdigit()}


def _same_name(a, b, product: str = "") -> bool:
    """'Comic Videos AI Pro', 'PRO Upgrade' and 'ComicVideo AI Pro' are the same offer: compare the
    words left after removing the product name and generic words (upgrade, OTO, edition...)."""
    ta, tb = _tokens(a, product), _tokens(b, product)
    if ta and tb:
        return ta <= tb or tb <= ta
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
        product = main.get("productName") or other.get("productName") or ""
        for i, (x, y) in enumerate(zip(mo, oo), 1):
            if not _same_name(x.get("name"), y.get("name"), product):
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
                if _same_name(x.get("name"), y.get("name"), m.get("productName") or ""):
                    for k in ("price", "priceType", "description", "downsell"):
                        if x.get(k) in (None, "") and y.get(k) not in (None, ""):
                            x[k] = y[k]
                    if not x.get("items") and y.get("items"):
                        x["items"] = y["items"]
        have_codes = {_norm(c.get("code")) for c in m.get("coupons") or [] if c.get("code")}
        for c in o.get("coupons") or []:
            if c.get("code") and _norm(c["code"]) not in have_codes:
                m.setdefault("coupons", []).append(c)
                have_codes.add(_norm(c["code"]))
        for k in ("bundles", "features", "goodFor", "limitations"):
            if not m.get(k) and o.get(k):
                m[k] = copy.deepcopy(o[k])
        for key in ("vendorBonuses", "affiliateBonuses"):
            have = {_norm(b.get("title")) for b in m.get(key) or []}
            for b in o.get(key) or []:
                if _norm(b.get("title")) not in have:
                    m.setdefault(key, []).append(b)
                    have.add(_norm(b.get("title")))
    return m


# ------------------------------------------------------------------ orchestration

def run(client, main_urls, sales_url: str | None = None,
        cart_open_override_utc: str | None = None, cart_close_override_utc: str | None = None,
        owner_notes: str | None = None, today: str | None = None, hints: dict | None = None) -> ExtractResult:
    """main_urls: the JV page and/or JV doc from the sheet (a single URL also works)."""
    from datetime import datetime, timezone
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if isinstance(main_urls, str):
        main_urls = [main_urls]
    main_urls = [u.strip() for u in main_urls if u and u.strip()]
    res = ExtractResult(facts={})
    if not main_urls:
        res.blocking.append("Fill in the JV page URL or the JV doc URL.")
        return res

    primaries: list[Source] = []
    for u in main_urls:
        try:
            src = fetch(u)
        except FetchError as e:
            res.notes.append(f"Could not read {u}: {e}")
            continue
        if src.thin:
            res.notes.append(f"{u} has almost no text (it may need JavaScript).")
            continue
        primaries.append(src)
    if not primaries:
        res.blocking.append("None of the JV sources could be read. " + " ".join(res.notes))
        return res

    seen = {p.url.rstrip("/") for p in primaries}
    linked: list[str] = []
    for p in primaries:
        for u in pick_follow_links(p):
            if u.rstrip("/") not in seen and u not in linked:
                linked.append(u)
    if sales_url and sales_url.rstrip("/") not in seen and sales_url not in linked:
        linked.append(sales_url)
    supporting: list[Source] = []
    for u in linked:
        try:
            s = fetch(u)
        except FetchError as e:
            res.notes.append(f"Skipped linked page: {e}")
            continue
        if s.thin:
            res.notes.append(f"Skipped {u}: almost no text (may need JavaScript).")
            continue
        supporting.append(s)

    for s in primaries + supporting:
        res.sources.append({"url": s.url, "kind": s.kind, "chars": len(s.text), "truncated": s.truncated})
        res.images += [i for i in s.images if i not in res.images]
        res.videos += [v for v in s.videos if v not in res.videos]
        # The sales page from the sheet, or a linked preview/sales page, holds the buyer-facing images.
        is_sales = (bool(sales_url) and s.url.rstrip("/") == sales_url.rstrip("/")) or \
            (s.kind == "web" and any(s is x for x in supporting))
        where = "the sales page" if is_sales else ("the JV doc" if s.kind == "gdoc" else s.url)
        known_v = {c["url"] for c in res.video_candidates}
        res.video_candidates += [dict(v, source=where) for v in s.video_info if v["url"] not in known_v]
        known_i = {c["url"] for c in res.image_candidates}
        res.image_candidates += [dict(i, source=where, sales=is_sales) for i in s.image_info if i["url"] not in known_i]

    labelled: list[tuple[str, dict]] = []
    for i, s in enumerate(primaries):
        role = "MAIN JV source (JV page or JV doc chosen by the site owner)"
        try:
            f, ti, to = extract_source(client, s, role)
        except LLMError as e:
            if i == 0 and len(primaries) == 1:
                raise
            res.notes.append(f"Skipped {s.url}: AI could not read it ({e}).")
            continue
        res.input_tokens += ti
        res.output_tokens += to
        labelled.append((f"main source {i + 1} ({s.url})", f))
    if not labelled:
        res.blocking.append("The AI could not read any JV source.")
        return res
    for i, s in enumerate(supporting, 1):
        try:
            f, ti, to = extract_source(client, s, "supporting page linked from the JV source")
        except LLMError as e:
            res.notes.append(f"Skipped linked page {s.url}: AI could not read it ({e}).")
            continue
        res.input_tokens += ti
        res.output_tokens += to
        labelled.append((f"linked page {i} ({s.url})", f))

    base = labelled[0][1]
    disagreements: list[str] = []
    for label, f in labelled[1:]:
        disagreements += compare(base, f, label)
        res.notes += coupon_conflicts(base, f, label) + bundle_conflicts(base, f, label)
    for label, f in labelled:
        disagreements += [f"{label} contradicts itself: {c}" for c in f.get("internalConflicts") or []]

    others = [f for _, f in labelled[1:]]
    facts = merge(base, others)
    if disagreements:
        try:
            decided, decisions, ti, to, decided_keys = reconcile(client, labelled, disagreements, today, hints)
            res.input_tokens += ti
            res.output_tokens += to
            facts = merge(decided, others)
            # Keep what the AI decided: merging must not re-add items it removed on purpose
            # (e.g. webinar gifts it ruled out as bonuses, or a leftover OTO from another product).
            for k in decided_keys & STICKY_DECISIONS:
                facts[k] = copy.deepcopy(decided[k])
            res.warnings += decisions or [f"Sources disagreed ({len(disagreements)} points); the AI chose without explaining."]
        except LLMError as e:
            res.warnings += disagreements
            res.warnings.append(f"The AI could not settle these, so the first JV source was used ({e}).")

    owner_notes = (owner_notes or "").strip()
    if owner_notes:
        try:
            facts, ti, to = apply_notes(client, facts, owner_notes)
            res.input_tokens += ti
            res.output_tokens += to
            res.notes.append("Notes for AI applied (they win over the AI's choices).")
        except LLMError as e:
            res.blocking.append(f"Notes for AI could not be applied: {e}")
            return res

    # times -> UTC. Sheet entries win; ambiguous times become warnings (row stays a draft).
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
            if required:
                res.blocking.append(f"{name} not found in the sources. Fill Cart open in the sheet.")
            else:
                res.notes.append(f"{name} not found in the sources.")
            continue
        utc, problems = to_utc(t.get("local"), t.get("zone"))
        facts[f"{key}Utc"] = utc
        if utc is None:
            res.blocking.append(f"{name} ('{t.get('raw')}'): " + "; ".join(problems) + f" Fill {name} in the sheet.")
        else:
            res.warnings += [f"{name} ('{t.get('raw')}'): {p}" for p in problems]
    open_utc = facts.get("cartOpenUtc")
    if open_utc and open_utc[:10] < today and facts.get("cartCloseUtc") and facts["cartCloseUtc"][:10] < today:
        res.warnings.append(f"The launch dates ({open_utc[:10]} to {facts['cartCloseUtc'][:10]}) are in the past. "
                            "They may be leftovers from an older launch.")

    if not facts.get("productName"):
        res.blocking.append("Product name not found.")
    if _num((facts.get("frontEnd") or {}).get("price")) is None:
        res.blocking.append("Front-end price not found.")
    for o in facts.get("otos") or []:
        if _num(o.get("price")) is None:
            res.blocking.append(f"OTO {o.get('position')} ({o.get('name')}) has no price.")
    for n in facts.get("offerNotes") or []:
        res.warnings.append(f"Offer choices: {n}")
    if facts.get("refundDays") is None:
        res.notes.append("Refund period not stated; that field will be left out.")
    if facts.get("bundles"):
        res.notes.append(f"Bundle offers found: {', '.join(b.get('name', '?') for b in facts['bundles'])}.")
    codes = [c.get("code") for c in facts.get("coupons") or [] if c.get("code")]
    if codes:
        res.notes.append(f"Coupons found: {', '.join(codes)}.")
    if facts.get("affiliateBonuses"):
        res.notes.append(f"{len(facts['affiliateBonuses'])} affiliate pack bonus(es) found; they will be listed as your bonuses.")
    nov = [b.get("title") for b in facts.get("vendorBonuses") or [] if b.get("value") is None]
    if nov:
        res.notes.append(f"{len(nov)} vendor bonus(es) have no stated value.")
    if any(p.truncated for p in primaries):
        res.notes.append("A JV source was longer than 60,000 characters; the end was cut.")
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
    lines.append(f"Vendor bonuses: {len(f.get('vendorBonuses') or [])}  |  Affiliate pack bonuses: "
                 f"{len(f.get('affiliateBonuses') or [])}  |  Features: {len(f.get('features') or [])}"
                 f"  |  Images found: {len(res.images)}  |  Videos found: {len(res.videos)}")
    return "\n".join(lines)


def to_json(res: ExtractResult) -> str:
    return json.dumps(res.facts, ensure_ascii=False, indent=2)
