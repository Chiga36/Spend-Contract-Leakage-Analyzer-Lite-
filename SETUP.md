# Setup guide

Step-by-step instructions to install and run the **Spend & Contract Leakage Analyzer** on a fresh machine,
with or without an LLM (Gemini, OpenAI or Claude). For what the app does, see [README.md](README.md).

When you're done, the app runs at **http://127.0.0.1:8000** as a single process (`python run.py`).

---

## 1. Requirements

| Tool | Version | Why |
|---|---|---|
| Python | **3.11 or newer** (3.13 recommended) | Backend, MCP server, data generator |
| Node.js | **20.19 or newer** (22 LTS recommended) | Builds the web UI once (Vite 8). Not needed on a machine that gets a ready-made `frontend/dist/` |
| Disk / network | about 500 MB, and internet access for `pip` / `npm` | |

An LLM key is **optional**. Without one the app runs fully offline.

---

## 2. Install Python and Node

### Ubuntu 22.04 / 24.04

Ubuntu 22.04 ships Python 3.10 (too old) and Node 12 (too old), so install newer versions next to them:

```bash
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.13 python3.13-venv
```

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
```

Check:

```bash
python3.13 --version    # Python 3.13.x
node --version          # v22.x
```

> `python3 --version` will still say 3.10. That's expected: Ubuntu's system tools need it, so **don't**
> change what `python3` points to. The project uses 3.13 through its own virtual environment (step 3).

### Windows

1. Install Python 3.13 from https://www.python.org/downloads/ and tick **"Add python.exe to PATH"**.
2. Install Node.js 22 LTS from https://nodejs.org/.
3. In a new terminal, run `python --version` and `node --version` to check both.

### macOS

```bash
brew install python@3.13 node@22
```

---

## 3. Install the project

Run these from the project folder (the one that has `run.py`).

### Linux / macOS

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python seed/generate.py
cd frontend && npm install && npm run build && cd ..
```

