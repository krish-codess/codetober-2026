"""Every endpoint: success, validation failure, authorization failure, malformed input."""

import dataclasses
import hashlib
import json

import pytest
from conftest import bench_payload, fake_run
from fastapi.testclient import TestClient

from squeeze import config, db, synth
from squeeze.api import create_app

DEVICE = {"authorization": "Bearer dev-token"}
ADMIN = {"authorization": "Bearer admin-token"}


@pytest.fixture
def client(cfg):
    return TestClient(create_app(cfg), raise_server_exceptions=False)


@pytest.fixture
def con(cfg, client):
    connection = db.connect(cfg.db_path)
    yield connection
    connection.close()


def shaped(response, status, code):
    body = response.json()
    assert response.status_code == status, body
    assert body["error"]["code"] == code and body["request_id"] == response.headers["x-request-id"]
    assert "Traceback" not in response.text
    return body["error"]


# --- health -------------------------------------------------------------------------------------


def test_health_ok_and_correlated(client):
    r = client.get("/healthz", headers={"x-request-id": "abc"})
    assert r.status_code == 200 and r.json()["checks"] == {"database": "ok", "packages": "ok", "raw_log": "ok"}
    assert r.headers["x-request-id"] == "abc"


def test_health_reports_the_broken_dependency(cfg, tmp_path):
    broken = dataclasses.replace(cfg, packages_dir=tmp_path / "missing")
    r = TestClient(create_app(broken)).get("/healthz")
    assert r.status_code == 503 and r.json()["checks"]["packages"].startswith("failed")
    assert r.json()["checks"]["database"] == "ok"


# --- upload -------------------------------------------------------------------------------------


def test_upload_is_idempotent_and_conflicts_are_refused(client, cfg):
    body = bench_payload()
    assert client.post("/v1/results", json=body, headers=DEVICE).status_code == 201
    again = client.post("/v1/results", json=body, headers=DEVICE)
    assert again.status_code == 200 and again.json() == {"status": "duplicate", "result_id": body["result_id"]}
    shaped(client.post("/v1/results", json={**body, "threads": 2}, headers=DEVICE), 409, "result_conflict")
    assert len(client.get("/v1/results").json()["items"]) == 1
    # the raw log holds exactly the one accepted result, so the database can be rebuilt from it
    assert len((cfg.raw_dir / "uploaded.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer nope"}, ADMIN, {"authorization": "dev-token"}])
def test_upload_needs_the_device_token(client, headers):
    shaped(client.post("/v1/results", json=bench_payload(), headers=headers), 401, "unauthorized")
    assert client.get("/v1/results").json()["items"] == []


def test_a_server_without_tokens_accepts_nobody(cfg):
    unset = TestClient(create_app(dataclasses.replace(cfg, device_token="", admin_token="")))
    empty = {"authorization": "Bearer "}
    shaped(unset.post("/v1/results", json=bench_payload(), headers=empty), 401, "unauthorized")
    shaped(unset.get("/v1/quarantine", headers=empty), 401, "unauthorized")


def test_invalid_results_are_quarantined_with_reasons_not_dropped(client):
    bad = bench_payload()
    bad["latency_ms"]["p95"] = 1.0  # below p50
    error = shaped(client.post("/v1/results", json=bad, headers=DEVICE), 422, "invalid_result")
    assert "p50 <= p95" in error["details"][0]["msg"]
    garbage = client.post("/v1/results", content=b'{"schema": 1, "latency', headers=DEVICE)
    assert shaped(garbage, 422, "invalid_result")["details"][0]["msg"].startswith("not JSON")
    shaped(client.post("/v1/results", json=bench_payload(target="rpi-5"), headers=DEVICE), 422, "invalid_result")
    assert client.get("/v1/results").json()["items"] == []
    page = client.get("/v1/quarantine", headers=ADMIN).json()
    assert len(page["items"]) == 3 and page["items"][0]["errors"][0]["loc"] == ["target"]


def test_units_and_timestamps_are_normalised_at_the_boundary(client, con):
    power = {"source": "ina219", "unit": "mW", "idle_w": 2700, "load_w": 5100, "energy_mj": 48.0, "samples": 9}
    body = bench_payload(power=False, measured_at=1791460800) | {"power": power}
    assert client.post("/v1/results", json=body, headers=DEVICE).status_code == 201
    row = con.execute("SELECT idle_w, load_w, measured_at FROM bench").fetchone()
    assert (row["idle_w"], row["load_w"], row["measured_at"]) == (2.7, 5.1, "2026-10-08T12:00:00+00:00")


def test_oversized_upload_is_refused(client, cfg):
    huge = bench_payload(power_unavailable="x" * (cfg.max_body + 1))
    shaped(client.post("/v1/results", json=huge, headers=DEVICE), 413, "too_large")


def test_quarantine_is_admin_only(client):
    shaped(client.get("/v1/quarantine"), 401, "unauthorized")
    shaped(client.get("/v1/quarantine", headers=DEVICE), 401, "unauthorized")
    shaped(client.get("/v1/quarantine?cursor=zzz", headers=ADMIN), 400, "bad_cursor")
    shaped(client.get("/v1/quarantine?limit=0", headers=ADMIN), 422, "invalid_request")


