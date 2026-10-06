"""Shared fixtures. Nothing is mocked: every test runs real DuckDB, PyArrow, ORC and Avro on real files."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bakeoff import bench, data
from bakeoff.config import Settings, read_json
from bakeoff.report import report


def make_settings(root: Path, **env: str) -> Settings:
    base = {"LAKE_DATA_DIR": str(root / "data"), "LAKE_PUBLISHED_DIR": str(root / "published"),
            "LAKE_SOURCE": "synthetic", "LAKE_SYNTH_ROWS": "4000", "LAKE_LAB_ROWS": "3000", "LAKE_SEED": "8",
            "LAKE_COLD_RUNS": "1", "LAKE_WARMUPS": "0", "LAKE_WARM_RUNS": "2", "LAKE_CELL_BUDGET_S": "0",
            "LAKE_THREADS": "2"}
    return Settings.from_env(base | env)


def run_stages(s: Settings, only: str | None = None) -> SimpleNamespace:
    """land -> profile -> ingest -> materialize -> bench -> columns -> report, as `bakeoff all` does."""
    landing, ds = data.taxi_dataset(s)
    info: dict[str, Any] = {}
    data.land(s, info)
    data.profile(s, landing, {})
    data.ingest(s, landing, ds, {})
    bench.materialize(s, {}, only)
    failed = bench.bench(s, ds, {}, only)
    bench.column_lab(s, {})
    report(s, ds, {}, publish=True)
    return SimpleNamespace(s=s, ds=ds, landing=landing, failed=failed,
                           results=read_json(s.data_dir / "results" / "results.json"),
                           ingest=read_json(s.data_dir / "clean" / "ingest.json"),
                           injected=read_json(s.data_dir / "landing" / "landing.json")["injected"])


@pytest.fixture(scope="session")
def pipeline(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """The whole pipeline, once, on a small seeded synthetic dataset with every defect injected."""
    return run_stages(make_settings(tmp_path_factory.mktemp("pipeline")))
