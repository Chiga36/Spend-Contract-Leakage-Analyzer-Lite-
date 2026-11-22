"""Each detector against small hand-written inputs (LLD §14): tier boundaries, same-day
duplicates, invoice-number normalization, terms parsing, overlap removal."""
from __future__ import annotations

from decimal import Decimal

import pytest

from backend.graph import detectors as det


# ---------------------------------------------------------------------------- builders
def vendor(vid, name, tax=None, address="1 Main St"):
    return {"vendor_id": vid, "name": name, "tax_id": tax, "address": address, "category": "Test"}


def contract(cid="C1", vid="V1", terms="NET 30", start="2024-01-01", end="2026-12-31"):
    return {"contract_id": cid, "vendor_id": vid, "start_date": start, "end_date": end,
            "payment_terms": terms, "document_file": f"{cid}.md"}


def tier(sku, price, min_qty=0, source="db"):
    return {"sku": sku, "unit_price": price, "min_qty": min_qty, "source": source, "clause": f"{sku} {price}"}


_line_no = 0


def invoice(iid, vid, d, lines=(), total=None, no=None):
    global _line_no
    out = []
    for sku, qty, price in lines:
        _line_no += 1
        out.append({"line_id": f"L{_line_no:05d}", "invoice_id": iid, "sku": sku, "quantity": qty, "unit_price": price})
    if total is None:
        total = float(sum(Decimal(str(q)) * Decimal(str(p)) for _, q, p in lines))
    return {"invoice_id": iid, "vendor_id": vid, "vendor_invoice_no": no or f"N-{iid}", "invoice_date": d,
            "total_amount": total, "lines": out}


def payment(pid, iid, d, amount):
    return {"payment_id": pid, "invoice_id": iid, "paid_date": d, "amount": amount}


def state(**kw):
    base = {"vendors": [vendor("V1", "Acme Supply")], "contracts": [], "invoices": [], "payments": [],
            "rate_cards": {}, "params": {"date_from": "2024-01-01", "date_to": "2026-12-31",
                                         "price_tolerance_pct": 2}}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------- helpers
def test_terms_parsing():
    assert det.parse_terms("2/10 NET 30") == (Decimal("2"), 10, 30)
    assert det.parse_terms("1.5/15 net 45") == (Decimal("1.5"), 15, 45)
    assert det.parse_terms("Terms: 2/10NET30") == (Decimal("2"), 10, 30)
    assert det.parse_terms("NET 30") is None
    assert det.parse_terms(None) is None


@pytest.mark.parametrize("a,b,same", [
    ("INV-00123", "inv 123", True),
    ("INV-00123", "INV123", True),
    ("000456", "456", True),
    ("2025-0457", "2025 0457", True),
    ("INV-123", "INV-1230", False),
    ("INV-123", "INV-123-R", False),
])
def test_invoice_number_normalization(a, b, same):
    assert (det.normalize_invoice_no(a) == det.normalize_invoice_no(b)) is same


def test_vendor_name_normalization():
    assert det.normalize_vendor_name("Pinnacle Packaging, Inc.") == "pinnacle packaging"
    assert det.normalize_vendor_name("Pinnacle Packaging Corp") == "pinnacle packaging"
    assert det.normalize_vendor_name("Acme Pvt. Ltd") == "acme"


