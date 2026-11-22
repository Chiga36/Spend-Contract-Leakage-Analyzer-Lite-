import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { Alert, Spinner, Tile, num } from "../ui.jsx";

function monthsBetween(from, to) {
  const [y1, m1] = from.split("-").map(Number);
  const [y2, m2] = to.split("-").map(Number);
  return (y2 - y1) * 12 + (m2 - m1) + 1;
}

// The CSV files an upload needs (contract_rates is optional).
const EXPECTED = [
  { file: "vendors.csv", what: "Vendor master: vendor_id, name, tax_id, address" },
  { file: "contracts.csv", what: "contract_id, vendor_id, start/end date, payment_terms, document_file" },
  { file: "contract_rates.csv", what: "Agreed prices: contract_id, sku, unit_price, min_qty", optional: true },
  { file: "invoices.csv", what: "invoice_id, vendor_id, vendor_invoice_no, invoice_date, total_amount" },
  { file: "invoice_lines.csv", what: "line_id, invoice_id, sku, quantity, unit_price" },
  { file: "payments.csv", what: "payment_id, invoice_id, paid_date, amount" },
];

const baseName = (f) => f.name.split(/[\\/]/).pop().toLowerCase();

function DataTiles({ data, range }) {
  return (
    <div className="tiles tiles-3">
      <Tile label="Vendors" value={num(data.vendors)} />
      <Tile label="Invoices" value={num(data.invoices)} sub={`${num(data.invoice_lines)} lines`} />
      <Tile label="Payments" value={num(data.payments)} />
      <Tile
        label="Contracts"
        value={num(data.contracts)}
        sub={`${data.contracts_without_rate_table} with prices only in the text`}
      />
      <Tile label="Period" value={`${monthsBetween(range.min, range.max)} months`} sub={`${range.min} to ${range.max}`} />
      <Tile label="Rate-table rows" value={num(data.contract_rates)} />
    </div>
  );
}

function Uploader({ onUploaded, previous, onPick }) {
  const [files, setFiles] = useState([]);
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [drag, setDrag] = useState(false);
  const input = useRef(null);

  const names = new Set(files.map(baseName));
  const hasZip = files.some((f) => baseName(f).endsWith(".zip"));
  const docs = files.filter((f) => /\.(md|txt)$/i.test(f.name) && !/^readme\./i.test(baseName(f))).length;
  const missing = EXPECTED.filter((e) => !e.optional && !names.has(e.file));
  const ready = files.length > 0 && (hasZip || missing.length === 0);

  function add(list) {
    const incoming = Array.from(list || []);
    setFiles((cur) => {
      const byName = new Map(cur.map((f) => [baseName(f), f]));
      for (const f of incoming) byName.set(baseName(f), f);
      return [...byName.values()];
    });
    setError(null);
  }

  async function upload() {
    setBusy(true);
    setError(null);
    try {
      onUploaded(await api.uploadDataset(files, label));
      setFiles([]);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="uploader">
      <div
        className={`dropzone ${drag ? "drag" : ""}`}
        onClick={() => input.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDrag(true);
        }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDrag(false);
          add(e.dataTransfer.files);
        }}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && input.current?.click()}
      >
        <strong>Drop files here or click to choose</strong>
        <span className="muted small">
          The CSV files below plus contract documents (.md or .txt), or one .zip with all of them
        </span>
        <input
          ref={input}
          type="file"
          multiple
          accept=".csv,.md,.txt,.zip"
          hidden
          onChange={(e) => {
            add(e.target.files);
            e.target.value = "";
          }}
        />
      </div>

      <ul className="expected small">
        {EXPECTED.map((e) => {
          const ok = names.has(e.file);
          return (
            <li key={e.file} className={ok ? "ok" : hasZip ? "" : e.optional ? "" : "todo"}>
              <span className="tick">{ok ? "✓" : hasZip ? "·" : e.optional ? "○" : "✗"}</span>
              <code>{e.file}</code>
              {e.optional && <span className="muted"> (optional)</span>}
              <span className="muted"> {e.what}</span>
            </li>
          );
        })}
        <li className={docs ? "ok" : ""}>
          <span className="tick">{docs ? "✓" : "○"}</span>
          Contract documents <span className="muted">({docs} selected · .md or .txt, named in document_file or as &lt;contract_id&gt;.txt)</span>
        </li>
        {hasZip && (
          <li className="ok">
            <span className="tick">✓</span>A .zip was selected; its contents are checked on upload.
          </li>
        )}
      </ul>

      <p className="small">
        <a href={api.sampleUrl} download>
          Download sample files (.zip)
        </a>
        <span className="muted">
          : the demo data in this format, with a README of every column. You can upload it unchanged to try this out.
        </span>
      </p>

      {files.length > 0 && (
        <label className="small">
          Name for this data (optional)
          <input type="text" value={label} maxLength={80} placeholder="e.g. Acme Corp FY2025 AP export" onChange={(e) => setLabel(e.target.value)} />
        </label>
      )}

      {error && (
        <Alert title={error.message}>
          {error.errors?.length > 0 && (
            <ul className="upload-errors">
              {error.errors.map((m) => (
                <li key={m}>{m}</li>
              ))}
            </ul>
          )}
        </Alert>
      )}

      <div className="upload-actions">
        <button className="btn btn-primary" type="button" onClick={upload} disabled={!ready || busy}>
          {busy ? "Checking…" : `Check and upload${files.length ? ` (${files.length} file${files.length > 1 ? "s" : ""})` : ""}`}
        </button>
        {files.length > 0 && (
          <button className="btn btn-ghost" type="button" onClick={() => setFiles([])} disabled={busy}>
            Clear
          </button>
        )}
        {!ready && files.length > 0 && <span className="muted small">Still needed: {missing.map((m) => m.file).join(", ")}</span>}
      </div>

      {previous.length > 0 && (
        <label className="small previous">
          Or use data uploaded earlier
          <select value="" onChange={(e) => e.target.value && onPick(e.target.value)}>
            <option value="">Choose…</option>
            {previous.map((d) => (
              <option key={d.dataset_id} value={d.dataset_id}>
                {d.label} · {d.created.slice(0, 16).replace("T", " ")}
              </option>
            ))}
          </select>
        </label>
      )}
    </div>
  );
}

