"""Uploaded datasets: the user's own ERP export (CSV files) and contract documents.

An upload is a set of CSV files, one per table, plus contract documents (.md or .txt), sent as
separate files or inside one .zip. They are checked (required columns, types, unique ids,
references between tables) and written to UPLOADS_DIR/<id>/erp.db and contracts/. The MCP server
then reads that folder read-only, exactly like the demo data.

    vendors.csv         vendor_id, name, tax_id*, address*, category*
    contracts.csv       contract_id, vendor_id, start_date, end_date, payment_terms*, document_file*
    contract_rates.csv  contract_id, sku, unit_price, min_qty*            (optional file)
    invoices.csv        invoice_id, vendor_id, vendor_invoice_no, invoice_date, total_amount
    invoice_lines.csv   line_id, invoice_id, sku*, quantity, unit_price
    payments.csv        payment_id, invoice_id, paid_date, amount
    (* = column may be empty or left out)

Dates are YYYY-MM-DD. Amounts may contain "$" and thousands separators.
"""
from __future__ import annotations

import csv
import io
import json
import re
import secrets
import shutil
import sqlite3
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path, PurePath

from seed.generate import SCHEMA

DATASET_ID = re.compile(r"^u[0-9a-f]{12}$")
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_FILES = 2000
MAX_ERRORS = 40
CONTRACT_SUFFIXES = (".md", ".txt")
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

# table -> [(column, type, required)], in the database's column order
TABLES: dict[str, list[tuple[str, str, bool]]] = {
    "vendors": [("vendor_id", "text", True), ("name", "text", True), ("tax_id", "text", False),
                ("address", "text", False), ("category", "text", False)],
    "contracts": [("contract_id", "text", True), ("vendor_id", "text", True), ("start_date", "date", True),
                  ("end_date", "date", True), ("payment_terms", "text", False), ("document_file", "text", False)],
    "contract_rates": [("contract_id", "text", True), ("sku", "text", True), ("unit_price", "number", True),
                       ("min_qty", "int", False)],
    "invoices": [("invoice_id", "text", True), ("vendor_id", "text", True), ("vendor_invoice_no", "text", True),
                 ("invoice_date", "date", True), ("total_amount", "number", True)],
    "invoice_lines": [("line_id", "text", True), ("invoice_id", "text", True), ("sku", "text", False),
                      ("quantity", "number", True), ("unit_price", "number", True)],
    "payments": [("payment_id", "text", True), ("invoice_id", "text", True), ("paid_date", "date", True),
                 ("amount", "number", True)],
}
OPTIONAL_TABLES = {"contract_rates"}
PRIMARY_KEYS = {"vendors": ("vendor_id",), "contracts": ("contract_id",), "invoices": ("invoice_id",),
                "invoice_lines": ("line_id",), "payments": ("payment_id",),
                "contract_rates": ("contract_id", "sku", "min_qty")}
REFERENCES = [  # (table, column, referenced table)
    ("contracts", "vendor_id", "vendors"), ("contract_rates", "contract_id", "contracts"),
    ("invoices", "vendor_id", "vendors"), ("invoice_lines", "invoice_id", "invoices"),
    ("payments", "invoice_id", "invoices"),
]


