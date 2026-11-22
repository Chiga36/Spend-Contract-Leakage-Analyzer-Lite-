"""LLM_MODE=claude plumbing, with a fake Anthropic client (no network, no cost).

1. A well-behaved fake: the needs_llm anomalies are found too (contract-text price tables,
   borderline vendor names), the decoy pair is rejected, explanations come from the "LLM", and
   every request carries the parameters LLD §9 requires.
2. A failing fake (refusals / connection errors): the run still completes with the offline
   result. A run never fails because of the LLM.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from contextlib import AsyncExitStack
from types import SimpleNamespace

import pytest
from langgraph.types import Command

from backend.graph import detectors as det
from backend.graph.build import build_graph
from backend.graph.nodes import Nodes
from backend.llm import FALLBACK_BETA, SYSTEM_EXPLAIN, SYSTEM_EXTRACT, SYSTEM_VENDORS, Llm
from backend.mcp_client import McpClient
from tests.test_end_to_end import _by_type, _is_found

SKU = re.compile(r"\b[A-Z]{3}-\d{4}\b")
PRICE = re.compile(r"\$([\d,]+\.\d{2})")
QTY = re.compile(r"([\d,]+) units")


def _message(payload: dict | None, stop_reason: str = "end_turn") -> SimpleNamespace:
    content = [SimpleNamespace(type="text", text=json.dumps(payload))] if payload is not None else []
    return SimpleNamespace(stop_reason=stop_reason, stop_details=SimpleNamespace(category="cyber") if stop_reason == "refusal" else None,
                           content=content, model="claude-opus-5-5",
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=200))


class FakeMessages:
    def __init__(self, mode: str):
        self.mode, self.calls = mode, []

    async def create(self, **kw):
        self.calls.append(kw)
        if self.mode == "refuse":
            return _message(None, "refusal")
        if self.mode == "error":
            raise ConnectionError("outbound HTTPS blocked by proxy")
        user = kw["messages"][0]["content"]
        if kw["system"] == SYSTEM_EXTRACT:
            text = user.split("<contract>")[1].split("</contract>")[0]
            rows = []
            for line in text.splitlines():
                skus, prices = SKU.findall(line), PRICE.findall(line)
                if line.startswith("2.") and skus and prices:
                    qty = QTY.search(line)
                    rows.append({"sku": skus[0], "unit_price": float(prices[-1].replace(",", "")),
                                 "min_qty": int(qty.group(1).replace(",", "")) if qty else 0, "clause": line[4:]})
            rows.append({"sku": "ZZZ-0000", "unit_price": 1.0, "min_qty": 0, "clause": "hallucinated"})  # must be dropped
            return _message({"rows": rows})
        if kw["system"] == SYSTEM_VENDORS:
            pairs = json.loads(user)
            number = lambda a: re.match(r"\d+", a or "").group(0) if re.match(r"\d+", a or "") else None  # noqa: E731
            return _message({"decisions": [{"pair_id": p["pair_id"], "same_company": number(p["a"]["address"]) == number(p["b"]["address"]),
                                            "reason": "same street address" } for p in pairs]})
        if kw["system"] == SYSTEM_EXPLAIN:
            items = json.loads(user)
            return _message({"items": [{"id": f["id"], "explanation": f"LLM: {f['vendor']}: {f['title']} " + "word " * 80}
                                       for f in items]})
        raise AssertionError("unexpected system prompt")


async def _run(dataset, out, mode):
    settings = dataset.settings(out, llm_mode="claude")
    llm = Llm(settings)
    fake = FakeMessages(mode)
    llm._client = SimpleNamespace(beta=SimpleNamespace(messages=fake))
    async with AsyncExitStack() as stack:
        mcp = McpClient(settings)
        await mcp.start(stack)
        graph = build_graph(Nodes(mcp, llm, settings))
        cfg = {"configurable": {"thread_id": "llm"}}
        dr = dataset.ground_truth["date_range"]
        events = []
        inp = {"run_id": f"llm{mode}", "iteration": 0,
               "params": {"date_from": dr["from"], "date_to": dr["to"], "price_tolerance_pct": 2.0}}
        async for mode_, chunk in graph.astream(inp, cfg, stream_mode=["updates", "custom"]):
            events.append((mode_, chunk))
        findings = (await graph.aget_state(cfg)).values["findings"]
        async for _ in graph.astream(Command(resume={"action": "approve", "decisions": {}}), cfg):
            pass
        return findings, [c for m, c in events if m == "custom"], fake.calls


def test_llm_finds_what_offline_cannot(dataset, tmp_path):
    findings, events, calls = asyncio.run(_run(dataset, tmp_path, "ok"))
    groups = _by_type(findings)
    missing = [a for a in dataset.ground_truth["anomalies"] if not _is_found(a, groups)]
    assert not missing, f"not found with the LLM: {missing[:3]}"
    expected = {t: v["amount"] for t, v in dataset.ground_truth["totals_by_type"].items()}
    actual = defaultdict(float)
    for f in findings:
        actual[f["type"]] += f["amount"]
    for t, exp in expected.items():
        assert actual[t] == pytest.approx(exp, rel=0.01), t

    decoy = tuple(sorted(dataset.ground_truth["decoys"][0]["vendor_ids"]))
    assert decoy not in {tuple(sorted(f["evidence"]["vendor_ids"])) for f in groups[det.DUPLICATE_VENDOR]}
    llm_vendor = [f for f in groups[det.DUPLICATE_VENDOR] if f["evidence"]["matched_by"] == "llm"]
    assert len(llm_vendor) == 2 and all(f["confidence"] == 0.75 for f in llm_vendor)
    llm_priced = [f for f in groups[det.PRICE_VARIANCE] if f["evidence"]["rate_source"] == "llm"]
    assert llm_priced and all(f["confidence"] == 0.75 for f in llm_priced)
    assert all(f["explanation_source"] == "llm" and len(f["explanation"].split()) <= 61 for f in findings)

    # Every request: structured JSON output, effort per use, server-side refusal fallbacks.
    efforts = {SYSTEM_EXTRACT: "high", SYSTEM_VENDORS: "low", SYSTEM_EXPLAIN: "medium"}
    for kw in calls:
        assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default" and kw["betas"] == [FALLBACK_BETA]
        assert kw["output_config"]["effort"] == efforts[kw["system"]]
        assert kw["output_config"]["format"]["type"] == "json_schema"
        assert "thinking" not in kw and "temperature" not in kw
    extract_calls = [kw for kw in calls if kw["system"] == SYSTEM_EXTRACT]
    assert len(extract_calls) == dataset.ground_truth["counts"]["contracts_without_rate_table"]
    assert all("<contract>" in kw["messages"][0]["content"] for kw in extract_calls)
    assert len([kw for kw in calls if kw["system"] == SYSTEM_EXPLAIN]) == -(-len(findings) // 10)

    tools = [e["tool"] for e in events if e["type"] == "tool_call"]
    assert tools.count("read_contract") == dataset.ground_truth["counts"]["contracts_without_rate_table"]
    assert all(e["outcome"] == "ok" for e in events if e["type"] == "llm_call")


@pytest.mark.parametrize("mode", ["refuse", "error"])
def test_llm_failure_falls_back_to_offline(dataset, tmp_path, mode):
    findings, events, calls = asyncio.run(_run(dataset, tmp_path, mode))
    assert calls, "the LLM should have been tried"
    llm_events = [e for e in events if e["type"] == "llm_call"]
    assert llm_events and all(e["outcome"] == ("refusal" if mode == "refuse" else "error") for e in llm_events)
    expected = sum(v["offline_amount"] for v in dataset.ground_truth["totals_by_type"].values())
    assert sum(f["amount"] for f in findings) == pytest.approx(expected, rel=0.01)
    assert all(f["explanation_source"] == "template" for f in findings)
    warnings = " ".join(e["message"] for e in events if e["type"] == "warning")
    assert "No valid price table" in warnings and "LLM unavailable" in warnings


# ---------------------------------------------------------------- LLM_MODE=openai
class FakeCompletions:
    """Answers like OpenAI's chat.completions.parse, reusing the Claude fake's logic."""
    def __init__(self, mode: str):
        self.inner, self.calls = FakeMessages(mode), []

    async def parse(self, **kw):
        self.calls.append(kw)
        system, user = kw["messages"][0]["content"], kw["messages"][1]["content"]
        msg = await self.inner.create(system=system, messages=[{"role": "user", "content": user}])
        refused = msg.stop_reason == "refusal"
        parsed = None if refused else kw["response_format"].model_validate_json(msg.content[0].text)
        message = SimpleNamespace(refusal="I can't help with that." if refused else None, parsed=parsed)
        return SimpleNamespace(model=kw["model"], usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=200),
                               choices=[SimpleNamespace(finish_reason="stop", message=message)])


async def _run_openai(dataset, out, mode, provider="openai"):
    settings = dataset.settings(out, llm_mode=provider)
    llm = Llm(settings)
    llm.model = "gpt-5" if provider == "openai" else "gemini-3.5-flash"
    fake = FakeCompletions(mode)
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    async with AsyncExitStack() as stack:
        mcp = McpClient(settings)
        await mcp.start(stack)
        graph = build_graph(Nodes(mcp, llm, settings))
        cfg = {"configurable": {"thread_id": "openai"}}
        dr = dataset.ground_truth["date_range"]
        events = []
        inp = {"run_id": f"oai{mode}", "iteration": 0,
               "params": {"date_from": dr["from"], "date_to": dr["to"], "price_tolerance_pct": 2.0}}
        async for mode_, chunk in graph.astream(inp, cfg, stream_mode=["updates", "custom"]):
            events.append((mode_, chunk))
        findings = (await graph.aget_state(cfg)).values["findings"]
        return findings, [c for m, c in events if m == "custom"], fake.calls


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_openai_compatible_finds_what_offline_cannot(dataset, tmp_path, provider):
    findings, events, calls = asyncio.run(_run_openai(dataset, tmp_path, "ok", provider))
    groups = _by_type(findings)
    missing = [a for a in dataset.ground_truth["anomalies"] if not _is_found(a, groups)]
    assert not missing, f"not found with {provider}: {missing[:3]}"
    efforts = {SYSTEM_EXTRACT: "high", SYSTEM_VENDORS: "low", SYSTEM_EXPLAIN: "medium"}
    for kw in calls:
        if provider == "openai":
            assert kw["model"] == "gpt-5" and kw["store"] is False
            assert kw["reasoning_effort"] == efforts[kw["messages"][0]["content"]]
        else:   # Gemini's compatible endpoint: no OpenAI-only parameters
            assert kw["model"] == "gemini-3.5-flash" and "store" not in kw and "reasoning_effort" not in kw
    assert all(e["outcome"] == "ok" for e in events if e["type"] == "llm_call")


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("mode", ["refuse", "error"])
def test_openai_compatible_failure_falls_back_to_offline(dataset, tmp_path, mode, provider):
    findings, events, calls = asyncio.run(_run_openai(dataset, tmp_path, mode, provider))
    assert calls
    llm_events = [e for e in events if e["type"] == "llm_call"]
    assert llm_events and all(e["outcome"] == ("refusal" if mode == "refuse" else "error") for e in llm_events)
    expected = sum(v["offline_amount"] for v in dataset.ground_truth["totals_by_type"].values())
    assert sum(f["amount"] for f in findings) == pytest.approx(expected, rel=0.01)
