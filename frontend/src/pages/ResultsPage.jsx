import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Alert, Bars, Spinner, Tile, money, num } from "../ui.jsx";

export default function ResultsPage({ runId, run, navigate }) {
  const [info, setInfo] = useState(null);
  const [loadError, setLoadError] = useState(null);

  useEffect(() => {
    api.getRun(runId).then(setInfo).catch((e) => setLoadError(e.message));
  }, [runId, run.status, run.report?.seq]);

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

  const report = info?.report || run.report;
  if (!report) {
    if (info?.status === "FAILED" || run.status === "FAILED") {
      return <Alert title="The run failed while writing the report.">{info?.error || run.error}</Alert>;
    }
    if (info?.status === "AWAITING_REVIEW") {
      return (
        <div className="card narrow">
          <h2>The review has not been submitted yet.</h2>
          <button className="btn btn-primary" onClick={() => navigate("review", runId)}>
            Go to review →
          </button>
        </div>
      );
    }
    return <Spinner label="Writing the report…" />;
  }

  const typeRows = (report.by_type || []).map((t) => ({ key: t.type, type: t.type, label: t.label, amount: t.amount, count: t.count }));
  const vendorRows = (report.top_vendors || []).map((v) => ({ label: v.vendor, amount: v.amount, count: v.count }));

  return (
    <div className="results">
      <div className="page-head">
        <div>
          <h1>Results</h1>
          <p className="muted">
            Report generated {report.generated_at} · run <code>{runId}</code>
            {info?.params && ` · ${info.params.date_from} to ${info.params.date_to}`}
          </p>
        </div>
        <div className="head-actions">
          <a className="btn btn-primary" href={api.reportUrl(runId)} target="_blank" rel="noreferrer">
            Open report
          </a>
          <a className="btn" href={api.csvUrl(runId)} download>
            Download CSV
          </a>
        </div>
      </div>

      <div className="tiles tiles-4">
        <Tile label="Approved leakage" value={money(report.approved_total)} sub={`${report.approved_count} findings`} tone="good" />
        <Tile label="Identified by the agent" value={money(report.identified_total)} sub={`${report.identified_count} findings`} />
        <Tile label="Rejected" value={num(report.rejected_count)} />
        <Tile label="Not reviewed" value={num(report.undecided_count)} sub="left out of the report" />
      </div>

      <div className="results-grid">
        <section className="card">
          <h2>Approved leakage by type</h2>
          <Bars rows={typeRows} empty="No findings were approved." />
        </section>
        <section className="card">
          <h2>Top 10 vendors</h2>
          <Bars rows={vendorRows} empty="No findings were approved." />
        </section>
      </div>

      <p className="muted small">
        Open the report in a new tab and use your browser&apos;s Print command (Save as PDF) to share it. The CSV opens
        directly in Excel.
      </p>
      <button className="btn btn-ghost" onClick={() => navigate("start")}>
        Start a new analysis
      </button>
    </div>
  );
}
