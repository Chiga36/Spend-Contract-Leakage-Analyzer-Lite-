"""Local MCP server: read-only access to the ERP data over stdio (LLD §6).

The backend starts this file as a child process and talks to it over stdin/stdout, so it has
no network port. Guarantees:
  * SQLite is opened with ``mode=ro``: writes are impossible.
  * No tool accepts SQL; every query below is fixed and parameterized.
  * ``read_contract`` only opens files inside the contracts folder (checked after resolving).
  * Every tool takes an optional ``dataset``: "demo" (the configured database) or the id of an
    uploaded dataset (``u`` + 12 hex characters), which maps to UPLOADS_DIR/<id>/. Any other value
    is refused, so a tool can never open a path outside those folders.
Logs go to stderr, because stdout carries the JSON-RPC stream.
"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from collections import defaultdict
from datetime import date
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

ROOT = Path(__file__).resolve().parent.parent


def _path(env: str, default: str) -> Path:
    p = Path(os.environ.get(env, default))
    return (p if p.is_absolute() else ROOT / p).resolve()


DB_PATH = _path("ERP_DB_PATH", "data/erp.db")
CONTRACTS_DIR = _path("CONTRACTS_DIR", "data/contracts")
UPLOADS_DIR = _path("UPLOADS_DIR", "data/uploads")
DEMO = "demo"
UPLOAD_ID = re.compile(r"^u[0-9a-f]{12}$")
CONTRACT_SUFFIXES = (".md", ".txt")
TABLES = ("vendors", "contracts", "contract_rates", "invoices", "invoice_lines", "payments")
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


@dataclass
class Source:
    db: sqlite3.Connection
    contracts_dir: Path


_sources: dict[str, Source] = {}


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _source(dataset: str | None) -> Source:
    """The read-only connection and contracts folder of a dataset (opened once, then reused)."""
    name = dataset or DEMO
    if name in _sources:
        return _sources[name]
    if name == DEMO:
        db_path, contracts_dir = DB_PATH, CONTRACTS_DIR
    elif UPLOAD_ID.match(name):
        db_path, contracts_dir = UPLOADS_DIR / name / "erp.db", UPLOADS_DIR / name / "contracts"
    else:
        raise ValueError(f"Unknown dataset {name!r}")
    if not db_path.is_file():
        raise ValueError(f"Dataset {name!r} not found")
    _sources[name] = Source(_connect(db_path), contracts_dir.resolve())
    return _sources[name]


if not DB_PATH.is_file():
    raise SystemExit(f"ERP database not found at {DB_PATH}. Run: python seed/generate.py")
mcp = FastMCP("erp-data", log_level="WARNING")
_source(DEMO)


def _rows(dataset: str | None, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in _source(dataset).db.execute(sql, args)]


def _iso(value: str, name: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD), got {value!r}") from None


@mcp.tool(annotations=READ_ONLY)
def get_summary(dataset: str = DEMO) -> dict:
    """Row counts per table, the invoice date range and this server's process id. Read-only."""
    db = _source(dataset).db
    counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}  # fixed names
    lo, hi = db.execute("SELECT MIN(invoice_date), MAX(invoice_date) FROM invoices").fetchone()
    no_rates = db.execute(
        "SELECT COUNT(*) FROM contracts WHERE contract_id NOT IN (SELECT DISTINCT contract_id FROM contract_rates)"
    ).fetchone()[0]
    return {"tables": counts, "invoice_date_min": lo, "invoice_date_max": hi,
            "contracts_without_rate_table": no_rates, "pid": os.getpid()}


@mcp.tool(annotations=READ_ONLY)
def list_vendors(dataset: str = DEMO) -> list[dict]:
    """All vendors: vendor_id, name, tax_id, address, category. Read-only."""
    return _rows(dataset, "SELECT vendor_id, name, tax_id, address, category FROM vendors ORDER BY vendor_id")


@mcp.tool(annotations=READ_ONLY)
def list_contracts(dataset: str = DEMO) -> list[dict]:
    """Contract metadata: contract_id, vendor_id, start/end date, payment_terms, document_file. Read-only."""
    return _rows(dataset, "SELECT contract_id, vendor_id, start_date, end_date, payment_terms, document_file "
                 "FROM contracts ORDER BY contract_id")


@mcp.tool(annotations=READ_ONLY)
def get_contract_rates(contract_id: str, dataset: str = DEMO) -> list[dict]:
    """Structured price tiers (sku, unit_price, min_qty per calendar year) for one contract.
    May be empty: some contracts state their prices only in the document text. Read-only."""
    return _rows(dataset, "SELECT contract_id, sku, unit_price, min_qty FROM contract_rates "
                 "WHERE contract_id = ? ORDER BY sku, min_qty", (contract_id,))


@mcp.tool(annotations=READ_ONLY)
def read_contract(contract_id: str, dataset: str = DEMO) -> str:
    """The contract document (Markdown or plain text) for one contract. Read-only."""
    src = _source(dataset)
    row = src.db.execute("SELECT document_file FROM contracts WHERE contract_id = ?", (contract_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown contract_id {contract_id!r}")
    if not row["document_file"]:
        raise ValueError(f"Contract {contract_id} has no document")
    path = (src.contracts_dir / row["document_file"]).resolve()
    if not path.is_relative_to(src.contracts_dir) or path.suffix.lower() not in CONTRACT_SUFFIXES:
        raise ValueError(f"Contract document for {contract_id} is outside the contracts folder")
    return path.read_text(encoding="utf-8")


@mcp.tool(annotations=READ_ONLY)
def get_invoices(date_from: str, date_to: str, dataset: str = DEMO) -> list[dict]:
    """Invoices dated between date_from and date_to (ISO dates, inclusive), each with its lines. Read-only."""
    lo, hi = _iso(date_from, "date_from"), _iso(date_to, "date_to")
    invs = _rows(dataset, "SELECT * FROM invoices WHERE invoice_date BETWEEN ? AND ? "
                 "ORDER BY invoice_date, invoice_id", (lo, hi))
    lines: dict[str, list[dict]] = defaultdict(list)
    for r in _source(dataset).db.execute("SELECT l.* FROM invoice_lines l JOIN invoices i ON i.invoice_id = l.invoice_id "
                        "WHERE i.invoice_date BETWEEN ? AND ? ORDER BY l.line_id", (lo, hi)):
        lines[r["invoice_id"]].append(dict(r))
    for inv in invs:
        inv["lines"] = lines[inv["invoice_id"]]
    return invs


@mcp.tool(annotations=READ_ONLY)
def get_payments(date_from: str, date_to: str, dataset: str = DEMO) -> list[dict]:
    """Payments with paid_date between date_from and date_to (ISO dates, inclusive). Read-only."""
    lo, hi = _iso(date_from, "date_from"), _iso(date_to, "date_to")
    return _rows(dataset, "SELECT * FROM payments WHERE paid_date BETWEEN ? AND ? ORDER BY paid_date, payment_id", (lo, hi))


if __name__ == "__main__":
    mcp.run()  # stdio transport
