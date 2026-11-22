"""Shared fixtures: one generated dataset per test session (seeded, so identical every time)."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import Settings  # noqa: E402
from seed.generate import generate  # noqa: E402


@dataclass
class Dataset:
    root: Path
    ground_truth: dict

    def settings(self, output_dir: Path, llm_mode: str = "off") -> Settings:
        return Settings(host="127.0.0.1", port=8000, erp_db_path=self.root / "erp.db",
                        contracts_dir=self.root / "contracts", output_dir=output_dir,
                        llm_mode=llm_mode, llm_model="claude-opus-5-5", price_tolerance_pct=2.0)


@pytest.fixture(scope="session")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Dataset:
    root = tmp_path_factory.mktemp("data")
    generate(root)
    return Dataset(root, json.loads((root / "planted_anomalies.json").read_text(encoding="utf-8")))
