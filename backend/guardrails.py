"""Guardrails around every LLM call: what may be sent, and what may be used from the answer.

All checks are deterministic Python (no extra model calls). Each time one fires, the run's live
stream gets a ``guardrail`` event (shown on the Analyze page) and the run falls back to its
offline behaviour for that item. A guardrail never fails a run.

Input guardrails (before a request leaves the machine)
  G1 redact        - e-mail addresses, phone numbers, bank/IBAN/card numbers, US SSNs and tax IDs
                     in the text sent to the LLM are replaced by placeholders.
  G2 injection     - untrusted text (contract text, vendor names/addresses, finding data) that looks
                     like instructions to the model ("ignore previous instructions", "system prompt",
                     role tags, ...) is not sent at all; that item is handled offline.
  G3 size          - a single request larger than MAX_REQUEST_CHARS is not sent.
  G4 budget        - at most LLM_MAX_CALLS_PER_RUN calls and LLM_MAX_CHARS_PER_RUN characters per run.

Output guardrails (before an answer is used)
  G5 price sanity  - an extracted contract price must be plausible against what the vendor actually
                     billed for that SKU, and a volume-tier price must be below the base price.
                     (On top of the existing checks: SKU on the vendor's invoices, price literally in
                     the contract text.)
  G6 explanation   - an explanation is used only if every number in it appears in the finding's data
                     (no invented amounts), it names the vendor or an ID, it fits the finding type, and
                     it has no links, e-mail addresses, code, markup or instruction-like text.
                     Otherwise the template explanation is used.
  G7 vendor reason - the LLM's reason for a vendor match is cleaned (no links/markup) and shortened.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from statistics import median

from .graph import detectors as det

# ----------------------------------------------------------------------------- limits
MAX_REQUEST_CHARS = 60_000
DEFAULT_MAX_CALLS_PER_RUN = 100       # a normal pass makes ~17 calls; up to 3 passes per run
DEFAULT_MAX_CHARS_PER_RUN = 1_500_000
PRICE_RATIO_MIN, PRICE_RATIO_MAX = 0.5, 2.0   # median billed price / extracted base price
MAX_REASON_CHARS = 200

# ----------------------------------------------------------------------------- G1 redaction
_REDACTIONS: list[tuple[str, re.Pattern]] = [
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?\b")),
    ("CARD", re.compile(r"\b(?:\d{4}[ -]?){3}\d{1,4}\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("TAX_ID", re.compile(r"\b\d{2}-\d{7}\b")),
    ("PHONE", re.compile(r"(?<![\w$.,])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")),
    ("BANK_ACCOUNT", re.compile(r"(?i)\b(?:account|acct|routing|aba|swift|sort code)(?:\s*(?:no\.?|number|#))?"
                                r"\s*[:#]?\s*[A-Z0-9][A-Z0-9 -]{5,30}\d")),
]


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Replace sensitive values with [REDACTED-<KIND>]. Returns the new text and counts per kind."""
    counts: dict[str, int] = {}
    for kind, pattern in _REDACTIONS:
        text, n = pattern.subn(f"[REDACTED-{kind}]", text)
        if n:
            counts[kind] = counts.get(kind, 0) + n
    return text, counts


# ----------------------------------------------------------------------------- G2 injection
_INJECTION = [
    r"ignore (?:all |any |the )?(?:previous|prior|above|earlier|preceding) (?:instructions|prompts?|rules|messages)",
    r"disregard (?:all |any |the )?(?:previous|prior|above|earlier) ",
    r"forget (?:all |any |everything|your) (?:previous |prior )?(?:instructions|rules|you were told)",
    r"\b(?:new|updated|override|overriding) (?:system )?instructions?\b",
    r"\bsystem prompt\b",
    r"\byou are (?:now|no longer) (?:a|an|the)\b",
    # "act as the exclusive distributor" is normal contract language: only model-directed roles count.
    r"\bact as (?:a|an|the|if you were) (?:ai|assistant|language model|chatbot|llm|system|different|new)\b",
    r"\bpretend (?:to be|you are)\b",
    r"\bjailbreak\b|\bDAN mode\b|\bdeveloper mode\b",
    r"<\s*/?\s*(?:system|assistant|user|instructions?)\s*>",
    r"\[\s*(?:system|assistant|INST)\s*\]",
    r"\b(?:respond|reply|answer|output) (?:only )?with\b.{0,40}\b(?:json|price|0|zero)\b",
    r"\bset (?:all |every |the )?(?:prices?|amounts?|unit prices?) to\b",
    r"\bdo not (?:report|flag|mention)\b",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in _INJECTION), re.IGNORECASE)


