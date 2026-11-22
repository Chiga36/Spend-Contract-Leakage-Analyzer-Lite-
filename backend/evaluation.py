"""Evaluation metrics: how accurate and relevant a run's output is, scored against the ground truth
that seed/generate.py plants in the synthetic data (data/planted_anomalies.json).

Everything here is deterministic Python: no extra LLM calls, so evaluating costs nothing.

Accuracy (detection)
  Per finding type and overall: precision (share of findings that are real planted anomalies),
  recall (share of planted anomalies found), F1, and dollar accuracy (found $ vs planted $).
  The decoy pair (two different companies with similar names) must not be reported.
  Only anomalies that the run's parameters can reach are expected: a price uplift at or below
  the run's tolerance is not, and neither is an anomaly whose invoices fall outside the date range.

LLM quality (only for what the LLM produced in this run)
  Price extraction: rows the LLM extracted from text-only contracts vs. the true price tables
  (precision / recall per row, tier rows separately).
  Explanations:
    faithfulness - every number in the explanation appears in the finding's own data
                   (the LLM must not invent or recalculate amounts);
    relevancy    - the explanation names the vendor or an ID/SKU from the finding, and uses
                   wording that matches the finding type.

Real client data has no ground truth, so evaluate() is only available when the file exists.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from .graph import detectors as det
from .guardrails import relevancy_problem, unsupported_numbers

TYPES = [det.DUPLICATE_VENDOR, det.DUPLICATE_PAYMENT, det.PRICE_VARIANCE, det.VOLUME_DISCOUNT,
         det.EARLY_PAY_DISCOUNT]

def load_ground_truth(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


# --------------------------------------------------------------------------- matching
def _finding_covers(f: dict, a: dict) -> bool:
    """Does finding f point at planted anomaly a (at least partly)?"""
    ev = f.get("evidence") or {}
    t = a["type"]
    if f["type"] != t:
        return False
    if t == det.DUPLICATE_VENDOR:
        return sorted(ev.get("vendor_ids") or []) == sorted(a["vendor_ids"])
    if t == det.DUPLICATE_PAYMENT:
        return (ev.get("duplicate") or {}).get("invoice_id") == a["duplicate_invoice_id"]
    if t == det.PRICE_VARIANCE:
        return a["line_id"] in (ev.get("line_ids") or [])
    if t == det.VOLUME_DISCOUNT:
        return bool(set(a["_lines"]) & set(ev.get("line_ids") or []))
    if t == det.EARLY_PAY_DISCOUNT:
        return a["invoice_id"] in (ev.get("invoice_ids") or [])
    return False


def _anomaly_detected(a: dict, findings: list[dict]) -> bool:
    if a["type"] == det.VOLUME_DISCOUNT:          # every in-range line must be claimed
        found = {lid for f in findings if f["type"] == det.VOLUME_DISCOUNT
                 for lid in (f.get("evidence") or {}).get("line_ids") or []}
        return set(a["_lines"]) <= found
    return any(_finding_covers(f, a) for f in findings)


def _label(a: dict) -> str:
    t = a["type"]
    if t == det.DUPLICATE_VENDOR:
        return " / ".join(a["vendors"])
    if t == det.DUPLICATE_PAYMENT:
        return f"{a['vendor']}: {a['duplicate_invoice_id']}"
    if t == det.PRICE_VARIANCE:
        return f"{a['vendor']}: {a['sku']} line {a['line_id']} (+{a['uplift_pct']}%)"
    if t == det.VOLUME_DISCOUNT:
        return f"{a['vendor']}: {a['sku']} tier from {a['min_qty']:,} units"
    return f"{a['vendor']}: {a['invoice_id']}"


# --------------------------------------------------------------------------- scope
def _expected(gt: dict, params: dict, invoices: list[dict] | None) -> tuple[list[dict], list[str]]:
    """Planted anomalies this run could find, given its tolerance and date range."""
    tol = float(params.get("price_tolerance_pct", det.DEFAULT_TOLERANCE_PCT))
    d_from = date.fromisoformat(params["date_from"]) if params.get("date_from") else date.min
    d_to = date.fromisoformat(params["date_to"]) if params.get("date_to") else date.max
    inv_date: dict[str, date] = {}
    line_inv: dict[str, str] = {}
    for inv in invoices or []:
        inv_date[inv["invoice_id"]] = date.fromisoformat(str(inv["invoice_date"])[:10]) \
            if inv.get("invoice_date") else None
        for ln in inv.get("lines") or []:
            line_inv[ln["line_id"]] = inv["invoice_id"]
    have_dates = bool(inv_date) and all(v is not None for v in inv_date.values())

    def in_range(invoice_id: str | None) -> bool:
        if not have_dates or invoice_id is None:
            return True                                # cannot tell: assume in scope
        d = inv_date.get(invoice_id)
        return d is not None and d_from <= d <= d_to

    out, notes = [], []
    below_tol = out_of_range = 0
    for a in gt["anomalies"]:
        a = dict(a)
        t = a["type"]
        if t == det.PRICE_VARIANCE and a["uplift_pct"] <= tol:
            below_tol += 1
            continue
        if t == det.VOLUME_DISCOUNT:
            a["_lines"] = [lid for lid in a["line_ids"] if in_range(line_inv.get(lid))] if have_dates \
                else list(a["line_ids"])
            if not a["_lines"]:
                out_of_range += 1
                continue
        else:
            key = {det.DUPLICATE_PAYMENT: a.get("duplicate_invoice_id"),
                   det.PRICE_VARIANCE: a.get("invoice_id"),
                   det.EARLY_PAY_DISCOUNT: a.get("invoice_id")}.get(t)
            if not in_range(key):
                out_of_range += 1
                continue
        out.append(a)
    if below_tol:
        notes.append(f"{below_tol} planted price uplift(s) are at or below the {tol:g}% tolerance and are "
                     f"not expected.")
    if out_of_range:
        notes.append(f"{out_of_range} planted anomaly(ies) fall outside the date range and are not expected.")
    return out, notes


# --------------------------------------------------------------------------- metrics
def _prf(tp_findings: int, n_findings: int, detected: int, expected: int) -> dict:
    precision = tp_findings / n_findings if n_findings else None
    recall = detected / expected if expected else None
    f1 = (2 * precision * recall / (precision + recall)) if precision and recall else (
        0.0 if precision is not None and recall is not None else None)
    return {"precision": _r(precision), "recall": _r(recall), "f1": _r(f1)}


def _r(x: float | None, n: int = 4) -> float | None:
    return None if x is None else round(x, n)


def detection_metrics(findings: list[dict], gt: dict, params: dict, invoices: list[dict] | None = None) -> dict:
    expected, notes = _expected(gt, params, invoices)
    by_type: dict[str, Any] = {}
    tot = defaultdict(float)
    for t in TYPES:
        exp_t = [a for a in expected if a["type"] == t]
        fs_t = [f for f in findings if f["type"] == t]
        detected = [a for a in exp_t if _anomaly_detected(a, fs_t)]
        missed = [a for a in exp_t if a not in detected]
        tp_f = [f for f in fs_t if any(_finding_covers(f, a) for a in exp_t)]
        fp_f = [f for f in fs_t if f not in tp_f]
        planted_amt = sum(a["amount"] for a in exp_t)
        matched_planted_amt = sum(a["amount"] for a in exp_t if any(_finding_covers(f, a) for f in tp_f))
        found_tp_amt = sum(f["amount"] for f in tp_f)
        by_type[t] = {
            "label": det.TYPE_LABELS.get(t, t),
            "expected": len(exp_t), "detected": len(detected), "findings": len(fs_t),
            "true_positive_findings": len(tp_f), "false_positive_findings": len(fp_f),
            **_prf(len(tp_f), len(fs_t), len(detected), len(exp_t)),
            "planted_amount": round(planted_amt, 2),
            "found_amount": round(sum(f["amount"] for f in fs_t), 2),
            "dollar_recall": _r(min(found_tp_amt / planted_amt, 1.0)) if planted_amt else None,
            # How close the amounts of correct findings are to the planted amounts they cover.
            "amount_error_pct": _r((found_tp_amt - matched_planted_amt) / matched_planted_amt * 100, 2)
            if matched_planted_amt else None,
            "missed": [{"id": a["id"], "label": _label(a), "amount": a["amount"], "needs_llm": a["needs_llm"]}
                       for a in missed],
            "false_positives": [{"id": f["id"], "title": f["title"], "amount": f["amount"]} for f in fp_f],
        }
        for k in ("expected", "detected", "findings", "true_positive_findings"):
            tot[k] += by_type[t][k]
        tot["planted_amount"] += planted_amt
        tot["found_tp_amount"] += found_tp_amt
        tot["found_amount"] += by_type[t]["found_amount"]

    decoys = []
    for d in gt.get("decoys") or []:
        reported = any(f["type"] == d["type"] and
                       sorted((f.get("evidence") or {}).get("vendor_ids") or []) == sorted(d["vendor_ids"])
                       for f in findings)
        decoys.append({"label": " / ".join(d["vendors"]), "reported": reported})

    overall = {
        "expected": int(tot["expected"]), "detected": int(tot["detected"]), "findings": int(tot["findings"]),
        **_prf(int(tot["true_positive_findings"]), int(tot["findings"]), int(tot["detected"]), int(tot["expected"])),
        "planted_amount": round(tot["planted_amount"], 2),
        "found_amount": round(tot["found_amount"], 2),
        "dollar_recall": _r(min(tot["found_tp_amount"] / tot["planted_amount"], 1.0)) if tot["planted_amount"] else None,
        "missed_needing_llm": sum(1 for v in by_type.values() for m in v["missed"] if m["needs_llm"]),
        "decoys_reported": sum(1 for d in decoys if d["reported"]),
    }
    return {"overall": overall, "by_type": by_type, "decoys": decoys, "notes": notes}


def extraction_metrics(rate_cards: dict[str, list[dict]] | None, gt: dict) -> dict | None:
    """LLM-extracted price rows vs. the true price tables of the text-only contracts."""
    truth = gt.get("text_contract_rates")
    if not truth:
        return None
    rate_cards = rate_cards or {}
    llm_cards = {cid: [r for r in rows if r.get("source") == "llm"] for cid, rows in rate_cards.items()}
    llm_cards = {cid: rows for cid, rows in llm_cards.items() if rows}
    if not llm_cards:
        return {"attempted": False, "contracts": len(truth)}

    def key(r: dict) -> tuple[str, int, float]:
        return (r["sku"], int(r["min_qty"]), round(float(r["unit_price"]), 2))

    per_contract: list[dict] = []
    tp = fp = fn = tier_tp = tier_total = 0
    for cid, rows in sorted(truth.items()):
        true = {key(r) for r in rows}
        got = {key(r) for r in llm_cards.get(cid, [])}
        c_tp = len(true & got)
        tp, fp, fn = tp + c_tp, fp + len(got - true), fn + len(true - got)
        tiers = {k for k in true if k[1] > 0}
        tier_tp += len(tiers & got)
        tier_total += len(tiers)
        per_contract.append({"contract_id": cid, "true_rows": len(true), "extracted_rows": len(got),
                             "correct_rows": c_tp,
                             "missing": [{"sku": k[0], "min_qty": k[1], "unit_price": k[2]} for k in sorted(true - got)],
                             "wrong": [{"sku": k[0], "min_qty": k[1], "unit_price": k[2]} for k in sorted(got - true)]})
    return {
        "attempted": True, "contracts": len(truth),
        "contracts_extracted": sum(1 for c in per_contract if c["extracted_rows"]),
        "contracts_exact": sum(1 for c in per_contract if c["correct_rows"] == c["true_rows"] == c["extracted_rows"]),
        **_prf(tp, tp + fp, tp, tp + fn),
        "tier_recall": _r(tier_tp / tier_total) if tier_total else None,
        "rows": {"true": tp + fn, "extracted": tp + fp, "correct": tp},
        "per_contract": per_contract,
    }


def explanation_metrics(findings: list[dict]) -> dict:
    """Faithfulness / relevancy of the LLM explanations that were shown, plus how many the output
    guardrail (G6) blocked. Uses the same checks as the guardrail (backend/guardrails.py)."""
    blocked = [{"id": f["id"], "title": f["title"], "reason": f["explanation_blocked"]}
               for f in findings if f.get("explanation_blocked")]
    llm = [f for f in findings if f.get("explanation_source") == "llm" and f.get("explanation")]
    if not llm and not blocked:
        return {"evaluated": 0, "source": "template" if findings else None}
    unfaithful, irrelevant = [], []
    for f in llm:
        extra = unsupported_numbers(f["explanation"], f)
        if extra:
            unfaithful.append({"id": f["id"], "title": f["title"], "unsupported_numbers": extra[:5]})
        problem = relevancy_problem(f["explanation"], f)
        if problem:
            irrelevant.append({"id": f["id"], "title": f["title"], "reason": problem})
    n = len(llm)
    attempted = n + len(blocked)
    return {
        "evaluated": n, "source": "llm",
        "faithfulness": _r(1 - len(unfaithful) / n) if n else None,
        "relevancy": _r(1 - len(irrelevant) / n) if n else None,
        # Share of the model's explanations that passed the guardrail on the first try.
        "pass_rate": _r(n / attempted) if attempted else None,
        "blocked_by_guardrail": len(blocked),
        "avg_words": round(sum(len(f["explanation"].split()) for f in llm) / n, 1) if n else None,
        "unfaithful": unfaithful[:20], "irrelevant": irrelevant[:20], "blocked": blocked[:20],
    }


def evaluate(findings: list[dict], gt: dict, params: dict, rate_cards: dict | None = None,
             invoices: list[dict] | None = None) -> dict:
    return {
        "ground_truth": {"seed": gt.get("seed"), "planted": len(gt["anomalies"])},
        "params": {k: params.get(k) for k in ("date_from", "date_to", "price_tolerance_pct")},
        "detection": detection_metrics(findings, gt, params, invoices),
        "extraction": extraction_metrics(rate_cards, gt),
        "explanations": explanation_metrics(findings),
    }
