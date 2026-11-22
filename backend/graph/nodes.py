"""Graph nodes (LLD §7.3): load_data, extract_rates, 5 detectors, aggregate, explain,
human_review, make_report."""
from __future__ import annotations

import asyncio
import functools
import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Callable

from langgraph.types import interrupt

from .. import guardrails as gr
from ..config import Settings
from ..llm import Llm
from ..mcp_client import McpClient
from ..models import PriceRow, ReviewPayload
from ..report.render import render_report
from . import detectors as det
from .state import RunState, emit

log = logging.getLogger(__name__)

MAX_ITERATIONS = 3
EXPLAIN_BATCH = 10
VENDOR_PAIR_BATCH = 25
REVIEWED = ("APPROVED", "ADJUSTED")


def node(name: str) -> Callable:
    """Emit ``node_started`` when a node begins. ``node_done`` comes from the "updates" stream."""
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(self: "Nodes", state: RunState) -> Any:
            emit({"type": "node_started", "node": name})
            return await fn(self, state)
        return wrapper
    return decorator


def route_after_review(state: RunState) -> str:
    return "rerun" if state.get("review_action") == "rerun" else "approve"


class Nodes:
    def __init__(self, mcp: McpClient, llm: Llm, settings: Settings):
        self.mcp, self.llm, self.settings = mcp, llm, settings

    # ------------------------------------------------------------------ load_data
    @node("load_data")
    async def load_data(self, state: RunState) -> dict:
        iteration = int(state.get("iteration") or 0) + 1
        if iteration > MAX_ITERATIONS:
            raise RuntimeError(f"Re-run limit reached: a run can be analyzed at most {MAX_ITERATIONS} times.")
        params = dict(state.get("params") or {})
        params.setdefault("price_tolerance_pct", self.settings.price_tolerance_pct)
        # Invoices from 1 January of the start year are loaded as history, so calendar-year
        # volume tiers and earlier duplicates are judged correctly even for a narrow window.
        history_from = date.fromisoformat(params["date_from"]).replace(month=1, day=1).isoformat()
        ds = {"dataset": params.get("dataset") or "demo"}
        window = {"date_from": history_from, "date_to": params["date_to"], **ds}
        vendors, contracts, invoices, payments = await asyncio.gather(
            self.mcp.call_tool("list_vendors", ds),
            self.mcp.call_tool("list_contracts", ds),
            self.mcp.call_tool("get_invoices", window),
            self.mcp.call_tool("get_payments", window),
        )
        return {"params": params, "iteration": iteration, "vendors": vendors, "contracts": contracts,
                "invoices": invoices, "payments": payments}

    # ------------------------------------------------------------------ extract_rates
    @node("extract_rates")
    async def extract_rates(self, state: RunState) -> dict:
        billed = gr.billed_prices(state.get("invoices") or [])      # for guardrail G5
        skus_by_vendor: dict[str, set[str]] = defaultdict(set)
        for inv in state.get("invoices") or []:
            for ln in inv.get("lines") or []:
                if ln.get("sku"):
                    skus_by_vendor[inv["vendor_id"]].add(ln["sku"])

        dataset = (state.get("params") or {}).get("dataset") or "demo"

        async def one(contract: dict) -> tuple[str, list[dict], str]:
            cid = contract["contract_id"]
            rows = await self.mcp.call_tool("get_contract_rates", {"contract_id": cid, "dataset": dataset})
            if rows:
                return cid, [db_tier(cid, r) for r in rows], "db"
            if not self.llm.enabled:
                return cid, [], "skipped"
            if not contract.get("document_file"):
                return cid, [], "no_document"
            text = await self.mcp.call_tool("read_contract", {"contract_id": cid, "dataset": dataset})
            skus = sorted(skus_by_vendor.get(contract["vendor_id"], ()))
            extracted = await self.llm.extract_price_table(cid, text, skus, run_id=state.get("run_id", "default"))
            tiers = validate_extracted(extracted or [], set(skus), text)
            tiers, rejected = gr.check_price_rows(tiers, contract["vendor_id"], billed)
            if rejected:
                emit_guardrail("extract_rates", "G5 price sanity",
                               f"Contract {cid}: {len(rejected)} implausible extracted price(s) not used: "
                               + "; ".join(rejected[:5]) + ".")
            return cid, tiers, "llm" if tiers else "llm_failed"

        results = await asyncio.gather(*(one(c) for c in state.get("contracts") or []))
        skipped = [cid for cid, _, how in results if how == "skipped"]
        failed = [cid for cid, _, how in results if how == "llm_failed"]
        no_doc = [cid for cid, _, how in results if how == "no_document"]
        if no_doc:
            emit({"type": "warning", "node": "extract_rates",
                  "message": f"{len(no_doc)} contract(s) have neither a price table nor a document, "
                             f"so their prices were not checked: {', '.join(no_doc)}."})
        if skipped:
            emit({"type": "warning", "node": "extract_rates",
                  "message": f"{len(skipped)} contract(s) have no structured price table and LLM_MODE=off, "
                             f"so their prices were not checked: {', '.join(skipped)}."})
        if failed:
            emit({"type": "warning", "node": "extract_rates",
                  "message": f"No valid price table could be extracted for {len(failed)} contract(s), "
                             f"so their prices were not checked: {', '.join(failed)}."})
        return {"rate_cards": {cid: tiers for cid, tiers, _ in results if tiers}}

    # ------------------------------------------------------------------ the five detectors
    async def _detector(self, name: str, state: RunState, fn: Callable[..., Any], *args: Any) -> dict:
        """Run one detector. If it raises, its result is an empty list plus a warning (LLD §13)."""
        try:
            findings = await asyncio.to_thread(fn, state, *args)   # pure CPU work, off the event loop
        except Exception as e:  # noqa: BLE001
            log.exception("Detector %s failed", name)
            emit({"type": "warning", "node": name,
                  "message": f"Detector {name} failed and was skipped: {type(e).__name__}: {e}"})
            findings = []
        for f in findings:
            emit({"type": "finding", "node": name,
                  "finding": {k: f[k] for k in ("type", "vendor", "title", "amount", "confidence")}})
        return {"detector_results": {name: findings}}

    @node("dup_vendors")
    async def dup_vendors(self, state: RunState) -> dict:
        confirmed: list[dict] = []
        try:
            _, borderline = det.vendor_pair_candidates(state.get("vendors") or [])
            if borderline and not self.llm.enabled:
                emit({"type": "warning", "node": "dup_vendors",
                      "message": f"{len(borderline)} borderline vendor-name pair(s) (80–90% similar) were "
                                 f"skipped because LLM_MODE=off."})
            elif borderline:
                for i in range(0, len(borderline), VENDOR_PAIR_BATCH):
                    batch = borderline[i:i + VENDOR_PAIR_BATCH]
                    verdicts = await self.llm.match_vendor_pairs(batch, run_id=state.get("run_id", "default"))
                    if verdicts is None:
                        emit({"type": "warning", "node": "dup_vendors",
                              "message": f"LLM unavailable: {len(batch)} borderline vendor pair(s) skipped."})
                        continue
                    for p in batch:
                        same, reason = verdicts.get(p["pair_id"], (False, ""))
                        if same:
                            confirmed.append({**p, "llm_reason": reason})
        except Exception as e:  # noqa: BLE001 - the deterministic matches still run below
            log.exception("Borderline vendor matching failed")
            emit({"type": "warning", "node": "dup_vendors", "message": f"Borderline vendor matching failed: {e}"})
        return await self._detector("dup_vendors", state, det.detect_duplicate_vendors, confirmed)

    @node("dup_payments")
    async def dup_payments(self, state: RunState) -> dict:
        return await self._detector("dup_payments", state, det.detect_duplicate_payments)

    @node("price_variance")
    async def price_variance(self, state: RunState) -> dict:
        return await self._detector("price_variance", state, det.detect_price_variance)

    @node("volume_discount")
    async def volume_discount(self, state: RunState) -> dict:
        return await self._detector("volume_discount", state, det.detect_missed_volume_discount)

    @node("early_pay")
    async def early_pay(self, state: RunState) -> dict:
        return await self._detector("early_pay", state, det.detect_missed_early_pay)

    # ------------------------------------------------------------------ aggregate
    @node("aggregate")
    async def aggregate(self, state: RunState) -> dict:
        findings, summary = det.aggregate_findings(state.get("detector_results") or {})
        return {"findings": findings, "summary": summary}

    # ------------------------------------------------------------------ explain
    @node("explain")
    async def explain(self, state: RunState) -> dict:
        findings = [dict(f) for f in state.get("findings") or []]
        texts: dict[str, str] = {}
        if self.llm.enabled and findings:
            run_id = state.get("run_id", "default")
            batches = [findings[i:i + EXPLAIN_BATCH] for i in range(0, len(findings), EXPLAIN_BATCH)]
            for result in await asyncio.gather(*(self.llm.explain_findings(b, run_id=run_id) for b in batches)):
                texts.update(result or {})
        blocked = 0
        for f in findings:
            text = texts.get(f["id"])
            f.pop("explanation_blocked", None)
            if text:
                problem = gr.check_explanation(text, f)        # output guardrail G6
                if problem:
                    blocked += 1
                    f["explanation_blocked"] = problem
                    emit_guardrail("explain", "G6 explanation",
                                   f"{f['id']} ({f['title']}): LLM explanation not used, {problem}.")
                    text = None
            f["explanation"] = text or template_explanation(f)
            f["explanation_source"] = "llm" if text else "template"
        if blocked:
            log.info("Guardrail G6 replaced %d LLM explanation(s) with the template", blocked)
        return {"findings": findings}

    # ------------------------------------------------------------------ human_review
    @node("human_review")
    async def human_review(self, state: RunState) -> dict:
        decision = interrupt({
            "findings": state.get("findings") or [],
            "summary": state.get("summary") or {},
            "iteration": state.get("iteration", 1),
            "params": state.get("params") or {},
        })
        payload = ReviewPayload.model_validate(decision)
        if payload.action == "rerun":
            new = payload.new_params.model_dump(mode="json", exclude_none=True) if payload.new_params else {}
            new.pop("dataset", None)                     # a re-run always uses the same data
            return {"params": {**(state.get("params") or {}), **new}, "review_action": "rerun", "decisions": None}
        decisions = {fid: d.model_dump(exclude_none=True) for fid, d in payload.decisions.items()}
        return {"findings": apply_decisions(state.get("findings") or [], decisions),
                "decisions": decisions, "review_action": "approve"}

    # ------------------------------------------------------------------ make_report
    @node("make_report")
    async def make_report(self, state: RunState) -> dict:
        report = await asyncio.to_thread(render_report, state, self.settings.output_dir, self.settings.llm_mode)
        run_id = state["run_id"]
        report.update(html_url=f"/api/runs/{run_id}/report.html", csv_url=f"/api/runs/{run_id}/findings.csv")
        emit({"type": "report_ready", **{k: v for k, v in report.items() if k not in ("html_path", "csv_path")}})
        return {"report": report}


