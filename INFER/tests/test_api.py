"""Every endpoint: success, validation failure, authorization failure, malformed input. Real database, real models."""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from tests.conftest import ADMIN_TOKEN, READ_TOKEN
from uplift import api, ingest, synth, train
from uplift.config import Settings

pytestmark = pytest.mark.integration

READ = {"Authorization": f"Bearer {READ_TOKEN}"}
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
NOBODY = uuid.UUID(int=0)


def _pool(url: str) -> ConnectionPool:
    return ConnectionPool(
        url, min_size=1, max_size=4, timeout=2, open=False, kwargs={"row_factory": dict_row, "connect_timeout": 2}
    )


@pytest.fixture(scope="module")
def client(trained: dict[str, Any], settings: Settings) -> Iterator[TestClient]:
    api.settings, api.pool = (
        settings,
        _pool(settings.api_database_url),
    )  # the API connects as its own least-privilege user
    with TestClient(api.app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def experiment(client: TestClient) -> dict[str, Any]:
    return client.get("/v1/datasets/synthetic/experiment", headers=READ).json()


@pytest.fixture(scope="module")
def champion(experiment: dict[str, Any]) -> dict[str, Any]:
    return next(r for r in experiment["runs"] if r["is_champion"])


def key() -> dict[str, str]:
    return {"Idempotency-Key": f"test-{uuid.uuid4()}"}


def assert_error(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code and error["message"] and error["request_id"] == response.headers["x-request-id"]
    assert "Traceback" not in response.text
    return error


# ---------------------------------------------------------------- authentication on every path

PATHS = [
    ("GET", "/v1/datasets"),
    ("GET", "/v1/datasets/synthetic/experiment"),
    ("GET", f"/v1/runs/{NOBODY}/deciles"),
    ("GET", f"/v1/runs/{NOBODY}/profiles"),
    ("GET", f"/v1/runs/{NOBODY}/scores"),
    ("GET", f"/v1/experiments/{NOBODY}/segments"),
    ("POST", f"/v1/runs/{NOBODY}/predict"),
    ("POST", "/v1/policies"),
    ("GET", f"/v1/policies/{NOBODY}"),
    ("GET", f"/v1/policies/{NOBODY}/export"),
]


@pytest.mark.parametrize(("method", "path"), PATHS)
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": READ_TOKEN[:-1]}, {"Authorization": "Basic abc"}],
)
def test_every_path_requires_a_valid_token(client: TestClient, method: str, path: str, headers: dict[str, str]) -> None:
    assert_error(client.request(method, path, headers=headers, json={}), 401, "unauthorized")


def test_the_read_token_cannot_create_or_export_policies(client: TestClient, champion: dict[str, Any]) -> None:
    body = {"run_id": champion["id"], "budget": 100, "cost_per_treatment": 1, "value_per_outcome": 20}
    assert_error(client.post("/v1/policies", headers=READ | key(), json=body), 403, "forbidden")
    assert_error(client.get(f"/v1/policies/{NOBODY}/export", headers=READ), 403, "forbidden")


def test_health_needs_no_token_and_exercises_dependencies(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok", "checks": {}}
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"database": "ok (schema 0002)", "artifacts": "ok"}


# ---------------------------------------------------------------- results


def test_datasets_report_units_quarantine_and_freshness(client: TestClient, trained: dict[str, Any]) -> None:
    by = {d["name"]: d for d in client.get("/v1/datasets", headers=READ).json()}
    d, m = by["synthetic"], trained["manifest"]
    assert d["units"] == m["units"] - m["invalid"] - m["conflicting_duplicate"]
    assert d["quarantine_reasons"]["missing_outcome"] == m["late_outcome"]
    assert d["quarantined"] == sum(d["quarantine_reasons"].values())
    assert d["experiment_id"] and d["stale"] is False and d["outcome"] == "visit"


def test_experiment_compares_every_learner_on_uplift_metrics(
    experiment: dict[str, Any], trained: dict[str, Any]
) -> None:
    assert experiment["status"] == "complete" and experiment["split_strategy"] == "temporal"
    assert experiment["audit"]["problems"] == [] and 0.45 < experiment["audit"]["treatment_auc"] < 0.55
    runs = {r["learner"]: r for r in experiment["runs"]}
    assert set(runs) == {"random", "response", "s_learner", "t_learner", "x_learner", "causal_forest"}
    assert [r["learner"] for r in experiment["runs"] if r["is_champion"]] == [trained["summary"]["champion"]]
    for r in runs.values():
        assert set(r["metrics"]["test"]) >= {"qini", "qini_ci95", "auuc", "uplift_at"}
        assert not {"accuracy", "auc", "f1", "precision", "recall"} & set(r["metrics"]["test"])  # uplift metrics only
        for curve in (r["curves"]["qini"], r["curves"]["uplift"]):
            assert len(curve) == 101 and curve[0] == [0.0, 0.0] and curve[-1][0] == 1.0
    # every learner was evaluated on the same test rows: all curves end at the same average treatment effect
    assert len({r["curves"]["qini"][-1][1] for r in runs.values()}) == 1
    assert experiment["audit"]["reference"]["ate_test"]["uplift"] == pytest.approx(
        runs["random"]["curves"]["qini"][-1][1], abs=1e-6
    )


def test_unknown_dataset_is_a_structured_404(client: TestClient) -> None:
    assert_error(client.get("/v1/datasets/nope/experiment", headers=READ), 404, "no_experiment")
    assert_error(client.get("/v1/nothing-here", headers=READ), 404, "not_found")


def test_deciles_and_profiles(client: TestClient, champion: dict[str, Any]) -> None:
    deciles = client.get(f"/v1/runs/{champion['id']}/deciles", headers=READ).json()
    assert [d["decile"] for d in deciles] == list(range(1, 11))
    assert deciles[0]["observed_uplift"] > deciles[-1]["observed_uplift"]  # the ranking means something
    assert deciles[0]["true_uplift"] > 0.1 and deciles[-1]["true_uplift"] < 0
    profiles = client.get(f"/v1/runs/{champion['id']}/profiles", headers=READ).json()
    assert profiles["high_uplift"]["traits"] and profiles["negative_uplift"]["n"] > 0
    assert_error(client.get(f"/v1/runs/{NOBODY}/deciles", headers=READ), 404, "run_not_found")
    assert_error(client.get("/v1/runs/not-a-uuid/deciles", headers=READ), 422, "invalid_request")


def test_segments_paginate_without_gaps_or_repeats(client: TestClient, experiment: dict[str, Any]) -> None:
    url = f"/v1/experiments/{experiment['id']}/segments"
    seen: list[dict[str, Any]] = []
    cursor = None
    while True:
        page = client.get(url, headers=READ, params={"limit": 37} | ({"cursor": cursor} if cursor else {})).json()
        seen += page["items"]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    everything = client.get(url, headers=READ, params={"limit": 200}).json()
    assert len(seen) > 100 and len({s["definition"] for s in seen}) == len(seen)
    assert [s["uplift"] for s in seen] == sorted(s["uplift"] for s in seen)  # most harmful first
    assert seen[:200] == everything["items"]
    hurts = client.get(url, headers=READ, params={"verdict": "hurts"}).json()["items"]
    assert hurts and all(s["verdict"] == "hurts" and s["ci_high"] < 0 for s in hurts)
    assert_error(client.get(url, headers=READ, params={"verdict": "bad"}), 422, "invalid_request")
    assert_error(client.get(url, headers=READ, params={"limit": 0}), 422, "invalid_request")
    assert_error(client.get(url, headers=READ, params={"cursor": "!!!"}), 400, "invalid_cursor")
    assert_error(client.get(f"/v1/experiments/{NOBODY}/segments", headers=READ), 404, "experiment_not_found")


def test_scores_paginate_in_a_stable_order(client: TestClient, champion: dict[str, Any]) -> None:
    url = f"/v1/runs/{champion['id']}/scores"
    first = client.get(url, headers=READ, params={"limit": 500}).json()
    second = client.get(url, headers=READ, params={"limit": 500, "cursor": first["next_cursor"]}).json()
    both = first["items"] + second["items"]
    assert len({s["unit_id"] for s in both}) == 1000
    assert [(-s["uplift"], s["unit_id"]) for s in both] == sorted(
        ((-s["uplift"], s["unit_id"]) for s in both), key=lambda k: (k[0], [-ord(c) for c in k[1]])
    )
    assert {s["split"] for s in both} == {"test"}
    assert (
        client.get(url, headers=READ, params={"limit": 1000}).json()["items"] == both
    )  # page boundaries change nothing
    assert client.get(url, headers=READ, params={"split": "train", "limit": 1}).json()["items"][0]["split"] == "train"
    assert_error(client.get(url, headers=READ, params={"limit": 5000}), 422, "invalid_request")
    assert_error(client.get(url, headers=READ, params={"split": "everything"}), 422, "invalid_request")
    assert_error(client.get(url, headers=READ, params={"cursor": "bm9wZQ=="}), 400, "invalid_cursor")


# ---------------------------------------------------------------- inference

SLEEPING_DOG = {
    "recency": 8,
    "history": 900.0,
    "mens": True,
    "womens": False,
    "newbie": False,
    "zip_code": "Urban",
    "channel": "Phone",
}
PERSUADABLE = {
    "recency": 2,
    "history": 60.0,
    "mens": True,
    "womens": False,
    "newbie": True,
    "zip_code": "Urban",
    "channel": "Web",
}


def test_predict_contract(client: TestClient, champion: dict[str, Any], experiment: dict[str, Any]) -> None:
    url = f"/v1/runs/{champion['id']}/predict"
    out = client.post(url, headers=READ, json={"units": [SLEEPING_DOG, PERSUADABLE, {}]}).json()
    assert out["learner"] == champion["learner"] and len(out["uplift"]) == 3
    dog, persuadable, unknown = out["uplift"]
    assert dog < 0 < persuadable  # the model learned who the e-mail hurts and who it moves
    assert dog < unknown < persuadable  # a unit with every feature missing still gets a (middling) answer
    # order and batch size do not change a unit's score
    assert client.post(url, headers=READ, json={"units": [PERSUADABLE]}).json()["uplift"] == [persuadable]
    response_run = next(r for r in experiment["runs"] if r["learner"] == "response")
    assert_error(
        client.post(f"/v1/runs/{response_run['id']}/predict", headers=READ, json={"units": [{}]}),
        409,
        "not_an_uplift_model",
    )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"units": []},
        {"units": [{"recency": 13}]},
        {"units": [{"history": -1}]},
        {"units": [{"channel": "Fax"}]},
        {"units": [{"zip_code": "Suburban"}]},
        {
            "units": [{"outcome": 1}]
        },  # unknown fields are refused: a client sending the label has a bug worth hearing about
        {"units": [{}] * 1001},
        {"units": "everyone"},
    ],
)
def test_predict_rejects_invalid_input(client: TestClient, champion: dict[str, Any], body: dict[str, Any]) -> None:
    error = assert_error(
        client.post(f"/v1/runs/{champion['id']}/predict", headers=READ, json=body), 422, "invalid_request"
    )
    assert error["details"] and all({"field", "problem"} <= set(d) for d in error["details"])