# ---------------------------------------------------------------------------- 1. duplicate vendors
def test_duplicate_vendors_tax_id_name_and_borderline():
    vendors = [
        vendor("V1", "Northwind Industrial Supply Inc", tax="12-3456789"),
        vendor("V2", "NW Indl", tax="123456789"),                       # same tax id, different format
        vendor("V3", "Pinnacle Packaging Corp", tax="98-7654321"),
        vendor("V4", "Pinnacle Packaging, Inc."),                       # identical normalized name
        vendor("V5", "Oakridge Print Services", tax="11-1111111"),
        vendor("V6", "Oakridge Pest Services", tax="22-2222222"),       # borderline (0.80-0.90)
    ]
    invoices = [invoice("I1", "V1", "2025-01-10", [("A", 10, 100.0)]),   # V1 spend 1,000
                invoice("I2", "V2", "2025-01-11", [("A", 2, 100.0)]),    # V2 spend   200
                invoice("I3", "V3", "2025-01-12", [("B", 50, 10.0)])]    # V3 spend   500
    s = state(vendors=vendors, invoices=invoices)
    matches, borderline = det.vendor_pair_candidates(vendors)
    assert {(m["a"]["vendor_id"], m["b"]["vendor_id"], m["matched_by"]) for m in matches} == {
        ("V1", "V2", "tax_id"), ("V3", "V4", "name")}
    assert [(p["a"]["vendor_id"], p["b"]["vendor_id"]) for p in borderline] == [("V5", "V6")]

    findings = {tuple(f["evidence"]["vendor_ids"]): f for f in det.detect_duplicate_vendors(s)}
    assert set(findings) == {("V1", "V2"), ("V3", "V4")}          # borderline skipped without the LLM
    tax = findings[("V1", "V2")]
    assert tax["confidence"] == 0.95 and tax["amount"] == 6.00     # min(1000, 200) x 3%
    assert tax["calculation"] == "min($1,000.00, $200.00) × 3% = $6.00"
    assert tax["vendor_id"] == "V1"                                # attributed to the larger record
    assert findings[("V3", "V4")]["confidence"] == 0.8
    assert findings[("V3", "V4")]["amount"] == 0.0                 # V4 has no spend

    confirmed = det.detect_duplicate_vendors(s, [{**borderline[0], "llm_reason": "same firm"}])
    llm = [f for f in confirmed if f["evidence"]["matched_by"] == "llm"]
    assert len(llm) == 1 and llm[0]["confidence"] == 0.75


# ---------------------------------------------------------------------------- 2. duplicate payments
def test_same_day_duplicate_payment():
    s = state(
        invoices=[invoice("I1", "V1", "2025-03-01", [("A", 1, 1234.56)], no="A-1"),
                  invoice("I2", "V1", "2025-03-01", [("A", 1, 1234.56)], no="A-2")],
        payments=[payment("P1", "I1", "2025-03-20", 1234.56), payment("P2", "I2", "2025-03-25", 1234.56)])
    [f] = det.detect_duplicate_payments(s)
    assert f["type"] == det.DUPLICATE_PAYMENT
    assert f["confidence"] == 0.75 and f["evidence"]["matched_by"] == "date"
    assert f["evidence"]["duplicate"]["invoice_id"] == "I2"
    assert f["amount"] == 1234.56
    assert f["evidence"]["line_ids"] == [ln["line_id"] for ln in s["invoices"][1]["lines"]]


def test_duplicate_payment_by_invoice_number_far_apart():
    s = state(
        invoices=[invoice("I1", "V1", "2025-01-05", total=5000.0, no="INV-00123"),
                  invoice("I2", "V1", "2025-03-01", total=5000.0, no="inv 123"),     # 55 days later
                  invoice("I3", "V1", "2025-06-01", total=5000.0, no="INV-999")],    # different no, far apart
        payments=[payment("P1", "I1", "2025-02-01", 5000.0), payment("P2", "I2", "2025-03-30", 3000.0),
                  payment("P3", "I2", "2025-04-15", 2000.0), payment("P4", "I3", "2025-07-01", 5000.0)])
    [f] = det.detect_duplicate_payments(s)
    assert f["confidence"] == 0.95 and f["evidence"]["matched_by"] == "invoice_no"
    assert f["evidence"]["invoice_ids"] == ["I1", "I2"]
    assert f["amount"] == 5000.0                                   # both payments of the later invoice


def test_duplicate_payment_requires_both_paid_and_window():
    s = state(
        invoices=[invoice("I1", "V1", "2025-01-05", total=700.0, no="X1"),
                  invoice("I2", "V1", "2025-01-10", total=700.0, no="X2"),     # unpaid
                  invoice("I3", "V1", "2025-02-20", total=700.0, no="X3")],    # 46 days later
        payments=[payment("P1", "I1", "2025-02-01", 700.0), payment("P3", "I3", "2025-03-01", 700.0)])
    assert det.detect_duplicate_payments(s) == []


