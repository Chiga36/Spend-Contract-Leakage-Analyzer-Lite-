#!/usr/bin/env python
"""Synthetic accounts-payable data with planted leakage (LLD §5.2).

The random seed is fixed, so every run writes identical files:

    <out>/erp.db                    SQLite database (schema: LLD §5.1)
    <out>/contracts/<id>.md         one Markdown document per contract
    <out>/planted_anomalies.json    ground truth for the tests; each entry has a needs_llm flag

Usage:
    python seed/generate.py              # writes ./data
    python seed/generate.py --out DIR
"""
from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Reuse the detectors' own normalization rules, so the ground truth cannot drift from them.
from backend.graph.detectors import (  # noqa: E402
    LEVERAGE_RATE,
    NAME_BORDERLINE_RATIO,
    NAME_MATCH_RATIO,
    normalize_invoice_no,
    normalize_tax_id,
    normalize_vendor_name,
    parse_terms,
)

SEED = 20261117
DATA_START = date(2024, 11, 1)
DATA_END = date(2026, 10, 31)
N_BASE_INVOICES = 1492            # + 8 planted duplicate invoices = 1,500
BUYER = "Brightline Manufacturing, Inc."
TARGETS = {"DUPLICATE_PAYMENT": 95_000, "PRICE_VARIANCE": 130_000,
           "VOLUME_DISCOUNT": 85_000, "EARLY_PAY_DISCOUNT": 40_000}
CENT = Decimal("0.01")


def d2(x) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


def money(x: Decimal) -> str:
    return f"${x:,.2f}"


# --------------------------------------------------------------------------- catalog
@dataclass(frozen=True)
class Item:
    desc: str
    unit: str
    price_lo: float
    price_hi: float
    qty_lo: int
    qty_hi: int


def _items(*rows: tuple) -> list[Item]:
    return [Item(*r) for r in rows]


CATEGORIES: dict[str, dict] = {
    "PKG": {"label": "Packaging",
            "descriptors": ["Packaging", "Container", "Box and Carton", "Pack Solutions", "Corrugated", "Shipping Supplies"],
            "items": _items(("Corrugated shipping carton 18x12x10", "carton", 1.30, 2.20, 2000, 9000),
                            ("Stretch wrap film 18in x 1500ft", "roll", 19, 27, 60, 260),
                            ("GMA pallet 48x40, grade A", "pallet", 10, 16, 150, 700),
                            ("Packing tape 2in x 110yd", "roll", 2.10, 3.60, 600, 2400),
                            ("Kraft void-fill paper roll", "roll", 38, 58, 30, 140),
                            ("Poly mailer 12x15.5, case of 500", "case", 31, 46, 40, 160))},
    "MRO": {"label": "Industrial supplies",
            "descriptors": ["Industrial Supply", "MRO Supply", "Bearing and Power", "Tool and Supply", "Fluid Power", "Maintenance Products"],
            "items": _items(("Nitrile gloves, case of 1,000", "case", 62, 88, 40, 260),
                            ("Safety glasses, clear lens", "each", 3.10, 6.40, 200, 1200),
                            ("Hydraulic hose assembly 1/2in", "each", 48, 86, 20, 120),
                            ("Ball bearing 6204-2RS", "each", 4.20, 8.80, 200, 1400),
                            ("Shop towels, box of 200", "box", 24, 36, 60, 320),
                            ("Industrial lubricant, 5 gal pail", "pail", 96, 148, 10, 60))},
    "JAN": {"label": "Janitorial",
            "descriptors": ["Janitorial Supply", "Sanitation Products", "Cleaning Systems", "Hygiene Supply"],
            "items": _items(("Trash liners 40-45 gal, case of 250", "case", 26, 39, 60, 320),
                            ("Foam hand soap refill 1200ml", "each", 9.50, 15.80, 120, 600),
                            ("Multifold paper towels, case of 16", "case", 31, 44, 80, 420),
                            ("Neutral floor cleaner, 5 gal pail", "pail", 42, 68, 15, 90))},
    "OFF": {"label": "Office supplies",
            "descriptors": ["Office Products", "Business Supply", "Office Essentials", "Paper and Print Supply"],
            "items": _items(("Copy paper 8.5x11, case of 10 reams", "case", 39, 52, 60, 360),
                            ("Toner cartridge, high yield", "each", 96, 158, 20, 140),
                            ("Hanging file folders, box of 25", "box", 12, 19, 30, 200),
                            ("Ergonomic office chair", "each", 210, 340, 4, 30))},
    "ITH": {"label": "IT hardware",
            "descriptors": ["Technologies", "Computer Systems", "IT Solutions", "Digital Hardware", "Networks"],
            "items": _items(("27in IPS monitor", "each", 189, 289, 20, 120),
                            ("USB-C docking station", "each", 145, 235, 20, 110),
                            ("Business laptop 14in", "each", 890, 1340, 6, 45),
                            ("Cat6 patch cable 10ft", "each", 3.20, 6.40, 200, 1500),
                            ("Wireless keyboard and mouse set", "set", 32, 58, 30, 200))},
    "ELE": {"label": "Electrical",
            "descriptors": ["Electric Supply", "Lighting", "Electrical Distributors", "Power Products"],
            "items": _items(("LED troffer 2x4, 4000K", "each", 58, 92, 40, 260),
                            ("THHN wire 12 AWG, 500ft spool", "spool", 84, 132, 20, 110),
                            ("20A single-pole breaker", "each", 9.80, 17.50, 60, 320),
                            ("LED high-bay fixture 150W", "each", 128, 196, 20, 90))},
    "LAB": {"label": "Lab supplies",
            "descriptors": ["Scientific", "Lab Supply", "Life Sciences", "Laboratory Products"],
            "items": _items(("Pipette tips 200uL, rack of 96", "rack", 7.20, 11.80, 300, 1600),
                            ("Glass sample vials 20ml, case of 500", "case", 74, 118, 15, 90),
                            ("Lab coat, poly-cotton", "each", 21, 34, 30, 160),
                            ("Microcentrifuge tubes 1.5ml, bag of 500", "bag", 13, 22, 60, 360))},
    "SAF": {"label": "Safety equipment",
            "descriptors": ["Safety Products", "Protective Equipment", "Safety Supply", "Workwear"],
            "items": _items(("Type I hard hat", "each", 11, 18, 80, 420),
                            ("Hi-vis vest, ANSI class 2", "each", 6.20, 10.90, 150, 800),
                            ("Fall-protection harness", "each", 92, 148, 10, 70),
                            ("Disposable earplugs, box of 200", "box", 18, 29, 40, 220))},
    "LOG": {"label": "Logistics",
            "descriptors": ["Freight", "Logistics", "Transport", "Distribution Services", "Warehousing"],
            "items": _items(("LTL freight shipment, regional", "shipment", 380, 860, 4, 30),
                            ("Pallet storage, per pallet-month", "pallet-month", 12, 19, 300, 1800),
                            ("Final-mile delivery", "delivery", 85, 160, 20, 140))},
    "FAC": {"label": "Facility services",
            "descriptors": ["Facility Services", "Building Maintenance", "Mechanical Services", "Property Services"],
            "items": _items(("Preventive maintenance visit", "visit", 420, 980, 1, 6),
                            ("HVAC service call", "call", 260, 640, 1, 8),
                            ("Filter replacement, AHU", "each", 38, 72, 10, 80))},
    "PRO": {"label": "Professional services",
            "descriptors": ["Consulting", "Advisory Group", "Partners", "Analytics"],
            "items": _items(("Consulting services", "hour", 160, 260, 10, 80),
                            ("Project management services", "hour", 130, 210, 10, 60))},
    "PRT": {"label": "Printing",
            "descriptors": ["Print Services", "Printing", "Graphics"],
            "items": _items(("Brochure print run, 2,500 copies", "run", 420, 1250, 1, 6),
                            ("Large-format banner", "each", 85, 190, 2, 20))},
    "PST": {"label": "Pest control",
            "descriptors": ["Pest Services", "Pest Control"],
            "items": _items(("Quarterly pest service", "visit", 240, 520, 1, 4),
                            ("Rodent station service", "each", 18, 32, 10, 40))},
}
CONTRACT_CATEGORY_MIX = ["PKG"] * 4 + ["MRO"] * 4 + ["JAN"] * 2 + ["OFF"] * 3 + ["ITH"] * 4 + \
                        ["ELE"] * 4 + ["LAB"] * 3 + ["SAF"] * 3 + ["LOG"] * 3            # 30 contracts