class DatasetError(Exception):
    """The upload cannot be used. `errors` lists every problem found (capped)."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:3]))
        self.errors = errors


# ----------------------------------------------------------------------------- reading files
def expand(files: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    """Unpack .zip files. Only the base name of each file is kept, so paths inside a zip are ignored."""
    out: list[tuple[str, bytes]] = []
    total = 0
    for name, data in files:
        base = PurePath(name.replace("\\", "/")).name
        if base.lower().endswith(".zip"):
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                raise DatasetError([f"{base} is not a valid .zip file."]) from None
            with zf:
                for info in zf.infolist():
                    inner = PurePath(info.filename.replace("\\", "/")).name
                    if info.is_dir() or not inner or inner.startswith((".", "__MACOSX")):
                        continue
                    if info.file_size > MAX_FILE_BYTES:
                        raise DatasetError([f"{inner} in {base} is larger than {MAX_FILE_BYTES // 2**20} MB."])
                    total += info.file_size
                    if total > MAX_TOTAL_BYTES:
                        raise DatasetError([f"The upload is larger than {MAX_TOTAL_BYTES // 2**20} MB."])
                    out.append((inner, zf.read(info)))
        else:
            if len(data) > MAX_FILE_BYTES:
                raise DatasetError([f"{base} is larger than {MAX_FILE_BYTES // 2**20} MB."])
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise DatasetError([f"The upload is larger than {MAX_TOTAL_BYTES // 2**20} MB."])
            out.append((base, data))
    if len(out) > MAX_FILES:
        raise DatasetError([f"Too many files ({len(out)}); at most {MAX_FILES}."])
    return out


def _text(name: str, data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise DatasetError([f"{name} is not a text file (expected UTF-8)."])


def _clean_number(raw: str) -> float:
    s = raw.replace("$", "").replace(",", "").replace(" ", "").strip()
    if s.startswith("(") and s.endswith(")"):          # accounting style negative
        s = "-" + s[1:-1]
    return float(s)


def _clean_date(raw: str) -> str:
    s = raw.strip()[:10]                                  # "2025-01-15 00:00:00" -> "2025-01-15"
    return date.fromisoformat(s).isoformat()


def parse_table(table: str, name: str, text: str, errors: list[str]) -> list[tuple]:
    spec = TABLES[table]
    reader = csv.DictReader(io.StringIO(text))
    header = {(h or "").strip().lower(): h for h in reader.fieldnames or []}
    missing = [c for c, _, req in spec if req and c not in header]
    if missing:
        errors.append(f"{name}: missing column(s) {', '.join(missing)}. "
                      f"Expected: {', '.join(c for c, _, _ in spec)}.")
        return []
    rows: list[tuple] = []
    for n, raw in enumerate(reader, start=2):             # row 1 is the header
        if not any((v or "").strip() for v in raw.values() if isinstance(v, str)):
            continue                                       # blank line
        values = []
        for col, kind, req in spec:
            v = (raw.get(header[col]) or "").strip() if col in header else ""
            if not v:
                if req:
                    errors.append(f"{name} row {n}: {col} is empty.")
                values.append(0 if (kind == "int" and col == "min_qty") else None)
                continue
            try:
                if kind == "number":
                    v = _clean_number(v)
                elif kind == "int":
                    v = int(_clean_number(v))
                elif kind == "date":
                    v = _clean_date(v)
            except ValueError:
                want = "a date (YYYY-MM-DD)" if kind == "date" else "a number"
                errors.append(f"{name} row {n}: {col} should be {want}, got {v!r}.")
                v = None
            values.append(v)
        rows.append(tuple(values))
        if len(errors) > MAX_ERRORS:
            break
    return rows


# ----------------------------------------------------------------------------- checks
def _check(tables: dict[str, list[tuple]], errors: list[str]) -> None:
    idx = {t: {c: i for i, (c, _, _) in enumerate(TABLES[t])} for t in TABLES}
    for t, key in PRIMARY_KEYS.items():
        seen: set = set()
        for r in tables.get(t, []):
            k = tuple(r[idx[t][c]] for c in key)
            if k in seen:
                errors.append(f"{t}.csv: duplicate {'/'.join(key)} {' / '.join(map(str, k))}.")
            seen.add(k)
    for t, col, ref in REFERENCES:
        known = {r[idx[ref][PRIMARY_KEYS[ref][0]]] for r in tables.get(ref, [])}
        bad = sorted({r[idx[t][col]] for r in tables.get(t, []) if r[idx[t][col]] not in known})
        if bad:
            errors.append(f"{t}.csv: {col} not found in {ref}.csv: {', '.join(map(str, bad[:8]))}"
                          + (f" and {len(bad) - 8} more." if len(bad) > 8 else "."))
    if not tables.get("invoices"):
        errors.append("invoices.csv has no rows.")
    c_end = idx["contracts"]
    for r in tables.get("contracts", []):
        if r[c_end["start_date"]] and r[c_end["end_date"]] and r[c_end["start_date"]] > r[c_end["end_date"]]:
            errors.append(f"contracts.csv: {r[c_end['contract_id']]} ends before it starts.")
    for r in tables.get("invoice_lines", []):
        if r[idx["invoice_lines"]["quantity"]] is not None and r[idx["invoice_lines"]["quantity"]] < 0:
            errors.append(f"invoice_lines.csv: {r[0]} has a negative quantity.")
            break


def _link_documents(tables: dict[str, list[tuple]], docs: dict[str, bytes], warnings: list[str]) -> list[tuple]:
    """Point each contract at its uploaded document ('' when there is none)."""
    i = {c: n for n, (c, _, _) in enumerate(TABLES["contracts"])}
    with_rates = {r[0] for r in tables.get("contract_rates", [])}
    by_lower = {n.lower(): n for n in docs}
    out, unchecked, not_found = [], [], []
    for r in tables.get("contracts", []):
        r = list(r)
        cid, ref = r[i["contract_id"]], (r[i["document_file"]] or "").strip()
        name = None
        if ref:
            name = by_lower.get(PurePath(ref.replace("\\", "/")).name.lower())
            if name is None:
                not_found.append(ref)
        else:
            name = next((by_lower[f"{cid}{s}".lower()] for s in CONTRACT_SUFFIXES if f"{cid}{s}".lower() in by_lower), None)
        r[i["document_file"]] = name or ""
        if not name and cid not in with_rates:
            unchecked.append(cid)
        out.append(tuple(r))
    if not_found:
        warnings.append(f"{len(not_found)} contract document(s) named in contracts.csv were not uploaded: "
                        f"{', '.join(not_found[:6])}{' …' if len(not_found) > 6 else ''}.")
    if unchecked:
        warnings.append(f"{len(unchecked)} contract(s) have no price table and no document, so their prices "
                        f"cannot be checked: {', '.join(unchecked[:8])}{' …' if len(unchecked) > 8 else ''}.")
    return out


# ----------------------------------------------------------------------------- build
def build_dataset(files: list[tuple[str, bytes]], uploads_dir: Path, label: str | None = None) -> dict:
    """Check an upload and write it as a new dataset. Returns its metadata; raises DatasetError."""
    files = expand(files)
    if not files:
        raise DatasetError(["No files were uploaded."])
    errors: list[str] = []
    warnings: list[str] = []
    csvs: dict[str, tuple[str, str]] = {}
    docs: dict[str, bytes] = {}
    ignored: list[str] = []
    for name, data in files:
        stem, suffix = PurePath(name).stem.lower(), PurePath(name).suffix.lower()
        if suffix == ".csv" and stem in TABLES:
            if stem in csvs:
                errors.append(f"{name} was uploaded twice.")
            csvs[stem] = (name, _text(name, data))
        elif stem == "readme":
            continue                                         # the README of the sample files
        elif suffix in CONTRACT_SUFFIXES:
            safe = SAFE_NAME.sub("_", name).strip("._") or "contract"
            docs[safe if PurePath(safe).suffix.lower() in CONTRACT_SUFFIXES else f"{safe}.txt"] = data
        else:
            ignored.append(name)
    if ignored:
        warnings.append(f"Ignored {len(ignored)} file(s) that are not one of the expected CSVs or a "
                        f".md/.txt contract: {', '.join(ignored[:6])}{' …' if len(ignored) > 6 else ''}.")
    missing = [f"{t}.csv" for t in TABLES if t not in csvs and t not in OPTIONAL_TABLES]
    if missing:
        errors.append(f"Missing file(s): {', '.join(missing)}.")
    tables = {t: parse_table(t, name, text, errors) for t, (name, text) in csvs.items()}
    if not errors:
        _check(tables, errors)
    if errors:
        raise DatasetError(errors[:MAX_ERRORS] + ([f"…and more ({len(errors) - MAX_ERRORS} not shown)."]
                                                  if len(errors) > MAX_ERRORS else []))
    tables["contracts"] = _link_documents(tables, docs, warnings)
    used_docs = {r[5] for r in tables["contracts"] if r[5]}

    dataset_id = "u" + secrets.token_hex(6)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    tmp = uploads_dir / f".tmp-{dataset_id}"
    (tmp / "contracts").mkdir(parents=True)
    try:
        db = sqlite3.connect(tmp / "erp.db")
        db.executescript(SCHEMA)
        for t, rows in tables.items():
            if rows:
                db.executemany(f"INSERT INTO {t} VALUES ({','.join('?' * len(TABLES[t]))})", rows)  # fixed names
        db.commit()
        db.close()
        for name in used_docs:
            (tmp / "contracts" / name).write_text(_text(name, docs[name]), encoding="utf-8")
        meta = {"dataset_id": dataset_id, "label": (label or "").strip()[:80] or "Uploaded data",
                "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "files": sorted(n for n, _ in files)[:200], "warnings": warnings}
        (tmp / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        tmp.rename(uploads_dir / dataset_id)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return meta


def list_datasets(uploads_dir: Path) -> list[dict]:
    out = []
    if uploads_dir.is_dir():
        for d in uploads_dir.iterdir():
            meta = d / "meta.json"
            if DATASET_ID.match(d.name) and meta.is_file() and (d / "erp.db").is_file():
                try:
                    out.append(json.loads(meta.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
    return sorted(out, key=lambda m: m.get("created", ""), reverse=True)


def get_meta(uploads_dir: Path, dataset_id: str) -> dict | None:
    if not DATASET_ID.match(dataset_id or ""):
        return None
    meta = uploads_dir / dataset_id / "meta.json"
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ----------------------------------------------------------------------------- sample files
SAMPLE_README = """Upload format for the Spend & Contract Leakage Analyzer
=========================================================