# ---------------------------------------------------------------------------- helpers
def emit_guardrail(node_name: str, rule: str, message: str) -> None:
    log.warning("Guardrail %s: %s", rule, message)
    emit({"type": "guardrail", "node": node_name, "rule": rule, "message": message})


def db_tier(contract_id: str, row: dict) -> dict:
    price, min_qty = Decimal(str(row["unit_price"])), int(row.get("min_qty") or 0)
    where = f"from {min_qty:,} units per calendar year" if min_qty else "base price"
    return {"sku": row["sku"], "unit_price": float(price), "min_qty": min_qty, "source": "db",
            "clause": f"Rate table {contract_id}: {row['sku']} at ${price:,.2f} per unit ({where})"}


def validate_extracted(rows: list[PriceRow], allowed_skus: set[str], contract_text: str) -> list[dict]:
    """Keep only LLM-extracted prices that are safe to use (LLD §9 prompt-injection guard):
    price > 0, the SKU appears on this vendor's invoices, and the price (and tier quantity) is
    literally written in the contract text."""
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for r in rows:
        sku = r.sku.strip()
        price = det.cents(Decimal(str(r.unit_price)))
        if sku not in allowed_skus or price <= 0 or r.min_qty < 0:
            continue
        if f"{price:,.2f}" not in contract_text and f"{price:.2f}" not in contract_text:
            continue
        if r.min_qty and f"{r.min_qty:,}" not in contract_text and str(r.min_qty) not in contract_text:
            continue
        if (sku, r.min_qty) in seen:
            continue
        seen.add((sku, r.min_qty))
        out.append({"sku": sku, "unit_price": float(price), "min_qty": int(r.min_qty), "source": "llm",
                    "clause": " ".join(r.clause.split())[:300]})
    return out


