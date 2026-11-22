"""Evaluation metrics (backend/evaluation.py): scored on real runs over the seeded data, plus
hand-made inputs for the LLM-quality checks."""
from __future__ import annotations

import asyncio

import pytest

from backend.evaluation import evaluate, explanation_metrics, extraction_metrics
from backend.graph import detectors as det
from tests import test_end_to_end as e2e
from tests import test_llm_path as llm_path


@pytest.fixture(scope="module")
def offline(dataset, tmp_path_factory):
    paused, findings, _, _ = asyncio.run(e2e._run(dataset, tmp_path_factory.mktemp("eval")))
    return paused.values, findings


def test_offline_run_scores_match_ground_truth(dataset, offline):
    values, findings = offline
    gt = dataset.ground_truth
    m = evaluate(findings, gt, values["params"], values.get("rate_cards"), values.get("invoices"))
    o = m["detection"]["overall"]
    offline_count = sum(v["offline_count"] for v in gt["totals_by_type"].values())
    assert o["expected"] == len(gt["anomalies"])
    assert o["detected"] == offline_count
    assert o["precision"] == 1.0                      # no false positives offline
    assert o["recall"] == pytest.approx(offline_count / len(gt["anomalies"]), abs=1e-4)
    assert o["decoys_reported"] == 0
    # Everything missed offline is exactly what needs the LLM.
    missed = [x for v in m["detection"]["by_type"].values() for x in v["missed"]]
    assert missed and all(x["needs_llm"] for x in missed)
    assert o["missed_needing_llm"] == len(missed)
    for v in m["detection"]["by_type"].values():
        if v["amount_error_pct"] is not None:
            assert abs(v["amount_error_pct"]) < 1.0
    assert m["extraction"] == {"attempted": False, "contracts": gt["counts"]["contracts_without_rate_table"]}
    assert m["explanations"]["evaluated"] == 0


def test_tolerance_and_date_range_change_what_is_expected(dataset, offline):
    values, findings = offline
    gt = dataset.ground_truth
    m = evaluate(findings, gt, {**values["params"], "price_tolerance_pct": 5.0}, invoices=values.get("invoices"))
    pv = m["detection"]["by_type"][det.PRICE_VARIANCE]
    at_or_below_5 = sum(1 for a in gt["anomalies"] if a["type"] == det.PRICE_VARIANCE and a["uplift_pct"] <= 5.0)
    assert at_or_below_5 > 0                         # the 3.5% and 4% uplifts (several lines each)
    assert pv["expected"] == gt["totals_by_type"][det.PRICE_VARIANCE]["count"] - at_or_below_5
    assert any("tolerance" in n for n in m["detection"]["notes"])

    narrow = {**values["params"], "date_from": "2026-01-01"}
    m2 = evaluate(findings, gt, narrow, invoices=values.get("invoices"))
    assert m2["detection"]["overall"]["expected"] < m["detection"]["overall"]["expected"] + 2
    assert any("date range" in n for n in m2["detection"]["notes"])


def test_false_positive_and_decoy_are_counted(dataset, offline):
    values, findings = offline
    gt = dataset.ground_truth
    decoy = gt["decoys"][0]
    fake = {"id": "F-FAKE", "type": det.DUPLICATE_VENDOR, "title": "decoy", "amount": 100.0,
            "vendor": decoy["vendors"][0], "evidence": {"vendor_ids": decoy["vendor_ids"]}}
    m = evaluate(findings + [fake], gt, values["params"], invoices=values.get("invoices"))
    dv = m["detection"]["by_type"][det.DUPLICATE_VENDOR]
    assert dv["false_positive_findings"] == 1 and dv["precision"] < 1.0
    assert m["detection"]["overall"]["decoys_reported"] == 1


def test_llm_run_scores_extraction_and_explanations(dataset, tmp_path):
    findings, _, _ = asyncio.run(llm_path._run(dataset, tmp_path, "ok"))
    gt = dataset.ground_truth
    # The fake LLM extracts every price row literally; the hallucinated row is dropped by validation.
    rate_cards = {}
    for cid, rows in gt["text_contract_rates"].items():
        rate_cards[cid] = [{**r, "source": "llm"} for r in rows]
    ex = extraction_metrics(rate_cards, gt)
    assert ex["precision"] == ex["recall"] == ex["tier_recall"] == 1.0
    assert ex["contracts_exact"] == ex["contracts"]
    m = evaluate(findings, gt, {"date_from": gt["date_range"]["from"], "date_to": gt["date_range"]["to"],
                                "price_tolerance_pct": 2.0})
    assert m["detection"]["overall"]["recall"] == 1.0 and m["detection"]["overall"]["precision"] == 1.0
    assert m["explanations"]["evaluated"] == len(findings)


def test_extraction_metrics_flag_missing_and_wrong_rows(dataset):
    gt = dataset.ground_truth
    cid, rows = next((c, r) for c, r in gt["text_contract_rates"].items() if any(x["min_qty"] for x in r))
    got = [{**r, "source": "llm"} for r in rows if not r["min_qty"]]           # drop the tier rows
    got.append({"sku": rows[0]["sku"], "unit_price": rows[0]["unit_price"] + 1, "min_qty": 0, "source": "llm"})
    ex = extraction_metrics({cid: got}, gt)
    c = next(x for x in ex["per_contract"] if x["contract_id"] == cid)
    assert c["missing"] and c["wrong"]
    assert ex["tier_recall"] < 1.0 and ex["precision"] < 1.0


def test_explanation_faithfulness_and_relevancy():
    base = {"type": det.PRICE_VARIANCE, "vendor": "Kilbride Scientific", "title": "LAB-1115 billed above contract rate",
            "amount": 19729.60, "calculation": "2 lines x ($232.10 - $190.80)", "evidence": {"sku": "LAB-1115"},
            "explanation_source": "llm"}
    good = {**base, "id": "F1", "explanation": "Kilbride Scientific billed LAB-1115 above the contract price, "
                                               "overcharging $19,729.60 across 2 lines."}
    invented = {**base, "id": "F2", "explanation": "Kilbride Scientific billed above contract; that is $25,000 "
                                                   "over 3 years."}
    off_topic = {**base, "id": "F3", "explanation": "This supplier relationship should be reviewed soon."}
    m = explanation_metrics([good, invented, off_topic])
    assert m["evaluated"] == 3
    assert [u["id"] for u in m["unfaithful"]] == ["F2"]
    assert [u["id"] for u in m["irrelevant"]] == ["F3"]
    assert m["faithfulness"] == pytest.approx(2 / 3, abs=1e-3) and m["relevancy"] == pytest.approx(2 / 3, abs=1e-3)
