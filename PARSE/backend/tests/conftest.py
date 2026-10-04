"""Test configuration.

Unit tests need nothing. Tests marked `db` run against a real PostgreSQL named by
TEST_DATABASE_URL (the compose `db` service locally, a service container in CI) and are skipped
when it is not set. Everything is seeded and deterministic: synthetic feed, hash embedder.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

TEST_URL = os.environ.get("TEST_DATABASE_URL")
_tmp = tempfile.mkdtemp(prefix="parse-test-")
os.environ.update(
    DATABASE_URL=TEST_URL or "postgresql+psycopg://unused:unused@127.0.0.1:1/unused",
    DATA_DIR=_tmp,
    EMBED_BACKEND="hash",
    SEED_SOURCE="synthetic",
    SEED_LABELS="250",
    ADMIN_TOKEN="test-admin-token",
    ANNOTATOR_TOKEN="test-annotator-token",
    VIEWER_TOKEN="test-viewer-token",
    LOG_LEVEL="WARNING",
)
os.environ.pop("MIGRATION_DATABASE_URL", None)
os.environ.pop("APP_DB_USER", None)

ADMIN = {"Authorization": "Bearer test-admin-token"}
ANNOTATOR = {"Authorization": "Bearer test-annotator-token"}
VIEWER = {"Authorization": "Bearer test-viewer-token"}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if TEST_URL:
        return
    skip = pytest.mark.skip(reason="TEST_DATABASE_URL not set")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


def reset_schema() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture(scope="module")
def engine() -> Iterator[object]:
    """A freshly migrated, empty database."""
    from parse_app.db import get_engine

    reset_schema()
    yield get_engine()


@pytest.fixture(scope="module")
def seeded(engine: object) -> dict[str, object]:
    """Fresh database + the full seed pipeline on synthetic data (taxonomy, feed with defects,
    embeddings, simulated labels, several model versions)."""
    from parse_app.config import get_settings
    from parse_app.pipeline import seed

    return seed(engine, get_settings())  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def client(seeded: dict[str, object]) -> Iterator[object]:
    from fastapi.testclient import TestClient

    from parse_app.api import app

    with TestClient(app) as c:
        yield c