def test_malformed_json_is_a_structured_error(client: TestClient, champion: dict[str, Any]) -> None:
    r = client.post(
        f"/v1/runs/{champion['id']}/predict",
        headers=READ | {"Content-Type": "application/json"},
        content=b'{"units": [',
    )
    assert_error(r, 422, "invalid_request")


# ---------------------------------------------------------------- policies


def test_policy_respects_the_budget_and_is_valued_against_both_blanket_policies(
    client: TestClient, champion: dict[str, Any]
) -> None:
    body = {"run_id": champion["id"], "budget": 1500, "cost_per_treatment": 1, "value_per_outcome": 20}
    r = client.post("/v1/policies", headers=ADMIN | key(), json=body)
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["n_targeted"] == 1500 and Decimal(p["spend"]) <= Decimal(
        p["budget"]
    )  # budget binds: 1500 of ~8000 held-out units
    assert p["min_uplift"] * 20 > 1
    e = p["evaluation"]
    assert e["evaluable"] and e["share_of_population_treated"] == pytest.approx(1500 / p["population"])
    # a fifth of the cost buys well over a fifth of the effect, because the model picks who responds
    assert e["share_of_treat_all_effect_captured"] > 1.5 * e["share_of_population_treated"]
    assert e["policy_minus_treat_none"]["ci95"][0] > 0
    assert set(e["profit_per_unit"]) == {"policy", "treat_all", "treat_none"}
    truth = e["true_incremental_outcomes_per_unit"]
    assert truth["policy"] > 0.75 * truth["oracle_same_size"]
    assert e["incremental_outcomes_per_unit"]["policy"] == pytest.approx(
        truth["policy"], abs=0.012
    )  # the estimate is near the truth
    assert client.get(f"/v1/policies/{p['id']}", headers=READ).json() == p

    export = client.get(f"/v1/policies/{p['id']}/export", headers=ADMIN)
    assert (
        export.headers["content-type"].startswith("text/csv") and "attachment" in export.headers["content-disposition"]
    )
    rows = list(csv.DictReader(io.StringIO(export.text)))
    assert len(rows) == p["n_targeted"] and len({r["unit_id"] for r in rows}) == len(rows)
    uplifts = [float(r["predicted_uplift"]) for r in rows]
    assert uplifts == sorted(uplifts, reverse=True) and uplifts[-1] == pytest.approx(p["min_uplift"], abs=1e-6)