Upload these CSV files (one per table, first row = column names) and the contract documents
(.md or .txt), either as separate files or together in one .zip. These sample files are an
export of the demo data, so you can upload this zip unchanged to try it.

{tables}

Columns marked * may be empty. Extra columns are ignored.
Dates: YYYY-MM-DD. Amounts: plain numbers ("$" and "," are accepted).
contract_rates.csv is optional: contracts without rows there are read from their document
(document_file, or a file named <contract_id>.md / .txt) when an LLM is configured.
min_qty is the yearly quantity from which a volume-tier price applies (0 = base price).
payment_terms like "2/10 NET 30" mean 2% discount if paid within 10 days, otherwise due in 30.
"""


def sample_zip(db_path: Path, contracts_dir: Path) -> bytes:
    """The demo data exported in the upload format, plus a README describing the columns."""
    buf = io.BytesIO()
    lines = []
    for t, spec in TABLES.items():
        cols = ", ".join(c + ("" if req else "*") for c, _, req in spec)
        lines.append(f"  {t}.csv{' (optional)' if t in OPTIONAL_TABLES else ''}\n      {cols}")
    db = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("README.txt", SAMPLE_README.format(tables="\n".join(lines)))
            for t, spec in TABLES.items():
                cols = [c for c, _, _ in spec]
                out = io.StringIO()
                w = csv.writer(out, lineterminator="\n")
                w.writerow(cols)
                w.writerows(db.execute(f"SELECT {', '.join(cols)} FROM {t}"))  # fixed names
                zf.writestr(f"{t}.csv", out.getvalue())
            for p in sorted(contracts_dir.glob("*.md")):
                zf.writestr(f"contracts/{p.name}", p.read_text(encoding="utf-8"))
    finally:
        db.close()
    return buf.getvalue()
