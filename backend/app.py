"""FastAPI app (LLD §8): lifespan (MCP child process), REST + Server-Sent Events, static UI."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from datetime import date

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import PROVIDERS, ROOT, azure_auth, get_settings, llm_configured, provider_host
from .datasets import DatasetError, build_dataset, get_meta, list_datasets, sample_zip
from .evaluation import evaluate, load_ground_truth
from .graph.build import build_graph
from .graph.nodes import MAX_ITERATIONS, Nodes
from .llm import Llm
from .mcp_client import McpClient
from .models import ReviewPayload, RunParams
from .runs import TERMINAL, RunManager, RunRecord

log = logging.getLogger(__name__)

settings = get_settings()
mcp_client = McpClient(settings)
llm = Llm(settings)
manager = RunManager(build_graph(Nodes(mcp_client, llm, settings)))

DIST = ROOT / "frontend" / "dist"
RUN_ID = re.compile(r"^[0-9a-f]{12}$")
KEEPALIVE_S = 15


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the MCP server as a child process (stdio) and share one session across all runs."""
    stack = AsyncExitStack()
    try:
        await mcp_client.start(stack)
    except Exception as e:  # noqa: BLE001 - reported by /api/health; Analyze is disabled
        mcp_client.session = None
        mcp_client.error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        log.error("MCP server failed to start: %s", mcp_client.error)
        with suppress(BaseException):
            await stack.aclose()
        stack = AsyncExitStack()
    try:
        yield
    finally:
        with suppress(BaseException):       # closes the session; the child process exits
            await stack.aclose()


app = FastAPI(title="Spend & Contract Leakage Analyzer", version="0.2", lifespan=lifespan)


def _get_run(run_id: str) -> RunRecord:
    rec = manager.runs.get(run_id) if RUN_ID.match(run_id) else None
    if rec is None:
        raise HTTPException(404, "Run not found. Run history is cleared when the server restarts.")
    return rec


# ---------------------------------------------------------------------------- API
@app.get("/api/health")
async def health() -> dict:
    provider = PROVIDERS.get(settings.llm_mode)
    out = {"mcp": "down", "mcp_error": mcp_client.error, "transport": "stdio", "server": mcp_client.server_name,
           "pid": mcp_client.pid, "llm_mode": settings.llm_mode,
           "llm_model": settings.llm_model if provider else None,
           "llm_provider": provider[0] if provider else None,
           "llm_host": provider_host(settings.llm_mode),
           "llm_key_name": ("AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT and AZURE_OPENAI_API_KEY"
                            if settings.llm_mode == "azure" else provider[2][0] if provider else None),
           "llm_key_configured": llm_configured(settings.llm_mode),
           "llm_auth": azure_auth() if settings.llm_mode == "azure" else None,
           "data": None, "date_range": None, "defaults": None, "max_iterations": MAX_ITERATIONS}
    if mcp_client.session is not None:
        try:
            s = await mcp_client.call_tool("get_summary")
        except Exception as e:  # noqa: BLE001
            out["mcp_error"] = str(e)
        else:
            out.update(mcp="up", mcp_error=None, pid=s.get("pid"),
                       data={**s["tables"], "contracts_without_rate_table": s.get("contracts_without_rate_table")},
                       date_range={"min": s["invoice_date_min"], "max": s["invoice_date_max"]},
                       defaults={"date_from": s["invoice_date_min"], "date_to": s["invoice_date_max"],
                                 "price_tolerance_pct": settings.price_tolerance_pct})
    return out


@app.post("/api/runs", status_code=201)
async def create_run(body: RunParams | None = None) -> dict:
    if not mcp_client.up:
        raise HTTPException(503, f"The MCP server is not running: {mcp_client.error or 'not started'}")
    body = body or RunParams()
    dataset = body.dataset or "demo"
    if dataset != "demo" and get_meta(settings.uploads_dir, dataset) is None:
        raise HTTPException(404, "Uploaded dataset not found. Upload the files again.")
    s = await mcp_client.call_tool("get_summary", {"dataset": dataset})
    if not s["invoice_date_min"]:
        raise HTTPException(422, "This dataset has no invoices.")
    date_from = body.date_from or date.fromisoformat(s["invoice_date_min"])
    date_to = body.date_to or date.fromisoformat(s["invoice_date_max"])
    if date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    tolerance = settings.price_tolerance_pct if body.price_tolerance_pct is None else body.price_tolerance_pct
    rec = manager.start({"date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
                         "price_tolerance_pct": tolerance, "dataset": dataset})
    return {"run_id": rec.run_id}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    return _get_run(run_id).view()


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request) -> Response:
    """Server-Sent Events. Replays from the start (or after Last-Event-ID on auto-reconnect)."""
    rec = _get_run(run_id)
    try:
        start = int(request.headers.get("last-event-id", "-1")) + 1
    except ValueError:
        start = 0
    if rec.status in TERMINAL and start >= len(rec.events):
        return Response(status_code=204)      # finished and fully delivered: tells EventSource to stop

    async def stream():
        i = start
        yield "retry: 3000\n\n"
        while True:
            async with rec.cond:
                try:
                    await asyncio.wait_for(rec.cond.wait_for(lambda: len(rec.events) > i), KEEPALIVE_S)
                except TimeoutError:
                    pass
            if i >= len(rec.events):
                yield ": keep-alive\n\n"
                continue
            while i < len(rec.events):
                yield f"id: {i}\ndata: {json.dumps(rec.events[i])}\n\n"
                i += 1
            if rec.status in TERMINAL and i >= len(rec.events):
                return

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/runs/{run_id}/review")
async def review(run_id: str, payload: ReviewPayload) -> dict:
    rec = _get_run(run_id)
    if rec.status != "AWAITING_REVIEW":
        raise HTTPException(409, f"Run is {rec.status}; a review is accepted only while it is AWAITING_REVIEW.")
    if payload.action == "rerun" and rec.iteration >= MAX_ITERATIONS:
        raise HTTPException(409, f"Re-run limit reached ({MAX_ITERATIONS} iterations). Submit the review instead.")
    known = {f["id"] for f in (rec.review or {}).get("findings", [])}
    unknown = sorted(set(payload.decisions) - known)
    if unknown:
        raise HTTPException(422, f"Unknown finding id(s): {', '.join(unknown[:10])}")
    manager.resume(rec, payload.model_dump(mode="json"))
    return {"run_id": run_id, "status": rec.status}


