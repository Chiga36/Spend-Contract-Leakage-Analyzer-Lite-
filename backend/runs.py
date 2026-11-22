"""RunManager: runs the graph as background tasks and keeps every event in memory (LLD §8.2).

Every event stays in ``RunRecord.events``, so a browser refresh replays the whole run.
Event types: node_started, node_done, tool_call, llm_call, finding, warning, review_required,
report_ready, error, status.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from langgraph.types import Command

log = logging.getLogger(__name__)

TERMINAL = frozenset({"COMPLETED", "FAILED"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class RunRecord:
    run_id: str
    params: dict
    created_at: str = field(default_factory=_now)
    status: str = "PENDING"            # PENDING | RUNNING | AWAITING_REVIEW | COMPLETED | FAILED
    iteration: int = 0
    events: list[dict] = field(default_factory=list)
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    review: dict | None = None         # the interrupt payload: findings + summary
    report: dict | None = None
    error: str | None = None

    async def emit(self, event: dict) -> None:
        async with self.cond:
            self.events.append({"seq": len(self.events), "ts": _now(), **event})
            self.cond.notify_all()

    def view(self) -> dict:
        report = {k: v for k, v in (self.report or {}).items() if k not in ("html_path", "csv_path")} or None
        return {"run_id": self.run_id, "status": self.status, "params": self.params, "created_at": self.created_at,
                "iteration": self.iteration, "review": self.review, "report": report, "error": self.error,
                "event_count": len(self.events)}


class RunManager:
    def __init__(self, graph: Any):
        self.graph = graph
        self.runs: dict[str, RunRecord] = {}
        self._tasks: set[asyncio.Task] = set()

    def start(self, params: dict) -> RunRecord:
        rec = RunRecord(run_id=uuid.uuid4().hex[:12], params=params)
        self.runs[rec.run_id] = rec
        self._spawn(self._drive(rec, {"run_id": rec.run_id, "params": params, "iteration": 0}))
        return rec

    def resume(self, rec: RunRecord, payload: dict) -> None:
        rec.status = "RUNNING"            # set before yielding, so a second submit gets 409
        self._spawn(self._drive(rec, Command(resume=payload)))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)             # keep a reference until it finishes
        task.add_done_callback(self._tasks.discard)

    async def _drive(self, rec: RunRecord, inp: Any) -> None:
        cfg = {"configurable": {"thread_id": rec.run_id}}
        rec.status = "RUNNING"
        await rec.emit({"type": "status", "status": rec.status})
        try:
            async for mode, chunk in self.graph.astream(inp, cfg, stream_mode=["updates", "custom"]):
                for event in self._to_events(rec, mode, chunk):
                    await rec.emit(event)
            snap = await self.graph.aget_state(cfg)
            rec.iteration = snap.values.get("iteration", rec.iteration)
            if snap.next:
                rec.status = "AWAITING_REVIEW"
            else:
                rec.status = "COMPLETED"
                rec.report = snap.values.get("report")
        except Exception as e:  # noqa: BLE001
            log.exception("Run %s failed", rec.run_id)
            rec.status = "FAILED"
            rec.error = str(e) or type(e).__name__
            await rec.emit({"type": "error", "message": rec.error})
        await rec.emit({"type": "status", "status": rec.status})

    def _to_events(self, rec: RunRecord, mode: str, chunk: Any) -> list[dict]:
        if mode == "custom":
            return [chunk] if isinstance(chunk, dict) else []
        events = []
        for node, update in (chunk or {}).items():
            if node == "__interrupt__":
                value = update[0].value if update else {}
                rec.review = value
                rec.iteration = value.get("iteration", rec.iteration)
                rec.params = value.get("params") or rec.params      # a re-run may have changed them
                events.append({"type": "review_required", "summary": value.get("summary"),
                               "count": len(value.get("findings") or []), "iteration": rec.iteration})
            else:
                events.append({"type": "node_done", "node": node, "summary": summarize_update(node, update)})
        return events


def summarize_update(node: str, update: Any) -> dict:
    """A small, display-friendly digest of a node's state update (never the raw data)."""
    if not isinstance(update, dict):
        return {}
    if node == "load_data":
        invoices = update.get("invoices") or []
        return {"vendors": len(update.get("vendors") or []), "contracts": len(update.get("contracts") or []),
                "invoices": len(invoices), "invoice_lines": sum(len(i.get("lines") or []) for i in invoices),
                "payments": len(update.get("payments") or []), "iteration": update.get("iteration")}
    if node == "extract_rates":
        cards = update.get("rate_cards") or {}
        sources = Counter(tiers[0].get("source") for tiers in cards.values() if tiers)
        return {"contracts_priced": len(cards), "from_db": sources.get("db", 0), "from_llm": sources.get("llm", 0)}
    if "detector_results" in update:
        found = next(iter(update["detector_results"].values()), [])
        return {"findings": len(found), "amount": round(sum(f["amount"] for f in found), 2)}
    if node == "aggregate":
        return update.get("summary") or {}
    if node == "explain":
        fs = update.get("findings") or []
        return {"explained": len(fs), "by_llm": sum(1 for f in fs if f.get("explanation_source") == "llm")}
    if node == "human_review":
        return {"action": update.get("review_action")}
    if node == "make_report":
        r = update.get("report") or {}
        return {k: r.get(k) for k in ("approved_count", "approved_total")}
    return {}
