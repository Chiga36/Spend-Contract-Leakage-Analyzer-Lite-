"""Graph assembly (LLD §7.1).

    START -> load_data -> extract_rates -> 5 detectors (parallel) -> aggregate -> explain
          -> human_review (interrupt) -> rerun: load_data | approve: make_report -> END

The five detectors hang off plain parallel edges, so LangGraph runs them in the same step and
waits for all of them before ``aggregate``. No ``Send`` API is needed.
"""
from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .nodes import Nodes, route_after_review
from .state import RunState

DETECTOR_NODES = ("dup_vendors", "dup_payments", "price_variance", "volume_discount", "early_pay")


def build_graph(nodes: Nodes, checkpointer=None):
    g = StateGraph(RunState)
    g.add_node("load_data", nodes.load_data)
    g.add_node("extract_rates", nodes.extract_rates)
    for name in DETECTOR_NODES:
        g.add_node(name, getattr(nodes, name))
    g.add_node("aggregate", nodes.aggregate)
    g.add_node("explain", nodes.explain)
    g.add_node("human_review", nodes.human_review)
    g.add_node("make_report", nodes.make_report)

    g.add_edge(START, "load_data")
    g.add_edge("load_data", "extract_rates")
    for name in DETECTOR_NODES:
        g.add_edge("extract_rates", name)
    g.add_edge(list(DETECTOR_NODES), "aggregate")          # waits for all five
    g.add_edge("aggregate", "explain")
    g.add_edge("explain", "human_review")
    g.add_conditional_edges("human_review", route_after_review, {"rerun": "load_data", "approve": "make_report"})
    g.add_edge("make_report", END)
    return g.compile(checkpointer=checkpointer or MemorySaver())
