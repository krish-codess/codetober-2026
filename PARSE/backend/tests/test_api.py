"""API contract tests: for every endpoint, success, validation failure, authorization failure and
malformed input. Runs the real app against a real database."""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import ADMIN, ANNOTATOR, VIEWER
from fastapi.testclient import TestClient

pytestmark = pytest.mark.db
V1 = "/api/v1"

# (method, path, minimum role, body)
PROTECTED = [
    ("GET", "/me", "viewer", None),
    ("GET", "/stats", "viewer", None),
    ("GET", "/taxonomy", "viewer", None),
    ("GET", "/taxonomy/changes", "viewer", None),
    ("POST", "/taxonomy/changes", "admin", {"op": "retire", "path": "x"}),
    ("GET", "/queue", "annotator", None),
    ("PUT", "/items/1/annotation", "annotator", {"node_ids": []}),
    ("POST", "/classify", "viewer", {"texts": ["x"]}),
    ("GET", "/models", "viewer", None),
    ("GET", "/metrics/nodes", "viewer", None),
    ("GET", "/metrics/efficiency", "viewer", None),
    ("POST", "/jobs", "admin", {"kind": "retrain"}),
    ("GET", "/jobs", "viewer", None),
    ("GET", "/jobs/1", "viewer", None),
    ("POST", "/feedback", "admin", None),
    ("GET", "/quarantine", "admin", None),
]
TOKENS = {"viewer": VIEWER, "annotator": ANNOTATOR, "admin": ADMIN}
RANK = {"viewer": 0, "annotator": 1, "admin": 2}


def assert_error(r: Any, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, r.text
    err = r.json()["error"]
    assert err["code"] == code and err["message"] and err["request_id"] == r.headers["x-request-id"]
    assert "Traceback" not in r.text
    return err


# --- authentication / authorization: every protected path ------------------------------------------


@pytest.mark.parametrize(("method", "path", "role", "body"), PROTECTED)
def test_auth_is_enforced_on_every_endpoint(client: TestClient, method: str, path: str, role: str, body: Any) -> None:
    r = client.request(method, V1 + path, json=body)
    assert_error(r, 401, "unauthenticated")
    assert r.headers["www-authenticate"] == "Bearer"
    assert_error(
        client.request(method, V1 + path, json=body, headers={"Authorization": "Bearer wrong"}), 401, "unauthenticated"
    )
    assert_error(
        client.request(method, V1 + path, json=body, headers={"Authorization": "Basic abc"}), 401, "unauthenticated"
    )
    for lower in (r_ for r_ in RANK if RANK[r_] < RANK[role]):
        err = assert_error(client.request(method, V1 + path, json=body, headers=TOKENS[lower]), 403, "forbidden")
        assert role in err["message"]


def test_revoked_token_stops_working(client: TestClient, engine: Any) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("UPDATE api_tokens SET revoked_at = now() WHERE name = 'seed-viewer'"))
    try:
        assert_error(client.get(V1 + "/me", headers=VIEWER), 401, "unauthenticated")
    finally:
        with engine.begin() as conn:
            conn.execute(text("UPDATE api_tokens SET revoked_at = NULL WHERE name = 'seed-viewer'"))
    assert client.get(V1 + "/me", headers=VIEWER).json() == {"name": "seed-viewer", "role": "viewer"}


# --- ops ----------------------------------------------------------------------------------------------


def test_health_exercises_dependencies(client: TestClient) -> None:
    r = client.get(V1 + "/health")
    body = r.json()
    assert r.status_code == 200 and body["status"] == "ok"
    assert body["checks"]["database"]["schema"] == "0001" and body["checks"]["model"]["model_version"] >= 1
    assert body["checks"]["embedder"]["ok"]


