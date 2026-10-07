"""Shared fixtures. Everything is seeded: a failure reproduces exactly.

`pipeline` runs the real batch once per session (generate -> ingest -> dbt build -> build) on a small
seeded year. `database` gives each test module a fresh PostgreSQL database with the migrations applied
and least-privilege login roles; it needs WRAPPED_TEST_ADMIN_URL and the tests that use it are skipped
without it.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from wrapped import config
from wrapped.build import build
from wrapped.cli import transform
from wrapped.config import Settings
from wrapped.generate import generate
from wrapped.ingest import ingest

ROOT = Path(__file__).resolve().parent.parent
SEED, USERS = 11, 500


@pytest.fixture(scope="session")
def pipeline(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    data_dir = tmp_path_factory.mktemp("wrapped-data")
    settings = dataclasses.replace(config.load(), data_dir=data_dir, year=2025, k_anonymity=5, duckdb_memory="1GB")
    generate(settings.raw_dir, USERS, settings.year, SEED, data_dir / "tmp")
    ingest(settings)
    transform(settings)
    build(settings)
    return settings


@pytest.fixture(scope="module")
def database() -> Iterator[dict[str, str]]:
    admin_url = os.environ.get("WRAPPED_TEST_ADMIN_URL")
    if not admin_url:
        pytest.skip("set WRAPPED_TEST_ADMIN_URL to a PostgreSQL superuser URL to run integration tests")
    name = f"wrapped_test_{uuid.uuid4().hex[:10]}"
    base = admin_url.rsplit("/", 1)[0]
    host = base.split("@", 1)[1]
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    owner_url = f"{base}/{name}"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        cwd=ROOT,
        env=os.environ | {"WRAPPED_MIGRATE_DATABASE_URL": owner_url},
    )
    with psycopg.connect(owner_url, autocommit=True) as conn:
        for login, group in (("wrapped_api_login", "wrapped_api_role"), ("wrapped_batch_login", "wrapped_batch_role")):
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [login]).fetchone()
            if not exists:
                conn.execute(f"CREATE ROLE {login} LOGIN PASSWORD 'test' IN ROLE {group}")
    try:
        yield {
            "owner": owner_url,
            "api": f"postgresql://wrapped_api_login:test@{host}/{name}",
            "batch": f"postgresql://wrapped_batch_login:test@{host}/{name}",
        }
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