def apply_decisions(findings: list[dict], decisions: dict[str, dict]) -> list[dict]:
    """Apply approve / reject / adjust. Findings with no decision stay undecided (left out of the report)."""
    out = []
    for f in findings:
        f = dict(f)
        d = decisions.get(f["id"])
        f["status"] = d["status"] if d else None
        f["note"] = (d or {}).get("note")
        if not d:
            f["final_amount"] = None
        elif d["status"] == "ADJUSTED":
            f["final_amount"] = round(float(d["amount"]), 2)
        elif d["status"] == "APPROVED":
            f["final_amount"] = f["amount"]
        else:
            f["final_amount"] = 0.0
        out.append(f)
    return out


def template_explanation(f: dict) -> str:
    """Offline explanation: one fixed sentence per finding type (LLD §7.3 explain)."""
    ev = f.get("evidence") or {}
    amount = det.fmt_money(f["amount"])
    vendor = f["vendor"]
    t = f["type"]
    if t == det.DUPLICATE_PAYMENT:
        orig, dup = ev.get("original", {}), ev.get("duplicate", {})
        how = ("the same invoice number" if ev.get("matched_by") == "invoice_no"
               else f"the same amount {ev.get('days_apart')} days apart")
        return (f"{vendor} was paid twice: invoice {dup.get('vendor_invoice_no')} ({dup.get('invoice_id')}) repeats "
                f"{orig.get('vendor_invoice_no')} ({orig.get('invoice_id')}) with {how}. "
                f"The later payment of {amount} should be recovered.")
    if t == det.PRICE_VARIANCE:
        n = len(ev.get("lines") or [])
        return (f"{vendor} billed {ev.get('sku')} above the rate in contract {ev.get('contract_id')} on {n} "
                f"invoice line{'s' if n != 1 else ''}, beyond the {ev.get('tolerance_pct', 2):g}% tolerance. "
                f"The overcharge is {amount}.")
    if t == det.VOLUME_DISCOUNT:
        return (f"{vendor} kept billing {ev.get('sku')} at the lower-volume price after this year's purchases "
                f"passed the {ev.get('tier_min_qty', 0):,}-unit tier in contract {ev.get('contract_id')}. "
                f"The discount not applied is {amount}.")
    if t == det.EARLY_PAY_DISCOUNT:
        return (f"Invoice {ev.get('vendor_invoice_no')} from {vendor} was paid after {ev.get('days_to_pay')} days, "
                f"outside the {ev.get('discount_days')}-day window of the {ev.get('payment_terms')} terms, "
                f"so the {amount} early-payment discount was lost.")
    if t == det.DUPLICATE_VENDOR:
        names = " and ".join(v["name"] for v in ev.get("vendors", []))
        return (f"{names} appear to be the same supplier ({ev.get('reason', '').rstrip('.')}). Split records "
                f"weaken negotiating leverage and allow duplicate payments; the estimated cost is {amount}.")
    return f"{det.TYPE_LABELS.get(t, t)} at {vendor}: {amount}."