def test_duplicate_payment_across_duplicate_vendor_records():
    vendors = [vendor("V1", "Northwind Industrial Supply", tax="12-3456789"), vendor("V2", "NW Supply", tax="12-3456789")]
    s = state(vendors=vendors,
              invoices=[invoice("I1", "V1", "2025-01-05", total=900.0, no="88"),
                        invoice("I2", "V2", "2025-01-09", total=900.0, no="0088")],
              payments=[payment("P1", "I1", "2025-02-01", 900.0), payment("P2", "I2", "2025-02-03", 900.0)])
    [f] = det.detect_duplicate_payments(s)
    assert f["evidence"]["cross_vendor"] is True and f["evidence"]["matched_by"] == "invoice_no"


# ---------------------------------------------------------------------------- 3 + 4. contract pricing
def tiered_state(lines_by_invoice, tol=2):
    invoices = [invoice(f"I{n}", "V1", d, lines) for n, (d, lines) in enumerate(lines_by_invoice, start=1)]
    return state(contracts=[contract()], invoices=invoices,
                 rate_cards={"C1": [tier("SKU", 10.0, 0), tier("SKU", 9.0, 100)]},
                 params={"date_from": "2024-01-01", "date_to": "2026-12-31", "price_tolerance_pct": tol})


def test_volume_tier_boundary():
    s = tiered_state([
        ("2025-01-10", [("SKU", 60, 10.0)]),   # prior   0 -> base
        ("2025-02-10", [("SKU", 39, 10.0)]),   # prior  60 -> base
        ("2025-03-10", [("SKU", 1, 10.0)]),    # prior  99 -> base (one short of the tier)
        ("2025-04-10", [("SKU", 10, 10.0)]),   # prior 100 -> tier reached, still billed 10.00: missed
        ("2025-05-10", [("SKU", 5, 9.0)]),     # prior 110 -> tier price billed: fine
        ("2026-01-05", [("SKU", 5, 10.0)]),    # new calendar year: prior 0 -> base, fine
    ])
    [f] = det.detect_missed_volume_discount(s)
    assert f["evidence"]["line_ids"] == [s["invoices"][3]["lines"][0]["line_id"]]
    assert f["amount"] == 10.0
    assert f["calculation"] == "(10.00 − 9.00) × 10 = $10.00"
    assert f["confidence"] == 0.85 and f["evidence"]["tier_min_qty"] == 100
    assert det.detect_price_variance(s) == []                      # the line belongs to one detector only


def test_volume_discount_does_not_depend_on_price_tolerance():
    s = tiered_state([("2025-01-10", [("SKU", 100, 10.0)]), ("2025-02-10", [("SKU", 50, 10.0)])], tol=20)
    [f] = det.detect_missed_volume_discount(s)
    assert f["amount"] == 50.0


def test_price_variance_and_tolerance():
    s = state(contracts=[contract()], rate_cards={"C1": [tier("SKU", 42.0)]},
              invoices=[invoice("I1", "V1", "2025-05-01", [("SKU", 1200, 48.5)]),
                        invoice("I2", "V1", "2025-05-02", [("SKU", 100, 42.8)])])   # +1.9%: within 2%
    [f] = det.detect_price_variance(s)
    assert f["calculation"] == "(48.50 − 42.00) × 1,200 = $7,800.00"
    assert f["amount"] == 7800.0 and f["confidence"] == 0.9 and f["evidence"]["rate_source"] == "db"

    s["params"]["price_tolerance_pct"] = 1
    [f] = det.detect_price_variance(s)
    assert f["amount"] == 7880.0 and len(f["evidence"]["lines"]) == 2

    s["rate_cards"] = {"C1": [tier("SKU", 42.0, source="llm")]}
    assert det.detect_price_variance(s)[0]["confidence"] == 0.75


def test_overcharge_above_every_tier_is_price_variance():
    s = tiered_state([("2025-01-10", [("SKU", 100, 9.0)]), ("2025-02-10", [("SKU", 10, 10.5)])])
    assert det.detect_missed_volume_discount(s) == []
    [f] = det.detect_price_variance(s)
    assert f["amount"] == 15.0                                     # (10.50 - 9.00) x 10


def test_lines_outside_contract_or_rate_card_are_ignored():
    s = state(contracts=[contract(start="2025-06-01")], rate_cards={"C1": [tier("SKU", 10.0)]},
              invoices=[invoice("I1", "V1", "2025-05-01", [("SKU", 10, 20.0)]),    # before the contract
                        invoice("I2", "V1", "2025-07-01", [("OTHER", 10, 20.0)])])  # SKU not on the card
    assert det.detect_price_variance(s) == []