def injection_match(text: str) -> str | None:
    """The first instruction-like phrase in text, or None."""
    m = _INJECTION_RE.search(text or "")
    return m.group(0)[:80] if m else None


# ----------------------------------------------------------------------------- G4 budget
@dataclass
class Budget:
    max_calls: int = DEFAULT_MAX_CALLS_PER_RUN
    max_chars: int = DEFAULT_MAX_CHARS_PER_RUN
    calls: dict[str, int] = field(default_factory=dict)
    chars: dict[str, int] = field(default_factory=dict)
    exhausted: set[str] = field(default_factory=set)

    def allow(self, run_id: str, size: int) -> str | None:
        """Reserve one call of `size` characters for run_id. Returns a reason if it is over budget."""
        calls, chars = self.calls.get(run_id, 0), self.chars.get(run_id, 0)
        if calls + 1 > self.max_calls:
            return f"call limit of {self.max_calls} per run reached"
        if chars + size > self.max_chars:
            return f"limit of {self.max_chars:,} characters sent per run reached"
        self.calls[run_id], self.chars[run_id] = calls + 1, chars + size
        return None


# ----------------------------------------------------------------------------- G5 price sanity
def billed_prices(invoices: list[dict]) -> dict[tuple[str, str], float]:
    """Median billed unit price per (vendor_id, sku)."""
    prices: dict[tuple[str, str], list[float]] = {}
    for inv in invoices or []:
        for ln in inv.get("lines") or []:
            if ln.get("sku") and ln.get("unit_price") is not None:
                prices.setdefault((inv["vendor_id"], ln["sku"]), []).append(float(ln["unit_price"]))
    return {k: median(v) for k, v in prices.items() if v}


def check_price_rows(rows: list[dict], vendor_id: str, billed: dict[tuple[str, str], float]) -> tuple[list[dict], list[str]]:
    """Keep plausible extracted rows. Returns (kept rows, reasons for the rejected ones)."""
    base = {r["sku"]: r["unit_price"] for r in rows if not r["min_qty"]}
    kept, rejected = [], []
    for r in rows:
        sku, price = r["sku"], float(r["unit_price"])
        typical = billed.get((vendor_id, sku))
        if not r["min_qty"] and typical and not (PRICE_RATIO_MIN <= typical / price <= PRICE_RATIO_MAX):
            rejected.append(f"{sku} ${price:,.2f} (billed ~${typical:,.2f})")
            continue
        if r["min_qty"]:
            b = base.get(sku)
            if b is None or price >= b:
                rejected.append(f"{sku} tier ${price:,.2f} from {r['min_qty']:,} units "
                                + ("(no base price)" if b is None else f"(not below base ${b:,.2f})"))
                continue
        kept.append(r)
    return kept, rejected


# ----------------------------------------------------------------------------- G6 explanation
NUMBER = re.compile(r"(?<![A-Za-z\-])\$?\d[\d,]*(?:\.\d+)?%?")
ID_LIKE = re.compile(r"\b(?:[A-Z]{1,4}-?\d{3,}|[A-Z]{3}-\d{4})\b")
_UNSAFE_OUTPUT = re.compile(
    r"https?://|www\.|\b[\w.+-]+@[\w-]+\.\w+|```|<[a-z/][^>]*>|\bas an ai\b|\blanguage model\b|"
    r"\bI (?:cannot|can't|am unable)\b", re.IGNORECASE)
