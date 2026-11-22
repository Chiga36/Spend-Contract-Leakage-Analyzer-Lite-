"""Pydantic models: API request bodies (LLD §7.5, §8.1) and the LLM's structured outputs (LLD §9)."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator


# --------------------------------------------------------------------------- API
class RunParams(BaseModel):
    """Body of POST /api/runs, and `new_params` of a re-run. Missing values use the defaults."""
    date_from: date | None = None
    date_to: date | None = None
    price_tolerance_pct: float | None = Field(default=None, ge=0, le=50)
    # "demo" (the synthetic data) or the id of an uploaded dataset. Not changeable on a re-run.
    dataset: str | None = Field(default=None, pattern=r"^(demo|u[0-9a-f]{12})$")

    @model_validator(mode="after")
    def _check_range(self) -> "RunParams":
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to")
        return self


class Decision(BaseModel):
    status: Literal["APPROVED", "REJECTED", "ADJUSTED"]
    note: str | None = Field(default=None, max_length=500)
    amount: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _adjusted_needs_amount(self) -> "Decision":
        if self.status == "ADJUSTED" and self.amount is None:
            raise ValueError("an ADJUSTED decision needs an amount")
        return self


class ReviewPayload(BaseModel):
    """Body of POST /api/runs/{id}/review; passed to the graph as Command(resume=...)."""
    action: Literal["approve", "rerun"]
    decisions: dict[str, Decision] = Field(default_factory=dict)
    new_params: RunParams | None = None


# --------------------------------------------------------------------------- LLM outputs
# No numeric constraints here: they are validated in code after the call.
class PriceRow(BaseModel):
    sku: str
    unit_price: float
    min_qty: int
    clause: str


class PriceTable(BaseModel):
    rows: list[PriceRow]


class PairDecision(BaseModel):
    pair_id: str
    same_company: bool
    reason: str


class PairDecisions(BaseModel):
    decisions: list[PairDecision]


class Explanation(BaseModel):
    id: str
    explanation: str


class Explanations(BaseModel):
    items: list[Explanation]
