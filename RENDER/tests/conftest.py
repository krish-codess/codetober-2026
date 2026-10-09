"""One small synthetic dataset, pushed through the whole pipeline once per test session."""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dots import evaluate, fetch, ingest, project, tiles
from dots.api import create_app
from dots.config import Settings, settings

VARIANTS = ("pca", "umap-n10-d0.1")


def make_settings(root: Path, **over) -> Settings:
    base = dataclasses.replace(
        settings(),
        data_dir=root,
        site_dir=root / "site",
        source="synth",
        synth_rows=4000,
        seed=15,
        landmarks=1500,
        tile_capacity=256,
        variants=VARIANTS,
        eval_queries=400,
        cors_origins=(),
    )
    return dataclasses.replace(base, **over)


@pytest.fixture(scope="session")
def built(tmp_path_factory) -> tuple[Settings, str, dict]:
    """(settings, source, manifest) after fetch -> ingest -> project -> evaluate -> tile."""
    s = make_settings(tmp_path_factory.mktemp("dots"))
    source = fetch.run(s)
    ingest.run(s, source)
    project.run(s, source)
    evaluate.run(s, source)
    manifest = tiles.run(s, source)
    return s, source, manifest


@pytest.fixture()
def client(built) -> TestClient:
    return TestClient(create_app(built[0]), raise_server_exceptions=False)


@pytest.fixture()
def site_copy(built, tmp_path) -> Settings:
    """A private copy of the published site that a test may damage."""
    s = built[0]
    shutil.copytree(s.site_dir, tmp_path / "site")
    return dataclasses.replace(s, site_dir=tmp_path / "site")
