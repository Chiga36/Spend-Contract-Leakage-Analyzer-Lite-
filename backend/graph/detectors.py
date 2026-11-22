"""The five leakage detectors and the overlap-removing aggregation (LLD §7.3-7.4).

Every detector is a pure function ``(state) -> list[finding]``: no I/O and no LLM calls, so
each one can be unit-tested with a few hand-written rows. All money arithmetic is done with
``Decimal``; amounts leave this module as floats rounded to cents.

A finding (before ``aggregate_findings`` assigns ids, scores and severities) looks like::

    {"type": "PRICE_VARIANCE", "vendor_id": "V0042", "vendor": "Acme Supply",
     "title": "...", "amount": 7800.0, "confidence": 0.9,
     "calculation": "(48.50 − 42.00) × 1,200 = $7,800.00",
     "evidence": {"invoice_ids": [...], "line_ids": [...], "payment_ids": [...], ...}}
"""
from __future__ import annotations

import copy
import re
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from difflib import SequenceMatcher
from typing import Any, Iterator

# --------------------------------------------------------------------------- finding types
DUPLICATE_VENDOR = "DUPLICATE_VENDOR"
DUPLICATE_PAYMENT = "DUPLICATE_PAYMENT"
PRICE_VARIANCE = "PRICE_VARIANCE"
VOLUME_DISCOUNT = "VOLUME_DISCOUNT"
EARLY_PAY_DISCOUNT = "EARLY_PAY_DISCOUNT"

TYPE_LABELS = {
    DUPLICATE_VENDOR: "Duplicate vendor",
    DUPLICATE_PAYMENT: "Duplicate payment",
    PRICE_VARIANCE: "Price above contract",
    VOLUME_DISCOUNT: "Missed volume discount",
    EARLY_PAY_DISCOUNT: "Missed early-pay discount",
}

# --------------------------------------------------------------------------- rule constants
NAME_MATCH_RATIO = 0.90          # >= : same vendor
NAME_BORDERLINE_RATIO = 0.80     # [0.80, 0.90): ask the LLM, or skip when it is off
LEVERAGE_RATE = Decimal("0.03")  # duplicate vendor: smaller record's spend x 3%
DUP_PAYMENT_WINDOW_DAYS = 14
DEFAULT_TOLERANCE_PCT = 2.0
SEVERITY_HIGH = Decimal("25000")
SEVERITY_MEDIUM = Decimal("5000")

CONF_TAX_ID, CONF_NAME, CONF_NAME_LLM = 0.95, 0.80, 0.75
CONF_DUP_INVOICE_NO, CONF_DUP_DATE = 0.95, 0.75
CONF_PRICE_DB, CONF_PRICE_LLM = 0.90, 0.75
CONF_VOLUME = 0.85
CONF_EARLY_PAY = 0.90

TERMS_RE = re.compile(r"(\d+(?:\.\d+)?)/(\d+)\s*NET\s*(\d+)", re.IGNORECASE)
_COMPANY_SUFFIXES = frozenset(
    {"inc", "incorporated", "llc", "ltd", "limited", "pvt", "private", "corp", "corporation", "co", "company"}
)
CENT = Decimal("0.01")
_MAX_CALC_TERMS = 6

Finding = dict[str, Any]


# --------------------------------------------------------------------------- small helpers
def dec(x: Any) -> Decimal:
    """Convert a SQLite REAL (float) or string to Decimal without binary noise."""
    return x if isinstance(x, Decimal) else Decimal(str(x))


