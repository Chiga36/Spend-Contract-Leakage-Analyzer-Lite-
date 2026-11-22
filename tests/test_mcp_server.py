"""MCP check (LLD §14): start the server over stdio, call each tool, and confirm it is read-only."""
from __future__ import annotations

import asyncio
import importlib
import shutil
import sqlite3
import sys
from contextlib import AsyncExitStack

import pytest

from backend.mcp_client import McpClient, McpToolError

EXPECTED_TOOLS = {"get_summary", "list_vendors", "list_contracts", "get_contract_rates", "read_contract",
                  "get_invoices", "get_payments"}


def test_every_tool_over_stdio(dataset, tmp_path):
    async def main():
        async with AsyncExitStack() as stack:
            client = McpClient(dataset.settings(tmp_path))
            await client.start(stack)
            listed = (await client.session.list_tools()).tools
            out = {"tools": {t.name: t for t in listed}, "pid": client.pid}
            out["summary"] = await client.call_tool("get_summary")
            out["vendors"] = await client.call_tool("list_vendors")
            out["contracts"] = await client.call_tool("list_contracts")
            out["rates"] = {c["contract_id"]: await client.call_tool("get_contract_rates",
                                                                     {"contract_id": c["contract_id"]})
                            for c in out["contracts"]}
            out["doc"] = await client.call_tool("read_contract", {"contract_id": out["contracts"][0]["contract_id"]})
            out["invoices"] = await client.call_tool("get_invoices", {"date_from": "2025-01-01", "date_to": "2025-01-31"})
            out["payments"] = await client.call_tool("get_payments", {"date_from": "2025-01-01", "date_to": "2025-01-31"})
            with pytest.raises(McpToolError, match="failed after 3 attempts"):
                await client.call_tool("read_contract", {"contract_id": "../../etc/passwd"})
            with pytest.raises(McpToolError):
                await client.call_tool("get_invoices", {"date_from": "2025-01-01'; DROP TABLE invoices;--",
                                                        "date_to": "2025-12-31"})
            out["audit"] = list(client.audit)
            return out

    r = asyncio.run(main())
    assert set(r["tools"]) == EXPECTED_TOOLS                      # no SQL tool, no write tool
    assert all(t.annotations and t.annotations.readOnlyHint for t in r["tools"].values())
    counts = dataset.ground_truth["counts"]
    assert r["summary"]["tables"]["vendors"] == counts["vendors"] == len(r["vendors"])
    assert r["summary"]["tables"]["invoices"] == counts["invoices"]
    assert r["summary"]["contracts_without_rate_table"] == counts["contracts_without_rate_table"]
    assert r["pid"] and r["pid"] != __import__("os").getpid()      # a separate local process
    assert len(r["contracts"]) == counts["contracts"]
    assert sum(1 for rows in r["rates"].values() if not rows) == counts["contracts_without_rate_table"]
    assert r["doc"].startswith("# Supply Agreement")
    assert r["invoices"] and all("2025-01-01" <= i["invoice_date"] <= "2025-01-31" and i["lines"] for i in r["invoices"])
    assert r["payments"] and all("2025-01-01" <= p["paid_date"] <= "2025-01-31" for p in r["payments"])
    ok = [a for a in r["audit"] if a["ok"]]
    assert all(a["transport"] == "stdio" and a["ms"] >= 0 and a["bytes"] > 0 for a in ok)


def test_database_is_read_only_and_paths_are_checked(dataset, tmp_path, monkeypatch):
    """Load the server module against a tampered copy: writes fail, traversal is refused."""
    data = tmp_path / "data"
    shutil.copytree(dataset.root, data)
    rw = sqlite3.connect(data / "erp.db")
    rw.execute("INSERT INTO contracts VALUES ('C9999', 'V0001', '2024-01-01', '2026-12-31', 'NET 30', '../erp.db')")
    rw.commit()
    rw.close()
    monkeypatch.setenv("ERP_DB_PATH", str(data / "erp.db"))
    monkeypatch.setenv("CONTRACTS_DIR", str(data / "contracts"))
    sys.modules.pop("mcp_server.server", None)
    server = importlib.import_module("mcp_server.server")
    db = server._source("demo").db
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("INSERT INTO vendors (vendor_id, name) VALUES ('X', 'Mallory')")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM payments")
        with pytest.raises(ValueError, match="outside the contracts folder"):
            server.read_contract("C9999")
        with pytest.raises(ValueError, match="Unknown contract_id"):
            server.read_contract("C0000")
        with pytest.raises(ValueError, match="ISO date"):
            server.get_payments("yesterday", "2025-01-01")
        assert server.read_contract("C0001").startswith("# Supply Agreement C0001")
        for bad in ("../data", "u12345", "/etc", "UABCDEF012345"):
            with pytest.raises(ValueError, match="Unknown dataset"):
                server.list_vendors(bad)
        with pytest.raises(ValueError, match="not found"):
            server.list_vendors("u0123456789ab")
    finally:
        db.close()
        sys.modules.pop("mcp_server.server", None)
