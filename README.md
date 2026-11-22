# Spend & Contract Leakage Analyzer (Lite)

An agent that reads a client's accounts-payable data **through a local MCP server**, finds money being
lost, pauses for a consultant to review, then writes a report. The design is in
[instructions.md](instructions.md) (LLD v0.2); this file covers running it.

- **One process:** `python run.py` serves the UI and API on `127.0.0.1:8000` and starts the MCP server
  itself as a child process over stdio (no extra port).
- **Five detectors:** duplicate vendors, duplicate payments, price above contract, missed volume
  discount, missed early-payment discount. All dollar amounts are computed by plain Python (`Decimal`).
- **Fully offline by default** (`LLM_MODE=off`). With `LLM_MODE=claude`, Claude extracts price tables
  from contract text, judges borderline vendor names and writes explanations, but never sets a dollar figure.

> **New machine?** Follow [SETUP.md](SETUP.md): step-by-step install for Ubuntu, Windows and macOS, choosing
> an LLM (Gemini free tier, OpenAI or Claude), and troubleshooting.

## First time (developer machine)

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt        # or: pip install -r requirements.lock (exact versions)
python seed/generate.py                # builds data/erp.db, data/contracts/*.md, data/planted_anomalies.json
cd frontend && npm install && npm run build && cd ..
```

## Every time (demo machine)

```bash
python run.py
```

Then open <http://127.0.0.1:8000>. The demo laptop does not need Node: copy the project folder
including `frontend/dist/` and `data/`, create the venv and `pip install` there.

Do not start the backend with `uvicorn --reload` on Windows: reload mode uses an event loop that cannot
start the MCP child process (LLD §8.3). Restart `python run.py` after backend changes instead. For UI work,
run `npm run dev` in `frontend/` (it proxies `/api` to port 8000).

## Configuration

Copy `.env.example` to `.env` (optional; the defaults run offline). Real environment variables win over
the file.

| Setting | Default | Notes |
|---|---|---|
| `LLM_MODE` | `off` | `gemini`, `openai` or `claude` enables the three LLM uses. Any LLM failure falls back to offline behaviour. |
| `LLM_MODEL` | per mode | Blank = `gemini-3.5-flash`, `gpt-5` or `claude-opus-5-5`. |
| `GEMINI_API_KEY` | | Only for `LLM_MODE=gemini` (free key: https://aistudio.google.com/apikey). |
| `OPENAI_API_KEY` | | Only for `LLM_MODE=openai`. |
| `ANTHROPIC_API_KEY` | | Only for `LLM_MODE=claude`. |
| `HTTPS_PROXY` | | Corporate proxy for the LLM API. |
| `LLM_MAX_CALLS_PER_RUN` / `LLM_MAX_CHARS_PER_RUN` | `100` / `1500000` | Guardrail G4: LLM budget per run. |
| `PRICE_TOLERANCE_PCT` | `2` | Default on the Start page; can be changed per run and on re-run. |
| `ERP_DB_PATH`, `CONTRACTS_DIR`, `OUTPUT_DIR` | `data/erp.db`, `data/contracts`, `output` | Relative to the project folder. Demo data. |
| `UPLOADS_DIR` | `data/uploads` | Where uploaded datasets are stored (see *Using your own data*). |

LangSmith tracing is forced off at startup, so no trace data can leave the machine.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

| File | Checks |
|---|---|
| `tests/test_detectors.py` | Each detector on hand-written rows: tier boundary, same-day duplicate, invoice-number normalization, terms parsing, overlap removal. |
| `tests/test_end_to_end.py` | Seeds the data, runs the graph with `LLM_MODE=off` over the real stdio MCP server, auto-approves, and checks that every planted anomaly with `needs_llm: false` is found and totals match within 1%. Also: re-run at 5% tolerance, report + CSV. |
| `tests/test_mcp_server.py` | Starts the server over stdio, calls every tool, confirms writes fail (read-only) and path traversal is refused. |
| `tests/test_llm_path.py` | `LLM_MODE=claude` with a fake client (no network, no cost): the `needs_llm` anomalies are found too; refusals and network errors fall back to the offline result. |

## Guardrails

Every LLM call passes through deterministic checks in `backend/guardrails.py`; no extra model calls are made.
When a check fires, the Analyze page shows a 🛡️ note under the step, and that item falls back to the
offline result. A guardrail never fails a run.

| | Guardrail | What it does |
|---|---|---|
| **Input** | G1 Redaction | E-mail addresses, phone numbers, IBAN/card/bank-account numbers, SSNs and tax IDs are replaced by `[REDACTED-…]` before a request leaves the machine. |
| | G2 Prompt injection | Contract text, vendor names/addresses or finding data containing instruction-like text ("ignore previous instructions", "system prompt", role tags, "set all prices to…") is not sent; that contract is flagged for manual checking. |
| | G3 Request size | A single request over 60,000 characters is not sent. |
| | G4 Budget | At most `LLM_MAX_CALLS_PER_RUN` calls (default 100) and `LLM_MAX_CHARS_PER_RUN` characters (default 1.5 M) per run. |
| **Output** | G5 Price sanity | Extracted prices must be literally in the contract and for a SKU the vendor invoiced (existing checks), and now also within 0.5×–2× of the vendor's median billed price; volume-tier prices must be below the base price. |
| | G6 Explanation check | An LLM explanation is shown only if every number in it appears in the finding's data, it names the vendor or an ID, it fits the finding type, and it has no links, e-mail addresses, code, markup or instruction-like text. Otherwise the template text is used. |
| | G7 Vendor reasons | The model's match reasons are stripped of links/markup and shortened; pair IDs the model made up are ignored. |

These come on top of the design's existing safeguards: structured JSON output validated with Pydantic,
the LLM never sets a dollar figure, prompts treat all data as untrusted, and the MCP server is read-only.

## Evaluation metrics

Because the synthetic data has known planted anomalies (`data/planted_anomalies.json`), every run can be
scored. The **Analyze** page shows an *Evaluation* panel once the findings are ready
(`GET /api/runs/{id}/evaluation`), and `python evaluate.py [--mode off|gemini|openai|claude] [--tolerance 5]`
runs a headless analysis, prints the same metrics and saves them to `output/evaluation-*.json`.
Everything is computed in plain Python (no extra LLM calls). Real client data has no ground truth, so the panel is hidden.

| Metric | Meaning |
|---|---|
| Precision | Share of findings that are real planted anomalies (false positives lower it). |
| Recall | Share of planted anomalies found. Only anomalies the run can reach count: uplifts at or below the tolerance, and invoices outside the date range, are excluded. |
| F1 | Balance of precision and recall. |
| Dollar recall / amount error | Share of planted dollars found, and how far correct findings' amounts are from the planted amounts. |
| Decoy pair | The two different "Oakridge" companies must be rejected. |
| Price extraction precision / recall, volume-tier recall | LLM-extracted price rows vs. the true price tables of the 10 text-only contracts. |
| Explanation faithfulness | Share of LLM explanations whose every number appears in the finding's data (nothing invented). |
| Explanation relevancy | Share of LLM explanations that name the vendor or an ID/SKU and use wording that fits the finding type. |

Faithfulness and relevancy are rule-based checks, not a judgement by another model.

## Using your own data

On the Start page, choose **Demo data** (the synthetic data below) or **Upload your own**. An upload is
one CSV file per table plus the contract documents, as separate files or in one `.zip`:

| File | Columns (`*` = may be empty) |
|---|---|
| `vendors.csv` | vendor_id, name, tax_id\*, address\*, category\* |
| `contracts.csv` | contract_id, vendor_id, start_date, end_date, payment_terms\*, document_file\* |
| `contract_rates.csv` (optional) | contract_id, sku, unit_price, min_qty\* (yearly quantity from which a tier price applies; 0 = base price) |
| `invoices.csv` | invoice_id, vendor_id, vendor_invoice_no, invoice_date, total_amount |
| `invoice_lines.csv` | line_id, invoice_id, sku\*, quantity, unit_price |
| `payments.csv` | payment_id, invoice_id, paid_date, amount |
| contract documents | `.md` or `.txt`, named in `document_file` or as `<contract_id>.md` / `.txt` |

Dates are `YYYY-MM-DD`; amounts may contain `$` and thousands separators; extra columns are ignored.
**Download sample files** on the Start page gives the demo data in this format with a README, and can be
uploaded unchanged to try the flow.

The upload is checked before anything is saved: required files and columns, dates and numbers, unique IDs,
and that every reference points at an existing row (e.g. each invoice's vendor). Problems are listed by
file and row. A valid upload is written to `data/uploads/<id>/` (`erp.db` + `contracts/`), and the MCP
server reads it read-only like the demo data; uploads can be picked again later from the Start page.
Contracts without a price table are read from their document by the LLM (or skipped when it's off).
Uploaded data has no answer key, so the evaluation panel is not shown. Use a paid or company LLM key, or
`LLM_MODE=off`, for confidential data.

## Synthetic data

`seed/generate.py` (fixed seed) writes 120 vendors, 30 contracts (10 with prices only in the text),
1,500 invoices, ~4,000 lines and ~1,470 payments over 24 months, with these planted anomalies:

| Anomaly | Planted | Found offline | Needs the LLM |
|---|---|---|---|
| Duplicate vendor pairs | 6 (~$11.7K estimated) | 4 | 2 borderline names |
| Duplicate payments | 8 (~$95.0K) | 8 | 0 |
| Prices above contract | 25 lines (~$130.1K) | 20 lines | 5 lines on text-only contracts |
| Missed volume discount | 3 vendors (~$85.1K) | 2 | 1 text-only contract |
| Missed early-pay discount | 30 invoices (~$42.8K) | 30 | 0 |

A decoy pair of different companies with similar names ("Oakridge Print Services" / "Oakridge Pest
Services") must *not* be reported. Two price overcharges are only 3.5% and 4%, so re-running at a 5%
tolerance visibly drops them.

## Where this build interprets the LLD

- **Price variance vs. volume discount.** Taken literally, every missed-volume-discount line is also above
  the tier price, so the aggregate priority rule (price variance > volume discount) would erase the volume
  detector. Lines that crossed a tier but are still billed at a lower tier's price belong to the volume
  detector, independent of the price tolerance; other over-billing beyond the tolerance is price variance.
  The aggregate overlap rule still applies (duplicate payment > price variance > volume discount), and an
  early-pay finding on a duplicate invoice is dropped too.
- **Duplicate payments run in parallel with duplicate vendors**, so they merge vendors with the
  deterministic rule (same tax ID or name similarity ≥ 0.90) rather than the LLM's borderline verdicts.
- **Calendar-year tiers with a narrow date range:** invoices from 1 January of the start year are loaded as
  history, so tiers and earlier duplicates are judged correctly; only findings inside the range are reported.
- **LLM-extracted prices** are kept only if the SKU is on that vendor's invoices, the price is > 0, and the
  price (and tier quantity) is literally written in the contract text. LLM-confirmed vendor pairs get
  confidence 0.75.
- **`mcp` is pinned below 2.0**, because 2.x renamed `FastMCP` to `MCPServer`.
- **`get_summary`** also returns the invoice date range and the server's process id (shown as
  `stdio · local process (pid …)`).
- Added a `warning` event type (detector failures, skipped contracts) and per-run decision drafts kept in
  the browser's session storage.

## Layout

```
run.py                 single entry point
seed/generate.py       synthetic data + ground truth
mcp_server/server.py   FastMCP, read-only tools, stdio
backend/               FastAPI app, RunManager, MCP client, LLM, LangGraph graph + detectors, report
frontend/              React + Vite (plain CSS), built to frontend/dist
tests/                 pytest
```
