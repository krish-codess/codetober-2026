"""Every endpoint: success, validation failure, authorization failure, malformed input.

The database is real. Only `run_suite` is replaced, by a function returning the real
report the session fixture produced, so a request does not re-run the whole suite."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tydlc import api, store

pytestmark = pytest.mark.integration
KEY = {"X-API-Key": "test-key"}


@pytest.fixture
def client(db: Any, pg_dsn: str, report: dict[str, Any],
           monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    monkeypatch.setenv("TYDLC_API_KEY", "test-key")
    monkeypatch.setattr(api, "run_suite", lambda *a, **kw: report)
    with TestClient(api.create_app(), raise_server_exceptions=False) as c:
        yield c


def _start(client: TestClient, key: str = "idem-key-1", **body: Any) -> Any:
    return client.post("/api/runs", json=body, headers={**KEY, "Idempotency-Key": key})


def _assert_error(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code and error["message"] and error["correlation_id"]
    assert "Traceback" not in response.text
    return error  # type: ignore[no-any-return]


def test_health_exercises_dependencies(client: TestClient) -> None:
    body = client.get("/healthz").json()
    assert body == {"status": "ok", "checks": {"postgres": "ok", "duckdb": "ok"}}


def test_start_run_then_read_everything(client: TestClient, report: dict[str, Any]) -> None:
    started = _start(client, seed=28, max_examples=40)
    assert started.status_code == 202
    run_id = started.json()["id"]

    run = client.get(f"/api/runs/{run_id}").json()  # the background task has finished
    assert run["status"] == "completed" and run["gate_ok"] is True and run["examples"] == 40

    runs = client.get("/api/runs").json()
    assert [r["id"] for r in runs["items"]] == [run_id] and runs["next_cursor"] is None

    catalog = client.get(f"/api/runs/{run_id}/invariants", params={"limit": 200}).json()
    assert len(catalog["items"]) == len(report["properties"])
    bug = next(i for i in catalog["items"] if i["name"] == "orders_amount_not_null")
    assert bug["status"] == "falsified" and bug["confidence"] == 0 and bug["known_bug"]
    assert (bug["history_runs"], bug["history_falsified"]) == (1, 1)
    held = next(i for i in catalog["items"] if i["name"] == "row_count_eq(orders,raw_orders)")
    assert held["status"] == "held" and 0 < held["confidence"] < 1 and held["failure_id"] is None

    failures = client.get("/api/failures", params={"run_id": run_id, "limit": 200}).json()
    assert {f["id"] for f in failures["items"]} >= {bug["failure_id"]}
    assert "minimal_dataset" not in failures["items"][0]  # the list stays light
    one = client.get("/api/failures", params={"property": "orders_amount_not_null"}).json()
    assert [f["id"] for f in one["items"]] == [bug["failure_id"]]

    detail = client.get(f"/api/failures/{bug['failure_id']}").json()
    assert detail["minimal_dataset"] == {
        "raw_customers": [{"id": 0, "first_name": None, "last_name": None}],
        "raw_orders": [{"id": 0, "user_id": 0, "order_date": None, "status": "placed"}],
        "raw_payments": []}
    assert detail["shrunk"] is True and detail["minimal_rows"] == 2

    freq = client.get("/api/analytics/failure-frequency").json()
    assert any(f["name"] == "orders_amount_not_null" and f["falsified_runs"] == 1 for f in freq)
    rates = client.get("/api/analytics/discovery-hit-rate").json()
    assert rates[0]["run_id"] == run_id and 0 < rates[0]["hit_rate"] < 1
    on_postgres = client.get("/api/analytics/failure-frequency", params={"engine": "postgres"})
    assert on_postgres.json() == []


def test_retrying_a_post_does_not_create_a_second_run(client: TestClient) -> None:
    first = _start(client, key="retry-me-please")
    again = _start(client, key="retry-me-please")
    assert (first.status_code, again.status_code) == (202, 200)
    assert again.json()["id"] == first.json()["id"]
    assert len(client.get("/api/runs").json()["items"]) == 1


def test_pagination_over_http(client: TestClient) -> None:
    ids = [_start(client, key=f"paging-key-{i}").json()["id"] for i in range(5)]
    page = client.get("/api/runs", params={"limit": 2}).json()
    seen = [r["id"] for r in page["items"]]
    while page["next_cursor"]:
        page = client.get("/api/runs", params={"limit": 2, "cursor": page["next_cursor"]}).json()
        seen += [r["id"] for r in page["items"]]
    assert seen == sorted(ids, reverse=True)


def test_a_crashing_run_is_recorded_as_failed(client: TestClient,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*a: Any, **kw: Any) -> None:
        raise RuntimeError("engine went away")

    monkeypatch.setattr(api, "run_suite", explode)
    run_id = _start(client, key="will-crash-1").json()["id"]
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "failed" and "engine went away" in run["error"]


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": ""}])
def test_starting_a_run_requires_the_key(client: TestClient, headers: dict[str, str]) -> None:
    response = client.post("/api/runs", json={}, headers={**headers, "Idempotency-Key": "k" * 8})
    _assert_error(response, 401, "unauthorized")
    assert client.get("/api/runs").json()["items"] == []


def test_no_configured_key_means_nobody_can_start_runs(client: TestClient,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYDLC_API_KEY", "")
    _assert_error(_start(client), 401, "unauthorized")


@pytest.mark.parametrize("body", [
    {"engine": "sqlite"}, {"max_examples": 0}, {"max_examples": 10**6}, {"seed": -1},
    {"seed": "abc"}])
def test_start_run_validation(client: TestClient, body: dict[str, Any]) -> None:
    error = _assert_error(_start(client, **body), 422, "validation_error")
    assert error["details"][0]["field"].startswith("body.")


def test_start_run_malformed(client: TestClient) -> None:
    headers = {**KEY, "Idempotency-Key": "malformed-1", "Content-Type": "application/json"}
    _assert_error(client.post("/api/runs", content=b"{not json", headers=headers),
                  422, "validation_error")
    _assert_error(client.post("/api/runs", json={}, headers=KEY), 422, "validation_error")
    _assert_error(client.post("/api/runs", json={}, headers={**KEY, "Idempotency-Key": "x"}),
                  422, "validation_error")  # too short to be a real key


@pytest.mark.parametrize("path", [
    "/api/runs?limit=0", "/api/runs?limit=9999", "/api/runs?engine=oracle", "/api/runs/abc",
    "/api/runs/abc/invariants", "/api/failures?run_id=x", "/api/failures/1.5",
    "/api/analytics/failure-frequency?runs=0", "/api/analytics/discovery-hit-rate?engine=x"])
def test_read_validation(client: TestClient, path: str) -> None:
    _assert_error(client.get(path), 422, "validation_error")


@pytest.mark.parametrize("path", [
    "/api/runs?cursor=%00%ff", "/api/runs?cursor=bm9wZQ==", "/api/failures?cursor=ImEi"])
def test_bad_cursor(client: TestClient, path: str) -> None:
    _assert_error(client.get(path), 400, "bad_cursor")


@pytest.mark.parametrize("path", [
    "/api/runs/424242", "/api/runs/424242/invariants", "/api/failures/424242", "/api/nope"])
def test_not_found(client: TestClient, path: str) -> None:
    _assert_error(client.get(path), 404, "not_found")


def test_database_outage_degrades_readably(client: TestClient,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """Failure injection: the database disappears while the API keeps serving."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    health = client.get("/healthz")
    assert health.status_code == 503
    body = health.json()
    assert body["status"] == "degraded" and body["checks"]["duckdb"] == "ok"
    assert body["checks"]["postgres"].startswith("failed: ")
    for path in ("/api/runs", "/api/failures", "/api/analytics/failure-frequency"):
        _assert_error(client.get(path), 503, "database_unavailable")
    assert client.get("/").status_code == 200  # the viewer itself still loads


def test_correlation_id_and_metrics(client: TestClient) -> None:
    response = client.get("/api/runs", headers={"X-Request-ID": "trace-123"})
    assert response.headers["x-request-id"] == "trace-123"
    assert client.get("/api/runs").headers["x-request-id"] != "trace-123"
    error = client.get("/api/runs/1", headers={"X-Request-ID": "trace-404"}).json()["error"]
    assert error["correlation_id"] == "trace-404"
    metrics = client.get("/metrics").text
    assert 'tydlc_http_requests_total{method="GET",route="/api/runs",status="200"}' in metrics
    assert 'route="/api/runs/{run_id}",status="404"' in metrics  # templated, not per-id


def test_openapi_documents_the_contract(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    assert {"/api/runs", "/api/runs/{run_id}", "/api/runs/{run_id}/invariants", "/api/failures",
            "/api/failures/{failure_id}", "/api/analytics/failure-frequency",
            "/api/analytics/discovery-hit-rate", "/healthz"} <= set(spec["paths"])
    assert {"401", "422", "202", "200"} <= set(spec["paths"]["/api/runs"]["post"]["responses"])
    assert store.Row  # keep the import honest
