"""Shared fixtures. Everything is seeded: a failure reproduces exactly.

Integration tests run against a real PostgreSQL, never a mock: set UPLIFT_TEST_ADMIN_URL to a superuser URL and each
session gets its own database with the migrations applied and least-privilege login roles. Without it they skip.

`trained` runs the real pipeline once per session on a seeded synthetic experiment:
generate -> ingest (main file, then the late file) -> train every learner -> batch score.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest

from uplift import ingest, synth, train
from uplift.config import Settings

ROOT = Path(__file__).resolve().parent.parent
UNITS, SEED = 40_000, 23
READ_TOKEN, ADMIN_TOKEN = "read-token-for-tests", "admin-token-for-tests"  # noqa: S105


def alembic(url: str, *args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        check=True,
        cwd=ROOT,
        env=os.environ | {"UPLIFT_MIGRATE_DATABASE_URL": url},
    )


@contextmanager
def temporary_database(migrate: bool = True) -> Iterator[dict[str, str]]:
    admin_url = os.environ.get("UPLIFT_TEST_ADMIN_URL")
    if not admin_url:
        pytest.skip("set UPLIFT_TEST_ADMIN_URL to a PostgreSQL superuser URL to run integration tests")
    name = f"uplift_test_{uuid.uuid4().hex[:10]}"
    base = admin_url.rsplit("/", 1)[0]
    host = base.split("@", 1)[1]
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    owner_url = f"{base}/{name}"
    try:
        if migrate:
            alembic(owner_url, "upgrade", "head")
            with psycopg.connect(owner_url, autocommit=True) as conn:
                for login, group in (
                    ("uplift_api_login", "uplift_api_role"),
                    ("uplift_batch_login", "uplift_batch_role"),
                ):
                    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [login]).fetchone():
                        conn.execute(f"CREATE ROLE {login} LOGIN PASSWORD 'test' IN ROLE {group}")
        yield {
            "owner": owner_url,
            "api": f"postgresql://uplift_api_login:test@{host}/{name}",
            "batch": f"postgresql://uplift_batch_login:test@{host}/{name}",
        }
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def database() -> Iterator[dict[str, str]]:
    with temporary_database() as urls:
        yield urls


@pytest.fixture(scope="session")
def settings(database: dict[str, str], tmp_path_factory: pytest.TempPathFactory) -> Settings:
    return Settings(
        env="dev",
        data_dir=tmp_path_factory.mktemp("uplift-data"),
        api_database_url=database["api"],
        batch_database_url=database["batch"],
        migrate_database_url=database["owner"],
        read_token=READ_TOKEN,
        admin_token=ADMIN_TOKEN,
        hillstrom_url="http://127.0.0.1:9/unused",
        log_level="WARNING",
    )


@pytest.fixture(scope="session")
def trained(settings: Settings) -> dict[str, Any]:
    out = settings.data_dir / "generated"
    manifest = synth.generate(out, UNITS, SEED)
    main = ingest.ingest(settings, out / "synthetic.csv", "synthetic")
    late = ingest.ingest(settings, out / "synthetic_late.csv", "synthetic")
    summary = train.train(settings, "synthetic", seed=7, rounds=60)
    scored = train.score(settings, "synthetic")
    return {"manifest": manifest, "main": main, "late": late, "summary": summary, "scored": scored, "dir": out}


@pytest.fixture()
def owner(database: dict[str, str]) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(database["owner"], row_factory=psycopg.rows.dict_row, autocommit=True) as conn:
        yield conn