def test_an_unlimited_budget_still_never_treats_predicted_losers(client: TestClient, champion: dict[str, Any]) -> None:
    body = {"run_id": champion["id"], "budget": 10_000_000, "cost_per_treatment": 1, "value_per_outcome": 20}
    p = client.post("/v1/policies", headers=ADMIN | key(), json=body).json()
    assert 0 < p["n_targeted"] < p["population"]  # stops at break-even: predicted uplift * 20 > 1
    assert p["min_uplift"] > 1 / 20
    e = p["evaluation"]
    # leaving out the units the model says do not pay back beats treating everyone, with evidence
    assert e["policy_minus_treat_all"]["estimate"] > 0
    # and by the generator's ground truth: fewer visits than blanket treatment, but more profit (value 20, cost 1)
    truth, share = e["true_incremental_outcomes_per_unit"], e["share_of_population_treated"]
    assert truth["policy"] < truth["treat_all"]
    assert 20 * truth["policy"] - share > 20 * truth["treat_all"] - 1


def test_zero_budget_is_treat_nobody(client: TestClient, champion: dict[str, Any]) -> None:
    body = {"run_id": champion["id"], "budget": 0, "cost_per_treatment": 1, "value_per_outcome": 20}
    p = client.post("/v1/policies", headers=ADMIN | key(), json=body).json()
    assert p["n_targeted"] == 0 and p["min_uplift"] is None and Decimal(p["spend"]) == 0
    assert p["evaluation"]["policy_minus_treat_none"]["estimate"] == 0
    assert client.get(f"/v1/policies/{p['id']}/export", headers=ADMIN).text.strip() == "unit_id,predicted_uplift"


