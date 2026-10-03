"""Shared fixtures. Synthetic data is seeded and generated once per session, so every failure
reproduces exactly (same seed -> byte-identical raw files)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from goldstandard.raw import RawStore
from goldstandard.sources import synthetic

REPO = Path(__file__).resolve().parents[1]
REFERENCE = REPO / "reference"
SMALL_ITEMS = (34, 35, 36, 587, 2048, 28668, 44992, 4051, 16272, 3689, 209, 17478)


def small_config(**kw) -> synthetic.SynthConfig:
    return synthetic.SynthConfig(start=date(2025, 9, 1), servers=2, seed=7, items=SMALL_ITEMS, **kw)


@pytest.fixture(scope="session")
def small_world(tmp_path_factory) -> dict:
    """Two servers, 12 items, 45 days of synthetic snapshots with all defect types enabled."""
    root = tmp_path_factory.mktemp("small_world")
    store = RawStore(root / "raw")
    cfg = small_config()
    stats = synthetic.generate(
        cfg, store, REFERENCE, until=datetime(2025, 10, 16, tzinfo=UTC), truth_dir=root / "truth"
    )
    return {"root": root, "store": store, "cfg": cfg, "stats": stats}


@pytest.fixture(scope="session")
def known_items() -> set[int]:
    return {i["type_id"] for i in json.loads((REFERENCE / "eve_universe.json").read_text())["items"]}


# ------------------------------------------------------------------------------------------ integration
import os  # noqa: E402
import shutil  # noqa: E402
import tarfile  # noqa: E402
import uuid  # noqa: E402

import psycopg  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from goldstandard import migrate  # noqa: E402
from goldstandard.config import Settings  # noqa: E402

ADMIN_DSN = os.environ.get("GS_TEST_ADMIN_DSN")


def _swap_db(dsn: str, name: str) -> str:
    return dsn.rsplit("/", 1)[0] + "/" + name


@pytest.fixture(scope="session")
def pg():
    """A fresh database per test session, migrated from zero with the real migrations."""
    if not ADMIN_DSN:
        pytest.skip("integration tests need GS_TEST_ADMIN_DSN (superuser DSN of a disposable postgres)")
    name = f"gs_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}" OWNER gs_owner')
    dsns = {
        k: _swap_db(os.environ[v], name)
        for k, v in (
            ("owner", "GS_OWNER_DATABASE_URL"),
            ("pipeline", "GS_DATABASE_URL"),
            ("api", "GS_API_DATABASE_URL"),
        )
    }
    migrate.upgrade(dsns["owner"])
    yield {"name": name, **dsns}
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def make_settings(data_dir: Path, dsns: dict) -> Settings:
    return Settings(
        data_dir=data_dir,
        database_url=SecretStr(dsns["pipeline"]),
        api_database_url=SecretStr(dsns["api"]),
        synth_start=date(2025, 9, 1),
        synth_servers=2,
        log_json=False,
    )


@pytest.fixture(scope="session")
def seeded(pg, small_world, tmp_path_factory):
    """Both worlds processed end to end into the session database (small synthetic world + real EVE fixture)."""
    from goldstandard import db
    from goldstandard.pipeline import Pipeline

    data = tmp_path_factory.mktemp("seeded")
    shutil.copytree(small_world["root"] / "raw", data / "raw")
    with tarfile.open(REFERENCE / "fixtures" / "eve_raw_2026-10-02.tar") as tar:
        tar.extractall(data / "raw", filter="data")
    cfg = make_settings(data, pg)
    pipe = Pipeline(cfg)
    with db.connect(cfg) as conn:
        reports = {"synthetic": pipe.run(conn, "synthetic")}
        pipe.stage_history()
        eve_days = [d for d in pipe.candidate_days("eve") if d <= date(2026, 1, 20)]
        reports["eve"] = pipe.run(conn, "eve", eve_days)
    return {"cfg": cfg, "pipe": pipe, "reports": reports, "data": data, **pg}