REGULAR_CATEGORY_WEIGHTS = {"PKG": 1, "MRO": 2, "JAN": 1, "OFF": 1, "ITH": 1.5, "ELE": 1, "LAB": 1,
                            "SAF": 1, "LOG": 1.5, "FAC": 2, "PRO": 2, "PRT": 1, "PST": 0.5}

BRANDS = """Alderbrook Ashcombe Avalon Bellweather Birchfield Blackstone Bramwell Briarwood Calder
Canterbury Carrington Castleford Cedarline Chadwick Chesterton Clarendon Clearbrook Cobalt Copperfield
Cornerstone Cresthaven Dalton Davenport Deerfield Dorchester Driftwood Dunmore Eastbourne Edgewater
Ellsworth Emberton Everett Fairbanks Falconer Fenmore Fieldstone Foxhollow Gainsborough Garland
Glenhaven Goldcrest Greystone Halverson Hampton Harrington Hartwell Hazelwood Highmark Hollister
Ironwood Ivybridge Jasperton Kingsbury Lakemont Langford Larchmont Lexington Linwood Lockwood Mapleton
Millstone Montclair Moorland Newbury Northcote Orchardale Pemberton Pendleton Penrose Quincy Radcliffe
Ravenwood Redfield Ridgeway Rockingham Rosewood Rutherford Saddleback Sandhurst Sherwood Silvercrest
Somerset Stonebridge Sutton Tanglewood Thornton Torrington Trafalgar Tremont Upton Valiant Waverly
Westfield Whitfield Wilshire Windham Woodbury Wrenfield Yardley Zenith Axford Beckett Corwin Elmhurst
Fulton Granger Hadley Ingram Jericho Keaton Lyndon Mercer Norwood Osborne Prescott Rowan Sterling Talbot
Ambergate Bexley Crowther Dunbar Esterly Fairweather Gilchrist Hawksmoor Ironside Juniper Kilbride
Lowell Merriweather Netherby Oldcastle Pickering Quayle Rainford Stratton Thackeray""".split()
SUFFIXES = ["Inc", "LLC", "Corp", "Co", "Ltd", "", "", "Group"]

# (original name, duplicate record name, category, how the detector should match, needs_llm)
PLANTED_VENDOR_PAIRS = [
    ("Northwind Industrial Supply Inc", "NW Industrial Supply", "MRO", "tax_id", False),
    ("Meridian Facility Services LLC", "Meridian Facilities Mgmt", "FAC", "tax_id", False),
    ("Pinnacle Packaging Corp", "Pinnacle Packaging, Inc.", "PKG", "name", False),
    ("Kestrel Logistics Group", "Kestral Logistics Group", "LOG", "name", False),
    ("Blue Harbor Electrical Services", "Blue Harbour Electric Svc", "ELE", "llm", True),
    ("Granite Peak Office Products", "Granite Peak Off. Prods", "OFF", "llm", True),
]
# Different companies whose names are borderline-similar: the LLM should say "not the same".
DECOY_VENDORS = [("Oakridge Print Services", "PRT"), ("Oakridge Pest Services", "PST")]

STREETS = ["Harbor", "Industrial", "Commerce", "Maple", "Lakeview", "Mill", "River", "Station", "Market",
           "Pioneer", "Enterprise", "Gateway", "Willow", "Cedar", "Union", "Franklin", "Madison", "Quarry"]
STREET_TYPES = [("Street", "St"), ("Avenue", "Ave"), ("Boulevard", "Blvd"), ("Drive", "Dr"), ("Road", "Rd"),
                ("Parkway", "Pkwy"), ("Way", "Wy")]
CITIES = [("Columbus", "OH", "432"), ("Indianapolis", "IN", "462"), ("Louisville", "KY", "402"),
          ("Nashville", "TN", "372"), ("Charlotte", "NC", "282"), ("Milwaukee", "WI", "532"),
          ("Kansas City", "MO", "641"), ("Omaha", "NE", "681"), ("Pittsburgh", "PA", "152"),
          ("Richmond", "VA", "232"), ("Grand Rapids", "MI", "495"), ("Des Moines", "IA", "503"),
          ("Dayton", "OH", "454"), ("Toledo", "OH", "436"), ("Fort Wayne", "IN", "468")]

DISCOUNT_TERMS = ["2/10 NET 30"] * 6 + ["1/10 NET 30"] * 3 + ["1.5/15 NET 45"] * 2 + ["2/15 NET 45"]
NET_TERMS = ["NET 30", "NET 45", "NET 60"]


# --------------------------------------------------------------------------- model
@dataclass
class Vendor:
    key: str
    name: str
    category: str
    tax_id: str | None
    address: str
    role: str = "regular"          # contract | regular | dup_original | dup_record | decoy
    vendor_id: str = ""
    skus: list[str] = field(default_factory=list)
    inv_style: int = 0
    inv_seq: int = 0
    inv_prefix: str = ""


@dataclass
class Sku:
    sku: str
    vendor_id: str
    item: Item
    price: Decimal                 # contract base price / list price