def test_creating_a_policy_is_safe_to_retry(client: TestClient, champion: dict[str, Any]) -> None:
    body = {"run_id": champion["id"], "budget": 300, "cost_per_treatment": 1.5, "value_per_outcome": 20}
    k = key()
    first = client.post("/v1/policies", headers=ADMIN | k, json=body)
    again = client.post("/v1/policies", headers=ADMIN | k, json=body)
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json() and first.json()["n_targeted"] == 200
    assert_error(
        client.post("/v1/policies", headers=ADMIN | k, json=body | {"budget": 301}), 409, "idempotency_conflict"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"budget": -1},
        {"cost_per_treatment": 0},
        {"value_per_outcome": -5},
        {"budget": "lots"},
        {"run_id": "not-a-uuid"},
        {"extra": 1},
        {"budget": None},
    ],
)
def test_policy_rejects_invalid_input(client: TestClient, champion: dict[str, Any], change: dict[str, Any]) -> None:
    body = {"run_id": champion["id"], "budget": 100, "cost_per_treatment": 1, "value_per_outcome": 20} | change
    assert_error(client.post("/v1/policies", headers=ADMIN | key(), json=body), 422, "invalid_request")


def test_policy_edge_cases_are_explained(
    client: TestClient, champion: dict[str, Any], experiment: dict[str, Any]
) -> None:
    body = {"run_id": champion["id"], "budget": 100, "cost_per_treatment": 1, "value_per_outcome": 20}
    error = assert_error(
        client.post("/v1/policies", headers=ADMIN, json=body), 422, "invalid_request"
    )  # no Idempotency-Key
    assert error["details"][0]["field"] == "header.idempotency-key"
    assert_error(
        client.post("/v1/policies", headers=ADMIN | {"Idempotency-Key": "short"}, json=body), 422, "invalid_request"
    )
    assert_error(
        client.post("/v1/policies", headers=ADMIN | key(), json=body | {"run_id": str(NOBODY)}), 404, "run_not_found"
    )
    other = next(r for r in experiment["runs"] if not r["is_champion"])
    assert_error(
        client.post("/v1/policies", headers=ADMIN | key(), json=body | {"run_id": other["id"]}), 409, "not_champion"
    )
    assert_error(client.get(f"/v1/policies/{NOBODY}", headers=READ), 404, "policy_not_found")
    assert_error(client.get(f"/v1/policies/{NOBODY}/export", headers=ADMIN), 404, "policy_not_found")


