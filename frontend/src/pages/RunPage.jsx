import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Alert, TypeTag, bytes, money, moneyShort, num, pct } from "../ui.jsx";
import EvaluationPanel from "./EvaluationPanel.jsx";

const STEPS = [
  { node: "load_data", label: "Load ERP data over MCP" },
  { node: "extract_rates", label: "Contract price tables" },
  { node: "dup_vendors", label: "Duplicate vendors", detector: true },
  { node: "dup_payments", label: "Duplicate payments", detector: true },
  { node: "price_variance", label: "Price above contract", detector: true },
  { node: "volume_discount", label: "Missed volume discount", detector: true },
  { node: "early_pay", label: "Missed early-pay discount", detector: true },
  { node: "aggregate", label: "Remove overlaps and rank" },
  { node: "explain", label: "Explain findings" },
  { node: "human_review", label: "Consultant review" },
];

const ICON = { running: "⏳", done: "✅", waiting: "⏸️", failed: "❌" };

function stepDetail(node, s, provider) {
  if (!s) return null;
  switch (node) {
    case "load_data":
      return `${num(s.invoices)} invoices · ${num(s.invoice_lines)} lines · ${num(s.payments)} payments`;
    case "extract_rates":
      return `${s.from_db} from rate tables${s.from_llm ? ` · ${s.from_llm} extracted by ${provider || "the LLM"}` : ""}`;
    case "aggregate":
      return `${s.count} findings · ${money(s.total)}`;
    case "explain":
      return s.by_llm ? `${s.by_llm} of ${s.explained} written by ${provider || "the LLM"}` : `${s.explained} template explanations`;
    default:
      return s.findings !== undefined ? `${s.findings} found · ${moneyShort(s.amount)}` : null;
  }
}