@dataclass
class Contract:
    contract_id: str
    vendor_id: str
    start: date
    end: date
    terms: str
    skus: list[str]
    in_db: bool
    tiers: dict[str, list[tuple[int, Decimal]]] = field(default_factory=dict)  # sku -> [(min_qty, price)]

    def tier_list(self, sku: str, base: Decimal) -> list[tuple[int, Decimal]]:
        return self.tiers.get(sku) or [(0, base)]


@dataclass
class Line:
    line_id: str
    invoice_id: str
    sku: str
    qty: int
    price: Decimal = Decimal(0)


@dataclass
class Invoice:
    invoice_id: str
    vendor_id: str
    vendor_invoice_no: str
    date: date
    lines: list[Line] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((d2(ln.qty * ln.price) for ln in self.lines), Decimal(0))


@dataclass
class Payment:
    payment_id: str
    invoice_id: str
    paid_date: date
    amount: Decimal


# --------------------------------------------------------------------------- generator
class Generator:
    def __init__(self, seed: int = SEED):
        self.rng = random.Random(seed)
        self.vendors: dict[str, Vendor] = {}
        self.skus: dict[str, Sku] = {}
        self.contracts: dict[str, Contract] = {}          # contract_id -> contract
        self.contract_of: dict[str, Contract] = {}        # vendor_id -> contract
        self.invoices: list[Invoice] = []
        self.payments: list[Payment] = []
        self.anomalies: list[dict] = []
        self.decoys: list[dict] = []
        self.vendor_pairs: list[tuple[str, str, str, bool]] = []
        self._volume_lines: set[str] = set()
        self._early_pay: set[str] = set()
        self._dup_invoice_ids: set[str] = set()
        self._tax_ids: set[str] = set()
        self._dup_of: dict[str, str] = {}                # duplicate invoice -> original invoice
        self.qty_mult: dict[str, float] = {}

    # ---------------------------------------------------------------- helpers
    def _tax_id(self) -> str:
        while True:
            t = f"{self.rng.randint(10, 98)}-{self.rng.randint(1000000, 9999999)}"
            if t not in self._tax_ids:
                self._tax_ids.add(t)
                return t

    def _address_parts(self) -> dict:
        r = self.rng
        city, state, zip3 = r.choice(CITIES)
        return {"num": r.randint(100, 9800), "street": r.choice(STREETS), "type": r.choice(STREET_TYPES),
                "unit": r.choice([None, None, r.randint(100, 450)]), "city": city, "state": state,
                "zip": f"{zip3}{r.randint(10, 99)}"}

    @staticmethod
    def _format_address(p: dict, short: bool = False) -> str:
        if short:
            unit = f" Ste {p['unit']}" if p["unit"] else ""
            return f"{p['num']} {p['street']} {p['type'][1]}{unit}, {p['city']} {p['state']} {p['zip']}"
        unit = f", Suite {p['unit']}" if p["unit"] else ""
        return f"{p['num']} {p['street']} {p['type'][0]}{unit}, {p['city']}, {p['state']} {p['zip']}"

    def _qty(self, item: Item, mult: float = 1.0) -> int:
        q = int(self.rng.randint(item.qty_lo, item.qty_hi) * mult)
        step = 50 if item.qty_hi >= 2000 else 10 if item.qty_hi >= 300 else 1
        return max(step, q // step * step)

    def _next_invoice_no(self, v: Vendor, d: date) -> str:
        v.inv_seq += self.rng.randint(1, 4) if v.inv_style != 2 else 1
        s = v.inv_seq
        return [f"INV-{s}", f"{s:06d}", f"{d.year}-{s:04d}", f"{v.inv_prefix}{s}", f"SI-{s:05d}"][v.inv_style]

    # ---------------------------------------------------------------- 1. vendors
    def make_vendors(self) -> None:
        r = self.rng
        taken: list[str] = []          # normalized names already used

        def add(v: Vendor) -> Vendor:
            self.vendors[v.key] = v
            taken.append(normalize_vendor_name(v.name))
            return v

        # Planted duplicate-vendor pairs and decoys first, so random names are kept away from them.
        for i, (orig, dup, cat, how, needs_llm) in enumerate(PLANTED_VENDOR_PAIRS):
            parts = self._address_parts()
            parts["unit"] = parts["unit"] or r.randint(100, 450)
            tax = self._tax_id()
            a = add(Vendor(f"P{i}a", orig, cat, tax, self._format_address(parts), role="dup_original"))
            b_tax = tax if how == "tax_id" else None
            b_addr = self._format_address(parts, short=(how == "llm"))
            b = add(Vendor(f"P{i}b", dup, cat, b_tax, b_addr, role="dup_record"))
            self.vendor_pairs.append((a.key, b.key, how, needs_llm))
        for i, (name, cat) in enumerate(DECOY_VENDORS):
            add(Vendor(f"D{i}", name, cat, self._tax_id(), self._format_address(self._address_parts()), role="decoy"))

        brands = BRANDS[:]
        r.shuffle(brands)

        def random_vendor(key: str, cat: str, role: str) -> Vendor:
            while True:
                brand = brands.pop()
                for _ in range(20):
                    suffix = r.choice(SUFFIXES)
                    name = " ".join(x for x in (brand, r.choice(CATEGORIES[cat]["descriptors"]), suffix) if x)
                    norm = normalize_vendor_name(name)
                    if all(SequenceMatcher(None, norm, t).ratio() < 0.75 for t in taken):
                        tax = None if r.random() < 0.08 and role != "contract" else self._tax_id()
                        return add(Vendor(key, name, cat, tax, self._format_address(self._address_parts()), role=role))

        for i, cat in enumerate(CONTRACT_CATEGORY_MIX):
            random_vendor(f"C{i}", cat, "contract")
        n_regular = 120 - len(self.vendors)
        cats, weights = zip(*REGULAR_CATEGORY_WEIGHTS.items())
        for i in range(n_regular):
            random_vendor(f"R{i}", r.choices(cats, weights)[0], "regular")

        order = list(self.vendors.values())
        r.shuffle(order)
        for n, v in enumerate(order, start=1):
            v.vendor_id = f"V{n:04d}"
            v.inv_style = r.randrange(5)
            v.inv_seq = r.choice([r.randint(10000, 40000), r.randint(100, 900)])
            v.inv_prefix = "".join(w[0] for w in normalize_vendor_name(v.name).split()[:2]).upper()
        self.vendors = {v.vendor_id: v for v in order}

    # ---------------------------------------------------------------- 2. catalogs + contracts
    def make_contracts(self) -> None:
        r = self.rng
        counters = {p: r.randint(1000, 1100) for p in CATEGORIES}
        for v in self.vendors.values():
            items = CATEGORIES[v.category]["items"]
            chosen = items[:] if v.role == "contract" else r.sample(items, k=min(len(items), r.randint(2, 4)))
            for item in chosen:
                counters[v.category] += r.randint(1, 9)
                code = f"{v.category}-{counters[v.category]}"
                self.skus[code] = Sku(code, v.vendor_id, item, d2(r.uniform(item.price_lo, item.price_hi)))
                v.skus.append(code)

        contract_vendors = sorted((v for v in self.vendors.values() if v.role == "contract"), key=lambda v: v.vendor_id)
        r.shuffle(contract_vendors)
        llm_only = set(r.sample(range(len(contract_vendors)), 10))
        terms = DISCOUNT_TERMS + [r.choice(NET_TERMS) for _ in range(len(contract_vendors) - len(DISCOUNT_TERMS))]
        r.shuffle(terms)
        for i, v in enumerate(contract_vendors):
            start = date(2023, 1, 1) + timedelta(days=r.randint(0, 600))
            years = 3 if date(start.year + 3, start.month, 1) > DATA_END else 4
            end = date(start.year + years, start.month, 1) - timedelta(days=1)
            skus = v.skus[:]
            if len(skus) >= 5:                       # one item stays off-contract
                skus.remove(r.choice(skus))
            c = Contract(f"C{i + 1:04d}", v.vendor_id, start, end, terms[i], skus, in_db=i not in llm_only)
            self.contracts[c.contract_id] = c
            self.contract_of[v.vendor_id] = c
            # Contracted suppliers are the large ones; early-pay terms come from the largest.
            self.qty_mult[v.vendor_id] = r.uniform(1.5, 3.0) * (1.6 if parse_terms(c.terms) else 1.0)

    # ---------------------------------------------------------------- 3. invoice skeletons
    def make_invoices(self) -> None:
        r = self.rng
        counts: dict[str, int] = {}
        others = []
        for v in self.vendors.values():
            if v.role == "contract":
                counts[v.vendor_id] = r.randint(24, 36)
            elif v.role == "dup_record":
                counts[v.vendor_id] = r.randint(3, 6)
            else:
                others.append(v)
        remaining = N_BASE_INVOICES - sum(counts.values()) - 2 * len(others)
        weights = [r.uniform(0.5, 2.0) * (1.6 if v.role == "dup_original" else 1) for v in others]
        shares = [remaining * w / sum(weights) for w in weights]
        alloc = [int(s) for s in shares]
        for idx in sorted(range(len(others)), key=lambda k: shares[k] - alloc[k], reverse=True)[:remaining - sum(alloc)]:
            alloc[idx] += 1
        for v, n in zip(others, alloc):
            counts[v.vendor_id] = 2 + n

        span = (DATA_END - DATA_START).days
        skeleton = []
        for vid, n in counts.items():
            for d in sorted(DATA_START + timedelta(days=r.randint(0, span)) for _ in range(n)):
                skeleton.append((d, vid))
        skeleton.sort()

        line_no = 0
        for n, (d, vid) in enumerate(skeleton, start=1):
            v = self.vendors[vid]
            inv = Invoice(f"INV-{n:06d}", vid, self._next_invoice_no(v, d), d)
            contract = self.contract_of.get(vid)
            if contract:
                k = r.choices([1, 2, 3, 4, 5], [0.08, 0.20, 0.30, 0.24, 0.18])[0]
                pool = v.skus[:]
                wts = [1.0 if s in contract.skus else 0.25 for s in pool]
            else:
                k = r.choices([1, 2, 3, 4], [0.30, 0.35, 0.22, 0.13])[0]
                pool, wts = v.skus[:], [1.0] * len(v.skus)
            picked: list[str] = []
            while pool and len(picked) < k:
                s = r.choices(pool, wts)[0]
                j = pool.index(s)
                pool.pop(j)
                wts.pop(j)
                picked.append(s)
            for s in sorted(picked):
                line_no += 1
                qty = self._qty(self.skus[s].item, self.qty_mult.get(vid, 1.0))
                inv.lines.append(Line(f"L-{line_no:06d}", inv.invoice_id, s, qty))
            self.invoices.append(inv)
        self._line_no = line_no

    # ---------------------------------------------------------------- shared: walk contract lines in order
    def _contract_lines(self):
        """(invoice, line, contract) for contracted SKUs, in the detectors' (date, invoice, line) order."""
        for inv in sorted(self.invoices, key=lambda i: (i.date, i.invoice_id)):
            c = self.contract_of.get(inv.vendor_id)
            if not c or not (c.start <= inv.date <= c.end):
                continue
            for ln in sorted(inv.lines, key=lambda x: x.line_id):
                if ln.sku in c.skus:
                    yield inv, ln, c

    def _yearly_qty(self) -> dict[tuple[str, str, int], int]:
        q: dict[tuple, int] = defaultdict(int)
        for inv, ln, c in self._contract_lines():
            q[(c.contract_id, ln.sku, inv.date.year)] += ln.qty
        return q

    @staticmethod
    def _nice(x: float) -> int:
        if x < 100:
            return max(10, int(round(x / 10.0)) * 10)
        mag = 10 ** (int(math.log10(x)) - 1)
        return int(round(x / mag) * mag)

    def _missed_volume(self, c: Contract, sku: str, threshold: int, tier_price: Decimal) -> tuple[Decimal, list[str]]:
        base = self.skus[sku].price
        ytd: dict[int, int] = defaultdict(int)
        amount, lines = Decimal(0), []
        for inv, ln, cc in self._contract_lines():
            if cc is not c or ln.sku != sku:
                continue
            prior = ytd[inv.date.year]
            ytd[inv.date.year] += ln.qty
            if prior >= threshold:
                amount += d2((base - tier_price) * ln.qty)
                lines.append(ln.line_id)
        return amount, lines

    # ---------------------------------------------------------------- 4. volume tiers (+ planted misses)
    def make_tiers(self) -> None:
        r = self.rng
        yearly = self._yearly_qty()
        options = []          # (contract, sku, discount, threshold, tier_price, amount, line_ids)
        for c in self.contracts.values():
            for sku in c.skus:
                q2025 = yearly.get((c.contract_id, sku, 2025), 0)
                if q2025 < 200:
                    continue
                threshold = self._nice(0.42 * q2025)
                for disc in (0.06, 0.075, 0.08, 0.10):
                    tier_price = d2(self.skus[sku].price * Decimal(str(1 - disc)))
                    amount, lines = self._missed_volume(c, sku, threshold, tier_price)
                    if len(lines) >= 6:
                        options.append((c, sku, disc, threshold, tier_price, amount, lines))

        def closest(pool, target, exclude_vendors):
            pool = [o for o in pool if o[0].vendor_id not in exclude_vendors]
            return min(pool, key=lambda o: abs(o[5] - Decimal(target)))

        llm_pick = closest([o for o in options if not o[0].in_db], 24_000, set())
        db_target = (TARGETS["VOLUME_DISCOUNT"] - float(llm_pick[5])) / 2
        db1 = closest([o for o in options if o[0].in_db], db_target, {llm_pick[0].vendor_id})
        db2 = closest([o for o in options if o[0].in_db], db_target, {llm_pick[0].vendor_id, db1[0].vendor_id})
        planted = [db1, db2, llm_pick]
        for c, sku, disc, threshold, tier_price, amount, lines in planted:
            c.tiers[sku] = [(0, self.skus[sku].price), (threshold, tier_price)]
            self._volume_lines.update(lines)
            self.anomalies.append({
                "type": "VOLUME_DISCOUNT", "vendor_id": c.vendor_id, "vendor": self.vendors[c.vendor_id].name,
                "contract_id": c.contract_id, "sku": sku, "min_qty": threshold,
                "base_price": float(self.skus[sku].price), "tier_price": float(tier_price),
                "line_ids": lines, "amount": float(amount), "needs_llm": not c.in_db,
            })

        # Other tiered SKUs, which vendors apply correctly (these must NOT be flagged).
        planted_keys = {(o[0].contract_id, o[1]) for o in planted}
        candidates = sorted({(c.contract_id, sku) for c, sku, *_ in options} - planted_keys)
        for cid, sku in r.sample(candidates, k=min(14, len(candidates))):
            c = self.contracts[cid]
            base = self.skus[sku].price
            q2025 = yearly[(cid, sku, 2025)]
            if r.random() < 0.2:      # a three-tier schedule
                c.tiers[sku] = [(0, base), (self._nice(0.3 * q2025), d2(base * Decimal("0.95"))),
                                (self._nice(0.6 * q2025), d2(base * Decimal("0.90")))]
            else:
                disc = Decimal(str(r.choice([0.05, 0.06, 0.075, 0.08, 0.10])))
                c.tiers[sku] = [(0, base), (self._nice(0.42 * q2025), d2(base * (1 - disc)))]

    # ---------------------------------------------------------------- 5. prices
    def price_lines(self) -> None:
        r = self.rng
        ytd: dict[tuple, int] = defaultdict(int)
        contract_line_ids = set()
        for inv, ln, c in self._contract_lines():
            contract_line_ids.add(ln.line_id)
            key = (c.contract_id, ln.sku, inv.date.year)
            prior = ytd[key]
            ytd[key] += ln.qty
            tiers = c.tier_list(ln.sku, self.skus[ln.sku].price)
            correct = [p for m, p in tiers if m <= prior][-1]
            ln.price = tiers[0][1] if ln.line_id in self._volume_lines else correct
        for inv in self.invoices:
            for ln in inv.lines:
                if ln.line_id not in contract_line_ids:          # off-contract or no contract
                    ln.price = d2(self.skus[ln.sku].price * Decimal(str(1 + r.uniform(-0.03, 0.05))))

    # ---------------------------------------------------------------- 6. price creep (planted)
    def plant_price_variance(self) -> None:
        r = self.rng
        by_key: dict[tuple, list[tuple[Invoice, Line]]] = defaultdict(list)
        for inv, ln, c in self._contract_lines():
            if ln.sku not in c.tiers:                             # only flat-priced SKUs
                by_key[(c.contract_id, ln.sku)].append((inv, ln))
        cands = [(k, v) for k, v in by_key.items() if len(v) >= 6]
        value = {k: sum(float(ln.price) * ln.qty for _, ln in v) / len(v) for k, v in cands}

        def pick(in_db: bool, n: int) -> list:
            pool = sorted((kv for kv in cands if self.contracts[kv[0][0]].in_db == in_db),
                          key=lambda kv: -value[kv[0]])[:max(n * 2, 10)]
            out, vendors = [], set()
            for kv in r.sample(pool, len(pool)):
                vid = self.contracts[kv[0][0]].vendor_id
                if vid not in vendors:
                    out.append(kv)
                    vendors.add(vid)
                if len(out) == n:
                    break
            return out

        db, llm = pick(True, 6), pick(False, 2)

        def exposure_of(p) -> float:
            return sum(float(ln.price) * ln.qty for _, ln in p[0][1][-p[1]:])

        # Two small uplifts (3.5%, 4%) on DB-priced contracts disappear when the tolerance is
        # raised to 5% (demo re-run). The LLM-only contracts get ordinary uplifts.
        plan = sorted([(kv, k, True) for kv, k in zip(db, [4, 4, 3, 3, 3, 3])], key=exposure_of) + \
               [(kv, k, False) for kv, k in zip(llm, [3, 2])]
        low = {0: 0.035, 1: 0.04}
        exposure = [exposure_of(p) for p in plan]
        base_u = [0, 0, 0.08, 0.11, 0.14, 0.12, 0.12, 0.15]
        low_total = sum(exposure[i] * u for i, u in low.items())
        high_idx = [i for i in range(len(plan)) if i not in low]
        scale = (TARGETS["PRICE_VARIANCE"] - low_total) / sum(exposure[i] * base_u[i] for i in high_idx)
        for i, ((key, rows), k, in_db) in enumerate(plan):
            u = low.get(i, min(0.35, max(0.06, base_u[i] * scale)))
            c = self.contracts[key[0]]
            base = self.skus[key[1]].price
            new_price = d2(base * Decimal(str(1 + u)))
            for inv, ln in rows[-k:]:
                ln.price = new_price
                self.anomalies.append({
                    "type": "PRICE_VARIANCE", "vendor_id": c.vendor_id, "vendor": self.vendors[c.vendor_id].name,
                    "contract_id": c.contract_id, "sku": key[1], "line_id": ln.line_id, "invoice_id": inv.invoice_id,
                    "quantity": ln.qty, "billed_price": float(new_price), "contract_price": float(base),
                    "uplift_pct": round(u * 100, 2), "amount": float(d2((new_price - base) * ln.qty)),
                    "needs_llm": not in_db,
                })

    # ---------------------------------------------------------------- 7. duplicate invoices (planted)
    def plant_duplicate_invoices(self) -> None:
        r = self.rng
        # Late enough cutoff that both the original and the copy (<= +33 days) are paid by DATA_END.
        cutoff = DATA_END - timedelta(days=90)
        keys = {v.key: v for v in self.vendors.values()}
        northwind, nw_record = keys["P0a"], keys["P0b"]
        eligible_roles = {"regular", "dup_original"}
        cands = [i for i in self.invoices
                 if self.vendors[i.vendor_id].role in eligible_roles and i.date <= cutoff
                 and Decimal(7000) <= i.total <= Decimal(19000)]
        nw_cands = ([i for i in cands if i.vendor_id == northwind.vendor_id]
                    or [i for i in self.invoices if i.vendor_id == northwind.vendor_id and i.date <= cutoff])
        others = [i for i in cands if i.vendor_id != northwind.vendor_id]
        best, best_err = None, None
        for _ in range(4000):
            pick = r.sample(others, 7)
            if len({i.vendor_id for i in pick}) < 7:
                continue
            combo = pick + [r.choice(nw_cands)]
            err = abs(sum(i.total for i in combo) - TARGETS["DUPLICATE_PAYMENT"])
            if best is None or err < best_err:
                best, best_err = combo, err

        variants = [
            ("invoice_no", lambda no: no, 33),                                   # re-keyed, same number
            ("invoice_no", lambda no: no.lower().replace("-", " "), 21),         # case / spacing
            ("invoice_no", lambda no: _insert_leading_zero(no), 9),              # extra leading zero
            ("invoice_no", lambda no: no.replace("-", ""), 26),                  # punctuation dropped
            ("date", None, 3), ("date", None, 8), ("date", None, 12),            # new number, within 14 days
            ("invoice_no", lambda no: no, 6),                                    # under the duplicate vendor record
        ]
        next_id = len(self.invoices)
        for n, (orig, (how, transform, days)) in enumerate(zip(best, variants)):
            vendor = nw_record if n == 7 else self.vendors[orig.vendor_id]
            d = orig.date + timedelta(days=days)
            if transform is None:
                number = self._next_invoice_no(vendor, d)
            else:
                number = transform(orig.vendor_invoice_no)
                assert normalize_invoice_no(number) == normalize_invoice_no(orig.vendor_invoice_no)
            next_id += 1
            dup = Invoice(f"INV-{next_id:06d}", vendor.vendor_id, number, d)
            for ln in orig.lines:
                self._line_no += 1
                dup.lines.append(Line(f"L-{self._line_no:06d}", dup.invoice_id, ln.sku, ln.qty, ln.price))
            self.invoices.append(dup)
            self._dup_invoice_ids.update({orig.invoice_id, dup.invoice_id})
            self._dup_of[dup.invoice_id] = orig.invoice_id
            self.anomalies.append({
                "type": "DUPLICATE_PAYMENT", "vendor_id": vendor.vendor_id, "vendor": vendor.name,
                "invoice_ids": [orig.invoice_id, dup.invoice_id], "duplicate_invoice_id": dup.invoice_id,
                "vendor_invoice_nos": [orig.vendor_invoice_no, number], "matched_by": how,
                "cross_vendor": vendor.vendor_id != orig.vendor_id, "amount": None,   # set after payments
                "needs_llm": False,
            })

    # ---------------------------------------------------------------- 8. payments (+ planted late payments)
    def make_payments(self) -> None:
        r = self.rng
        cands = []
        for inv in self.invoices:
            c = self.contract_of.get(inv.vendor_id)
            if c and parse_terms(c.terms) and inv.date <= DATA_END - timedelta(days=60):
                cands.append((inv, d2(inv.total * parse_terms(c.terms)[0] / 100)))
        cands.sort(key=lambda x: -x[1])
        pool = cands[:150]
        best, best_err = None, None
        for _ in range(4000):
            pick = r.sample(pool, 30)
            err = abs(sum(x[1] for x in pick) - TARGETS["EARLY_PAY_DISCOUNT"])
            if best is None or err < best_err:
                best, best_err = pick, err
        self._early_pay = {inv.invoice_id for inv, _ in best}

        n = 0
        first_paid: dict[str, date] = {}
        for inv in sorted(self.invoices, key=lambda i: (i.date, i.invoice_id)):
            c = self.contract_of.get(inv.vendor_id)
            terms = parse_terms(c.terms) if c else None
            total = inv.total
            parts: list[tuple[date, Decimal]]
            if terms:
                pct, disc_days, net_days = terms
                if inv.invoice_id in self._early_pay:
                    parts = [(inv.date + timedelta(days=r.randint(disc_days + 6, net_days + 12)), total)]
                else:
                    parts = [(inv.date + timedelta(days=r.randint(3, disc_days - 1)), d2(total * (1 - pct / 100)))]
            elif c:
                net = int(c.terms.split()[-1])
                parts = [(inv.date + timedelta(days=r.randint(net - 8, net + 6)), total)]
            else:
                paid = inv.date + timedelta(days=r.randint(18, 45))
                if inv.invoice_id in self._dup_of:        # the copy is paid after the original
                    paid = max(paid, first_paid[self._dup_of[inv.invoice_id]] + timedelta(days=r.randint(3, 12)))
                if inv.invoice_id not in self._dup_invoice_ids and r.random() < 0.06:
                    first = d2(total * Decimal(str(r.uniform(0.4, 0.7))))
                    parts = [(paid, first), (paid + timedelta(days=r.randint(7, 20)), total - first)]
                else:
                    parts = [(paid, total)]
            first_paid[inv.invoice_id] = parts[0][0]
            for paid_date, amount in parts:
                if paid_date <= DATA_END:
                    n += 1
                    self.payments.append(Payment(f"PAY-{n:06d}", inv.invoice_id, paid_date, amount))

    # ---------------------------------------------------------------- 9. ground truth
    def finish_ground_truth(self) -> None:
        paid: dict[str, Decimal] = defaultdict(Decimal)
        first_paid: dict[str, date] = {}
        for p in self.payments:
            paid[p.invoice_id] += p.amount
            first_paid[p.invoice_id] = min(first_paid.get(p.invoice_id, p.paid_date), p.paid_date)
        by_id = {i.invoice_id: i for i in self.invoices}

        for a in self.anomalies:
            if a["type"] == "DUPLICATE_PAYMENT":
                orig_id, dup_id = a["invoice_ids"]
                assert orig_id in paid and dup_id in paid, "both invoices must be paid"
                assert first_paid[dup_id] > first_paid[orig_id], "the duplicate is paid after the original"
                a["amount"] = float(paid[dup_id])

        for inv_id in sorted(self._early_pay):
            inv = by_id[inv_id]
            c = self.contract_of[inv.vendor_id]
            pct, disc_days, _ = parse_terms(c.terms)
            days = (first_paid[inv_id] - inv.date).days
            assert days > disc_days
            self.anomalies.append({
                "type": "EARLY_PAY_DISCOUNT", "vendor_id": inv.vendor_id, "vendor": self.vendors[inv.vendor_id].name,
                "invoice_id": inv_id, "contract_id": c.contract_id, "payment_terms": c.terms,
                "days_to_pay": days, "amount": float(d2(inv.total * pct / 100)), "needs_llm": False,
            })

        spend: dict[str, Decimal] = defaultdict(Decimal)
        for inv in self.invoices:
            spend[inv.vendor_id] += inv.total
        keys = {v.key: v for v in self.vendors.values()}
        for a_key, b_key, how, needs_llm in self.vendor_pairs:
            a, b = keys[a_key], keys[b_key]
            ratio = SequenceMatcher(None, normalize_vendor_name(a.name), normalize_vendor_name(b.name)).ratio()
            self.anomalies.append({
                "type": "DUPLICATE_VENDOR", "vendor_ids": sorted([a.vendor_id, b.vendor_id]),
                "vendors": [a.name, b.name], "matched_by": how, "name_similarity": round(ratio, 3),
                "amount": float(d2(min(spend[a.vendor_id], spend[b.vendor_id]) * LEVERAGE_RATE)),
                "needs_llm": needs_llm,
            })
        d0, d1 = (keys["D0"], keys["D1"])
        self.decoys.append({
            "type": "DUPLICATE_VENDOR", "vendor_ids": sorted([d0.vendor_id, d1.vendor_id]),
            "vendors": [d0.name, d1.name],
            "name_similarity": round(SequenceMatcher(None, normalize_vendor_name(d0.name),
                                                     normalize_vendor_name(d1.name)).ratio(), 3),
            "note": "Different companies with borderline-similar names. Must NOT be reported.",
        })

    # ---------------------------------------------------------------- invariants
    def verify(self) -> None:
        planted = {frozenset((a, b)): (how, llm) for a, b, how, llm in self.vendor_pairs}
        decoy = frozenset(("D0", "D1"))
        vs = sorted(self.vendors.values(), key=lambda v: v.vendor_id)
        for i, a in enumerate(vs):
            for b in vs[i + 1:]:
                pair = frozenset((a.key, b.key))
                ratio = SequenceMatcher(None, normalize_vendor_name(a.name), normalize_vendor_name(b.name)).ratio()
                same_tax = bool(a.tax_id) and normalize_tax_id(a.tax_id) == normalize_tax_id(b.tax_id)
                if pair in planted:
                    how = planted[pair][0]
                    ok = (same_tax if how == "tax_id" else
                          not same_tax and ratio >= NAME_MATCH_RATIO if how == "name" else
                          not same_tax and NAME_BORDERLINE_RATIO <= ratio < NAME_MATCH_RATIO)
                elif pair == decoy:
                    ok = not same_tax and NAME_BORDERLINE_RATIO <= ratio < NAME_MATCH_RATIO
                else:
                    ok = not same_tax and ratio < NAME_BORDERLINE_RATIO
                assert ok, f"vendor pair violates plan: {a.name!r} / {b.name!r} ratio={ratio:.3f} tax={same_tax}"
        assert len(self.vendors) == 120 and len(self.contracts) == 30
        assert sum(1 for c in self.contracts.values() if not c.in_db) == 10
        for c in self.contracts.values():
            assert c.start <= DATA_START and c.end >= DATA_END, c.contract_id

    # ---------------------------------------------------------------- run
    def run(self) -> None:
        self.make_vendors()
        self.make_contracts()
        self.make_invoices()
        self.make_tiers()
        self.price_lines()
        self.plant_price_variance()
        self.plant_duplicate_invoices()
        self.make_payments()
        self.finish_ground_truth()
        self.verify()


def _insert_leading_zero(no: str) -> str:
    for i, ch in enumerate(no):
        if ch.isdigit():
            return no[:i] + "0" + no[i:]
    return "0" + no


# --------------------------------------------------------------------------- writers
SCHEMA = """
CREATE TABLE vendors (
    vendor_id    TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    tax_id       TEXT,
    address      TEXT,
    category     TEXT
);
CREATE TABLE contracts (
    contract_id   TEXT PRIMARY KEY,
    vendor_id     TEXT NOT NULL,
    start_date    TEXT NOT NULL,
    end_date      TEXT NOT NULL,
    payment_terms TEXT,
    document_file TEXT NOT NULL
);
CREATE TABLE contract_rates (
    contract_id TEXT,
    sku         TEXT,
    unit_price  REAL,
    min_qty     INTEGER DEFAULT 0,
    PRIMARY KEY (contract_id, sku, min_qty)
);
CREATE TABLE invoices (
    invoice_id        TEXT PRIMARY KEY,
    vendor_id         TEXT NOT NULL,
    vendor_invoice_no TEXT NOT NULL,
    invoice_date      TEXT NOT NULL,
    total_amount      REAL NOT NULL
);
CREATE TABLE invoice_lines (
    line_id     TEXT PRIMARY KEY,
    invoice_id  TEXT NOT NULL,
    sku         TEXT,
    quantity    REAL NOT NULL,
    unit_price  REAL NOT NULL
);
CREATE TABLE payments (
    payment_id  TEXT PRIMARY KEY,
    invoice_id  TEXT NOT NULL,
    paid_date   TEXT NOT NULL,
    amount      REAL NOT NULL
);
"""


def write_db(g: Generator, path: Path) -> None:
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO vendors VALUES (?,?,?,?,?)",
                   [(v.vendor_id, v.name, v.tax_id, v.address, CATEGORIES[v.category]["label"])
                    for v in sorted(g.vendors.values(), key=lambda v: v.vendor_id)])
    db.executemany("INSERT INTO contracts VALUES (?,?,?,?,?,?)",
                   [(c.contract_id, c.vendor_id, c.start.isoformat(), c.end.isoformat(), c.terms, f"{c.contract_id}.md")
                    for c in g.contracts.values()])
    rates = []
    for c in g.contracts.values():
        if c.in_db:                    # ~10 contracts keep their prices only in the Markdown text
            for sku in c.skus:
                for min_qty, price in c.tier_list(sku, g.skus[sku].price):
                    rates.append((c.contract_id, sku, float(price), min_qty))
    db.executemany("INSERT INTO contract_rates VALUES (?,?,?,?)", rates)
    db.executemany("INSERT INTO invoices VALUES (?,?,?,?,?)",
                   [(i.invoice_id, i.vendor_id, i.vendor_invoice_no, i.date.isoformat(), float(i.total))
                    for i in g.invoices])
    db.executemany("INSERT INTO invoice_lines VALUES (?,?,?,?,?)",
                   [(ln.line_id, ln.invoice_id, ln.sku, float(ln.qty), float(ln.price))
                    for i in g.invoices for ln in i.lines])
    db.executemany("INSERT INTO payments VALUES (?,?,?,?)",
                   [(p.payment_id, p.invoice_id, p.paid_date.isoformat(), float(p.amount)) for p in g.payments])
    db.commit()
    db.close()