# ---------------------------------------------------------------- observability and failure


def test_request_ids_are_echoed_or_issued(client: TestClient) -> None:
    assert (
        client.get("/v1/datasets", headers=READ | {"X-Request-ID": "trace-1234-abcd"}).headers["x-request-id"]
        == "trace-1234-abcd"
    )
    issued = client.get("/v1/datasets", headers=READ | {"X-Request-ID": "bad id\twith junk"}).headers["x-request-id"]
    assert len(issued) == 32 and issued.isalnum()


def test_metrics_count_requests_by_route(client: TestClient) -> None:
    client.get("/v1/datasets", headers=READ)
    text = client.get("/metrics").text
    assert 'uplift_http_requests_total{method="GET",route="/v1/datasets",status="200"}' in text
    assert 'uplift_http_requests_total{method="GET",route="/v1/datasets",status="401"}' in text
    assert "uplift_policies_created_total" in text and "uplift_http_request_seconds_bucket" in text


def test_stale_flag_when_data_arrives_after_training(client: TestClient, settings: Settings) -> None:
    out = settings.data_dir / "stale"
    synth.generate(out, 6000, seed=77)
    ingest.ingest(settings, out / "synthetic.csv", "stale_demo")
    train.train(settings, "stale_demo", ["t_learner"], rounds=5)
    assert client.get("/v1/datasets/stale_demo/experiment", headers=READ).json()["stale"] is False
    champion = next(
        r for r in client.get("/v1/datasets/stale_demo/experiment", headers=READ).json()["runs"] if r["is_champion"]
    )
    body = {"run_id": champion["id"], "budget": 10, "cost_per_treatment": 1, "value_per_outcome": 20}
    assert_error(
        client.post("/v1/policies", headers=ADMIN | key(), json=body), 409, "not_scored"
    )  # trained but never scored
    ingest.ingest(settings, out / "synthetic_late.csv", "stale_demo")
    assert client.get("/v1/datasets/stale_demo/experiment", headers=READ).json()["stale"] is True
    assert {d["name"]: d["stale"] for d in client.get("/v1/datasets", headers=READ).json()}["stale_demo"] is True


def test_database_outage_degrades_to_503_and_recovers(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    dead = _pool("postgresql://nobody@127.0.0.1:9/none")
    dead.open(wait=False)
    healthy = api.pool
    monkeypatch.setattr(api, "pool", dead)
    try:
        r = client.get("/v1/datasets", headers=READ)
        assert_error(r, 503, "database_unavailable")
        assert r.headers["retry-after"] == "5"
        ready = client.get("/readyz")
        assert ready.status_code == 503 and ready.json()["checks"]["database"].startswith("failed")
        assert client.get("/healthz").status_code == 200  # alive, not ready: a restart would not help
        assert client.get("/v1/datasets", headers={}).status_code == 401  # auth is still enforced first
    finally:
        monkeypatch.setattr(api, "pool", healthy)
        dead.close()
    assert client.get("/v1/datasets", headers=READ).status_code == 200


def test_a_missing_model_artifact_is_a_503_not_a_crash(
    client: TestClient, champion: dict[str, Any], settings: Settings, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses

    api._model.cache_clear()
    monkeypatch.setattr(api, "settings", dataclasses.replace(settings, data_dir=tmp_path))
    assert_error(
        client.post(f"/v1/runs/{champion['id']}/predict", headers=READ, json={"units": [{}]}), 503, "model_unavailable"
    )
    assert client.get("/readyz").json()["checks"]["artifacts"].startswith("failed")
    monkeypatch.undo()
    api._model.cache_clear()
    assert client.post(f"/v1/runs/{champion['id']}/predict", headers=READ, json={"units": [{}]}).status_code == 200


def test_openapi_is_generated_from_the_code(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    assert {"/v1/policies", "/v1/runs/{run_id}/predict", "/v1/datasets/{name}/experiment", "/readyz"} <= set(
        spec["paths"]
    )
    assert "Idempotency-Key" in str(spec["paths"]["/v1/policies"]["post"]["parameters"]).replace(
        "idempotency-key", "Idempotency-Key"
    )