def cents(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def fmt_money(x: Any) -> str:
    v = cents(dec(x))
    return f"{'−' if v < 0 else ''}${abs(v):,.2f}"


def fmt_price(x: Any) -> str:
    return f"{cents(dec(x)):,.2f}"


def fmt_qty(x: Any) -> str:
    v = dec(x)
    return f"{int(v):,}" if v == v.to_integral_value() else f"{v.normalize():,}"


def fmt_pct(x: Any) -> str:
    return f"{dec(x).normalize():f}"


def to_date(s: str) -> date:
    return date.fromisoformat(str(s)[:10])


def _window(state: dict) -> tuple[date, date]:
    """The analysis window. Invoices before it may be loaded only as tier/duplicate history."""
    p = state.get("params") or {}
    lo = to_date(p["date_from"]) if p.get("date_from") else date.min
    hi = to_date(p["date_to"]) if p.get("date_to") else date.max
    return lo, hi


def _tolerance(state: dict) -> Decimal:
    p = state.get("params") or {}
    return dec(p.get("price_tolerance_pct", DEFAULT_TOLERANCE_PCT)) / 100


def _vendor_names(state: dict) -> dict[str, str]:
    return {v["vendor_id"]: v["name"] for v in state.get("vendors") or []}


def normalize_vendor_name(name: str | None) -> str:
    """Lowercase, strip punctuation and legal suffixes (Inc/LLC/Ltd/Pvt/Corp...)."""
    s = re.sub(r"[^\w\s]", "", (name or "").lower()).replace("_", " ")
    return " ".join(t for t in s.split() if t not in _COMPANY_SUFFIXES)


def normalize_tax_id(tax_id: str | None) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", tax_id or "").upper()


def normalize_invoice_no(no: str | None) -> str:
    """Uppercase, letters/digits only, no leading zeros: 'inv-00123' == 'INV123'."""
    s = re.sub(r"[^A-Z0-9]", "", (no or "").upper())
    return re.sub(r"(?<![0-9])0+(?=[0-9])", "", s)


def parse_terms(terms: str | None) -> tuple[Decimal, int, int] | None:
    """'2/10 NET 30' -> (Decimal('2'), 10, 30). None when there is no early-pay discount."""
    m = TERMS_RE.search(terms or "")
    return (Decimal(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def severity_for(amount: Any) -> str:
    a = dec(amount)
    return "HIGH" if a >= SEVERITY_HIGH else "MEDIUM" if a >= SEVERITY_MEDIUM else "LOW"


# =========================================================================== 1. duplicate vendors
def vendor_pair_candidates(vendors: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (matches, borderline) vendor pairs.

    matches: same tax ID, or normalized-name ratio >= 0.90 (deterministic).
    borderline: ratio in [0.80, 0.90); only an LLM can decide these.
    """
    rows = [(v, normalize_vendor_name(v.get("name")), normalize_tax_id(v.get("tax_id")))
            for v in sorted(vendors, key=lambda v: v["vendor_id"])]
    matches: list[dict] = []
    borderline: list[dict] = []
    for i, (va, na, ta) in enumerate(rows):
        for vb, nb, tb in rows[i + 1:]:
            sm = SequenceMatcher(None, na, nb)
            if ta and ta == tb:
                matches.append({"a": va, "b": vb, "matched_by": "tax_id", "ratio": round(sm.ratio(), 3)})
                continue
            if sm.real_quick_ratio() < NAME_BORDERLINE_RATIO or sm.quick_ratio() < NAME_BORDERLINE_RATIO:
                continue
            ratio = sm.ratio()
            pair = {"a": va, "b": vb, "ratio": round(ratio, 3)}
            if ratio >= NAME_MATCH_RATIO:
                matches.append({**pair, "matched_by": "name"})
            elif ratio >= NAME_BORDERLINE_RATIO:
                borderline.append({**pair, "matched_by": "borderline",
                                   "pair_id": f"{va['vendor_id']}~{vb['vendor_id']}"})
    return matches, borderline


def vendor_canonical_map(vendors: list[dict]) -> dict[str, str]:
    """vendor_id -> canonical vendor_id, merging deterministic duplicate-vendor pairs (union-find)."""
    parent = {v["vendor_id"]: v["vendor_id"] for v in vendors}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for m in vendor_pair_candidates(vendors)[0]:
        ra, rb = find(m["a"]["vendor_id"]), find(m["b"]["vendor_id"])
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    return {vid: find(vid) for vid in parent}


def vendor_spend(state: dict) -> dict[str, Decimal]:
    lo, hi = _window(state)
    spend: dict[str, Decimal] = defaultdict(Decimal)
    for inv in state.get("invoices") or []:
        if lo <= to_date(inv["invoice_date"]) <= hi:
            spend[inv["vendor_id"]] += dec(inv["total_amount"])
    return spend


def detect_duplicate_vendors(state: dict, confirmed_borderline: list[dict] | None = None) -> list[Finding]:
    """Same tax ID -> match (0.95). Name ratio >= 0.90 -> match (0.8). Borderline pairs only
    count when the LLM confirmed them (passed in by the graph node; 0.75)."""
    matches, _ = vendor_pair_candidates(state.get("vendors") or [])
    pairs = matches + [{**p, "matched_by": "llm"} for p in confirmed_borderline or []]
    spend = vendor_spend(state)
    confidence = {"tax_id": CONF_TAX_ID, "name": CONF_NAME, "llm": CONF_NAME_LLM}
    findings: list[Finding] = []
    for p in pairs:
        a, b = p["a"], p["b"]
        sa, sb = spend.get(a["vendor_id"], Decimal(0)), spend.get(b["vendor_id"], Decimal(0))
        big, small = (a, b) if sa >= sb else (b, a)
        amount = cents(min(sa, sb) * LEVERAGE_RATE)
        if p["matched_by"] == "tax_id":
            reason = f"Both records carry tax ID {a.get('tax_id')}."
        elif p["matched_by"] == "name":
            reason = f"Normalized names are {p['ratio']:.0%} similar (threshold 90%)."
        else:
            reason = f"Names are {p['ratio']:.0%} similar (borderline); LLM judged them the same company. {p.get('llm_reason', '')}".strip()

        def card(v: dict, s: Decimal) -> dict:
            return {"vendor_id": v["vendor_id"], "name": v["name"], "tax_id": v.get("tax_id"),
                    "address": v.get("address"), "spend": float(cents(s))}

        findings.append({
            "type": DUPLICATE_VENDOR,
            "vendor_id": big["vendor_id"],
            "vendor": big["name"],
            "title": f"Duplicate vendor records: {a['name']} / {b['name']}",
            "amount": float(amount),
            "confidence": confidence[p["matched_by"]],
            "calculation": f"min({fmt_money(sa)}, {fmt_money(sb)}) × 3% = {fmt_money(amount)}",
            "evidence": {
                "vendor_ids": [a["vendor_id"], b["vendor_id"]],
                "vendors": [card(a, sa), card(b, sb)],
                "smaller_vendor_id": small["vendor_id"],
                "matched_by": p["matched_by"],
                "name_similarity": p["ratio"],
                "reason": reason,
                "assumption": "Estimated lost negotiating leverage: 3% of the smaller record's spend "
                              "in the analysis window.",
                "invoice_ids": [], "line_ids": [], "payment_ids": [],
            },
        })
    return findings


# =========================================================================== 2. duplicate payments
def detect_duplicate_payments(state: dict) -> list[Finding]:
    """Same vendor (duplicate-vendor pairs merged) and same amount, and either the normalized
    invoice number matches or the invoice dates are within 14 days. Both must be paid.
    Amount at risk: the later payment."""
    lo, hi = _window(state)
    names = _vendor_names(state)
    canon = vendor_canonical_map(state.get("vendors") or [])
    pays: dict[str, list[dict]] = defaultdict(list)
    for p in state.get("payments") or []:
        pays[p["invoice_id"]].append(p)

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for inv in state.get("invoices") or []:
        if inv["invoice_id"] in pays:                       # both must be paid
            key = (canon.get(inv["vendor_id"], inv["vendor_id"]), cents(dec(inv["total_amount"])))
            groups[key].append(inv)

    findings: list[Finding] = []
    for invs in groups.values():
        if len(invs) < 2:
            continue
        invs.sort(key=lambda i: (i["invoice_date"], i["invoice_id"]))
        flagged: set[str] = set()
        for j, later in enumerate(invs):
            later_date = to_date(later["invoice_date"])
            if j == 0 or not lo <= later_date <= hi:
                continue
            originals = [e for e in invs[:j] if e["invoice_id"] not in flagged]
            norm_no = normalize_invoice_no(later["vendor_invoice_no"])
            match = next((e for e in originals if normalize_invoice_no(e["vendor_invoice_no"]) == norm_no), None)
            how = "invoice_no"
            if match is None:
                how = "date"
                match = next((e for e in reversed(originals)
                              if (later_date - to_date(e["invoice_date"])).days <= DUP_PAYMENT_WINDOW_DAYS), None)
            if match is None:
                continue
            flagged.add(later["invoice_id"])
            later_pays = sorted(pays[later["invoice_id"]], key=lambda p: (p["paid_date"], p["payment_id"]))
            orig_pays = sorted(pays[match["invoice_id"]], key=lambda p: (p["paid_date"], p["payment_id"]))
            amount = cents(sum((dec(p["amount"]) for p in later_pays), Decimal(0)))
            days_apart = (later_date - to_date(match["invoice_date"])).days

            def card(inv: dict, ps: list[dict]) -> dict:
                return {"invoice_id": inv["invoice_id"], "vendor_id": inv["vendor_id"],
                        "vendor": names.get(inv["vendor_id"], inv["vendor_id"]),
                        "vendor_invoice_no": inv["vendor_invoice_no"], "invoice_date": inv["invoice_date"],
                        "total_amount": inv["total_amount"],
                        "payments": [{"payment_id": p["payment_id"], "paid_date": p["paid_date"],
                                      "amount": p["amount"]} for p in ps]}

            cross_vendor = match["vendor_id"] != later["vendor_id"]
            reason = (f"invoice number {match['vendor_invoice_no']!r} ≈ {later['vendor_invoice_no']!r}"
                      if how == "invoice_no" else f"same amount, invoice dates {days_apart} days apart")
            findings.append({
                "type": DUPLICATE_PAYMENT,
                "vendor_id": later["vendor_id"],
                "vendor": names.get(later["vendor_id"], later["vendor_id"]),
                "title": ("Same invoice paid under two vendor records" if cross_vendor
                          else f"Invoice paid twice ({later['vendor_invoice_no']})"),
                "amount": float(amount),
                "confidence": CONF_DUP_INVOICE_NO if how == "invoice_no" else CONF_DUP_DATE,
                "calculation": f"Later payment {', '.join(p['payment_id'] for p in later_pays)} "
                               f"on {later_pays[-1]['paid_date']} = {fmt_money(amount)}",
                "evidence": {
                    "invoice_ids": [match["invoice_id"], later["invoice_id"]],
                    "payment_ids": [p["payment_id"] for p in orig_pays + later_pays],
                    "line_ids": [ln["line_id"] for ln in later.get("lines") or []],
                    "matched_by": how,
                    "reason": reason,
                    "days_apart": days_apart,
                    "cross_vendor": cross_vendor,
                    "original": card(match, orig_pays),
                    "duplicate": card(later, later_pays),
                },
            })
    return findings


# =========================================================================== 3 + 4. contract pricing
def _tier_index(rate_cards: dict[str, list[dict]]) -> dict[tuple[str, str], list[dict]]:
    index: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for contract_id, tiers in (rate_cards or {}).items():
        for t in tiers:
            index[(contract_id, t["sku"])].append(t)
    for tiers in index.values():
        tiers.sort(key=lambda t: dec(t["min_qty"]))
    return index


def _contracts_by_vendor(state: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for c in state.get("contracts") or []:
        out[c["vendor_id"]].append(c)
    return out


def _active_contract(contracts: list[dict], d: date) -> dict | None:
    for c in contracts:
        if to_date(c["start_date"]) <= d <= to_date(c["end_date"]):
            return c
    return None


def iter_contract_lines(state: dict) -> Iterator[dict]:
    """Walk invoice lines on contracted SKUs in (date, invoice, line) order, tracking the
    calendar-year quantity per contract + SKU. A tier applies to a line once the quantity
    bought *before* that line in the same calendar year has reached the tier's min_qty."""
    by_vendor = _contracts_by_vendor(state)
    tiers_for = _tier_index(state.get("rate_cards") or {})
    ytd: dict[tuple, Decimal] = defaultdict(Decimal)
    for inv in sorted(state.get("invoices") or [], key=lambda i: (i["invoice_date"], i["invoice_id"])):
        d = to_date(inv["invoice_date"])
        contract = _active_contract(by_vendor.get(inv["vendor_id"], []), d)
        if contract is None:
            continue
        for line in sorted(inv.get("lines") or [], key=lambda ln: ln["line_id"]):
            tiers = tiers_for.get((contract["contract_id"], line.get("sku")))
            if not tiers:
                continue
            qty = dec(line["quantity"])
            year_key = (contract["contract_id"], line["sku"], d.year)
            prior = ytd[year_key]
            ytd[year_key] = prior + qty
            reached = [t for t in tiers if dec(t["min_qty"]) <= prior]
            if not reached:
                continue
            yield {"invoice": inv, "line": line, "date": d, "contract": contract,
                   "tier": reached[-1], "lower_tiers": reached[:-1], "prior_ytd": prior,
                   "qty": qty, "billed": dec(line["unit_price"]), "price": dec(reached[-1]["unit_price"])}


def classify_line(ctx: dict, tol: Decimal) -> str | None:
    """Decide which detector owns an over-billed contract line.

    VOLUME_DISCOUNT: a higher tier was reached but the line is still billed at a lower tier's
    price (this does not depend on the price tolerance). PRICE_VARIANCE: billed above the
    applicable tier price by more than the tolerance in any other way. Without this split
    every missed-volume line would also be a price variance, and the aggregate priority rule
    would erase the volume detector entirely.
    """
    billed, price = ctx["billed"], ctx["price"]
    if billed <= price:
        return None
    lower_prices = [dec(t["unit_price"]) for t in ctx["lower_tiers"] if dec(t["unit_price"]) > price]
    if lower_prices and min(lower_prices) - CENT <= billed <= max(lower_prices) * (1 + tol):
        return VOLUME_DISCOUNT
    if billed > price * (1 + tol):
        return PRICE_VARIANCE
    return None


def line_calculation(lines: list[dict]) -> str:
    terms = [f"({fmt_price(ln['billed_price'])} − {fmt_price(ln['contract_price'])}) × {fmt_qty(ln['quantity'])}"
             for ln in lines]
    if len(terms) > _MAX_CALC_TERMS:
        terms = terms[:_MAX_CALC_TERMS - 1] + [f"… ({len(lines) - _MAX_CALC_TERMS + 1} more lines)"]
    total = sum((dec(ln["amount"]) for ln in lines), Decimal(0))
    return " + ".join(terms) + f" = {fmt_money(total)}"


def _line_title(kind: str, sku: str, n: int) -> str:
    what = "billed above contract rate" if kind == PRICE_VARIANCE else "volume tier price not applied"
    return f"{sku} {what} ({n} line{'s' if n != 1 else ''})"


def rebuild_line_finding(f: Finding, lines: list[dict]) -> Finding:
    """(Re)compute amount, calculation and id lists of a line-based finding from its lines."""
    f = copy.deepcopy(f)
    ev = f["evidence"]
    ev["lines"] = lines
    ev["line_ids"] = [ln["line_id"] for ln in lines]
    ev["invoice_ids"] = sorted({ln["invoice_id"] for ln in lines})
    f["amount"] = float(cents(sum((dec(ln["amount"]) for ln in lines), Decimal(0))))
    f["calculation"] = line_calculation(lines)
    f["title"] = _line_title(f["type"], ev["sku"], len(lines))
    return f


def _line_findings(state: dict, kind: str) -> list[Finding]:
    tol = _tolerance(state)
    lo, hi = _window(state)
    names = _vendor_names(state)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for ctx in iter_contract_lines(state):
        if lo <= ctx["date"] <= hi and classify_line(ctx, tol) == kind:
            groups[(ctx["invoice"]["vendor_id"], ctx["contract"]["contract_id"], ctx["line"]["sku"])].append(ctx)

    findings: list[Finding] = []
    for (vendor_id, contract_id, sku), ctxs in groups.items():
        lines = [{
            "line_id": c["line"]["line_id"], "invoice_id": c["invoice"]["invoice_id"],
            "vendor_invoice_no": c["invoice"]["vendor_invoice_no"], "invoice_date": c["invoice"]["invoice_date"],
            "quantity": float(c["qty"]), "billed_price": float(c["billed"]), "contract_price": float(c["price"]),
            "tier_min_qty": c["tier"]["min_qty"], "prior_ytd_qty": float(c["prior_ytd"]),
            "amount": float(cents((c["billed"] - c["price"]) * c["qty"])),
        } for c in ctxs]
        tiers_used = [c["tier"] for c in ctxs]
        llm_rates = any(t.get("source") == "llm" for t in tiers_used)
        if kind == PRICE_VARIANCE:
            confidence = CONF_PRICE_LLM if llm_rates else CONF_PRICE_DB
            clause_tier = tiers_used[0]
        else:
            confidence = CONF_VOLUME
            clause_tier = max(tiers_used, key=lambda t: dec(t["min_qty"]))
        evidence = {
            "contract_id": contract_id, "sku": sku,
            "clause": clause_tier.get("clause") or "",
            "rate_source": "llm" if llm_rates else "db",
            "tolerance_pct": float(_tolerance(state) * 100),
        }
        if kind == VOLUME_DISCOUNT:
            evidence["tier_min_qty"] = clause_tier["min_qty"]
            evidence["tier_price"] = clause_tier["unit_price"]
        base = {"type": kind, "vendor_id": vendor_id, "vendor": names.get(vendor_id, vendor_id),
                "confidence": confidence,
                "evidence": {**evidence, "payment_ids": []}}
        findings.append(rebuild_line_finding(base, lines))
    return findings


def detect_price_variance(state: dict) -> list[Finding]:
    """Billed price > applicable tier price x (1 + tolerance). Grouped by vendor + SKU."""
    return _line_findings(state, PRICE_VARIANCE)


def detect_missed_volume_discount(state: dict) -> list[Finding]:
    """Lines after a higher tier's yearly min_qty was crossed, still billed at a lower tier's price."""
    return _line_findings(state, VOLUME_DISCOUNT)


# =========================================================================== 5. early-pay discount
def detect_missed_early_pay(state: dict) -> list[Finding]:
    """Terms like '2/10 NET 30'. Flag invoices first paid after the discount window."""
    lo, hi = _window(state)
    names = _vendor_names(state)
    by_vendor = _contracts_by_vendor(state)
    pays: dict[str, list[dict]] = defaultdict(list)
    for p in state.get("payments") or []:
        pays[p["invoice_id"]].append(p)

    findings: list[Finding] = []
    for inv in sorted(state.get("invoices") or [], key=lambda i: (i["invoice_date"], i["invoice_id"])):
        d = to_date(inv["invoice_date"])
        if not lo <= d <= hi or inv["invoice_id"] not in pays:
            continue
        contract = _active_contract(by_vendor.get(inv["vendor_id"], []), d)
        terms = parse_terms(contract["payment_terms"]) if contract else None
        if not terms:
            continue
        pct, discount_days, _net_days = terms
        inv_pays = sorted(pays[inv["invoice_id"]], key=lambda p: (p["paid_date"], p["payment_id"]))
        days = (to_date(inv_pays[0]["paid_date"]) - d).days
        if days <= discount_days:
            continue
        total = dec(inv["total_amount"])
        amount = cents(total * pct / 100)
        if amount <= 0:
            continue
        findings.append({
            "type": EARLY_PAY_DISCOUNT,
            "vendor_id": inv["vendor_id"],
            "vendor": names.get(inv["vendor_id"], inv["vendor_id"]),
            "title": f"Early-payment discount missed on {inv['vendor_invoice_no']}",
            "amount": float(amount),
            "confidence": CONF_EARLY_PAY,
            "calculation": f"{fmt_money(total)} × {fmt_pct(pct)}% = {fmt_money(amount)} "
                           f"(paid after {days} days; discount window {discount_days} days)",
            "evidence": {
                "invoice_ids": [inv["invoice_id"]],
                "payment_ids": [p["payment_id"] for p in inv_pays],
                "line_ids": [],
                "contract_id": contract["contract_id"],
                "payment_terms": contract["payment_terms"],
                "clause": f"Payment terms: {contract['payment_terms']}",
                "vendor_invoice_no": inv["vendor_invoice_no"],
                "invoice_date": inv["invoice_date"],
                "paid_date": inv_pays[0]["paid_date"],
                "days_to_pay": days,
                "discount_pct": float(pct),
                "discount_days": discount_days,
                "invoice_total": inv["total_amount"],
            },
        })
    return findings


# =========================================================================== aggregate
LINE_PRIORITY = (DUPLICATE_PAYMENT, PRICE_VARIANCE, VOLUME_DISCOUNT)


def aggregate_findings(detector_results: dict[str, list[Finding]]) -> tuple[list[Finding], dict]:
    """Remove overlaps, score, rank and number the findings.

    An invoice line counts once, with priority duplicate payment > price variance > volume
    discount. An early-pay finding on an invoice that is itself a duplicate is dropped too.
    score = amount x confidence; severity HIGH >= $25K, MEDIUM >= $5K.
    """
    by_type: dict[str, list[Finding]] = defaultdict(list)
    for name in sorted(detector_results):
        for f in detector_results[name] or []:
            by_type[f["type"]].append(copy.deepcopy(f))

    kept: list[Finding] = []
    claimed_lines: set[str] = set()
    duplicate_invoices: set[str] = set()
    for f in by_type[DUPLICATE_PAYMENT]:
        kept.append(f)
        claimed_lines.update(f["evidence"].get("line_ids") or [])
        duplicate_invoices.add(f["evidence"]["duplicate"]["invoice_id"])
    for kind in LINE_PRIORITY[1:]:
        for f in by_type[kind]:
            lines = f["evidence"].get("lines") or []
            remaining = [ln for ln in lines if ln["line_id"] not in claimed_lines]
            if not remaining:
                continue
            if len(remaining) != len(lines):
                removed = len(lines) - len(remaining)
                f = rebuild_line_finding(f, remaining)
                f["evidence"]["overlap_note"] = (f"{removed} line(s) removed: already counted under "
                                                 f"a higher-priority finding.")
            claimed_lines.update(ln["line_id"] for ln in remaining)
            kept.append(f)
    kept += [f for f in by_type[EARLY_PAY_DISCOUNT] if f["evidence"]["invoice_ids"][0] not in duplicate_invoices]
    kept += by_type[DUPLICATE_VENDOR]
    kept = [f for f in kept if dec(f["amount"]) > 0]

    for f in kept:
        f["score"] = float(cents(dec(f["amount"]) * dec(f["confidence"])))
        f["severity"] = severity_for(f["amount"])
    kept.sort(key=lambda f: (-f["score"], -f["amount"], f["type"], f["vendor"], f["title"]))
    for i, f in enumerate(kept, start=1):
        f["id"] = f"F-{i:03d}"

    return kept, summarize(kept)


def summarize(findings: list[Finding], amount_key: str = "amount") -> dict:
    by_type: dict[str, Decimal] = defaultdict(Decimal)
    count_by_type: dict[str, int] = defaultdict(int)
    by_severity = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    total = Decimal(0)
    for f in findings:
        amt = dec(f.get(amount_key, f["amount"]))
        total += amt
        by_type[f["type"]] += amt
        count_by_type[f["type"]] += 1
        by_severity[f.get("severity") or severity_for(amt)] += 1
    return {
        "total": float(cents(total)),
        "count": len(findings),
        "by_type": {t: float(cents(v)) for t, v in sorted(by_type.items(), key=lambda kv: -kv[1])},
        "count_by_type": dict(count_by_type),
        "by_severity": by_severity,
    }