_PROSE_PRICE = [
    "Item {sku} ({desc}) shall be invoiced at {price} per {unit}.",
    "The agreed unit price for {desc} (SKU {sku}) is {price} per {unit}.",
    "Supplier will charge {price} for each {unit} of {sku}, {desc}.",
]
_PROSE_TIER = [
    "Once Buyer's cumulative purchases of {sku} in a calendar year reach {qty} units, every further unit of "
    "that item purchased in the same calendar year shall be invoiced at {price} per {unit}.",
    "Volume pricing for {sku}: after {qty} units have been purchased within a calendar year, the price for "
    "all subsequent units in that calendar year falls to {price} per {unit}.",
]


def contract_markdown(g: Generator, c: Contract, rng: random.Random) -> str:
    v = g.vendors[c.vendor_id]
    terms = parse_terms(c.terms)
    if terms:
        pct, disc_days, net_days = terms
        pay_clause = (f"**{c.terms}.** Buyer may deduct {pct.normalize():f}% of the invoice total when payment is made "
                      f"within {disc_days} days of the invoice date; otherwise the full amount is due within "
                      f"{net_days} days of the invoice date.")
    else:
        pay_clause = f"**{c.terms}.** The full invoice amount is due within {c.terms.split()[-1]} days of the invoice date."
    out = [
        f"# Supply Agreement {c.contract_id}", "",
        "| | |", "|---|---|",
        f"| **Supplier** | {v.name} |",
        f"| **Supplier address** | {v.address} |",
        f"| **Buyer** | {BUYER} |",
        f"| **Term** | {c.start.isoformat()} to {c.end.isoformat()} |",
        f"| **Payment terms** | {c.terms} |", "",
        "## 1. Scope", "",
        f"Supplier shall supply the products listed in Section 2 ({CATEGORIES[v.category]['label'].lower()}) "
        f"to Buyer's facilities against Buyer purchase orders.", "",
        "## 2. Pricing", "",
        "All prices are in US dollars, exclusive of sales tax, and fixed for the Term.", "",
    ]
    def unit_of(s: Sku) -> str:
        return "unit" if s.item.unit == "each" else s.item.unit

    if c.in_db:
        out += ["| SKU | Description | Unit | Unit price | Volume price |", "|---|---|---|---|---|"]
        for sku in c.skus:
            s = g.skus[sku]
            tiers = c.tier_list(sku, s.price)
            vol = "; ".join(f"{money(p)} per {unit_of(s)} from {m:,} units per calendar year"
                            for m, p in tiers[1:]) or "—"
            out.append(f"| {sku} | {s.item.desc} | {unit_of(s)} | {money(tiers[0][1])} | {vol} |")
    else:
        n = 0
        for sku in c.skus:
            s = g.skus[sku]
            tiers = c.tier_list(sku, s.price)
            n += 1
            out.append(f"2.{n} " + rng.choice(_PROSE_PRICE).format(
                sku=sku, desc=s.item.desc, price=money(tiers[0][1]), unit=unit_of(s)))
            out.append("")
            for m, p in tiers[1:]:
                n += 1
                out.append(f"2.{n} " + rng.choice(_PROSE_TIER).format(
                    sku=sku, qty=f"{m:,}", price=money(p), unit=unit_of(s)))
                out.append("")
    out += [
        "", "## 3. Volume pricing", "",
        "Volume prices apply to all units of an item purchased after Buyer's cumulative purchases of that item "
        "in the same calendar year have reached the stated quantity. Supplier is responsible for applying the "
        "volume price on its invoices without a separate request from Buyer.", "",
        "## 4. Payment terms", "", pay_clause, "",
        "## 5. Price changes", "",
        "No change to any price in Section 2 is effective unless made by a written amendment signed by both parties.", "",
        "## 6. Other charges", "",
        "Orders below $500.00 carry a delivery charge of $45.00 per delivery. Rush orders requested with less than "
        "48 hours' notice carry a surcharge of 8% of the order value. No other fees may be invoiced.", "",
        "## 7. Governing law", "",
        "This Agreement is governed by the laws of the State of Ohio.", "",
    ]
    return "\n".join(out)