# ---------------------------------------------------------------------------- 5. early-pay discount
def test_missed_early_pay_discount():
    s = state(contracts=[contract(terms="2/10 NET 30")],
              invoices=[invoice("I1", "V1", "2025-01-01", total=10000.0),
                        invoice("I2", "V1", "2025-02-01", total=5000.0),
                        invoice("I3", "V1", "2025-03-01", total=8000.0)],                # unpaid
              payments=[payment("P1", "I1", "2025-01-11", 9800.0),                      # day 10: in window
                        payment("P2", "I2", "2025-02-12", 5000.0)])                     # day 11: missed
    [f] = det.detect_missed_early_pay(s)
    assert f["evidence"]["invoice_ids"] == ["I2"]
    assert f["amount"] == 100.0 and f["confidence"] == 0.9
    assert f["calculation"].startswith("$5,000.00 × 2% = $100.00")


# ---------------------------------------------------------------------------- aggregate
def test_aggregate_overlap_priority_scoring_and_ids():
    dup_lines = ["L-A", "L-B"]
    dup = {"type": det.DUPLICATE_PAYMENT, "vendor_id": "V1", "vendor": "Acme", "title": "dup", "amount": 30000.0,
           "confidence": 0.95, "calculation": "", "evidence": {"invoice_ids": ["I1", "I2"], "line_ids": dup_lines,
                                                               "payment_ids": [], "duplicate": {"invoice_id": "I2"}}}

    def line(lid, amount):
        return {"line_id": lid, "invoice_id": "I2", "vendor_invoice_no": "N", "invoice_date": "2025-01-01",
                "quantity": 1.0, "billed_price": amount, "contract_price": 0.0, "tier_min_qty": 0,
                "prior_ytd_qty": 0.0, "amount": amount}

    pv = det.rebuild_line_finding({"type": det.PRICE_VARIANCE, "vendor_id": "V1", "vendor": "Acme", "confidence": 0.9,
                                   "evidence": {"sku": "S", "contract_id": "C1", "payment_ids": []}},
                                  [line("L-A", 100.0), line("L-C", 6000.0)])
    vd = det.rebuild_line_finding({"type": det.VOLUME_DISCOUNT, "vendor_id": "V1", "vendor": "Acme", "confidence": 0.85,
                                   "evidence": {"sku": "S", "contract_id": "C1", "payment_ids": []}},
                                  [line("L-C", 50.0)])                      # fully claimed by price variance
    ep = {"type": det.EARLY_PAY_DISCOUNT, "vendor_id": "V1", "vendor": "Acme", "title": "ep", "amount": 600.0,
          "confidence": 0.9, "calculation": "", "evidence": {"invoice_ids": ["I2"], "line_ids": [], "payment_ids": []}}
    findings, summary = det.aggregate_findings({"dup_payments": [dup], "price_variance": [pv],
                                                "volume_discount": [vd], "early_pay": [ep]})
    assert [f["type"] for f in findings] == [det.DUPLICATE_PAYMENT, det.PRICE_VARIANCE]
    assert [f["id"] for f in findings] == ["F-001", "F-002"]
    pv_out = findings[1]
    assert pv_out["amount"] == 6000.0 and pv_out["evidence"]["line_ids"] == ["L-C"]
    assert "overlap_note" in pv_out["evidence"]
    assert findings[0]["severity"] == "HIGH" and pv_out["severity"] == "MEDIUM"
    assert findings[0]["score"] == 28500.0                         # 30,000 x 0.95
    assert summary == {"total": 36000.0, "count": 2,
                       "by_type": {det.DUPLICATE_PAYMENT: 30000.0, det.PRICE_VARIANCE: 6000.0},
                       "count_by_type": {det.DUPLICATE_PAYMENT: 1, det.PRICE_VARIANCE: 1},
                       "by_severity": {"HIGH": 1, "MEDIUM": 1, "LOW": 0}}


def test_severity_thresholds():
    assert det.severity_for(25000) == "HIGH"
    assert det.severity_for(24999.99) == "MEDIUM"
    assert det.severity_for(5000) == "MEDIUM"
    assert det.severity_for(4999.99) == "LOW"
