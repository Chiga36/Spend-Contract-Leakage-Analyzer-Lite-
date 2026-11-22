"""Settings, read from the process environment plus an optional `.env` file (LLD §11).

`.env` is parsed by a tiny reader so `python-dotenv` is not needed. Real environment
variables always win over values in the file, and empty values are ignored.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODELS = {"claude": "claude-opus-5-5", "openai": "gpt-5", "gemini": "gemini-3.5-flash"}
# What the UI and report show for each mode: label, API host, key variable(s).
PROVIDERS = {"claude": ("Claude", "api.anthropic.com", ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")),
             "openai": ("OpenAI", "api.openai.com", ("OPENAI_API_KEY",)),
             "gemini": ("Gemini", "generativelanguage.googleapis.com", ("GEMINI_API_KEY", "GOOGLE_API_KEY"))}
# Gemini is called through Google's OpenAI-compatible endpoint, with the openai SDK.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
log = logging.getLogger(__name__)


def load_env_file(path: Path = ROOT / ".env") -> None:
    """Load KEY=VALUE lines into os.environ (comments, blanks and empty values skipped)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        comment = re.search(r"\s#", f" {value}")                    # a "#" after whitespace starts a comment
        value = (value[:comment.start()] if comment else value).strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    erp_db_path: Path
    contracts_dir: Path
    output_dir: Path
    llm_mode: str            # "off" | "claude" | "openai" | "gemini"
    llm_model: str
    price_tolerance_pct: float
    # Planted anomalies of the synthetic data; evaluation metrics are only available when it exists.
    ground_truth_path: Path = ROOT / "data" / "planted_anomalies.json"
    # Guardrail G4: LLM budget per run (see backend/guardrails.py).
    llm_max_calls_per_run: int = 100
    llm_max_chars_per_run: int = 1_500_000
    # Datasets uploaded from the Start page: UPLOADS_DIR/<id>/erp.db and contracts/.
    uploads_dir: Path = ROOT / "data" / "uploads"


def _path(name: str, default: str) -> Path:
    p = Path(os.environ.get(name, default))
    return p if p.is_absolute() else ROOT / p


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_env_file()
    # langchain-core (pulled in by LangGraph) can send traces to LangSmith, which would take data
    # off the machine. Force it off regardless of any machine-wide setting (LLD §0, observability).
    for var in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
        os.environ[var] = "false"
    llm_mode = os.environ.get("LLM_MODE", "off").strip().lower()
    if llm_mode not in ("off", *PROVIDERS):
        log.warning("Unknown LLM_MODE=%r, falling back to 'off'", llm_mode)
        llm_mode = "off"
    return Settings(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        erp_db_path=_path("ERP_DB_PATH", "data/erp.db"),
        contracts_dir=_path("CONTRACTS_DIR", "data/contracts"),
        output_dir=_path("OUTPUT_DIR", "output"),
        llm_mode=llm_mode,
        llm_model=os.environ.get("LLM_MODEL") or DEFAULT_MODELS.get(llm_mode, ""),
        price_tolerance_pct=float(os.environ.get("PRICE_TOLERANCE_PCT", "2")),
        ground_truth_path=_path("GROUND_TRUTH_PATH", "data/planted_anomalies.json"),
        uploads_dir=_path("UPLOADS_DIR", "data/uploads"),
        llm_max_calls_per_run=int(os.environ.get("LLM_MAX_CALLS_PER_RUN", "100")),
        llm_max_chars_per_run=int(os.environ.get("LLM_MAX_CHARS_PER_RUN", "1500000")),
    )