def write_contracts(g: Generator, folder: Path) -> None:
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    rng = random.Random(SEED + 1)
    for c in sorted(g.contracts.values(), key=lambda c: c.contract_id):
        (folder / f"{c.contract_id}.md").write_text(contract_markdown(g, c, rng), encoding="utf-8")


def write_ground_truth(g: Generator, path: Path) -> dict:
    order = ["DUPLICATE_VENDOR", "DUPLICATE_PAYMENT", "PRICE_VARIANCE", "VOLUME_DISCOUNT", "EARLY_PAY_DISCOUNT"]
    anomalies = sorted(g.anomalies, key=lambda a: order.index(a["type"]))
    for n, a in enumerate(anomalies, start=1):
        a["id"] = f"GT-{n:03d}"
    totals = {t: {"count": 0, "amount": 0.0, "offline_count": 0, "offline_amount": 0.0} for t in order}
    for a in anomalies:
        t = totals[a["type"]]
        t["count"] += 1
        t["amount"] = round(t["amount"] + a["amount"], 2)
        if not a["needs_llm"]:
            t["offline_count"] += 1
            t["offline_amount"] = round(t["offline_amount"] + a["amount"], 2)
    doc = {
        "seed": SEED,
        "date_range": {"from": DATA_START.isoformat(), "to": DATA_END.isoformat()},
        "counts": {"vendors": len(g.vendors), "contracts": len(g.contracts),
                   "contracts_without_rate_table": sum(1 for c in g.contracts.values() if not c.in_db),
                   "invoices": len(g.invoices), "invoice_lines": sum(len(i.lines) for i in g.invoices),
                   "payments": len(g.payments)},
        "totals_by_type": totals,
        "anomalies": [{"id": a.pop("id"), **a} for a in anomalies],
        "decoys": g.decoys,
        # The true price tables of the contracts that state prices only in their text: used to
        # score LLM price extraction (backend/evaluation.py). Not visible to the agent.
        "text_contract_rates": {
            c.contract_id: [{"sku": sku, "unit_price": float(price), "min_qty": min_qty}
                            for sku in c.skus for min_qty, price in c.tier_list(sku, g.skus[sku].price)]
            for c in sorted(g.contracts.values(), key=lambda c: c.contract_id) if not c.in_db},
    }
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return doc