### Windows (PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python seed\generate.py
cd frontend; npm install; npm run build; cd ..
```

What each step does:

| Step | Result |
|---|---|
| `venv` | A private Python environment in `.venv/` |
| `pip install -r requirements.txt` | FastAPI, LangGraph, MCP, and the Anthropic and OpenAI clients. The OpenAI client is also used for Gemini |
| `seed/generate.py` | The synthetic test data: `data/erp.db`, `data/contracts/*.md` and `data/planted_anomalies.json` (the answer key for the evaluation metrics) |
| `npm install && npm run build` | The web UI in `frontend/dist/` |

> **Use `requirements.txt`, not `requirements.lock`, on Linux and macOS.** The lock file was generated
> on Windows and pins `pywin32`, which only installs on Windows.

---

## 4. Configure (optional): `.env`

Copy the template and edit the copy:

```bash
cp .env.example .env
```

On Windows: `copy .env.example .env`

> Put keys in **`.env`**, never in `.env.example`. The template is meant to be shared; `.env` is
> listed in `.gitignore`, so it's never committed.

### Option A: offline (default, no key)

```ini
LLM_MODE=off
```

Nothing leaves the machine. Contracts whose prices exist only as text, and borderline vendor-name matches,
are skipped and listed as warnings.

### Option B: Google Gemini (free tier, no billing)

1. Create a key at https://aistudio.google.com/apikey.
2. In `.env`:

```ini
LLM_MODE=gemini
GEMINI_API_KEY=your-key-here
LLM_MODEL=                 # blank = gemini-3.5-flash
```

Free-tier notes:
- The free tier allows only a few requests per minute. The app sends at most 2 at a time and backs off
  automatically, so a run takes a minute or two.
- The free tier also has a **daily quota per model** (20 requests per day on a recent key). A full run makes
  about 17 requests, so expect about **one full Gemini run per day per model**. Each model has its own
  quota: when one runs out, switch `LLM_MODEL` (for example to `gemini-3.5-flash-lite`) or wait for the reset.
- Google may use data sent with free-tier keys to improve its products. That's fine for the synthetic
  data; use a paid or company key for real client data.
- Google retires older models for new users (for example `gemini-2.5-flash` now returns 404). If calls fail
  with 404, set `LLM_MODEL` to a newer model, such as `gemini-3.8-flash`.

### Option C: OpenAI

```ini
LLM_MODE=openai
OPENAI_API_KEY=sk-...
LLM_MODEL=                 # blank = gpt-5
```

The OpenAI account must have API credits (https://platform.openai.com/settings/organization/billing/).

### Option D: Anthropic Claude

```ini
LLM_MODE=claude
ANTHROPIC_API_KEY=sk-ant-...
LLM_MODEL=                 # blank = claude-opus-5-5
```

### Other settings

| Setting | Default | Notes |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8000` | |
| `PRICE_TOLERANCE_PCT` | `2` | Default on the Start page; can be changed per run |
| `HTTPS_PROXY` | | Corporate proxy for the LLM API |
| `ERP_DB_PATH`, `CONTRACTS_DIR`, `OUTPUT_DIR` | `data/erp.db`, `data/contracts`, `output` | Relative to the project folder |
| `GROUND_TRUTH_PATH` | `data/planted_anomalies.json` | Turns on the evaluation metrics |
| `UPLOADS_DIR` | `data/uploads` | Where datasets uploaded from the Start page are saved |
| `LLM_MAX_CALLS_PER_RUN` / `LLM_MAX_CHARS_PER_RUN` | `100` / `1500000` | Guardrail budget per run (see README, *Guardrails*) |

Real environment variables override `.env`. **Restart the app after any change to `.env`.**

---

## 5. Run

```bash
.venv/bin/python run.py          # Windows: .venv\Scripts\python run.py
```

Open **http://127.0.0.1:8000**. The header shows `MCP · stdio · pid …` (data server running) and
`LLM · Gemini` (or `LLM off · offline`).

Then:
1. **Start**: choose the data. **Demo data** is the synthetic data from step 3. **Upload your own** takes
   your ERP export as CSV files plus contract documents (or one .zip); click **Download sample files** for
   the exact format (README → *Using your own data*). Then click **Analyze**.
2. **Analyze**: watch the steps. When it finishes, the **Evaluation against planted anomalies** panel
   scores the run (precision, recall, F1, dollar recall, LLM extraction and explanation quality).
3. **Review**: approve, reject or adjust findings, or re-run with another tolerance.
4. **Results**: the HTML report and CSV, also saved in `output/<run id>/`.

Stop the app with `Ctrl+C`.

---

## 6. Check it works

Run the tests:

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

All tests should pass. They use fake LLM clients, so they need no key and make no network calls.

To score a full run from the terminal and compare providers:

```bash
.venv/bin/python evaluate.py --mode off
.venv/bin/python evaluate.py --mode gemini
```

Expected: offline finds 64 of 72 planted anomalies (100% precision, 88.9% recall). With a working LLM,
recall should reach or approach 72 of 72.

---

## 7. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ensurepip is not available` when creating the venv | Install the venv package for your Python: `sudo apt install python3.13-venv` |
| `No module named pip` | Use the venv's pip (`.venv/bin/pip`), not the system one |
| `Unable to locate package python3.13` | Add the deadsnakes PPA first (step 2) |
| `pywin32==312` not found | You used `requirements.lock` on Linux/macOS. Use `requirements.txt` |
| `npm run build` fails or Vite complains about Node | Node is too old. Install Node 22 (step 2) |
| `sudo: a password is required` | Type your password in the terminal where `sudo` asks for it (nothing shows on screen while you type) |
| UI says *"No GEMINI_API_KEY is set"* | The key isn't in `.env` (check it isn't in `.env.example`), or the app wasn't restarted |
| LLM calls fail with **404** | That model was retired. Set `LLM_MODEL` to a current one (see Option B) |
| LLM calls fail with **429 – no credits** (OpenAI) | Add API credits to the OpenAI account |
| LLM calls fail with **429** (Gemini free tier) | Per-minute limit: the app retries, so wait a minute and run again. *"Quota exceeded … per day"*: the model's daily free quota is used up. Switch `LLM_MODEL` to another model or wait for the reset |
| 🛡️ *Guardrail* notes on the Analyze page | Expected: an input or output check fired (e.g. tax IDs redacted). See README, *Guardrails* |
| LLM calls fail with **503 – high demand** | Google's model is overloaded. Try later or use another model in `LLM_MODEL` |
| `address already in use` on port 8000 | Another copy is running. Stop it, or set `PORT=8001` in `.env` |
| Upload says *missing column* / *not found in vendors.csv* | Fix the listed file and row; the sample files show the expected format. Nothing is saved until the whole upload is valid |
| Evaluation panel doesn't appear | `data/planted_anomalies.json` is missing (run `seed/generate.py`), or the run used uploaded data, which has no answer key |

If an LLM call fails, the run still completes with the offline result, and the failed calls are listed on
the Analyze page.
