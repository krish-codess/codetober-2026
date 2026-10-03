"""Shared fixtures. Everything that touches PostgreSQL uses TEST_DATABASE_URL, a
database the tests own and wipe; without it those tests skip with that reason."""

from __future__ import annotations

import copy
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tydlc.engine import Engine, make_engine
from tydlc.runner import run_suite
from tydlc.subjects import Subject, get_subject

SEED = 28


@pytest.fixture(scope="session")
def subject() -> Subject:
    return get_subject("jaffle")


@pytest.fixture(scope="session")
def duck(subject: Subject) -> Iterator[Engine]:
    engine = make_engine("duckdb", subject)
    yield engine
    engine.close()


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("data")


@pytest.fixture(scope="session")
def session_report(subject: Subject, data_dir: Path) -> dict[str, Any]:
    """One real, complete run (discovery, sweep, shrink) shared by the slower tests."""
    return run_suite(subject, "duckdb", seed=SEED, max_examples=40, data_dir=data_dir,
                     run_key="session")


@pytest.fixture
def report(session_report: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(session_report)


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is not set (see README: running the tests)")
    return dsn


@pytest.fixture
def db(pg_dsn: str) -> Iterator[Any]:
    """A connection to a freshly migrated, empty schema."""
    from tydlc import store

    conn = store.connect(pg_dsn, attempts=1)
    store.migrate(conn, 0)
    store.migrate(conn)
    yield conn
    conn.close()
