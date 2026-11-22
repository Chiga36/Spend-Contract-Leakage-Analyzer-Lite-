"""Uploaded datasets (backend/datasets.py): validation, the sample files, and a full analysis of an
uploaded dataset through the MCP server."""
from __future__ import annotations

import asyncio
import io
import zipfile
from contextlib import AsyncExitStack
from dataclasses import replace

import pytest
from langgraph.types import Command

from backend import datasets as ds
from backend.graph.build import build_graph
from backend.graph.nodes import Nodes
from backend.llm import Llm
from backend.mcp_client import McpClient, McpToolError

GOOD = {
    "vendors.csv": "vendor_id,name,tax_id,address,category\nV1,Acme Inc,,1 Main St,Office\nV2,Bolt Co,,2 Side St,\n",
    "contracts.csv": "contract_id,vendor_id,start_date,end_date,payment_terms,document_file\n"
                     "C1,V1,2025-01-01,2025-12-31,2/10 NET 30,\nC2,V2,2025-01-01,2025-12-31,NET 30,c2-terms.txt\n",
    "contract_rates.csv": "contract_id,sku,unit_price,min_qty\nC1,A-100,10.00,0\n",
    "invoices.csv": "invoice_id,vendor_id,vendor_invoice_no,invoice_date,total_amount\n"
                    "I1,V1,X1,2025-02-01,\"$1,200.00\"\nI2,V2,Y1,2025-03-01,500\n",
    "invoice_lines.csv": "line_id,invoice_id,sku,quantity,unit_price\nL1,I1,A-100,100,12.00\nL2,I2,B-200,50,10\n",
    "payments.csv": "payment_id,invoice_id,paid_date,amount\nP1,I1,2025-02-05,1200\n",
    "c2-terms.txt": "Item B-200 shall be invoiced at $10.00 per unit.",
}


def _files(overrides: dict | None = None, drop: tuple = ()) -> list[tuple[str, bytes]]:
    files = {**GOOD, **(overrides or {})}
    return [(n, t.encode()) for n, t in files.items() if n not in drop]


def _errors(files, tmp_path) -> list[str]:
    with pytest.raises(ds.DatasetError) as e:
        ds.build_dataset(files, tmp_path)
    return e.value.errors


def test_valid_upload_is_written(tmp_path):
    meta = ds.build_dataset(_files(), tmp_path, "Test data")
    root = tmp_path / meta["dataset_id"]
    assert ds.DATASET_ID.match(meta["dataset_id"]) and meta["label"] == "Test data"
    assert (root / "erp.db").is_file() and (root / "contracts" / "c2-terms.txt").is_file()
    assert ds.list_datasets(tmp_path)[0]["dataset_id"] == meta["dataset_id"]
    assert ds.get_meta(tmp_path, "../etc") is None


@pytest.mark.parametrize("files,expected", [
    (_files(drop=("payments.csv",)), "Missing file(s): payments.csv"),
    (_files({"vendors.csv": "vendor_id,title\nV1,Acme\n"}), "missing column(s) name"),
    (_files({"invoices.csv": "invoice_id,vendor_id,vendor_invoice_no,invoice_date,total_amount\nI1,V1,X1,01/02/2025,5\n"}),
     "invoice_date should be a date"),
    (_files({"invoice_lines.csv": "line_id,invoice_id,sku,quantity,unit_price\nL1,I1,A,ten,1\n"}), "quantity should be a number"),
    (_files({"invoices.csv": "invoice_id,vendor_id,vendor_invoice_no,invoice_date,total_amount\nI1,V9,X1,2025-02-01,5\n"}),
     "vendor_id not found in vendors.csv: V9"),
    (_files({"vendors.csv": "vendor_id,name\nV1,A\nV1,B\nV2,C\n"}), "duplicate vendor_id V1"),
    (_files({"payments.csv": "payment_id,invoice_id,paid_date,amount\nP1,,2025-02-05,1\n"}), "invoice_id is empty"),
])
def test_invalid_uploads_are_rejected_with_reasons(files, expected, tmp_path):
    errors = _errors(files, tmp_path)
    assert any(expected in e for e in errors), errors
    assert not any(p.name.startswith("u") for p in tmp_path.iterdir())      # nothing half-written


def test_missing_documents_become_warnings_and_paths_are_ignored(tmp_path):
    contracts = ("contract_id,vendor_id,start_date,end_date,payment_terms,document_file\n"
                 "C1,V1,2025-01-01,2025-12-31,,\nC2,V2,2025-01-01,2025-12-31,,../../etc/passwd\n")
    meta = ds.build_dataset(_files({"contracts.csv": contracts}, drop=("c2-terms.txt",)), tmp_path)
    text = " ".join(meta["warnings"])
    assert "not uploaded: ../../etc/passwd" in text and "no price table and no document" in text and "C2" in text


def test_zip_upload_uses_base_names_only(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in GOOD.items():
            zf.writestr(f"../../export/{name}", text)
    meta = ds.build_dataset([("export.zip", buf.getvalue())], tmp_path)
    assert (tmp_path / meta["dataset_id"] / "contracts" / "c2-terms.txt").is_file()
    assert not (tmp_path.parent / "export").exists()


def test_sample_zip_round_trips_to_identical_findings(dataset, tmp_path):
    """The downloadable sample files, uploaded unchanged, analyse to exactly the demo results."""
    sample = ds.sample_zip(dataset.root / "erp.db", dataset.root / "contracts")
    names = set(zipfile.ZipFile(io.BytesIO(sample)).namelist())
    assert {f"{t}.csv" for t in ds.TABLES} <= names and "README.txt" in names
    uploads = tmp_path / "uploads"
    meta = ds.build_dataset([("sample.zip", sample)], uploads)
    assert meta["warnings"] == []
    settings = replace(dataset.settings(tmp_path / "out"), uploads_dir=uploads)
    dr = dataset.ground_truth["date_range"]

    async def run(name: str):
        async with AsyncExitStack() as stack:
            mcp = McpClient(settings)
            await mcp.start(stack)
            if name != "demo":
                assert (await mcp.call_tool("get_summary", {"dataset": name}))["tables"] == \
                       (await mcp.call_tool("get_summary"))["tables"]
                with pytest.raises(McpToolError):
                    await mcp.call_tool("list_vendors", {"dataset": "../../data"})
            graph = build_graph(Nodes(mcp, Llm(settings), settings))
            cfg = {"configurable": {"thread_id": f"t-{name}"}}
            params = {"date_from": dr["from"], "date_to": dr["to"], "price_tolerance_pct": 2.0, "dataset": name}
            async for _ in graph.astream({"run_id": f"r{name}", "iteration": 0, "params": params}, cfg):
                pass
            findings = (await graph.aget_state(cfg)).values["findings"]
            # a re-run cannot switch to another dataset
            async for _ in graph.astream(Command(resume={"action": "rerun", "decisions": {},
                                                         "new_params": {"dataset": "demo"}}), cfg):
                pass
            assert (await graph.aget_state(cfg)).values["params"]["dataset"] == name
            return [(f["type"], f["vendor"], f["amount"]) for f in findings]

    demo = asyncio.run(run("demo"))
    assert demo and asyncio.run(run(meta["dataset_id"])) == demo
