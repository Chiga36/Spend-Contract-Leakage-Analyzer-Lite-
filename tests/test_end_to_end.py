"""End to end (LLD §14): seed the data, run the graph with LLM_MODE=off over the real stdio MCP
server, auto-approve at the interrupt, then check that every planted anomaly with
needs_llm=false is found and that the totals match the ground truth within 1%."""
from __future__ import annotations

import asyncio
import csv
from collections import defaultdict
from contextlib import AsyncExitStack

import pytest
from langgraph.types import Command

from backend.graph import detectors as det
from backend.graph.build import build_graph
from backend.graph.nodes import Nodes
from backend.llm import Llm
from backend.mcp_client import McpClient

RUN_ID = "e2e0000000ab"


async def _run(dataset, output_dir, tolerance=2.0):
    settings = dataset.settings(output_dir)
    async with AsyncExitStack() as stack:
        mcp = McpClient(settings)
        await mcp.start(stack)
        graph = build_graph(Nodes(mcp, Llm(settings), settings))
        cfg = {"configurable": {"thread_id": RUN_ID}}
        dr = dataset.ground_truth["date_range"]
        events = []
        inp = {"run_id": RUN_ID, "iteration": 0,
               "params": {"date_from": dr["from"], "date_to": dr["to"], "price_tolerance_pct": tolerance}}
        async for mode, chunk in graph.astream(inp, cfg, stream_mode=["updates", "custom"]):
            events.append((mode, chunk))
        paused = await graph.aget_state(cfg)
        findings = paused.values["findings"]
        decisions = {f["id"]: {"status": "APPROVED"} for f in findings}
        async for _ in graph.astream(Command(resume={"action": "approve", "decisions": decisions, "new_params": None}),
                                     cfg, stream_mode="updates"):
            pass
        final = await graph.aget_state(cfg)
        return paused, findings, final, events


@pytest.fixture(scope="module")
def offline_run(dataset, tmp_path_factory):
    out = tmp_path_factory.mktemp("output")
    paused, findings, final, events = asyncio.run(_run(dataset, out))
    return {"paused": paused, "findings": findings, "final": final, "events": events, "out": out}


def _by_type(findings):
    groups = defaultdict(list)
    for f in findings:
        groups[f["type"]].append(f)
    return groups


def _is_found(anomaly, groups) -> bool:
    t = anomaly["type"]
    fs = groups.get(t, [])
    if t == det.DUPLICATE_VENDOR:
        return any(sorted(f["evidence"]["vendor_ids"]) == sorted(anomaly["vendor_ids"]) for f in fs)
    if t == det.DUPLICATE_PAYMENT:
        return any(f["evidence"]["duplicate"]["invoice_id"] == anomaly["duplicate_invoice_id"] for f in fs)
    if t == det.PRICE_VARIANCE:
        return any(anomaly["line_id"] in f["evidence"]["line_ids"] for f in fs)
    if t == det.VOLUME_DISCOUNT:
        found = {lid for f in fs for lid in f["evidence"]["line_ids"]}
        return set(anomaly["line_ids"]) <= found
    if t == det.EARLY_PAY_DISCOUNT:
        return any(anomaly["invoice_id"] in f["evidence"]["invoice_ids"] for f in fs)
    raise AssertionError(t)


def test_graph_pauses_for_review(offline_run):
    assert offline_run["paused"].next == ("human_review",)
    assert offline_run["findings"], "the agent found nothing"


def test_every_offline_anomaly_is_found(dataset, offline_run):
    groups = _by_type(offline_run["findings"])
    missing = [a for a in dataset.ground_truth["anomalies"] if not a["needs_llm"] and not _is_found(a, groups)]
    assert not missing, f"{len(missing)} planted anomalies not found, e.g. {missing[:3]}"


def test_llm_only_anomalies_are_not_found_offline(dataset, offline_run):
    """The visible 'value of the LLM' moment: these need contract-text extraction or a name judgement."""
    groups = _by_type(offline_run["findings"])
    llm_only = [a for a in dataset.ground_truth["anomalies"] if a["needs_llm"]]
    assert llm_only and not any(_is_found(a, groups) for a in llm_only)


