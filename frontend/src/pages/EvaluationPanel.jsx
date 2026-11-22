import { useEffect, useState } from "react";
import { api } from "../api.js";
import { money } from "../ui.jsx";

// Accuracy and relevancy metrics of a run, scored against the synthetic data's planted anomalies.
const p = (v) => (v === null || v === undefined ? "–" : `${(v * 100).toFixed(1)}%`);

function tone(v) {
  if (v === null || v === undefined) return "";
  return v >= 0.95 ? "m-good" : v >= 0.8 ? "m-warn" : "m-bad";
}

function Metric({ label, value, hint }) {
  return (
    <div className={`metric ${tone(value)}`} title={hint}>
      <div className="metric-value">{p(value)}</div>
      <div className="metric-label">{label}</div>
    </div>
  );
}

export default function EvaluationPanel({ runId, status, iteration }) {
  const [m, setM] = useState(null);
  const [err, setErr] = useState(null);
  const [open, setOpen] = useState(false);
  const ready = status === "AWAITING_REVIEW" || status === "COMPLETED";

  useEffect(() => {
    if (!ready) return;
    setErr(null);
    api.evaluation(runId).then(setM).catch((e) => setErr(e));
  }, [runId, ready, iteration]);

  if (!ready) return null;
  if (err) {
    // 404: no ground truth (real client data), so there is nothing to score against.
    return err.status === 404 ? null : (
      <section className="card eval">
        <h2>Evaluation</h2>
        <p className="muted small">Metrics could not be computed: {err.message}</p>
      </section>
    );
  }
  if (!m) return null;

  const d = m.detection;
  const o = d.overall;
  const ex = m.extraction;
  const xp = m.explanations;
  const issues = Object.values(d.by_type).flatMap((t) => [
    ...t.missed.map((x) => ({ kind: "Missed", ...x, text: x.label, llm: x.needs_llm })),
    ...t.false_positives.map((x) => ({ kind: "False positive", ...x, text: x.title })),
  ]);

  return (
    <section className="card eval">
      <div className="card-head">
        <h2>Evaluation against planted anomalies</h2>
        <span className="muted small">
          {o.detected} of {o.expected} found · {money(o.found_amount)} of {money(o.planted_amount)} planted
        </span>
      </div>

      <h3>Accuracy</h3>
      <div className="metrics">
        <Metric label="Precision" value={o.precision} hint="Share of findings that are real planted anomalies" />
        <Metric label="Recall" value={o.recall} hint="Share of planted anomalies that were found" />
        <Metric label="F1 score" value={o.f1} hint="Balance of precision and recall" />
        <Metric label="Dollar recall" value={o.dollar_recall} hint="Share of planted dollars found by correct findings" />
        <div
          className={`metric ${o.decoys_reported ? "m-bad" : "m-good"}`}
          title="Different companies with similar names must not be reported"
        >
          <div className="metric-value">{o.decoys_reported ? "Reported" : "Rejected"}</div>
          <div className="metric-label">Decoy pair</div>
        </div>
      </div>

      <div className="table-wrap">
        <table className="eval-table">
          <thead>
            <tr>
              <th>Type</th>
              <th className="num">Found</th>
              <th className="num">Precision</th>
              <th className="num">Recall</th>
              <th className="num">F1</th>
              <th className="num">$ recall</th>
              <th className="num">Amount error</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(d.by_type).map(([t, v]) => (
              <tr key={t}>
                <td>{v.label}</td>
                <td className="num">
                  {v.detected}/{v.expected}
                </td>
                <td className={`num ${tone(v.precision)}`}>{p(v.precision)}</td>
                <td className={`num ${tone(v.recall)}`}>{p(v.recall)}</td>
                <td className={`num ${tone(v.f1)}`}>{p(v.f1)}</td>
                <td className={`num ${tone(v.dollar_recall)}`}>{p(v.dollar_recall)}</td>
                <td className="num">
                  {v.amount_error_pct === null ? "–" : `${v.amount_error_pct > 0 ? "+" : ""}${v.amount_error_pct}%`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h3>LLM quality</h3>
      {ex?.attempted || xp?.evaluated > 0 || xp?.blocked_by_guardrail > 0 ? (
        <div className="metrics">
          {ex?.attempted && (
            <>
              <Metric label="Price extraction precision" value={ex.precision} hint="Extracted price rows that match the contract" />
              <Metric label="Price extraction recall" value={ex.recall} hint="Contract price rows that were extracted" />
              <Metric label="Volume-tier recall" value={ex.tier_recall} hint="Volume-tier rows that were extracted" />
            </>
          )}
          {(xp?.evaluated > 0 || xp?.blocked_by_guardrail > 0) && (
            <>
              <Metric
                label="Explanation faithfulness"
                value={xp.faithfulness}
                hint="Explanations with no number that is not in the finding's data"
              />
              <Metric
                label="Explanation relevancy"
                value={xp.relevancy}
                hint="Explanations that name the vendor or an ID and match the finding type"
              />
              <Metric
                label="Passed guardrail"
                value={xp.pass_rate}
                hint={`LLM explanations accepted by the output guardrail (${xp.blocked_by_guardrail} replaced by the template)`}
              />
            </>
          )}
        </div>
      ) : (
        <p className="muted small">The LLM was not used in this run, so only detection accuracy is scored.</p>
      )}

      {d.notes.map((n) => (
        <p key={n} className="muted small">
          {n}
        </p>
      ))}

      {(issues.length > 0 || ex?.attempted || xp?.evaluated > 0 || xp?.blocked_by_guardrail > 0) && (
        <button className="link small" onClick={() => setOpen(!open)}>
          {open ? "Hide details" : `Show details (${issues.length} missed or false)`}
        </button>
      )}
      {open && (
        <div className="eval-details small">
          {issues.length > 0 && (
            <ul>
              {issues.map((x) => (
                <li key={`${x.kind}-${x.id}`}>
                  <strong>{x.kind}</strong> <code>{x.id}</code> {x.text} · {money(x.amount)}
                  {x.llm ? <span className="muted"> · needs the LLM</span> : null}
                </li>
              ))}
            </ul>
          )}
          {ex?.attempted &&
            ex.per_contract
              .filter((c) => c.missing.length || c.wrong.length)
              .map((c) => (
                <p key={c.contract_id}>
                  <code>{c.contract_id}</code> extraction: {c.correct_rows}/{c.true_rows} rows right
                  {c.missing.length > 0 &&
                    ` · missing ${c.missing
                      .map((r) => `${r.sku} $${r.unit_price}${r.min_qty ? ` from ${r.min_qty} units` : ""}`)
                      .join(", ")}`}
                  {c.wrong.length > 0 && ` · wrong ${c.wrong.map((r) => `${r.sku} $${r.unit_price} (min ${r.min_qty})`).join(", ")}`}
                </p>
              ))}
          {xp?.blocked?.map((u) => (
            <p key={`b-${u.id}`}>
              <strong>Blocked by guardrail</strong> <code>{u.id}</code> {u.title}: {u.reason}
            </p>
          ))}
          {xp?.unfaithful?.map((u) => (
            <p key={`u-${u.id}`}>
              <strong>Unfaithful</strong> <code>{u.id}</code> {u.title}: numbers not in the finding{" "}
              {u.unsupported_numbers.join(", ")}
            </p>
          ))}
          {xp?.irrelevant?.map((u) => (
            <p key={`i-${u.id}`}>
              <strong>Off-topic</strong> <code>{u.id}</code> {u.title}: {u.reason}
            </p>
          ))}
        </div>
      )}
    </section>
  );
}