def generate(out_dir: Path) -> dict:
    """Build the whole dataset into out_dir and return the ground-truth document."""
    out_dir.mkdir(parents=True, exist_ok=True)
    g = Generator()
    g.run()
    write_db(g, out_dir / "erp.db")
    write_contracts(g, out_dir / "contracts")
    return write_ground_truth(g, out_dir / "planted_anomalies.json")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "data", help="output folder (default: ./data)")
    args = ap.parse_args()
    doc = generate(args.out)
    c = doc["counts"]
    print(f"Wrote {args.out}: {c['vendors']} vendors, {c['contracts']} contracts "
          f"({c['contracts_without_rate_table']} without a rate table), {c['invoices']:,} invoices, "
          f"{c['invoice_lines']:,} lines, {c['payments']:,} payments")
    print(f"{'Planted anomaly':<22}{'count':>7}{'amount':>14}{'offline':>9}{'offline $':>14}{'target':>10}")
    for t, v in doc["totals_by_type"].items():
        target = f"{TARGETS[t]:,}" if t in TARGETS else "est."
        print(f"{t:<22}{v['count']:>7}{v['amount']:>14,.2f}{v['offline_count']:>9}{v['offline_amount']:>14,.2f}{target:>10}")


if __name__ == "__main__":
    main()
