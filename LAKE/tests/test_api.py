"""API contract: success, validation failure, malformed input and degraded states for every endpoint.

There is no authorization-failure case because there is no authorization: the API is read-only
over published results and has no notion of a user (see DECISIONS.md).
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from bakeoff.api import create_app
from tests.conftest import make_settings


@pytest.fixture(scope="module")
def client(pipeline) -> TestClient:
    return TestClient(create_app(pipeline.s), raise_server_exceptions=False)


@pytest.fixture
def empty(tmp_path: Path) -> TestClient:
    return TestClient(create_app(make_settings(tmp_path)))


def assert_error(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code and error["message"] and error["request_id"] == response.headers["x-request-id"]
    assert "Traceback" not in response.text
    return error


# ---------------------------------------------------------------- results

def test_results_roundtrip_and_revalidation(client: TestClient, pipeline) -> None:
    r = client.get("/api/results")
    assert r.status_code == 200 and r.json()["origin"] == "local"
    assert [v["id"] for v in r.json()["variants"]] == [v["id"] for v in pipeline.results["variants"]]
    again = client.get("/api/results", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304 and again.content == b""
    assert client.get("/api/results", headers={"If-None-Match": '"stale"'}).status_code == 200


def test_published_results_are_served_when_there_is_no_local_run(pipeline, tmp_path: Path) -> None:
    s = make_settings(tmp_path, LAKE_PUBLISHED_DIR=str(pipeline.s.published_dir))
    r = TestClient(create_app(s)).get("/api/results")
    assert r.status_code == 200 and r.json()["origin"] == "published"


def test_no_results_is_a_structured_404_not_a_crash(empty: TestClient) -> None:
    assert "bakeoff all" in assert_error(empty.get("/api/results"), 404, "no_results")["message"]
    assert_error(empty.get("/api/measurements"), 404, "no_results")
    assert_error(empty.post("/api/recommend", json={}), 404, "no_results")


def test_corrupt_results_are_a_503_with_the_fix(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    (s.data_dir / "results").mkdir(parents=True)
    for bad in ("{not json", json.dumps({"schema_version": 999})):
        (s.data_dir / "results" / "results.json").write_text(bad, encoding="utf-8")
        client = TestClient(create_app(s))
        assert "bakeoff report" in assert_error(client.get("/api/results"), 503, "results_unreadable")["message"]
        assert client.get("/healthz").status_code == 503


# ---------------------------------------------------------------- measurements

def test_measurements_paginate_completely_in_stable_order(client: TestClient, pipeline) -> None:
    total = duckdb.connect().execute("SELECT count(*) FROM read_parquet(?)",
                                     [str(pipeline.s.data_dir / "results" / "measurements.parquet")]).fetchone()[0]
    seen, cursor, pages = [], 0, 0
    while cursor is not None:
        page = client.get("/api/measurements", params={"cursor": cursor, "limit": 97}).json()
        seen += [m["seq"] for m in page["items"]]
        cursor, pages = page["next_cursor"], pages + 1
        assert pages < 100
    assert seen == sorted(set(seen)) and len(seen) == total and pages == -(-total // 97)


def test_measurements_filter(client: TestClient) -> None:
    page = client.get("/api/measurements", params={"variant": "orc-zstd", "query": "count", "limit": 500}).json()
    assert page["next_cursor"] is None and len(page["items"]) == 4
    assert {(m["variant"], m["query"]) for m in page["items"]} == {("orc-zstd", "count")}
    assert sorted(m["kind"] for m in page["items"]) == ["cold", "counted", "warm", "warm"]
    counted = next(m for m in page["items"] if m["kind"] == "counted")
    assert counted["bytes_read"] > 0 and counted["seconds"] is None
    assert client.get("/api/measurements", params={"variant": "'; DROP TABLE m; --"}).json()["items"] == []


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"cursor": -1}, {"limit": "many"},
                                    {"variant": "x" * 65}])
def test_measurements_reject_bad_parameters(client: TestClient, params: dict) -> None:
    error = assert_error(client.get("/api/measurements", params=params), 422, "validation_failed")
    assert error["details"][0]["field"] == f"query.{next(iter(params))}"


# ---------------------------------------------------------------- cost

def test_recommend_success(client: TestClient, pipeline) -> None:
    body = {"dataset_gb": 500, "queries_per_month": 20000, "mix": {"projection": 2, "filter_clustered": 1},
            "provider": "gcp", "engine": "serverless", "objective": "cost"}
    r = client.post("/api/recommend", json=body)
    assert r.status_code == 200
    out = r.json()
    assert out["pick"] == out["ranked"][0]["variant"] and out["pick"] in {v["id"] for v in pipeline.results["variants"]}
    assert [x["score"] for x in out["ranked"]] == sorted(x["score"] for x in out["ranked"])
    assert out["price"]["scan_per_tb"] == 5.68 and out["workload"]["mix"]["projection"] == pytest.approx(2 / 3)
    assert client.post("/api/recommend", json=body).json() == out  # pure: retrying is safe


def test_recommend_defaults(client: TestClient) -> None:
    out = client.post("/api/recommend", json={}).json()
    assert out["workload"]["provider"] == "aws" and len(out["workload"]["mix"]) == 6


@pytest.mark.parametrize("body, field", [
    ({"dataset_gb": -1}, "body.dataset_gb"), ({"dataset_gb": "big"}, "body.dataset_gb"),
    ({"provider": "oracle"}, "body.provider"), ({"queries_per_month": 1.5}, "body.queries_per_month"),
    ({"engine": "spark"}, "body.engine"), ({"mix": {"projection": "lots"}}, "body.mix.projection"),
])
def test_recommend_validation_failures(client: TestClient, body: dict, field: str) -> None:
    error = assert_error(client.post("/api/recommend", json=body), 422, "validation_failed")
    assert field in [d["field"] for d in error["details"]]


def test_recommend_rejects_an_unknown_query_class(client: TestClient) -> None:
    error = assert_error(client.post("/api/recommend", json={"mix": {"joins": 1}}), 422, "invalid_workload")
    assert "filter_clustered" in error["message"]  # says what IS valid


def test_recommend_malformed_and_oversized_bodies(client: TestClient) -> None:
    headers = {"Content-Type": "application/json"}
    assert_error(client.post("/api/recommend", content=b'{"dataset_gb": ', headers=headers), 422, "validation_failed")
    assert_error(client.post("/api/recommend", content=b"[1, 2]", headers=headers), 422, "validation_failed")
    assert_error(client.post("/api/recommend", content=b" " * 70_000, headers=headers), 413, "body_too_large")


def test_pricing_lists_sources(client: TestClient) -> None:
    p = client.get("/api/pricing").json()
    assert set(p["providers"]) == {"aws", "gcp", "azure"} and p["as_of"]
    assert all(v["sources"] and v["verified"] for v in p["providers"].values())


# ---------------------------------------------------------------- errors, ops

def test_unknown_routes_and_methods_are_structured(client: TestClient) -> None:
    assert_error(client.get("/api/nope"), 404, "not_found")
    assert_error(client.delete("/api/results"), 405, "method_not_allowed")


def test_health_exercises_dependencies(client: TestClient, empty: TestClient) -> None:
    ok = client.get("/healthz")
    assert ok.status_code == 200 and ok.json()["status"] == "ok"
    assert set(ok.json()["checks"]) == {"results", "measurements", "engine"}
    assert "executions readable" in ok.json()["checks"]["measurements"]["detail"]
    degraded = empty.get("/healthz")
    assert degraded.status_code == 503 and degraded.json()["status"] == "degraded"
    assert degraded.json()["checks"]["engine"]["ok"] and not degraded.json()["checks"]["results"]["ok"]


def test_metrics_and_request_ids(client: TestClient) -> None:
    first, second = client.get("/api/pricing"), client.get("/api/pricing")
    assert first.headers["x-request-id"] != second.headers["x-request-id"]
    assert first.headers["x-content-type-options"] == "nosniff"
    text = client.get("/metrics").text
    assert 'bakeoff_http_requests_total{route="/api/pricing",status="200"}' in text
    assert "bakeoff_results_age_seconds" in text


def test_committed_openapi_matches_the_code(pipeline) -> None:
    committed = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"
    assert json.loads(committed.read_text(encoding="utf-8")) == create_app(pipeline.s).openapi(), \
        "docs/openapi.json is stale: run `bakeoff docs`"