export default function RunPage({ runId, run, health, navigate }) {
  const [info, setInfo] = useState(null);
  const [loadError, setLoadError] = useState(null);

  useEffect(() => {
    api.getRun(runId).then(setInfo).catch((e) => setLoadError(e.message));
  }, [runId, run.iteration]);

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

  const warningsFor = (node) => run.warnings.filter((w) => w.node === node);
  const agg = run.summaries.aggregate;
  const llmOn = Boolean(health?.llm_provider);
  const totalBytes = run.tools.reduce((a, t) => a + (t.bytes || 0), 0);
  const params = info?.params;

  return (
    <div className="run">
      <div className="page-head">
        <div>
          <h1>Analysis {run.iteration > 1 ? `· pass ${run.iteration}` : ""}</h1>
          {params && (
            <p className="muted">
              {params.date_from} to {params.date_to} · price tolerance {params.price_tolerance_pct}% · run{" "}
              <code>{runId}</code>
            </p>
          )}
        </div>
        <span className={`status status-${run.status || "PENDING"}`}>{(run.status || "connecting").replace("_", " ")}</span>
      </div>

      {run.status === "AWAITING_REVIEW" && run.review && (
        <div className="banner banner-ready">
          <div>
            <strong>Analysis complete.</strong> {run.review.count} findings worth {money(run.review.summary?.total)} are
            ready for your review.
          </div>
          <button className="btn btn-primary" onClick={() => navigate("review", runId)}>
            Review findings →
          </button>
        </div>
      )}
      {run.status === "COMPLETED" && (
        <div className="banner banner-ready">
          <div>
            <strong>Review submitted and report written.</strong>
          </div>
          <button className="btn btn-primary" onClick={() => navigate("results", runId)}>
            See results →
          </button>
        </div>
      )}
      {run.status === "FAILED" && (
        <Alert title="The run failed.">
          {run.error}{" "}
          <button className="link" onClick={() => navigate("start")}>
            Start a new analysis
          </button>
        </Alert>
      )}

      <div className="run-grid">
        <section className="card">
          <h2>Steps</h2>
          <ol className="steps">
            {STEPS.map((s) => {
              const state = run.steps[s.node];
              const warns = warningsFor(s.node);
              return (
                <li key={s.node} className={`step-item ${state || "pending"} ${s.detector ? "detector" : ""}`}>
                  <span className="step-icon">{ICON[state] || "○"}</span>
                  <div>
                    <div className="step-label">{s.label}</div>
                    {stepDetail(s.node, run.summaries[s.node], health?.llm_provider) && (
                      <div className="step-detail">{stepDetail(s.node, run.summaries[s.node], health?.llm_provider)}</div>
                    )}
                    {warns.map((w) => (
                      <div key={w.seq} className={`step-warning ${w.guardrail ? "step-guardrail" : ""}`}>
                        {w.guardrail ? "🛡️" : "⚠️"} {w.message}
                      </div>
                    ))}
                  </div>
                </li>
              );
            })}
          </ol>
        </section>

        <section className="card findings-feed">
          <div className="total-box">
            <div className="total-label">{agg ? "Total leakage identified" : "Leakage found so far"}</div>
            <div className="total-value">{money(agg ? agg.total : run.runningTotal)}</div>
            <div className="muted small">
              {agg
                ? `${agg.count} findings after removing overlaps · ${agg.by_severity?.HIGH ?? 0} high severity`
                : `${run.findings.length} findings so far`}
            </div>
          </div>
          <div className="feed">
            {run.findings.length === 0 && <p className="muted">Findings will appear here as each check finishes.</p>}
            {[...run.findings].reverse().slice(0, 120).map((f) => (
              <div key={f.seq} className="feed-card">
                <div className="feed-top">
                  <TypeTag type={f.type} />
                  <span className="feed-amount">{money(f.amount)}</span>
                </div>
                <div className="feed-title">{f.title}</div>
                <div className="muted small">
                  {f.vendor} · confidence {pct(f.confidence)}
                </div>
              </div>
            ))}
          </div>
        </section>

        <section className="card local">
          <h2>Data stays local</h2>
          <div className="transport">
            <span className="dot" /> stdio · local process (pid {run.pid || health?.pid || "…"})
          </div>
          <p className="muted small">
            Raw data is read only by the MCP server running on this laptop. {num(run.tools.length)} tool calls ·{" "}
            {bytes(totalBytes)} received over stdin/stdout.
          </p>
          <ul className="toollog">
            {[...run.tools].reverse().map((t) => (
              <li key={t.seq} className={t.ok ? "" : "failed"}>
                <code className="tool">{t.tool}</code>
                <span className="args">{Object.values(t.args || {}).join(" → ")}</span>
                <span className="meta">
                  {t.ok
                    ? `${num(t.rows)} ${t.tool === "read_contract" ? "document" : t.rows === 1 ? "row" : "rows"}${t.lines ? ` + ${num(t.lines)} lines` : ""} · ${bytes(t.bytes)} · ${t.ms} ms`
                    : `failed (attempt ${t.attempt})`}
                </span>
              </li>
            ))}
          </ul>
          <div className="llm-box">
            {llmOn ? (
              <>
                <h3>{health.llm_provider} API calls ({run.llm.length})</h3>
                <p className="muted small">
                  🛡️ Guardrails: {run.guardrails.length === 0 ? "nothing blocked or redacted" : `${run.guardrails.length} triggered`}
                  {" "}· sensitive data redacted, injected instructions blocked, prices and explanations checked before use.
                </p>
                <p className="muted small">Only excerpts are sent: one contract, name pairs, or finding summaries.</p>
                <ul className="toollog">
                  {[...run.llm].reverse().map((c) => (
                    <li key={c.seq} className={c.outcome === "ok" ? "" : "failed"}>
                      <code className="tool">{c.purpose}</code>
                      <span className="args">{c.outcome}{c.detail ? `: ${c.detail}` : ""}</span>
                      <span className="meta">
                        {num(c.sent_chars)} chars sent · {c.ms} ms
                      </span>
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <p className="muted small">
                <strong>LLM_MODE=off.</strong> No outbound traffic: nothing leaves this machine.
              </p>
            )}
          </div>
        </section>
      </div>

      <EvaluationPanel runId={runId} status={run.status} iteration={run.iteration} />
    </div>
  );
}