export default function StartPage({ health, healthError, onRetry, navigate }) {
  const [source, setSource] = useState("demo"); // "demo" | "upload"
  const [uploaded, setUploaded] = useState(null); // dataset view from the API
  const [previous, setPrevious] = useState([]);
  const [form, setForm] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const up = health?.mcp === "up";
  const offline = !health?.llm_provider;
  const active =
    source === "demo"
      ? health?.data && { data: health.data, range: health.date_range }
      : uploaded && { data: uploaded.data, range: uploaded.date_range };

  useEffect(() => {
    if (up) api.datasets().then(setPrevious).catch(() => setPrevious([]));
  }, [up, uploaded]);

  // Reset the form to the chosen data's full period whenever the data changes.
  const activeKey = source === "demo" ? "demo" : uploaded?.dataset_id;
  useEffect(() => {
    if (!active) return setForm(null);
    setForm((f) => ({
      date_from: active.range.min,
      date_to: active.range.max,
      price_tolerance_pct: f?.price_tolerance_pct ?? health?.defaults?.price_tolerance_pct ?? 2,
    }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeKey, health?.defaults]);

  async function pick(id) {
    setError(null);
    try {
      setUploaded(await api.dataset(id));
    } catch (e) {
      setError(e.message);
    }
  }

  async function analyze(e) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { run_id } = await api.startRun({
        date_from: form.date_from,
        date_to: form.date_to,
        price_tolerance_pct: Number(form.price_tolerance_pct),
        dataset: source === "demo" ? "demo" : uploaded.dataset_id,
      });
      navigate("run", run_id);
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));
  const range = active?.range;

  return (
    <div className="start">
      <section className="hero">
        <h1>Find the money leaking out of accounts payable</h1>
        <p>
          The agent reads invoices, payments and contracts through a local MCP server, runs five deterministic
          leakage checks, pauses for your review, and writes a report. Every dollar figure is calculated by code,
          never by the language model.
        </p>
      </section>

      {healthError && (
        <Alert title="The backend is not reachable.">
          {healthError}. Start it with <code>python run.py</code>, then{" "}
          <button className="link" onClick={onRetry}>
            try again
          </button>
          .
        </Alert>
      )}
      {health && !up && (
        <Alert title="The local MCP server is not running, so analysis is disabled.">
          {health.mcp_error || "Unknown error."} If the data is missing, run <code>python seed/generate.py</code> and
          restart the server.{" "}
          <button className="link" onClick={onRetry}>
            Check again
          </button>
        </Alert>
      )}
      {!health && !healthError && <Spinner label="Connecting to the local server…" />}

      {up && health.data && (
        <div className="start-grid">
          <section className="card">
            <div className="card-head">
              <h2>Data</h2>
              <span className="source">Source: local MCP · stdio · pid {health.pid}</span>
            </div>

            <div className="segmented" role="tablist" aria-label="Data source">
              <button
                type="button"
                role="tab"
                aria-selected={source === "demo"}
                className={source === "demo" ? "on" : ""}
                onClick={() => setSource("demo")}
              >
                Demo data
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={source === "upload"}
                className={source === "upload" ? "on" : ""}
                onClick={() => setSource("upload")}
              >
                Upload your own
              </button>
            </div>

            {source === "demo" && (
              <>
                <p className="muted small">
                  Synthetic accounts-payable data with 72 planted leaks, so the run can be scored for accuracy.
                </p>
                <DataTiles data={health.data} range={health.date_range} />
              </>
            )}

            {source === "upload" && !uploaded && (
              <Uploader onUploaded={setUploaded} previous={previous} onPick={pick} />
            )}

            {source === "upload" && uploaded && (
              <>
                <div className="dataset-head">
                  <div>
                    <strong>{uploaded.label}</strong>
                    <span className="muted small"> · uploaded {uploaded.created.slice(0, 16).replace("T", " ")} · {uploaded.dataset_id}</span>
                  </div>
                  <button className="link small" type="button" onClick={() => setUploaded(null)}>
                    Use different files
                  </button>
                </div>
                <DataTiles data={uploaded.data} range={uploaded.date_range} />
                {uploaded.warnings?.map((w) => (
                  <Alert key={w} tone="warn">
                    {w}
                  </Alert>
                ))}
                <p className="muted small">
                  Uploaded data has no answer key, so the accuracy panel is not shown; the review step is where you
                  confirm the findings.
                </p>
              </>
            )}

            <p className="muted small">
              The database and contract files are read only by the MCP server process on this machine. It opens the
              database read-only and offers no tool that accepts SQL.
            </p>
          </section>

          <section className="card">
            <h2>Run an analysis</h2>
            {!form && <p className="muted">Upload the data, or switch to the demo data, to start an analysis.</p>}
            {form && range && (
              <form onSubmit={analyze} className="form">
                <div className="form-row">
                  <label>
                    From
                    <input type="date" value={form.date_from} min={range.min} max={range.max} onChange={set("date_from")} required />
                  </label>
                  <label>
                    To
                    <input type="date" value={form.date_to} min={range.min} max={range.max} onChange={set("date_to")} required />
                  </label>
                </div>
                <label>
                  Price tolerance (%)
                  <input
                    type="number"
                    min="0"
                    max="50"
                    step="0.5"
                    value={form.price_tolerance_pct}
                    onChange={set("price_tolerance_pct")}
                    required
                  />
                  <span className="hint">Invoice prices up to this much above the contract rate are not flagged.</span>
                </label>
                <div className={`mode-note ${offline ? "mode-off" : "mode-on"}`}>
                  {offline ? (
                    <>
                      <strong>LLM off: fully offline.</strong> Nothing leaves this machine. Contracts whose prices
                      exist only in the text, and borderline vendor-name matches, are skipped and listed as warnings.
                    </>
                  ) : (
                    <>
                      <strong>LLM on: {health.llm_model}.</strong> {health.llm_provider} reads one contract at a time to extract
                      missing price tables, judges borderline vendor names, and writes short explanations. Only
                      those excerpts are sent to {health.llm_host}.
                      {!health.llm_key_configured && ` No ${health.llm_key_name} is set, so these steps will fall back to offline behaviour.`}
                      {source === "upload" &&
                        " For confidential client data, use a paid or company API key (free tiers may use what you send), or set LLM_MODE=off."}
                    </>
                  )}
                </div>
                {error && <Alert>{error}</Alert>}
                <button className="btn btn-primary btn-lg" type="submit" disabled={busy || !up}>
                  {busy ? "Starting…" : source === "demo" ? "Analyze demo data" : "Analyze uploaded data"}
                </button>
              </form>
            )}
            {!form && error && <Alert>{error}</Alert>}
          </section>
        </div>
      )}
    </div>
  );
}