_STOP_WORDS = {"corp", "group", "services", "service", "supply", "supplies", "products", "company", "inc",
               "llc", "ltd", "limited", "the", "and"}

TOPIC_WORDS = {
    det.DUPLICATE_VENDOR: ("duplicate", "same company", "same vendor", "same supplier", "two records",
                           "two vendor", "merged", "twice", "both records"),
    det.DUPLICATE_PAYMENT: ("duplicate", "twice", "paid again", "double", "same invoice", "two payments"),
    det.PRICE_VARIANCE: ("contract", "above", "over", "overcharg", "higher", "agreed"),
    det.VOLUME_DISCOUNT: ("volume", "tier", "discount", "threshold"),
    det.EARLY_PAY_DISCOUNT: ("early", "discount", "within", "days"),
}


def numbers(text: str) -> set[str]:
    out = set()
    for m in NUMBER.findall(text or ""):
        n = m.strip("$%").replace(",", "")
        try:
            out.add(f"{float(n):.2f}")
        except ValueError:
            continue
    return out


def finding_text(f: dict) -> str:
    keep = {k: f.get(k) for k in ("vendor", "title", "amount", "calculation", "evidence", "confidence")}
    return json.dumps(keep, default=str)


def unsupported_numbers(explanation: str, f: dict) -> list[str]:
    """Numbers in the explanation that do not appear anywhere in the finding's data."""
    source = numbers(finding_text(f)) | {f"{float(f.get('amount') or 0):.2f}"}
    return sorted(n for n in numbers(explanation) - source if float(n) not in (0.0, 1.0, 2.0))


def _identifiers(f: dict) -> set[str]:
    """IDs a relevant explanation may cite: SKUs, contract/invoice/line IDs, vendor invoice numbers."""
    ev = f.get("evidence") or {}
    ids = set(ID_LIKE.findall(finding_text(f)))
    # Any token with a digit in the title (e.g. "2025-34486", "029489") or in the evidence ID lists.
    sources = [f.get("title") or ""] + [str(x) for k, v in ev.items() if k.endswith(("_id", "_ids", "_no", "_nos", "sku"))
                                        for x in (v if isinstance(v, list) else [v])]
    for s in sources:
        ids.update(t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]{3,}", s) if any(c.isdigit() for c in t))
    return ids


def relevancy_problem(explanation: str, f: dict) -> str | None:
    low = (explanation or "").lower()
    ids = _identifiers(f)
    vendor_tokens = [w for w in re.findall(r"[a-z]{4,}", (f.get("vendor") or "").lower()) if w not in _STOP_WORDS]
    if not (any(t in low for t in vendor_tokens) or any(i.lower() in low for i in ids)):
        return "does not name the vendor or an ID"
    if not any(w in low for w in TOPIC_WORDS.get(f.get("type"), ())):
        return "wording does not match the finding type"
    return None


def check_explanation(explanation: str, f: dict) -> str | None:
    """None if the explanation may be shown, otherwise the reason it is blocked."""
    if not explanation or not explanation.strip():
        return "empty"
    m = _UNSAFE_OUTPUT.search(explanation)
    if m:
        return f"contains disallowed content ({m.group(0)[:30]!r})"
    if injection_match(explanation):
        return "contains instruction-like text"
    extra = unsupported_numbers(explanation, f)
    if extra:
        return f"numbers not in the finding: {', '.join(extra[:4])}"
    return relevancy_problem(explanation, f)


# ----------------------------------------------------------------------------- G7 vendor reason
def clean_reason(reason: str) -> str:
    text = re.sub(r"https?://\S+|www\.\S+|<[^>]+>|[`*_#>]", "", reason or "")
    text = " ".join(text.split())
    return text[:MAX_REASON_CHARS].rstrip() + ("…" if len(text) > MAX_REASON_CHARS else "")
