"""Graph state (LLD §7.2) and the helper that sends live events to the run's stream."""
from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.config import get_stream_writer


def merge_detector_results(a: dict | None, b: dict | None) -> dict:
    """Each detector writes only its own key, so parallel writes never collide and a re-run
    simply overwrites the previous results."""
    return {**(a or {}), **(b or {})}


class RunState(TypedDict, total=False):
    run_id: str
    params: dict                       # date_from, date_to, price_tolerance_pct
    vendors: list[dict]
    contracts: list[dict]
    invoices: list[dict]               # each with its "lines"
    payments: list[dict]
    rate_cards: dict[str, list[dict]]  # contract_id -> tiers {sku, unit_price, min_qty, source, clause}
    detector_results: Annotated[dict[str, list[dict]], merge_detector_results]
    findings: list[dict]               # ranked, set by aggregate (+ explanation, + review status)
    summary: dict
    decisions: dict | None
    review_action: str | None          # "approve" | "rerun"
    iteration: int
    report: dict | None


def emit(event: dict[str, Any]) -> None:
    """Send a custom event (tool_call, llm_call, finding, warning, ...) to the live stream.

    Works anywhere inside a graph run, including tasks started with asyncio.gather. Outside a
    run (for example when /api/health calls an MCP tool) it does nothing.
    """
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer(event)
