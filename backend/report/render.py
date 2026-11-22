"""Writes output/<run_id>/report.html (string.Template + CSS bars) and findings.csv (csv module).

Only APPROVED and ADJUSTED findings are included (LLD §7.3 make_report). Adjusted findings use
the reviewer's amount.
"""
from __future__ import annotations

import csv
import html
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from string import Template

from ..config import PROVIDERS
from ..graph.detectors import TYPE_LABELS, cents, dec, fmt_money

TEMPLATE = Path(__file__).with_name("report.html")
INCLUDED = ("APPROVED", "ADJUSTED")
CSV_COLUMNS = ["finding_id", "type", "type_label", "severity", "vendor_id", "vendor", "title", "status",
               "identified_amount", "final_amount", "confidence", "reviewer_note", "calculation", "explanation",
               "contract_id", "sku", "invoice_ids", "line_ids", "payment_ids"]


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _csv_safe(value: object) -> object:
    """Stop spreadsheet formula injection from free-text fields (e.g. reviewer notes)."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _bar_rows(rows: list[tuple[str, Decimal, int]]) -> str:
    if not rows:
        return '<div class="empty">No approved findings.</div>'
    top = max(amount for _, amount, _ in rows) or Decimal(1)
    out = []
    for name, amount, count in rows:
        pct = float(amount / top * 100) if top else 0.0
        out.append(f'<div class="name">{_esc(name)} <span class="count">· {count}</span></div>'
                   f'<div class="bar"><span style="width:{pct:.1f}%"></span></div>'
                   f'<div class="num">{fmt_money(amount)}</div>')
    return "\n".join(out)


def _finding_html(f: dict) -> str:
    ev = f.get("evidence") or {}
    refs = []
    for key, label in (("invoice_ids", "Invoices"), ("line_ids", "Lines"), ("payment_ids", "Payments")):
        ids = ev.get(key) or []
        if ids:
            shown = ", ".join(ids[:12]) + (f" … (+{len(ids) - 12})" if len(ids) > 12 else "")
            refs.append(f"{label}: {_esc(shown)}")
    if ev.get("contract_id"):
        refs.append(f"Contract: {_esc(ev['contract_id'])}")
    clause = f'<div class="refs">Clause: “{_esc(ev["clause"])}”</div>' if ev.get("clause") else ""
    amount_line = fmt_money(f["final_amount"])
    if f.get("status") == "ADJUSTED":
        amount_line += f' <span class="count">(identified {fmt_money(f["amount"])})</span>'
    note = f'<div class="note"><strong>Reviewer note:</strong> {_esc(f["note"])}</div>' if f.get("note") else ""
    return f"""
  <div class="finding">
    <div class="head">
      <span class="id">{_esc(f['id'])}</span>
      <span class="title">{_esc(f['title'])}</span>
      <span class="pill {_esc(f.get('severity'))}">{_esc(f.get('severity'))}</span>
      <span class="pill {_esc(f.get('status'))}">{_esc(f.get('status'))}</span>
      <span class="amt">{amount_line}</span>
    </div>
    <div class="sub">{_esc(TYPE_LABELS.get(f['type'], f['type']))} · {_esc(f['vendor'])} · confidence {f['confidence']:.0%}</div>
    <div class="calc">{_esc(f['calculation'])}</div>
    <div class="expl">{_esc(f.get('explanation'))}</div>
    {clause}
    <div class="refs">{' · '.join(refs)}</div>
    {note}
  </div>"""


def render_report(state: dict, output_dir: Path, llm_mode: str) -> dict:
    run_id = state["run_id"]
    params = state.get("params") or {}
    findings = state.get("findings") or []
    included = [f for f in findings if f.get("status") in INCLUDED]
    rejected = sum(1 for f in findings if f.get("status") == "REJECTED")
    undecided = sum(1 for f in findings if not f.get("status"))

    approved_total = sum((dec(f["final_amount"]) for f in included), Decimal(0))
    identified_total = sum((dec(f["amount"]) for f in findings), Decimal(0))
    by_type: dict[str, list] = defaultdict(lambda: [Decimal(0), 0])
    by_vendor: dict[str, list] = defaultdict(lambda: [Decimal(0), 0])
    for f in included:
        for bucket, key in ((by_type, f["type"]), (by_vendor, f["vendor"])):
            bucket[key][0] += dec(f["final_amount"])
            bucket[key][1] += 1
    type_rows = sorted(((TYPE_LABELS.get(t, t), a, n) for t, (a, n) in by_type.items()), key=lambda r: -r[1])
    vendor_rows = sorted(((v, a, n) for v, (a, n) in by_vendor.items()), key=lambda r: -r[1])[:10]

    out = output_dir / run_id
    out.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    if llm_mode in PROVIDERS:
        data_statement = ("Raw data (the ERP database and contract files) was read only by a local MCP server "
                          f"process on the analysis laptop. The {PROVIDERS[llm_mode][0]} API received only small excerpts: one "
                          "contract's text at a time for price extraction, vendor name/address pairs, and "
                          "per-finding evidence summaries for the explanations.")
    else:
        data_statement = ("Raw data (the ERP database and contract files) was read only by a local MCP server "
                          "process on the analysis laptop, and the analysis ran fully offline: no data left the "
                          "machine.")

    dataset = params.get("dataset") or "demo"
    data_statement = (("Data source: the built-in synthetic demo data. " if dataset == "demo"
                       else f"Data source: uploaded dataset {dataset}. ") + data_statement)

    page = Template(TEMPLATE.read_text(encoding="utf-8")).substitute(
        run_id=_esc(run_id),
        date_from=_esc(params.get("date_from")),
        date_to=_esc(params.get("date_to")),
        tolerance=_esc(f"{float(params.get('price_tolerance_pct', 2)):g}"),
        iteration=_esc(state.get("iteration", 1)),
        generated_at=_esc(generated_at),
        approved_total=fmt_money(approved_total),
        approved_count=len(included),
        identified_total=fmt_money(identified_total),
        rejected_count=rejected,
        undecided_count=undecided,
        type_rows=_bar_rows(type_rows),
        vendor_rows=_bar_rows(vendor_rows),
        finding_rows="\n".join(_finding_html(f) for f in included) or '<p class="empty">No findings were approved.</p>',
        data_statement=_esc(data_statement),
    )
    html_path = out / "report.html"
    html_path.write_text(page, encoding="utf-8")

    csv_path = out / "findings.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:      # BOM: Excel opens UTF-8 correctly
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for f in included:
            ev = f.get("evidence") or {}
            writer.writerow({k: _csv_safe(v) for k, v in {
                "finding_id": f["id"], "type": f["type"], "type_label": TYPE_LABELS.get(f["type"], f["type"]),
                "severity": f.get("severity"), "vendor_id": f.get("vendor_id"), "vendor": f["vendor"],
                "title": f["title"], "status": f["status"],
                "identified_amount": f"{cents(dec(f['amount'])):.2f}",
                "final_amount": f"{cents(dec(f['final_amount'])):.2f}",
                "confidence": f["confidence"], "reviewer_note": f.get("note") or "",
                "calculation": f["calculation"], "explanation": f.get("explanation") or "",
                "contract_id": ev.get("contract_id") or "", "sku": ev.get("sku") or "",
                "invoice_ids": " ".join(ev.get("invoice_ids") or []),
                "line_ids": " ".join(ev.get("line_ids") or []),
                "payment_ids": " ".join(ev.get("payment_ids") or []),
            }.items()})

    return {
        "run_id": run_id,
        "generated_at": generated_at,
        "html_path": str(html_path),
        "csv_path": str(csv_path),
        "approved_count": len(included),
        "approved_total": float(cents(approved_total)),
        "identified_count": len(findings),
        "identified_total": float(cents(identified_total)),
        "rejected_count": rejected,
        "undecided_count": undecided,
        "by_type": [{"type": t, "label": TYPE_LABELS.get(t, t), "amount": float(cents(a)), "count": n}
                    for t, (a, n) in sorted(by_type.items(), key=lambda kv: -kv[1][0])],
        "top_vendors": [{"vendor": v, "amount": float(cents(a)), "count": n} for v, a, n in vendor_rows],
    }