def test_decoy_vendor_pair_not_reported(dataset, offline_run):
    pairs = {tuple(sorted(f["evidence"]["vendor_ids"])) for f in _by_type(offline_run["findings"])[det.DUPLICATE_VENDOR]}
    for decoy in dataset.ground_truth["decoys"]:
        assert tuple(sorted(decoy["vendor_ids"])) not in pairs


def test_totals_match_ground_truth_within_1pct(dataset, offline_run):
    expected = {t: v["offline_amount"] for t, v in dataset.ground_truth["totals_by_type"].items()}
    actual = defaultdict(float)
    for f in offline_run["findings"]:
        actual[f["type"]] += f["amount"]
    for t, exp in expected.items():
        assert actual[t] == pytest.approx(exp, rel=0.01), f"{t}: found {actual[t]:,.2f}, planted {exp:,.2f}"
    assert sum(actual.values()) == pytest.approx(sum(expected.values()), rel=0.01)


def test_findings_are_ranked_and_complete(offline_run):
    fs = offline_run["findings"]
    assert [f["id"] for f in fs] == [f"F-{i:03d}" for i in range(1, len(fs) + 1)]
    assert [f["score"] for f in fs] == sorted((f["score"] for f in fs), reverse=True)
    for f in fs:
        assert f["severity"] == det.severity_for(f["amount"])
        assert f["calculation"] and f["explanation"] and f["explanation_source"] == "template"
        assert {"invoice_ids", "line_ids", "payment_ids"} <= set(f["evidence"])


def test_events_show_local_mcp_calls(offline_run):
    custom = [c for m, c in offline_run["events"] if m == "custom"]
    tools = [e for e in custom if e["type"] == "tool_call"]
    assert {e["tool"] for e in tools} >= {"list_vendors", "list_contracts", "get_invoices", "get_payments",
                                          "get_contract_rates"}
    assert "read_contract" not in {e["tool"] for e in tools}       # LLM off: contract text is never read
    assert all(e["transport"] == "stdio" and e["pid"] and e["ok"] for e in tools)
    assert not [e for e in custom if e["type"] == "llm_call"]
    warnings = " ".join(e["message"] for e in custom if e["type"] == "warning")
    assert "LLM_MODE=off" in warnings
    started = {e["node"] for e in custom if e["type"] == "node_started"}   # first pass, up to the interrupt
    assert started == {"load_data", "extract_rates", "dup_vendors", "dup_payments", "price_variance",
                       "volume_discount", "early_pay", "aggregate", "explain", "human_review"}
    assert any(m == "updates" and "__interrupt__" in c for m, c in offline_run["events"])


def test_report_and_csv_written(offline_run):
    final = offline_run["final"]
    report = final.values["report"]
    assert final.next == ()
    assert report["approved_count"] == len(offline_run["findings"])
    assert report["approved_total"] == pytest.approx(sum(f["amount"] for f in offline_run["findings"]), abs=0.01)
    html = (offline_run["out"] / RUN_ID / "report.html").read_text(encoding="utf-8")
    assert "Spend &amp; Contract Leakage Report" in html and "F-001" in html and "$" in html
    with (offline_run["out"] / RUN_ID / "findings.csv").open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == report["approved_count"]
    assert {r["status"] for r in rows} == {"APPROVED"}


def test_rerun_with_higher_tolerance_drops_small_price_variances(dataset, tmp_path):
    """Planted 3.5% / 4% uplifts disappear at 5% tolerance; volume findings do not."""
    _, findings, _, _ = asyncio.run(_run(dataset, tmp_path, tolerance=5.0))
    groups = _by_type(findings)
    small = [a for a in dataset.ground_truth["anomalies"]
             if a["type"] == det.PRICE_VARIANCE and not a["needs_llm"] and a["uplift_pct"] < 5]
    large = [a for a in dataset.ground_truth["anomalies"]
             if a["type"] == det.PRICE_VARIANCE and not a["needs_llm"] and a["uplift_pct"] > 5]
    assert small and not any(_is_found(a, groups) for a in small)
    assert all(_is_found(a, groups) for a in large)
    assert all(_is_found(a, groups) for a in dataset.ground_truth["anomalies"]
               if a["type"] == det.VOLUME_DISCOUNT and not a["needs_llm"])