# --- reads --------------------------------------------------------------------------------------


def test_tradeoff_joins_verified_accuracy_to_measurements(client, con):
    assert client.get("/v1/tradeoff?target=rpi5").json()["points"] == []  # empty system: no run yet
    db.ingest_pipeline(con, fake_run())
    for variant, p50, power in (("teacher", 400.0, True), ("student", 20.0, True), ("student", 24.0, True),
                                ("student", 22.0, False), ("student-int8", 9.0, True)):  # fmt: skip
        db.ingest_bench(con, bench_payload(variant, p50=p50, power=power), "t", {"rpi5"})
    body = client.get("/v1/tradeoff?target=rpi5").json()
    by = {p["name"]: p for p in body["points"]}
    assert list(by) == ["teacher", "student", "student-int8", "student-mixed"]
    assert by["student"]["bench"]["runs"] == 3 and by["student"]["bench"]["p50_ms"] == 22.0  # median run
    assert by["student"]["bench"]["energy_mj"] == 48.0  # the run without a sensor does not zero it
    assert by["student-mixed"]["bench"] is None and by["student-mixed"]["meets_budget"] is None  # not measured
    assert by["teacher"]["meets_budget"] is False and by["student"]["meets_budget"] is True  # budget 50 ms
    # the fastest model failed its accuracy gate, so it is not on the frontier
    assert {n for n, p in by.items() if p["pareto"]} == {"teacher", "student"}
    assert body["baselines"][0]["name"] == "majority class" and body["runtimes_measured"] == ["onnxruntime"]
    assert client.get("/v1/tradeoff?target=rpi5&runtime=openvino").json()["points"][0]["bench"] is None


def test_tradeoff_rejects_bad_requests(client):
    assert shaped(client.get("/v1/tradeoff?target=nope"), 404, "unknown_target")["details"] == sorted(config.targets())
    shaped(client.get("/v1/tradeoff"), 422, "invalid_request")
    shaped(client.get("/v1/tradeoff?target=" + "x" * 100), 422, "invalid_request")
    shaped(client.get("/v1/nothing-here"), 404, "not_found")


def test_reads_are_conditional_and_invalidate_on_ingest(client, con):
    first = client.get("/v1/targets")
    tag = first.headers["etag"]
    assert client.get("/v1/targets", headers={"if-none-match": tag}).status_code == 304
    db.ingest_bench(con, bench_payload(), "t", {"rpi5"})
    fresh = client.get("/v1/targets", headers={"if-none-match": tag})
    assert fresh.status_code == 200 and fresh.headers["etag"] != tag
    assert {t["name"]: t["results"] for t in fresh.json()}["rpi5"] == 1


def test_results_paginate_with_a_stable_cursor(client, con):
    ids = {db.ingest_bench(con, bench_payload(p50=10.0 + i), "t", {"rpi5"})[2] for i in range(23)}
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 5, "target": "rpi5", **({"cursor": cursor} if cursor else {})}
        page = client.get("/v1/results", params=params).json()
        seen += [r["result_id"] for r in page["items"]]
        pages += 1
        if pages == 2:  # a result arriving mid-walk must not shift or repeat the remaining pages
            db.ingest_bench(con, bench_payload(p50=99.0), "t", {"rpi5"})
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 23 and set(seen) == ids and pages == 5
    shaped(client.get("/v1/results?cursor=not-a-cursor"), 400, "bad_cursor")
    shaped(client.get("/v1/results?limit=1000"), 422, "invalid_request")


def test_sensitivity(client, con):
    shaped(client.get("/v1/sensitivity"), 404, "no_run")
    db.ingest_pipeline(con, fake_run())
    body = client.get("/v1/sensitivity").json()
    assert [r["node"] for r in body["rows"]] == ["conv0", "conv1"] and body["rows"][0]["kept_float"] is True


def package_record(package_id, filename, target):
    manifest = {"name": filename.removesuffix(".tar.gz"), "target": {"name": target}, "run_id": "run0",
                "git_commit": "abc", "dataset_version": "d1", "variants": [{"name": "student"}]}  # fmt: skip
    return {"package_id": package_id, "filename": filename, "size_bytes": 20, "manifest": manifest}


def test_packages_list_and_download(client, con, cfg):
    archive = cfg.packages_dir / "squeeze-rpi5-run0.tar.gz"
    archive.write_bytes(b"not really a tarball")
    hosted = package_record(hashlib.sha256(archive.read_bytes()).hexdigest(), archive.name, "rpi5")
    db.ingest_package(con, hosted)
    db.ingest_package(con, package_record("f" * 64, "squeeze-rpi4-run0.tar.gz", "rpi4"))
    listed = client.get("/v1/packages").json()
    assert [(p["target"], p["hosted"]) for p in listed] == [("rpi4", False), ("rpi5", True)]
    assert [p["name"] for p in client.get("/v1/packages?target=rpi5").json()] == ["squeeze-rpi5-run0"]
    got = client.get(f"/v1/packages/{hosted['package_id']}/download")
    assert got.status_code == 200 and hashlib.sha256(got.content).hexdigest() == hosted["package_id"]
    shaped(client.get(f"/v1/packages/{'f' * 64}/download"), 404, "package_not_hosted")
    shaped(client.get("/v1/packages/nope/download"), 404, "unknown_package")