@app.get("/api/runs/{run_id}/evaluation")
async def run_evaluation(run_id: str) -> dict:
    """Accuracy and relevancy metrics of this run against the synthetic data's planted anomalies."""
    rec = _get_run(run_id)
    if rec.status not in ("AWAITING_REVIEW", "COMPLETED"):
        raise HTTPException(409, f"Run is {rec.status}; metrics are available once its findings are ready.")
    values = (await manager.graph.aget_state({"configurable": {"thread_id": run_id}})).values
    params = values.get("params") or rec.params
    gt = load_ground_truth(settings.ground_truth_path) if (params.get("dataset") or "demo") == "demo" else None
    if gt is None:
        raise HTTPException(404, "No ground truth for this data (only the synthetic seed data has one).")
    result = await asyncio.to_thread(evaluate, values.get("findings") or [], gt, params,
                                     values.get("rate_cards"), values.get("invoices"))
    result["llm"] = {"mode": settings.llm_mode, "model": settings.llm_model if settings.llm_mode in PROVIDERS else None}
    return result


# ---------------------------------------------------------------------------- datasets
async def _dataset_view(meta: dict) -> dict:
    s = await mcp_client.call_tool("get_summary", {"dataset": meta["dataset_id"]})
    return {**meta, "data": {**s["tables"], "contracts_without_rate_table": s.get("contracts_without_rate_table")},
            "date_range": {"min": s["invoice_date_min"], "max": s["invoice_date_max"]}}


@app.get("/api/datasets")
async def datasets() -> list[dict]:
    """Datasets uploaded earlier (newest first)."""
    return list_datasets(settings.uploads_dir)


@app.post("/api/datasets", status_code=201)
async def upload_dataset(files: list[UploadFile] = File(...), label: str | None = Form(None)) -> dict:
    """Upload the ERP export (CSV per table) and contract documents, or one .zip with all of them."""
    if not mcp_client.up:
        raise HTTPException(503, f"The MCP server is not running: {mcp_client.error or 'not started'}")
    raw = [(f.filename or "file", await f.read()) for f in files]
    try:
        meta = await asyncio.to_thread(build_dataset, raw, settings.uploads_dir, label)
    except DatasetError as e:
        raise HTTPException(422, {"message": "The files could not be used.", "errors": e.errors}) from None
    return await _dataset_view(meta)


@app.get("/api/datasets/sample.zip")
async def dataset_sample() -> Response:
    """The demo data in the upload format: a template, and a file that can be uploaded as-is."""
    data = await asyncio.to_thread(sample_zip, settings.erp_db_path, settings.contracts_dir)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="leakage-analyzer-sample-data.zip"'})


@app.get("/api/datasets/{dataset_id}")
async def dataset(dataset_id: str) -> dict:
    meta = get_meta(settings.uploads_dir, dataset_id)
    if meta is None:
        raise HTTPException(404, "Uploaded dataset not found.")
    return await _dataset_view(meta)


def _output_file(run_id: str, name: str):
    if not RUN_ID.match(run_id):
        raise HTTPException(404, "Run not found")
    path = settings.output_dir / run_id / name
    if not path.is_file():
        raise HTTPException(404, "The report is not ready yet.")
    return path


@app.get("/api/runs/{run_id}/report.html")
async def report_html(run_id: str) -> FileResponse:
    return FileResponse(_output_file(run_id, "report.html"), media_type="text/html; charset=utf-8")


@app.get("/api/runs/{run_id}/findings.csv")
async def findings_csv(run_id: str) -> FileResponse:
    return FileResponse(_output_file(run_id, "findings.csv"), media_type="text/csv; charset=utf-8",
                        filename=f"findings-{run_id}.csv")


# ---------------------------------------------------------------------------- static UI (mounted last)
if DIST.is_dir():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="ui")
else:
    @app.get("/", response_class=HTMLResponse)
    async def ui_not_built() -> str:
        return ("<h1>UI not built</h1><p>Build it once on a machine with Node.js: "
                "<code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code>, then restart "
                "<code>python run.py</code>. The API is available under <code>/api</code>.</p>")
