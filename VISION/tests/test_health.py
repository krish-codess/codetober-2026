import dataclasses

from fastapi.testclient import TestClient

from squeeze.api import create_app


def test_health_ok_and_correlated(cfg):
    r = TestClient(create_app(cfg)).get("/healthz", headers={"x-request-id": "abc"})
    assert r.status_code == 200 and r.json()["checks"] == {"database": "ok", "packages": "ok"}
    assert r.headers["x-request-id"] == "abc"


def test_health_reports_the_broken_dependency(cfg, tmp_path):
    broken = dataclasses.replace(cfg, packages_dir=tmp_path / "missing")
    r = TestClient(create_app(broken)).get("/healthz")
    assert r.status_code == 503 and r.json()["checks"]["packages"].startswith("failed")