def test_download_cannot_escape_the_package_directory(client, con, cfg):
    (cfg.packages_dir.parent / "secret.txt").write_text("s3cret")
    db.ingest_package(con, package_record("e" * 64, "../secret.txt", "rpi5"))
    shaped(client.get(f"/v1/packages/{'e' * 64}/download"), 404, "package_not_hosted")


# --- behaviour under failure, observability -----------------------------------------------------


def test_unexpected_errors_are_shaped_and_carry_the_request_id(client, monkeypatch):
    monkeypatch.setattr(db, "tradeoff", lambda *a, **k: 1 / 0)
    response = client.get("/v1/tradeoff?target=rpi5", headers={"x-request-id": "trace-me"})
    assert shaped(response, 500, "internal")["message"].endswith("request_id")
    assert response.json()["request_id"] == "trace-me" and "ZeroDivision" not in response.text


def test_metrics_count_requests_and_ingest_outcomes(client):
    client.post("/v1/results", json=bench_payload(), headers=DEVICE)
    client.post("/v1/results", json=bench_payload(), headers=DEVICE)
    client.post("/v1/results", json={"schema": 1}, headers=DEVICE)
    text = client.get("/metrics").text
    assert 'squeeze_ingest_results_total{outcome="created"} 1' in text
    assert 'squeeze_ingest_results_total{outcome="duplicate"} 1' in text
    assert 'squeeze_ingest_results_total{outcome="quarantined"} 1' in text
    assert 'squeeze_http_requests_total{method="POST",route="/v1/results",status="201"} 1' in text
    assert 'squeeze_http_request_duration_ms_bucket{route="/v1/results",le="+Inf"} 3' in text


# --- ingest from raw files ----------------------------------------------------------------------


def test_generated_uploads_hit_every_boundary_rule_and_nothing_vanishes(con):
    targets = set(config.targets())
    pairs = synth.bench_results([{"name": "student", "sha256": "a" * 64}], ["rpi5", "rpi4"], 600, seed=3)
    counts = {}
    for payload, expected in pairs:
        status = db.ingest_bench(con, payload, "generator", targets)[0]
        assert status == expected, (expected, status, payload)
        counts[status] = counts.get(status, 0) + 1
    assert set(counts) == {"created", "duplicate", "quarantined"} and sum(counts.values()) == 600
    stored = con.execute("SELECT COUNT(*) FROM bench").fetchone()[0]
    assert stored == counts["created"] and con.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0] > 0
    assert con.execute("SELECT MAX(load_w) FROM bench").fetchone()[0] < 10  # milliwatts were converted


def test_raw_files_rebuild_the_database_in_any_order(client, con, tmp_path):
    raw = tmp_path / "rawfiles"
    raw.mkdir()
    # 'a-...' sorts first: the device results are read before the run that produced the model
    lines = [json.dumps(bench_payload(p50=p)) for p in (20.0, 21.0)]
    (raw / "a-bench.jsonl").write_text("\n".join([*lines, "", "not json"]) + "\n")
    targets = set(config.targets())
    assert db.ingest_dir(con, raw, targets) == {"created": 2, "quarantined": 1}
    assert [r["known_model"] for r in client.get("/v1/results").json()["items"]] == [False, False]  # kept anyway
    (raw / "pipeline-run0.json").write_text(json.dumps(fake_run()))
    assert db.ingest_dir(con, raw, targets) == {"duplicate": 2, "quarantined": 1, "pipeline": 1}
    assert [r["known_model"] for r in client.get("/v1/results").json()["items"]] == [True, True]
    assert db.ingest_dir(con, raw, targets) == {"duplicate": 2, "quarantined": 1, "pipeline": 1}  # idempotent
    tables = ("run", "variant", "bench", "quarantine")
    assert [con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables] == [1, 4, 2, 1]


def test_latest_run_wins(client, con):
    db.ingest_pipeline(con, fake_run("old", "2026-10-01T00:00:00Z"))
    db.ingest_pipeline(con, fake_run("new", "2026-10-08T00:00:00Z"))
    assert client.get("/v1/tradeoff?target=rpi5").json()["run"]["run_id"] == "new"


def test_pareto():
    assert db.pareto([(10, 0.9), (20, 0.95), (30, 0.94), (10, 0.9), (5, 0.5)]) == [True, True, False, True, True]


def test_openapi_documents_the_upload_body_and_error_shape(client):
    spec = client.get("/openapi.json").json()
    assert "latency_ms" in spec["components"]["schemas"]["BenchResult"]["properties"]
    post = spec["paths"]["/v1/results"]["post"]
    assert post["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/BenchResult")
    assert post["responses"]["422"]["content"]["application/json"]["schema"]["$ref"].endswith("/Error")
