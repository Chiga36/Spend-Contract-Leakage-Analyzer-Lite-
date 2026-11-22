"""Run one full analysis without the UI and print its evaluation metrics.

    python evaluate.py                    # uses LLM_MODE / LLM_MODEL from .env
    python evaluate.py --mode off         # offline baseline, for comparison
    python evaluate.py --mode gemini --model gemini-3.5-flash --tolerance 5

Scores the findings against the synthetic data's planted anomalies (data/planted_anomalies.json)
and writes the full metrics to output/evaluation-<mode>-<time>.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from contextlib import AsyncExitStack
from datetime import datetime



def pct(x: float | None) -> str:
    return "  n/a" if x is None else f"{x * 100:5.1f}%"


def print_report(m: dict) -> None:
    d = m["detection"]
    print(f"\nEvaluation  ·  LLM_MODE={m['llm']['mode']}" + (f" ({m['llm']['model']})" if m["llm"]["model"] else "")
          + f"  ·  tolerance {m['params']['price_tolerance_pct']}%  ·  {m['params']['date_from']} to {m['params']['date_to']}")
    print("\nDETECTION ACCURACY")
    print(f"  {'Type':<28}{'found/exp':>10}{'precision':>11}{'recall':>9}{'F1':>8}{'$ recall':>10}{'amount err':>12}")
    for t, v in d["by_type"].items():
        err = "  n/a" if v["amount_error_pct"] is None else f"{v['amount_error_pct']:+.2f}%"
        print(f"  {v['label']:<28}{v['detected']:>4}/{v['expected']:<5}{pct(v['precision']):>11}{pct(v['recall']):>9}"
              f"{pct(v['f1']):>8}{pct(v['dollar_recall']):>10}{err:>12}")
    o = d["overall"]
    print(f"  {'OVERALL':<28}{o['detected']:>4}/{o['expected']:<5}{pct(o['precision']):>11}{pct(o['recall']):>9}"
          f"{pct(o['f1']):>8}{pct(o['dollar_recall']):>10}")
    print(f"  Found ${o['found_amount']:,.2f} of ${o['planted_amount']:,.2f} planted.  "
          f"Decoys wrongly reported: {o['decoys_reported']}.")
    for t, v in d["by_type"].items():
        for x in v["missed"]:
            print(f"    missed  {x['id']}  {x['label']}  ${x['amount']:,.2f}" + ("  (needs LLM)" if x["needs_llm"] else ""))
        for x in v["false_positives"]:
            print(f"    false+  {x['id']}  {x['title']}  ${x['amount']:,.2f}")
    for n in d["notes"]:
        print(f"  note: {n}")

    e = m["extraction"]
    print("\nLLM PRICE EXTRACTION (text-only contracts)")
    if not e or not e.get("attempted"):
        print("  not run (LLM off or every extraction failed)")
    else:
        print(f"  {e['contracts_extracted']}/{e['contracts']} contracts extracted, {e['contracts_exact']} exactly right.  "
              f"Rows: precision {pct(e['precision']).strip()}, recall {pct(e['recall']).strip()}, "
              f"volume-tier recall {pct(e['tier_recall']).strip()}")
        for c in e["per_contract"]:
            for r in c["missing"]:
                print(f"    missing {c['contract_id']}  {r['sku']} ${r['unit_price']:,.2f}"
                      + (f" from {r['min_qty']:,} units" if r["min_qty"] else ""))
            for r in c["wrong"]:
                print(f"    wrong   {c['contract_id']}  {r['sku']} ${r['unit_price']:,.2f} min_qty {r['min_qty']}")

    x = m["explanations"]
    print("\nLLM EXPLANATIONS")
    if not x.get("evaluated") and not x.get("blocked_by_guardrail"):
        print("  not evaluated (template explanations)")
    else:
        print(f"  {x['evaluated']} explanations · faithfulness {pct(x['faithfulness']).strip()} "
              f"(no invented numbers) · relevancy {pct(x['relevancy']).strip()} · avg {x['avg_words']} words")
        print(f"  Output guardrail: {x['blocked_by_guardrail']} blocked and replaced by the template "
              f"(pass rate {pct(x['pass_rate']).strip()})")
        for b in x.get("blocked", []):
            print(f"    blocked     {b['id']}  {b['title']}: {b['reason']}")
        for u in x["unfaithful"]:
            print(f"    unfaithful  {u['id']}  {u['title']}: {', '.join(u['unsupported_numbers'])}")
        for u in x["irrelevant"]:
            print(f"    off-topic   {u['id']}  {u['title']}: {u['reason']}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", help="off | gemini | openai | claude (default: LLM_MODE from .env)")
    ap.add_argument("--model", help="model name (default: LLM_MODEL or the mode's default)")
    ap.add_argument("--tolerance", type=float, help="price tolerance in percent (default: PRICE_TOLERANCE_PCT)")
    args = ap.parse_args()
    if args.mode:
        os.environ["LLM_MODE"] = args.mode
    if args.model:
        os.environ["LLM_MODEL"] = args.model
    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(name)s: %(message)s")

    from backend.config import PROVIDERS, get_settings
    from backend.evaluation import evaluate, load_ground_truth
    from backend.graph.build import build_graph
    from backend.graph.nodes import Nodes
    from backend.llm import Llm
    from backend.mcp_client import McpClient

    settings = get_settings()
    gt = load_ground_truth(settings.ground_truth_path)
    if gt is None:
        print(f"No ground truth at {settings.ground_truth_path}. Run first:  python seed/generate.py")
        return 1
    tol = settings.price_tolerance_pct if args.tolerance is None else args.tolerance
    params = {"date_from": gt["date_range"]["from"], "date_to": gt["date_range"]["to"], "price_tolerance_pct": tol}
    run_id = "eval" + datetime.now().strftime("%H%M%S%f")[:8]
    print(f"Running the analysis (LLM_MODE={settings.llm_mode}) ...", flush=True)
    async with AsyncExitStack() as stack:
        mcp = McpClient(settings)
        await mcp.start(stack)
        graph = build_graph(Nodes(mcp, Llm(settings), settings))
        cfg = {"configurable": {"thread_id": run_id}}
        guards = []
        async for mode, chunk in graph.astream({"run_id": run_id, "iteration": 0, "params": params}, cfg,
                                               stream_mode=["updates", "custom"]):
            if mode == "custom" and isinstance(chunk, dict) and chunk.get("type") == "guardrail":
                guards.append(chunk)
        # The run stops at the review step: nothing is approved, so no report is written.
        values = (await graph.aget_state(cfg)).values

    m = evaluate(values.get("findings") or [], gt, values["params"], values.get("rate_cards"), values.get("invoices"))
    m["guardrails"] = guards
    m["llm"] = {"mode": settings.llm_mode, "model": settings.llm_model if settings.llm_mode in PROVIDERS else None}
    print_report(m)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    out = settings.output_dir / f"evaluation-{settings.llm_mode}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(m, indent=2), encoding="utf-8")
    print(f"\nGUARDRAILS\n  {len(guards)} triggered" + ("" if guards else " (nothing redacted or blocked)"))
    for g in guards:
        print(f"    {g['rule']:<16} {g['message'][:150]}")
    print(f"\nFull metrics: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