def test_health_reports_database_outage(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import create_engine

    import parse_app.api as api

    dead = create_engine("postgresql+psycopg://x:x@127.0.0.1:1/x", connect_args={"connect_timeout": 1})
    monkeypatch.setattr(api, "get_engine", lambda: dead)
    r = client.get(V1 + "/health")
    assert r.status_code == 503 and r.json()["status"] == "down" and not r.json()["checks"]["database"]["ok"]
    # ...and ordinary endpoints degrade to a structured, retryable 503 rather than a stack trace
    r = client.get(V1 + "/stats", headers=VIEWER)
    assert_error(r, 503, "database_unavailable")
    assert r.headers["retry-after"] == "5"


def test_request_id_is_echoed_or_generated(client: TestClient) -> None:
    assert client.get(V1 + "/health", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"] == "abc-123"
    generated = client.get(V1 + "/health", headers={"X-Request-ID": "bad id with spaces"}).headers["x-request-id"]
    assert len(generated) == 32
    assert_error(client.get(V1 + "/nope"), 404, "not_found")
    assert "http_requests_total" in client.get("/metrics").text


def test_stats(client: TestClient) -> None:
    s = client.get(V1 + "/stats", headers=VIEWER).json()
    assert s["pool"] > 1000 and s["test"] == 1200 and s["labelled"] >= 250 and s["active_model"]
    assert sum(v["pool"] for v in s["by_lang"].values()) == s["pool"] and "duplicate_delivery" in s["quarantined"]


# --- queue + annotation: the primary journey -------------------------------------------------------------


def test_queue_pagination_is_stable_and_complete(client: TestClient) -> None:
    first = client.get(V1 + "/queue?limit=7", headers=ANNOTATOR).json()
    assert len(first["items"]) == 7 and first["next_cursor"] and first["remaining"] > 100
    second = client.get(V1 + f"/queue?limit=7&cursor={first['next_cursor']}", headers=ANNOTATOR).json()
    both = client.get(V1 + "/queue?limit=14", headers=ANNOTATOR).json()
    assert [i["id"] for i in first["items"] + second["items"]] == [i["id"] for i in both["items"]]
    item = first["items"][0]
    assert item["suggestions"] and item["scored_by_model"] == first["active_model"] and 0 <= item["confidence"] <= 1
    probs = [s["prob"] for s in item["suggestions"]]
    assert probs == sorted(probs, reverse=True)
    sw = client.get(V1 + "/queue?lang=sw&limit=5", headers=ANNOTATOR).json()
    assert sw["items"] and all(i["lang"] == "sw" for i in sw["items"]) and sw["remaining"] < first["remaining"]


@pytest.mark.parametrize(
    "query", ["limit=0", "limit=101", "limit=abc", "mode=fastest", "lang=EN", "cursor=!!!", "cursor=e30="]
)
def test_queue_rejects_bad_parameters(client: TestClient, query: str) -> None:
    assert_error(client.get(V1 + f"/queue?{query}", headers=ANNOTATOR), 422, "validation_error")


def test_annotation_put_closes_ancestors_is_idempotent_and_leaves_the_queue(client: TestClient) -> None:
    nodes = {n["path"]: n for n in client.get(V1 + "/taxonomy", headers=VIEWER).json()["nodes"]}
    leaf = nodes["hotel/rooms/cleanliness"]
    item = client.get(V1 + "/queue?limit=1", headers=ANNOTATOR).json()["items"][0]
    url = V1 + f"/items/{item['id']}/annotation"
    r = client.put(url, json={"node_ids": [leaf["id"]]}, headers=ANNOTATOR)
    expected = sorted([leaf["id"], nodes["hotel/rooms"]["id"], nodes["hotel"]["id"]])
    assert r.status_code == 200 and r.json() == {"feedback_id": item["id"], "node_ids": expected}
    assert client.put(url, json={"node_ids": [leaf["id"]]}, headers=ANNOTATOR).json() == r.json()  # retry-safe
    assert item["id"] not in [i["id"] for i in client.get(V1 + "/queue?limit=50", headers=ANNOTATOR).json()["items"]]
    after = {n["path"]: n for n in client.get(V1 + "/taxonomy", headers=VIEWER).json()["nodes"]}
    assert after["hotel/rooms/cleanliness"]["n_labels"] == leaf["n_labels"] + 1


def test_annotation_validation_and_conflicts(client: TestClient, engine: Any) -> None:
    from sqlalchemy import text

    item = client.get(V1 + "/queue?limit=1", headers=ANNOTATOR).json()["items"][0]["id"]
    assert_error(
        client.put(V1 + f"/items/{item}/annotation", json={"node_ids": [999999]}, headers=ANNOTATOR),
        422,
        "validation_error",
    )
    assert_error(
        client.put(V1 + f"/items/{item}/annotation", json={"node_ids": "1"}, headers=ANNOTATOR), 422, "validation_error"
    )
    assert_error(client.put(V1 + f"/items/{item}/annotation", json={}, headers=ANNOTATOR), 422, "validation_error")
    assert_error(
        client.put(
            V1 + f"/items/{item}/annotation",
            content=b"{not json",
            headers={**ANNOTATOR, "Content-Type": "application/json"},
        ),
        422,
        "validation_error",
    )
    assert_error(
        client.put(V1 + "/items/abc/annotation", json={"node_ids": []}, headers=ANNOTATOR), 422, "validation_error"
    )
    assert_error(
        client.put(V1 + "/items/99999999/annotation", json={"node_ids": []}, headers=ANNOTATOR), 404, "not_found"
    )
    with engine.connect() as conn:
        test_item = conn.execute(text("SELECT id FROM feedback WHERE split = 'test' LIMIT 1")).scalar_one()
    assert_error(
        client.put(V1 + f"/items/{test_item}/annotation", json={"node_ids": []}, headers=ANNOTATOR), 409, "conflict"
    )


# --- classification ---------------------------------------------------------------------------------------


def test_classify_returns_consistent_calibrated_predictions(client: TestClient) -> None:
    nodes = {n["id"]: n for n in client.get(V1 + "/taxonomy", headers=VIEWER).json()["nodes"]}
    r = client.post(V1 + "/classify", headers=VIEWER, json={
        "texts": ["the room was dirty (ref 000001)", "battery dies after two hours", "zzz qqq"], "auto_threshold": 0.8})  # fmt: skip
    body = r.json()
    assert r.status_code == 200 and len(body["results"]) == 3 and body["model_version"] >= 1
    for res in body["results"]:
        ids = {lab["node_id"] for lab in res["labels"]}
        assert all(nodes[i]["parent_id"] is None or nodes[i]["parent_id"] in ids for i in ids)  # child implies parent
        assert 0 <= res["confidence"] <= 1 and res["route"] == ("auto" if res["confidence"] >= 0.8 else "review")
    assert "hotel" in {lab["path"] for lab in body["results"][0]["labels"]}


@pytest.mark.parametrize(
    "body",
    [{}, {"texts": []}, {"texts": [""]}, {"texts": ["x" * 4001]}, {"texts": ["a"] * 65}, {"texts": "a"},
     {"texts": ["a"], "auto_threshold": 1.5}, {"texts": [1]}],
)  # fmt: skip
def test_classify_validates_input(client: TestClient, body: dict[str, Any]) -> None:
    err = assert_error(client.post(V1 + "/classify", headers=VIEWER, json=body), 422, "validation_error")
    assert err["details"][0]["field"].startswith("body")


def test_classify_degrades_clearly_without_an_embedder(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import parse_app.api as api

    monkeypatch.setattr(api.state, "embedder", None)
    monkeypatch.setattr(api.state, "embedder_error", "FetchError: could not download model")
    err = assert_error(
        client.post(V1 + "/classify", headers=VIEWER, json={"texts": ["x"]}), 503, "embedder_unavailable"
    )
    assert "could not download" in err["message"]
    health = client.get(V1 + "/health").json()
    assert health["status"] == "degraded" and not health["checks"]["embedder"]["ok"]
    assert client.get(V1 + "/queue?limit=1", headers=ANNOTATOR).status_code == 200  # labelling keeps working


# --- models & metrics --------------------------------------------------------------------------------------


def test_models_list_is_paginated_newest_first(client: TestClient) -> None:
    page = client.get(V1 + "/models?limit=2", headers=VIEWER).json()
    assert len(page["items"]) == 2 and page["items"][0]["id"] > page["items"][1]["id"] and page["next_cursor"]
    nxt = client.get(V1 + f"/models?limit=2&cursor={page['next_cursor']}", headers=VIEWER).json()
    assert nxt["items"][0]["id"] < page["items"][1]["id"]
    m = page["items"][0]
    assert {"train_data_sha256", "code_version", "params", "artifact_sha256", "gate"} <= set(m) and "artifact" not in m
    assert_error(client.get(V1 + "/models?cursor=zzz", headers=VIEWER), 422, "validation_error")


def test_node_metrics_by_node_and_language(client: TestClient) -> None:
    body = client.get(V1 + "/metrics/nodes", headers=VIEWER).json()
    assert body["lang"] == "all" and set(body["languages"]) == {"de", "en", "es", "sw"}
    by_path = {n["path"]: n for n in body["nodes"]}
    hotel = by_path["hotel"]
    assert (
        hotel["support"] > 100 and 0 <= hotel["precision_ci"][0] <= hotel["precision"] <= hotel["precision_ci"][1] <= 1
    )
    assert 0 < body["overall"]["hf1"] <= 1 and body["overall"]["consistent"] == 1.0
    assert (
        body["calibration"]["routing"]
        and body["calibration"]["ece_item"] < body["calibration"]["ece_item_uncalibrated"] + 0.05
    )
    sw = client.get(V1 + "/metrics/nodes?lang=sw", headers=VIEWER).json()
    assert sw["lang"] == "sw" and {n["path"]: n for n in sw["nodes"]}["hotel"]["support"] < hotel["support"]
    assert_error(client.get(V1 + "/metrics/nodes?lang=xx", headers=VIEWER), 404, "not_found")
    assert_error(client.get(V1 + "/metrics/nodes?lang=ENGLISH", headers=VIEWER), 422, "validation_error")
    assert_error(client.get(V1 + "/metrics/nodes?model_version=99999", headers=VIEWER), 404, "not_found")


def test_efficiency_curve_has_one_point_per_model(client: TestClient) -> None:
    body = client.get(V1 + "/metrics/efficiency", headers=VIEWER).json()
    ns = [p["n_labeled"] for p in body["live"]]
    assert len(ns) >= 4 and ns == sorted(ns) and all(0 <= p["hf1"] <= 1 for p in body["live"])


# --- jobs ---------------------------------------------------------------------------------------------------


def test_job_creation_is_idempotent_and_observable(client: TestClient) -> None:
    headers = {**ADMIN, "Idempotency-Key": "retrain-key-0001"}
    r = client.post(V1 + "/jobs", json={"kind": "retrain"}, headers=headers)
    assert r.status_code == 202 and r.json()["status"] == "queued" and r.json()["progress"] == 0
    assert client.post(V1 + "/jobs", json={"kind": "retrain"}, headers=headers).json()["id"] == r.json()["id"]
    job = client.get(V1 + f"/jobs/{r.json()['id']}", headers=VIEWER).json()
    assert job["requested_by"] == "seed-admin" and job["kind"] == "retrain"
    assert client.get(V1 + "/jobs?limit=1", headers=VIEWER).json()["items"][0]["id"] == r.json()["id"]
    assert_error(client.post(V1 + "/jobs", json={"kind": "retrain"}, headers=ADMIN), 422, "validation_error")  # no key
    assert_error(client.post(V1 + "/jobs", json={"kind": "explode"}, headers=headers), 422, "validation_error")
    assert_error(
        client.post(V1 + "/jobs", json={"kind": "retrain"}, headers={**ADMIN, "Idempotency-Key": "short"}),
        422,
        "validation_error",
    )
    assert_error(client.get(V1 + "/jobs/424242", headers=VIEWER), 404, "not_found")


# --- ingestion ----------------------------------------------------------------------------------------------


def test_feedback_ingest_quarantines_bad_lines_and_is_idempotent(client: TestClient) -> None:
    lines = [
        json.dumps({"id": "api-1", "text": "the bed was uncomfortable and the room smelled", "lang": "en"}),
        json.dumps({"id": "api-2", "text": "   "}),
        '{"id": "api-3", "text": "trunc',
        json.dumps({"id": "api-1", "text": "the bed was uncomfortable and the room smelled", "lang": "en"}),
    ]
    payload = "\n".join(lines).encode()
    headers = {**ADMIN, "Content-Type": "application/x-ndjson"}
    r = client.post(V1 + "/feedback", content=payload, headers=headers)
    body = r.json()
    assert r.status_code == 200, r.text
    assert (body["n_records"], body["n_accepted"], body["n_quarantined"]) == (4, 1, 3) and not body["already_ingested"]
    assert body["quarantined_by_reason"] == {"empty_text": 1, "malformed_json": 1, "duplicate_delivery": 1}
    assert body["job"]["kind"] == "embed_score" and body["job"]["status"] == "queued"
    again = client.post(V1 + "/feedback", content=payload, headers=headers).json()
    assert (
        again["already_ingested"] and again["batch_id"] == body["batch_id"] and again["job"]["id"] == body["job"]["id"]
    )

    q = client.get(V1 + "/quarantine?reason=malformed_json&limit=50", headers=ADMIN).json()
    assert any(
        i["batch_id"] == body["batch_id"] and i["payload_preview"].startswith('{"id": "api-3"') for i in q["items"]
    )


def test_feedback_ingest_rejects_empty_and_oversized_bodies(client: TestClient) -> None:
    headers = {**ADMIN, "Content-Type": "application/x-ndjson"}
    assert_error(client.post(V1 + "/feedback", content=b"  \n", headers=headers), 422, "validation_error")
    assert_error(client.post(V1 + "/feedback", content=b"x" * 2_000_001, headers=headers), 413, "payload_too_large")


def test_quarantine_pagination(client: TestClient) -> None:
    seen: list[tuple[int, int]] = []
    cursor = ""
    for _ in range(50):
        page = client.get(V1 + f"/quarantine?limit=10{cursor}", headers=ADMIN).json()
        seen += [(i["batch_id"], i["line_no"]) for i in page["items"]]
        if not page["next_cursor"]:
            break
        cursor = f"&cursor={page['next_cursor']}"
    assert len(seen) == len(set(seen)) >= 40 and seen == sorted(seen)
    assert_error(client.get(V1 + "/quarantine?reason=DROP TABLE", headers=ADMIN), 422, "validation_error")


# --- taxonomy changes over HTTP ---------------------------------------------------------------------------------


def test_taxonomy_change_end_to_end(client: TestClient) -> None:
    before = client.get(V1 + "/taxonomy", headers=VIEWER).json()
    headers = {**ADMIN, "Idempotency-Key": "split-rooms-comfort-1"}
    body = {"op": "split", "path": "hotel/rooms/comfort",
            "children": [{"name": "bed", "title": "Bed"}, {"name": "noise", "title": "Noise"}]}  # fmt: skip
    r = client.post(V1 + "/taxonomy/changes", json=body, headers=headers)
    out = r.json()
    assert (
        r.status_code == 201
        and out["version"] == before["version"] + 1
        and out["labels_flagged"] > 0
        and not out["replayed"]
    )
    replay = client.post(V1 + "/taxonomy/changes", json=body, headers=headers)
    assert replay.status_code == 200 and replay.json()["replayed"] and replay.json()["version"] == out["version"]

    after = client.get(V1 + "/taxonomy", headers=VIEWER).json()
    paths = {n["path"]: n for n in after["nodes"]}
    assert after["version"] == out["version"] and {"hotel/rooms/comfort/bed", "hotel/rooms/comfort/noise"} <= set(paths)
    assert paths["hotel/rooms/comfort"]["n_review"] > 0  # pool items now waiting for a targeted look

    review = client.get(V1 + "/queue?mode=review&limit=5", headers=ANNOTATOR).json()
    assert review["remaining"] == paths["hotel/rooms/comfort"]["n_review"] and review["items"]
    item = review["items"][0]
    assert (
        item["review_reason"] == "split:hotel/rooms/comfort"
        and paths["hotel/rooms/comfort"]["id"] in item["current_node_ids"]
    )
    client.put(
        V1 + f"/items/{item['id']}/annotation",
        json={"node_ids": [paths["hotel/rooms/comfort/bed"]["id"]]},
        headers=ANNOTATOR,
    )
    assert (
        client.get(V1 + "/queue?mode=review&limit=5", headers=ANNOTATOR).json()["remaining"] == review["remaining"] - 1
    )

    # the reference set needs the same targeted refresh: admin may, annotator may not
    flagged_test = next(
        i
        for i in client.get(V1 + "/queue?mode=review&limit=100", headers=ANNOTATOR).json()["items"]
        + _all_review(client)
        if i["split"] == "test"
    )
    url = V1 + f"/items/{flagged_test['id']}/annotation"
    payload = {"node_ids": [paths["hotel/rooms/comfort/noise"]["id"]]}
    assert_error(client.put(url, json=payload, headers=ANNOTATOR), 409, "conflict")
    assert client.put(url, json=payload, headers=ADMIN).status_code == 200
    assert_error(client.put(url, json=payload, headers=ADMIN), 409, "conflict")  # no longer flagged: protected again

    changes = client.get(V1 + "/taxonomy/changes?limit=1", headers=VIEWER).json()
    assert (
        changes["items"][0]["op"] == "split" and changes["items"][0]["actor"] == "seed-admin" and changes["next_cursor"]
    )
    jobs = client.get(V1 + "/jobs?limit=20", headers=VIEWER).json()["items"]
    assert any(
        j["kind"] == "retrain" and j["requested_by"] == "seed-admin" for j in jobs
    )  # retrain queued automatically


def _all_review(client: TestClient) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(100):
        page = client.get(V1 + f"/queue?mode=review&limit=100{cursor}", headers=ANNOTATOR).json()
        items += page["items"]
        if not page["next_cursor"]:
            break
        cursor = f"&cursor={page['next_cursor']}"
    return items


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"op": "explode", "path": "hotel"}, 422, "validation_error"),
        ({"op": "split", "path": "hotel", "children": [{"name": "only_one", "title": "x"}]}, 422, "validation_error"),
        ({"op": "add", "parent": "hotel", "name": "Bad Name!", "title": "x"}, 422, "validation_error"),
        ({"op": "add", "parent": "hotel", "name": "rooms", "title": "Rooms"}, 409, "conflict"),
        ({"op": "retire", "path": "does/not/exist"}, 404, "not_found"),
        ({"op": "move", "path": "hotel", "new_parent": "hotel/rooms"}, 422, "validation_error"),
        ({"op": "merge", "source": "hotel", "target": "hotel"}, 422, "validation_error"),
    ],
)
def test_taxonomy_change_errors_leave_no_trace(
    client: TestClient, body: dict[str, Any], status: int, code: str
) -> None:
    version = client.get(V1 + "/taxonomy", headers=VIEWER).json()["version"]
    key = f"bad-change-{abs(hash(json.dumps(body, sort_keys=True))) % 10**8:08d}"
    assert_error(
        client.post(V1 + "/taxonomy/changes", json=body, headers={**ADMIN, "Idempotency-Key": key}), status, code
    )
    assert client.get(V1 + "/taxonomy", headers=VIEWER).json()["version"] == version


def test_openapi_document_describes_the_contract(client: TestClient) -> None:
    spec = client.get(V1 + "/openapi.json").json()
    paths = spec["paths"]
    assert all(
        V1 + p.replace("/1/", "/{item_id}/").replace("/jobs/1", "/jobs/{job_id}") in paths for _, p, _, _ in PROTECTED
    )
    put = paths[V1 + "/items/{item_id}/annotation"]["put"]
    assert {"200", "401", "403", "404", "409", "422"} <= set(put["responses"])
    assert spec["components"]["schemas"]["ErrorBody"]["required"] == ["code", "message", "request_id"]
