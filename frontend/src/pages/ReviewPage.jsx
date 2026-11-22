import { Fragment, useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { Alert, Ids, Severity, Spinner, Tile, TypeTag, TYPE_LABELS, money, num, pct } from "../ui.jsx";

const STATUS_LABEL = { APPROVED: "Approved", REJECTED: "Rejected", ADJUSTED: "Adjusted" };

function storageKey(runId, iteration) {
  return `review:${runId}:${iteration}`;
}

function loadDecisions(key) {
  try {
    return JSON.parse(window.sessionStorage.getItem(key) || "{}");
  } catch {
    return {};
  }
}

function saveDecisions(key, value) {
  try {
    window.sessionStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable: decisions just are not kept across a refresh */
  }
}

export default function ReviewPage({ runId, run, health, navigate }) {
  const [info, setInfo] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [decisions, setDecisions] = useState({});
  const [expanded, setExpanded] = useState(null);
  const [sort, setSort] = useState({ key: "rank", dir: "asc" });
  const [typeFilter, setTypeFilter] = useState("ALL");
  const [tolerance, setTolerance] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    api
      .getRun(runId)
      .then((r) => {
        setInfo(r);
        if (r.review) {
          setDecisions(loadDecisions(storageKey(runId, r.review.iteration)));
          setTolerance(String(r.review.params?.price_tolerance_pct ?? 2));
        }
      })
      .catch((e) => setLoadError(e.message));
  }, [runId, run.review?.seq, run.status]);

  const review = info?.status === "AWAITING_REVIEW" ? info.review : null;
  const findings = review?.findings || [];
  const iteration = review?.iteration || 1;
  const maxIterations = health?.max_iterations || 3;

  useEffect(() => {
    if (review) saveDecisions(storageKey(runId, iteration), decisions);
  }, [decisions, review, runId, iteration]);

  const rows = useMemo(() => {
    const list = typeFilter === "ALL" ? findings : findings.filter((f) => f.type === typeFilter);
    if (sort.key === "amount") {
      return [...list].sort((a, b) => (sort.dir === "desc" ? b.amount - a.amount : a.amount - b.amount));
    }
    return list;
  }, [findings, sort, typeFilter]);

  const stats = useMemo(() => {
    let approved = 0;
    let approvedCount = 0;
    let decided = 0;
    for (const f of findings) {
      const d = decisions[f.id];
      if (!d?.status) continue;
      decided += 1;
      if (d.status === "APPROVED") {
        approved += f.amount;
        approvedCount += 1;
      } else if (d.status === "ADJUSTED") {
        approved += Number(d.amount) || 0;
        approvedCount += 1;
      }
    }
    return { approved, approvedCount, decided };
  }, [findings, decisions]);

  if (loadError) {
    return (
      <Alert title="This run is not available.">
        {loadError}{" "}
        <button className="link" onClick={() => navigate("start")}>
          Start a new analysis
        </button>
      </Alert>
    );
  }
  if (!info) return <Spinner label="Loading findings…" />;
  if (!review) {
    return (
      <div className="card narrow">
        {info.status === "COMPLETED" ? (
          <>
            <h2>This review has been submitted.</h2>
            <button className="btn btn-primary" onClick={() => navigate("results", runId)}>
              See results →
            </button>
          </>
        ) : info.status === "FAILED" ? (
          <Alert title="The run failed.">{info.error}</Alert>
        ) : (
          <>
            <h2>The analysis is still running.</h2>
            <button className="btn" onClick={() => navigate("run", runId)}>
              Watch it live →
            </button>
          </>
        )}
      </div>
    );
  }

  const summary = review.summary || {};
  const setDecision = (id, patch) => setDecisions((d) => ({ ...d, [id]: { ...(d[id] || {}), ...patch } }));
  const clearDecision = (id) =>
    setDecisions((d) => {
      const next = { ...d };
      delete next[id];
      return next;
    });
  const approveWhere = (pred) =>
    setDecisions((d) => {
      const next = { ...d };
      for (const f of findings) if (!next[f.id]?.status && pred(f)) next[f.id] = { ...next[f.id], status: "APPROVED" };
      return next;
    });

  function payloadDecisions() {
    const out = {};
    for (const [id, d] of Object.entries(decisions)) {
      if (!d?.status) continue;
      out[id] = { status: d.status };
      if (d.note?.trim()) out[id].note = d.note.trim();
      if (d.status === "ADJUSTED") out[id].amount = Number(d.amount);
    }
    return out;
  }

  async function submit() {
    const bad = Object.entries(decisions).find(
      ([, d]) => d.status === "ADJUSTED" && (d.amount === "" || !(Number(d.amount) >= 0)),
    );
    if (bad) {
      setError(`Enter a valid adjusted amount for ${bad[0]}.`);
      setExpanded(bad[0]);
      return;
    }
    if (stats.approvedCount === 0 && !window.confirm("No findings are approved, so the report will be empty. Submit anyway?")) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.submitReview(runId, { action: "approve", decisions: payloadDecisions(), new_params: null });
      navigate("results", runId);
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  }

  async function rerun() {
    const value = Number(tolerance);
    if (!(value >= 0 && value <= 50)) {
      setError("Tolerance must be between 0 and 50%.");
      return;
    }
    if (stats.decided > 0 && !window.confirm("Re-running discards the decisions made on this screen. Continue?")) return;
    setBusy(true);
    setError(null);
    try {
      await api.submitReview(runId, { action: "rerun", decisions: {}, new_params: { price_tolerance_pct: value } });
      navigate("run", runId);
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  }

  const toggleAmountSort = () =>
    setSort((s) => (s.key !== "amount" ? { key: "amount", dir: "desc" } : s.dir === "desc" ? { key: "amount", dir: "asc" } : { key: "rank", dir: "asc" }));
  const types = Object.keys(summary.by_type || {});

  return (
    <div className="review">
      <div className="page-head">
        <div>
          <h1>Review findings</h1>
          <p className="muted">
            Approve, reject or adjust each finding. Only approved and adjusted findings go into the report. Pass{" "}
            {iteration} of {maxIterations} · tolerance {review.params?.price_tolerance_pct}%
          </p>
        </div>
      </div>

      <div className="tiles tiles-5">
        <Tile label="Identified" value={money(summary.total)} sub={`${summary.count} findings`} />
        <Tile label="High severity" value={num(summary.by_severity?.HIGH)} sub="≥ $25,000 each" tone="bad" />
        <Tile label="Medium severity" value={num(summary.by_severity?.MEDIUM)} sub="≥ $5,000 each" tone="warn" />
        <Tile label="Decided" value={`${stats.decided} / ${findings.length}`} sub="undecided are left out" />
        <Tile label="Approved for report" value={money(stats.approved)} sub={`${stats.approvedCount} findings`} tone="good" />
      </div>

      <div className="toolbar">
        <div className="toolbar-left">
          <button className="btn" onClick={() => approveWhere((f) => f.severity === "HIGH")}>
            Approve all HIGH
          </button>
          <button className="btn" onClick={() => approveWhere(() => true)}>
            Approve all remaining
          </button>
          <button className="btn btn-ghost" onClick={() => setDecisions({})} disabled={stats.decided === 0}>
            Clear decisions
          </button>
        </div>
        <label className="inline">
          Type
          <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
            <option value="ALL">All types ({findings.length})</option>
            {types.map((t) => (
              <option key={t} value={t}>
                {TYPE_LABELS[t]} ({summary.count_by_type?.[t] ?? 0})
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="card table-card">
        <table className="findings">
          <thead>
            <tr>
              <th className="sortable" onClick={() => setSort({ key: "rank", dir: "asc" })}>
                ID {sort.key === "rank" ? "▲" : ""}
              </th>
              <th>Severity</th>
              <th>Type</th>
              <th>Vendor / finding</th>
              <th className="num sortable" onClick={toggleAmountSort} title="Sort by amount">
                Amount {sort.key === "amount" ? (sort.dir === "desc" ? "▼" : "▲") : "↕"}
              </th>
              <th className="num">Conf.</th>
              <th>Decision</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((f) => {
              const d = decisions[f.id];
              const open = expanded === f.id;
              return (
                <Fragment key={f.id}>
                  <tr className={`row ${open ? "open" : ""} ${d?.status ? `decided-${d.status}` : ""}`} onClick={() => setExpanded(open ? null : f.id)}>
                    <td className="mono">{f.id}</td>
                    <td>
                      <Severity value={f.severity} />
                    </td>
                    <td>
                      <TypeTag type={f.type} />
                    </td>
                    <td>
                      <div className="cell-vendor">{f.vendor}</div>
                      <div className="cell-title">{f.title}</div>
                    </td>
                    <td className="num strong">{money(f.amount)}</td>
                    <td className="num">{pct(f.confidence)}</td>
                    <td onClick={(e) => e.stopPropagation()}>
                      <div className="quick">
                        <button
                          className={`qbtn ok ${d?.status === "APPROVED" ? "on" : ""}`}
                          title="Approve"
                          onClick={() => (d?.status === "APPROVED" ? clearDecision(f.id) : setDecision(f.id, { status: "APPROVED" }))}
                        >
                          ✓
                        </button>
                        <button
                          className={`qbtn no ${d?.status === "REJECTED" ? "on" : ""}`}
                          title="Reject"
                          onClick={() => (d?.status === "REJECTED" ? clearDecision(f.id) : setDecision(f.id, { status: "REJECTED" }))}
                        >
                          ✕
                        </button>
                        <button
                          className={`qbtn adj ${d?.status === "ADJUSTED" ? "on" : ""}`}
                          title="Adjust amount"
                          onClick={() => {
                            setDecision(f.id, { status: "ADJUSTED", amount: d?.amount ?? f.amount });
                            setExpanded(f.id);
                          }}
                        >
                          ±
                        </button>
                        {d?.status && (
                          <span className={`dec dec-${d.status}`}>
                            {STATUS_LABEL[d.status]}
                            {d.status === "ADJUSTED" && d.amount !== "" ? ` ${money(Number(d.amount))}` : ""}
                          </span>
                        )}
                      </div>
                    </td>
                  </tr>
                  {open && (
                    <tr className="detail-row">
                      <td colSpan={7}>
                        <FindingDetail
                          f={f}
                          decision={d}
                          onDecide={(patch) => setDecision(f.id, patch)}
                          onClear={() => clearDecision(f.id)}
                        />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>

      {error && <Alert>{error}</Alert>}

      <div className="action-bar">
        <div className="action-summary">
          <strong>{stats.decided}</strong> of {findings.length} decided · approved <strong>{money(stats.approved)}</strong>
        </div>
        <div className="action-rerun">
          <label className="inline">
            Re-run with tolerance
            <input type="number" min="0" max="50" step="0.5" value={tolerance} onChange={(e) => setTolerance(e.target.value)} />%
          </label>
          <button className="btn" onClick={rerun} disabled={busy || iteration >= maxIterations} title={iteration >= maxIterations ? "Re-run limit reached" : ""}>
            Re-run
          </button>
        </div>
        <button className="btn btn-primary btn-lg" onClick={submit} disabled={busy}>
          {busy ? "Submitting…" : "Submit review →"}
        </button>
      </div>
    </div>
  );
}

function FindingDetail({ f, decision, onDecide, onClear }) {
  const ev = f.evidence || {};
  const note = decision?.note || "";
  return (
    <div className="detail">
      <div className="detail-main">
        <p className="explanation">
          {f.explanation}
          <span className="muted small"> {f.explanation_source === "llm" ? "· written by the LLM" : "· template"}</span>
        </p>
        <div className="calc-label">Calculation</div>
        <div className="calc">{f.calculation}</div>
        <Evidence f={f} ev={ev} />
        <div className="refs">
          <div>
            <span className="ref-label">Invoices</span> <Ids ids={ev.invoice_ids} />
          </div>
          <div>
            <span className="ref-label">Lines</span> <Ids ids={ev.line_ids} max={10} />
          </div>
          <div>
            <span className="ref-label">Payments</span> <Ids ids={ev.payment_ids} />
          </div>
        </div>
      </div>
      <div className="detail-decide">
        <div className="decide-buttons">
          <button className={`btn ${decision?.status === "APPROVED" ? "btn-ok" : ""}`} onClick={() => onDecide({ status: "APPROVED" })}>
            Approve
          </button>
          <button className={`btn ${decision?.status === "REJECTED" ? "btn-bad" : ""}`} onClick={() => onDecide({ status: "REJECTED" })}>
            Reject
          </button>
          <button
            className={`btn ${decision?.status === "ADJUSTED" ? "btn-warn" : ""}`}
            onClick={() => onDecide({ status: "ADJUSTED", amount: decision?.amount ?? f.amount })}
          >
            Adjust
          </button>
        </div>
        {decision?.status === "ADJUSTED" && (
          <label>
            Adjusted amount ($)
            <input
              type="number"
              min="0"
              step="0.01"
              value={decision.amount}
              onChange={(e) => onDecide({ amount: e.target.value })}
            />
          </label>
        )}
        <label>
          Note
          <input
            type="text"
            maxLength={500}
            placeholder={decision?.status === "REJECTED" ? "e.g. Known rebate" : "Optional (choose a decision above)"}
            value={note}
            onChange={(e) => onDecide({ note: e.target.value })}
          />
        </label>
        {decision && (
          <button className="link" onClick={onClear}>
            Clear decision
          </button>
        )}
      </div>
    </div>
  );
}

function Evidence({ f, ev }) {
  if (f.type === "DUPLICATE_PAYMENT") {
    const card = (title, x) => (
      <div className="compare-card">
        <div className="compare-title">{title}</div>
        <div>
          <code>{x.invoice_id}</code> · {x.vendor}
        </div>
        <div>
          Vendor invoice no. <strong>{x.vendor_invoice_no}</strong> · {x.invoice_date}
        </div>
        <div>Total {money(x.total_amount)}</div>
        {x.payments?.map((p) => (
          <div key={p.payment_id} className="muted small">
            Paid {money(p.amount)} on {p.paid_date} (<code>{p.payment_id}</code>)
          </div>
        ))}
      </div>
    );
    return (
      <>
        <p className="muted small">Matched by {ev.reason}.</p>
        <div className="compare">
          {card("Original", ev.original || {})}
          {card("Duplicate", ev.duplicate || {})}
        </div>
      </>
    );
  }
  if (f.type === "DUPLICATE_VENDOR") {
    return (
      <>
        <p className="muted small">
          {ev.reason} {ev.assumption}
        </p>
        <div className="compare">
          {(ev.vendors || []).map((v) => (
            <div className="compare-card" key={v.vendor_id}>
              <div className="compare-title">
                <code>{v.vendor_id}</code> {v.name}
              </div>
              <div>Tax ID {v.tax_id || <span className="muted">missing</span>}</div>
              <div className="muted small">{v.address}</div>
              <div>Spend in period {money(v.spend)}</div>
            </div>
          ))}
        </div>
      </>
    );
  }
  if (f.type === "EARLY_PAY_DISCOUNT") {
    return (
      <>
        <Clause ev={ev} />
        <dl className="facts">
          <dt>Invoice</dt>
          <dd>
            <code>{ev.invoice_ids?.[0]}</code> ({ev.vendor_invoice_no}) dated {ev.invoice_date}, total {money(ev.invoice_total)}
          </dd>
          <dt>First paid</dt>
          <dd>
            {ev.paid_date}: after {ev.days_to_pay} days (discount window {ev.discount_days} days)
          </dd>
        </dl>
      </>
    );
  }
  // PRICE_VARIANCE / VOLUME_DISCOUNT
  const lines = ev.lines || [];
  return (
    <>
      <Clause ev={ev} />
      {ev.overlap_note && <p className="muted small">{ev.overlap_note}</p>}
      <table className="lines">
        <thead>
          <tr>
            <th>Line</th>
            <th>Invoice</th>
            <th>Date</th>
            <th className="num">Qty</th>
            <th className="num">Billed</th>
            <th className="num">Contract</th>
            <th className="num">Year-to-date before</th>
            <th className="num">Amount</th>
          </tr>
        </thead>
        <tbody>
          {lines.slice(0, 12).map((l) => (
            <tr key={l.line_id}>
              <td className="mono">{l.line_id}</td>
              <td className="mono">{l.invoice_id}</td>
              <td>{l.invoice_date}</td>
              <td className="num">{num(l.quantity)}</td>
              <td className="num">{l.billed_price.toFixed(2)}</td>
              <td className="num">{l.contract_price.toFixed(2)}</td>
              <td className="num">{num(l.prior_ytd_qty)}</td>
              <td className="num">{money(l.amount)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {lines.length > 12 && <p className="muted small">+{lines.length - 12} more lines (all included in the amount).</p>}
    </>
  );
}

function Clause({ ev }) {
  if (!ev.clause) return null;
  const source =
    ev.rate_source === "llm" ? "extracted by the LLM from the contract text" : ev.contract_id ? "contract data" : "";
  return (
    <blockquote className="clause">
      “{ev.clause}”
      <footer>
        {ev.contract_id && <code>{ev.contract_id}</code>} {ev.sku && <code>{ev.sku}</code>} {source}
      </footer>
    </blockquote>
  );
}
